"""Find the same product (same brand, size and pack) across stores.

Store titles differ a lot ("Minimalist 10% Niacinamide Face Serum With Matmarine + Zinc"
vs "Minimalist 10% Niacinamide + Zinc Face Serum for Men & Women | ..."), so matching is
hard gates first, then a similarity score:

  gates   same brand; same product type when both known; same active percentages and
          SPF when both list them; same pack count; never a combo/kit.
  size    equal quantity (g and ml are treated as one unit, stores mix them) = "exact";
          a candidate with no size listed is only ever a "size unknown" match.
  score   IDF-weighted Dice overlap of the remaining name words (IDF within the brand).

Precision matters more than recall: a wrong "same product" sends people to buy the wrong
thing, while a miss just shows fewer stores.
"""
import csv
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

MIN_SCORE = 0.7           # tuned on the real catalogue: >=0.7 exact-size pairs agree on MRP ~93%
UNIQUE_MIN_SCORE = 0.4    # ...or lower, when each side is the other's only gate-passing listing

_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|millilitre|g|gm|gms|gram|grams|kg|l|ltr|litre)\b", re.I)
_PACK = re.compile(r"\b(?:pack|set|combo) of\s*(\d+)\b|\b(\d+)\s*x\s*\d|\bx\s*(\d+)\b", re.I)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_SPF = re.compile(r"\bspf\s*(\d+)", re.I)
_COMBO = re.compile(r"\b(combo|kit|duo|trio|bundle|routine|regime|regimen|hamper|gift|collection|"
                    r"essentials|set)\b", re.I)

# First match wins, so the more specific kinds come first.
TYPES = [
    ("lip", r"\blip"),
    ("fragrance", r"perfume|parfum|edp|edt|body mist|cologne"),
    ("deodorant", r"deodorant|deo|roll-on|roll on|antiperspirant"),
    ("powder", r"powder|talc"),
    ("shampoo", r"shampoo|hair cleanser"),
    ("conditioner", r"conditioner"),
    ("hair_oil", r"hair oil|oil.*hair"),
    ("hair", r"hair|scalp"),
    ("body", r"\bbody\b|hand cream|foot cream|shower"),
    ("eye", r"\beye\b|under eye|undereye"),
    ("sunscreen", r"sunscreen|sun screen|sunblock|sun block|\bspf\b|sun stick|sun fluid"),
    ("cleanser", r"cleanser|face ?wash|cleansing|micellar|foaming wash"),
    ("toner", r"toner|\bmist\b"),
    ("mask", r"\bmask\b|\bpack\b(?! of)"),
    ("serum", r"serum|ampoule|essence|\bdrops\b|booster|concentrate"),
    ("exfoliant", r"exfoliat|peel"),
    ("moisturizer", r"moisturi[sz]er|\bcream\b|gel cream|lotion|emulsion|\bbalm\b"),
]
TYPES = [(k, re.compile(p, re.I)) for k, p in TYPES]

STOP = set("""
for with and the of in to a an all skin type types men women man woman unisex face facial
new pack ml g gm gms grams gram kg l ltr size buy online india original genuine free
reduces reduce reducing helps help daily use oily dry normal combination sensitive acne
prone glowing glow radiant even tone brightening bright spots dark marks pores open
blemishes hydration hydrating moisturising moisturizing lightweight non greasy sticky
step made safe fragrance paraben sulphate sulfate cruelty vegan dermatologist tested
""".split())


# Product-type words: "face wash" vs "cleanser" is the same thing said differently, so
# these never count as a variant conflict.
TYPE_WORDS = set("""
wash cleanser cleansing foam foaming gel cream creme lotion serum moisturizer moisturiser
sunscreen toner mist mask scrub oil balm butter milk emulsion fluid essence spray stick
night day shampoo conditioner soap bar water micellar treatment care
""".split())


def _norm(text):
    t = (text or "").lower().replace("&", " and ").replace("’", "'")
    t = re.sub(r"(moisturi|hydrati|brighteni|exfolia)s", r"\1z", t)
    t = re.sub(r"(\d)\s+%", r"\1%", t)
    return t


def _num(s):
    x = float(s)
    return int(x) if x == int(x) else x


def quantities(text):
    """Distinct quantities in g/ml (kg and litres converted)."""
    out = []
    for n, unit in _QTY.findall(_norm(text)):
        v = float(n) * (1000 if unit.lower() in ("kg", "l", "ltr", "litre") else 1)
        v = _num(v)
        if v and v not in out:
            out.append(v)
    return out


@dataclass
class Features:
    product_id: str
    brand: str
    store: str
    size: float | None          # g or ml; None = not listed
    pack: int
    combo: bool
    kind: str | None
    pcts: frozenset
    spf: int | None
    words: frozenset = field(default_factory=frozenset)
    head: str = ""              # first distinctive word, usually the product line
    lead: tuple = ()            # first few non-type words: where variants (Peach/Berries) live
    size_from_mrp: bool = False # size wasn't listed; worked out from the MRP (see Matcher)


def features(p):
    name = _norm(p.name)
    size_text = _norm(p.size)
    pack = 1
    m = _PACK.search(size_text) or _PACK.search(name)
    if m:
        pack = int(next(g for g in m.groups() if g))
    q_size, q_name = quantities(size_text), quantities(name)
    brand_n = _norm(p.brand)
    combo = (bool(_COMBO.search(name) or _COMBO.search(size_text)) or len(q_name) > 1
             or len(q_size) > 1 or (bool(brand_n) and name.count(brand_n) > 1))   # "X 30 ml + X SPF 50"
    # "(Pack of 2)" alone is a multipack, not a combo.
    if combo and pack > 1 and not re.search(r"combo|kit|duo|trio|bundle|routine|regime|\+", name + size_text):
        combo = False
    size = (q_size or q_name or [None])[0]
    kind = next((k for k, rx in TYPES if rx.search(name)), None)
    pcts = frozenset(_num(x) for x in _PCT.findall(name))
    spf = _SPF.search(name)
    brand_words = set(re.findall(r"[a-z0-9]+", _norm(p.brand)))
    words, head, lead = set(), "", []
    for w in re.findall(r"[a-z][a-z0-9'-]+", name.split("|")[0]):   # Flipkart: text after "|" is SEO filler
        w = w.strip("'-")
        if len(w) > 1 and w not in STOP and w not in brand_words:
            w = w[:-1] if w.endswith("s") and len(w) > 4 else w
            words.add(w)
            head = head or w
            if w not in TYPE_WORDS and len(lead) < 4 and w.isalpha() and len(w) > 2:
                lead.append(w)
    return Features(p.product_id, p.brand.lower(), p.store, size, pack, combo, kind, pcts,
                    int(spf.group(1)) if spf else None, frozenset(words), head, tuple(lead))


@dataclass
class Match:
    product_id: str
    store: str
    score: float
    size_known: bool


CATALOG_IDS = {"nykaa": ("nyk", "nykaa_id"), "flipkart": ("fk", "flipkart_id"),
               "tira": ("tira", "tira_id"), "purplle": ("pur", "purplle_id"),
               "maccaron": ("mac", "maccaron_id")}


def load_mrp(catalog_dir):
    """{product_id: MRP} from the catalogue snapshots (newest file per store wins)."""
    mrp = {}
    for f in sorted(Path(catalog_dir).glob("*_*.csv")):      # dated names sort oldest first
        ids = CATALOG_IDS.get(f.name.split("_")[0])
        if not ids:
            continue
        with open(f, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    v = float(r.get("mrp") or 0)
                except ValueError:
                    continue
                if v > 0 and r.get(ids[1]):
                    mrp[f"{ids[0]}-{r[ids[1]]}"] = v
    return mrp


class Matcher:
    def __init__(self, products, mrp=None):
        self.products = {p.product_id: p for p in products}
        self.feats = {p.product_id: features(p) for p in products}
        self.by_brand = defaultdict(list)
        df = defaultdict(Counter)
        for f in self.feats.values():
            self.by_brand[f.brand].append(f)
            df[f.brand].update(f.words)
        self.idf = {}
        for b, counts in df.items():
            n = len(self.by_brand[b])
            self.idf[b] = {w: math.log(1 + n / c) for w, c in counts.items()}
        self.mrp = mrp or {}
        if self.mrp:
            self._sizes_from_mrp()

    def _sizes_from_mrp(self):
        """A multi-size listing with no size shown (~30% of Nykaa) carries its default
        size's MRP. MRP is printed on the pack, so if exactly one size of the same product
        on the other stores has that MRP, and some other size doesn't, that's its size."""
        found = {}
        for pid, a in self.feats.items():
            mine = self.mrp.get(pid)
            if a.size is not None or a.combo or not mine:
                continue
            by_size = defaultdict(set)
            for b in self.by_brand.get(a.brand, ()):
                if b.store == a.store or b.size is None or not self.compatible(a, b):
                    continue
                theirs = self.mrp.get(b.product_id)
                if theirs and self.score(a, b) >= MIN_SCORE:
                    by_size[b.size].add(abs(theirs - mine) <= 1)
            hits = [size for size, agree in by_size.items() if agree == {True}]
            if len(hits) == 1 and len(by_size) > 1:
                found[pid] = hits[0]
        for pid, size in found.items():          # applied after the loop: no chaining
            self.feats[pid].size = size
            self.feats[pid].size_from_mrp = True

    def score(self, a, b):
        idf = self.idf.get(a.brand, {})
        wa = {w: idf.get(w, 1.0) for w in a.words}
        wb = {w: idf.get(w, 1.0) for w in b.words}
        common = sum(min(wa[w], wb[w]) for w in a.words & b.words)
        total = sum(wa.values()) + sum(wb.values())
        if not total or not wa or not wb:
            return 0.0
        dice = 2 * common / total
        # SEO-padded titles dilute Dice, so also ask: is the shorter title's identity
        # (e.g. "Sensibio H2O Micellar Water") fully present in the longer one?
        contain = common / min(sum(wa.values()), sum(wb.values()))
        return 0.6 * contain + 0.4 * dice

    @staticmethod
    def compatible(a, b):
        if a.brand != b.brand or a.combo or b.combo or a.pack != b.pack:
            return False
        if a.kind and b.kind and a.kind != b.kind:
            return False
        if a.pcts and b.pcts and a.pcts != b.pcts:
            return False
        if a.spf and b.spf and a.spf != b.spf:
            return False
        if a.size is not None and b.size is not None and a.size != b.size:
            return False
        # "Sensibio H2O Micellar" vs "Sebium H2O Micellar": each title's leading line
        # word must appear in the other, or at least one of them must.
        if a.head and b.head and a.head not in b.words and b.head not in a.words:
            return False
        # Variant swap: each title leads with a word the other lacks (Skin Fruits *Peach*
        # vs *Berries*, Squalane wash with *Kiwi* vs *Aloe*). Only the shorter title's lead
        # is trusted fully, so check both directions.
        if [w for w in a.lead if w not in b.words] and [w for w in b.lead if w not in a.words]:
            return False
        # A title with nothing but type words ("Fixderma Face Moisturizer for Men") can't be
        # told apart from the brand's other moisturizers, so it never matches.
        if not a.lead or not b.lead:
            return False
        return True

    def _candidates(self, a, store):
        """[(score, Features)] for listings in `store` that pass every hard gate."""
        out = []
        for b in self.by_brand.get(a.brand, ()):
            if b.store != store or b.product_id == a.product_id or not self.compatible(a, b):
                continue
            # One side missing percentages while the other has them is a weak conflict.
            out.append((self.score(a, b) - (0.1 if bool(a.pcts) != bool(b.pcts) else 0), b))
        return out

    def matches(self, product_id, min_score=MIN_SCORE, mutual=True):
        """Best match in each other store, best first: [Match].

        Accepted when the score is >= min_score, or when both sizes are known, the score is
        >= UNIQUE_MIN_SCORE and each listing is the other's only gate-passing candidate.
        Exact-size matches win over size-unknown ones; a size-unknown one is returned only
        if no exact one exists in that store. With mutual=True a pairing is dropped when
        the candidate has a better partner back in our store (Sensibio vs Sebium)."""
        a = self.feats.get(product_id)
        if not a or a.combo:
            return []
        stores = {b.store for b in self.by_brand.get(a.brand, ())} - {a.store}
        out = []
        for store in stores:
            cands = self._candidates(a, store)
            sized = [c for c in cands if c[1].size is not None and a.size is not None]
            pick = None
            if sized:
                s, b = max(sized, key=lambda c: c[0])
                if s >= min_score:
                    pick = (s, b, True)
                elif len(sized) == 1 and s >= UNIQUE_MIN_SCORE and bool(a.pcts) == bool(b.pcts):
                    back = [c for c in self._candidates(b, a.store)
                            if c[1].size is not None]
                    if len(back) == 1 and back[0][1].product_id == a.product_id:
                        pick = (s, b, True)
            if pick is None:
                unsized = [c for c in cands if c not in sized and c[0] >= min_score]
                if unsized:
                    s, b = max(unsized, key=lambda c: c[0])
                    pick = (s, b, False)
            if pick is None:
                continue
            s, b, known = pick
            if mutual and s >= min_score:
                back = [c for c in self._candidates(b, a.store) if c[0] >= min_score]
                if back:
                    bs, bb = max(back, key=lambda c: ((c[1].size is not None), c[0]))
                    if bb.product_id != a.product_id and (
                            (bb.size, bb.pack) == (a.size, a.pack) or bs > s + 0.05):
                        continue
            out.append(Match(b.product_id, store, round(s, 3), known))
        return sorted(out, key=lambda m: (not m.size_known, -m.score))

    def sizes_elsewhere(self, product_id, min_score=MIN_SCORE):
        """For a listing with no size: the same product at each known size on other
        stores, as {size: [Match]} (best listing per store and size)."""
        a = self.feats.get(product_id)
        if not a or a.combo or a.size is not None:
            return {}
        best = {}
        for b in self.by_brand.get(a.brand, ()):
            if b.store == a.store or b.size is None or not self.compatible(a, b):
                continue
            s = self.score(a, b) - (0.1 if bool(a.pcts) != bool(b.pcts) else 0)
            k = (b.size, b.store)
            if s >= min_score and (k not in best or s > best[k].score):
                best[k] = Match(b.product_id, b.store, round(s, 3), True)
        out = {}
        for (size, _), m in sorted(best.items()):
            out.setdefault(size, []).append(m)
        return out

    def search(self, query, limit=5):
        """Free-text search over names: [(score, product_id)], best first.

        A brand named in the query limits results to that brand; a percentage or size in
        the query must not contradict the product's."""
        q = _norm(query)
        qpcts = frozenset(_num(x) for x in _PCT.findall(q))
        qsize = (quantities(q) or [None])[0]
        rest = _PCT.sub(" ", _QTY.sub(" ", q))
        qtoks = re.findall(r"[a-z0-9]+", rest.replace("'", ""))
        brand = None
        for b in sorted(self.by_brand, key=len, reverse=True):
            bt = [t for t in re.findall(r"[a-z0-9]+", b.replace("'", "")) if t != "the"]
            n = len(bt)
            if bt and any(qtoks[i:i + n] == bt for i in range(len(qtoks) - n + 1)):
                brand = b
                for i in range(len(qtoks) - n + 1):
                    if qtoks[i:i + n] == bt:
                        qtoks = qtoks[:i] + qtoks[i + n:]
                        break
                break
        qwords = {w[:-1] if w.endswith("s") and len(w) > 4 else w for w in qtoks
                  if len(w) > 1 and not w.isdigit()} - STOP
        if not qwords and not brand:
            return []
        hits = []
        for pid, f in (((x.product_id, x) for x in self.by_brand[brand]) if brand
                       else self.feats.items()):
            if qpcts and f.pcts and qpcts != f.pcts:
                continue
            if qwords:
                found = sum(1 for w in qwords if w in f.words or
                            (len(w) >= 5 and any(x.startswith(w) or w.startswith(x)
                                                 for x in f.words if len(x) >= 5)))
                if not found:
                    continue
                s = found / len(qwords)
            else:
                s = 1.0
            if qpcts and not f.pcts:
                s -= 0.2
            if qsize and f.size and qsize != f.size:
                s -= 0.3
            if f.combo:
                s -= 0.2
            s -= 0.02 * max(0, len(f.words) - 4) / 4     # prefer tighter titles on ties
            hits.append((round(s, 3), pid))
        hits.sort(key=lambda h: (-h[0], h[1]))
        return hits[:limit]
