"""Cross-store matching. Titles are real ones from the 2026-11 catalogues, including the
look-alikes that fooled earlier versions of the matcher."""
import csv
import unittest
from pathlib import Path

from skd import match
from skd.posts import Product


def P(pid, store, brand, name, size=""):
    return Product(product_id=pid, name=name, brand=brand, size=size, store=store)


CATALOG = [
    # Minimalist 10% niacinamide serum, 10 ml and 30 ml, on four stores
    P("pur-10", "purplle", "Minimalist", "Minimalist Niacinamide 10% with Matmarine + Zinc + Acetyl Glucosamine Regulates Sebum & Evens Tone", "10 ml"),
    P("pur-30", "purplle", "Minimalist", "Minimalist Niacinamide 10% with Matmarine + Zinc + Acetyl Glucosamine Regulates Sebum & Evens Tone", "30 ml"),
    P("pur-30x2", "purplle", "Minimalist", "Minimalist Niacinamide 10% with Matmarine + Zinc + Acetyl Glucosamine Regulates Sebum & Evens Tone", "30 ml (Pack of 2)"),
    P("pur-5pct", "purplle", "Minimalist", "Minimalist 5% Niacinamide Face Serum With bifida ferment +oat extract + hyaluronic acid, clears, heals & soothes skin, 30 ml"),
    P("pur-combo", "purplle", "Minimalist", "Minimalist 10% Niacinamide face Serum 30 ml + Minimalist SPF 50 Sunscreen with niacinamide 50 gm"),
    P("fk-10", "flipkart", "Minimalist", "Minimalist 10% Niacinamide + Zinc Face Serum for Men & Women | Skincare for Oily, Acne-Prone Skin, Open Pores & Blemishes", "10 ml"),
    P("fk-30", "flipkart", "Minimalist", "Minimalist 10% Niacinamide Face Serum for Acne Marks & Blemishes Prone Skin", "30 ml"),
    P("tira-30", "tira", "Minimalist", "Minimalist 10% Niacinamide Face Serum With Matmarine + Zinc For Reducing Oil & Blemishes (30ml)"),
    P("nyk-10", "nykaa", "Minimalist", "Minimalist 10% Niacinamide Face Serum With Matmarine + Zinc For Reducing Oil & Blemishes", "10ml"),
    P("nyk-nosize", "nykaa", "Minimalist", "Minimalist 10% Niacinamide Face Serum With Matmarine + Zinc For Reducing Oil & Blemishes"),
    # Bioderma: two product lines with near-identical titles
    P("pur-sensibio", "purplle", "Bioderma", "Bioderma Sensibio H2O Micellar Water, Cleansing and Make-Up Removing Solution", "100 ml"),
    P("fk-sebium", "flipkart", "Bioderma", "BIODERMA Sebium H2O Purifying Micellar Cleansing Water and Makeup Remover", "100 ml"),
    # Shampoo vs conditioner, perfume vs deodorant
    P("pur-shampoo", "purplle", "L'Oreal Paris", "L'Oreal Paris Total Repair 5 Shampoo For Damaged Hair | Powered by Keratin XS", "180 ml"),
    P("fk-cond", "flipkart", "L'Oreal Paris", "Loreal Paris Total Repair 5 Rapid Reviver Deep Conditioner With Micro Keratin", "180ml"),
    P("pur-edp", "purplle", "Plum", "Plum BodyLovin' Vanilla Vibes Perfume | Long Lasting & Premium Warm Vanilla Fragrance", "50 ml"),
    P("fk-deo", "flipkart", "Plum", "Plum BodyLovin Vanilla Vibes Deodorant Roll-On Controls Underarm Odor", "50ml"),
    # Flavour variants of one line
    P("pur-peach", "purplle", "Joy", "Joy Skin Fruits 24hr Super Soft Moisturiser with Peach & Active Hyaluronic", "200 ml"),
    P("fk-berry", "flipkart", "Joy", "Joy Skin Fruits Super Soft Moisturizer (Berries + Active Vitamin E)", "200 ml"),
    # A title with nothing distinctive in it
    P("pur-dewrav", "purplle", "Fixderma", "Fixderma Dewrav Night Cream with Retinol, Niacinamide & Aloin Dermatologist-tested", "50 g"),
    P("fk-generic", "flipkart", "Fixderma", "Fixderma Brightening Face Moisturizer for Men |Reduces Evens Skin Tone", "50 g"),
]


class Features(unittest.TestCase):
    def setUp(self):
        self.f = {p.product_id: match.features(p) for p in CATALOG}

    def test_size_from_size_column_or_title(self):
        self.assertEqual(self.f["pur-30"].size, 30)
        self.assertEqual(self.f["tira-30"].size, 30)            # "(30ml)" in the title
        self.assertIsNone(self.f["nyk-nosize"].size)

    def test_pack_and_combo(self):
        self.assertEqual(self.f["pur-30x2"].pack, 2)
        self.assertFalse(self.f["pur-30x2"].combo)              # a multipack, not a combo
        self.assertTrue(self.f["pur-combo"].combo)              # brand named twice, two sizes
        self.assertFalse(self.f["tira-30"].combo)               # "+ Zinc ... (30ml)" is one product

    def test_actives_and_kind(self):
        self.assertEqual(self.f["fk-10"].pcts, frozenset({10}))
        self.assertEqual(self.f["fk-10"].kind, "serum")
        self.assertEqual(self.f["pur-shampoo"].kind, "shampoo")
        self.assertEqual(self.f["fk-cond"].kind, "conditioner")

    def test_spelling_normalised(self):
        self.assertIn("moisturizer", self.f["pur-peach"].words)


class Matching(unittest.TestCase):
    def setUp(self):
        self.m = match.Matcher(CATALOG)

    def ids(self, pid, exact_only=True):
        return {x.product_id for x in self.m.matches(pid) if x.size_known or not exact_only}

    def test_same_product_found_on_every_store(self):
        self.assertIn("tira-30", self.ids("pur-30"))
        self.assertEqual(self.ids("tira-30") - {"fk-30"}, {"pur-30"})
        self.assertEqual(self.ids("pur-10"), {"fk-10", "nyk-10"})
        self.assertEqual(self.ids("fk-10"), {"pur-10", "nyk-10"})

    def test_never_mixes_sizes_packs_or_strengths(self):
        for pid in ("pur-10", "pur-30", "pur-30x2", "pur-5pct"):
            got = self.ids(pid)
            for other in got:
                self.assertEqual(self.m.feats[other].size, self.m.feats[pid].size)
                self.assertEqual(self.m.feats[other].pack, self.m.feats[pid].pack)
        self.assertEqual(self.ids("pur-30x2"), set())
        self.assertEqual(self.ids("pur-5pct"), set())

    def test_combo_never_matches(self):
        self.assertEqual(self.m.matches("pur-combo"), [])

    def test_size_unknown_listing_is_flagged_not_exact(self):
        all_m = {x.product_id: x for x in self.m.matches("tira-30")}
        self.assertNotIn("nyk-nosize", self.ids("tira-30"))
        if "nyk-nosize" in all_m:
            self.assertFalse(all_m["nyk-nosize"].size_known)

    def test_look_alikes_rejected(self):
        for a, b in [("pur-sensibio", "fk-sebium"), ("pur-shampoo", "fk-cond"),
                     ("pur-edp", "fk-deo"), ("pur-peach", "fk-berry"), ("pur-dewrav", "fk-generic")]:
            self.assertNotIn(b, self.ids(a, exact_only=False), f"{a} wrongly matched {b}")

    def test_search_prefers_named_brand(self):
        hits = [pid for _, pid in self.m.search("minimalist niacinamide 10% 30ml", limit=3)]
        self.assertTrue(hits)
        for pid in hits:
            self.assertEqual(self.m.products[pid].brand, "Minimalist")
            self.assertNotEqual(pid, "pur-5pct")


class SizeFromMrp(unittest.TestCase):
    """Nykaa's multi-size listing shows no size but carries the default size's MRP."""
    ITEMS = [
        P("nyk-1", "nykaa", "Dot & Key", "Dot & Key Vitamin C + E Super Bright Gel Moisturizer For Face, Visibly Boosts Glow"),
        P("pur-25", "purplle", "Dot & Key", "Dot & Key Vitamin C + E Super Bright Gel Moisturizer For Face, Visibly Boosts Glow, For All Skin Types", "25 ml"),
        P("pur-60", "purplle", "Dot & Key", "Dot & Key Vitamin C + E Super Bright Gel Moisturizer For Face, Visibly Boosts Glow, For All Skin Types", "60 ml"),
    ]

    def test_unique_mrp_gives_the_size(self):
        m = match.Matcher(self.ITEMS, {"nyk-1": 495, "pur-25": 350, "pur-60": 495})
        self.assertEqual(m.feats["nyk-1"].size, 60)
        self.assertTrue(m.feats["nyk-1"].size_from_mrp)
        self.assertEqual({x.product_id for x in m.matches("nyk-1") if x.size_known}, {"pur-60"})

    def test_ambiguous_or_missing_mrp_leaves_size_unknown(self):
        for mrp in ({"nyk-1": 495, "pur-25": 495, "pur-60": 495},     # both sizes same MRP
                    {"nyk-1": 400, "pur-25": 350, "pur-60": 495},     # matches neither
                    {"pur-25": 350, "pur-60": 495}):                  # ours unknown
            m = match.Matcher(self.ITEMS, mrp)
            self.assertIsNone(m.feats["nyk-1"].size, mrp)


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless((ROOT / "data" / "watchlist.csv").exists(), "needs the real watchlist")
class RealCatalogue(unittest.TestCase):
    """Outside check: Indian MRP is printed on the pack, so one SKU has one MRP on every
    store. Exact-size matches must mostly agree on it (resellers and old stock account
    for the rest)."""

    @classmethod
    def setUpClass(cls):
        from skd import run
        cls.m = match.Matcher(run.load_watchlist(ROOT / "data" / "watchlist.csv"))
        cls.mrp = {}
        ids = {"nykaa": ("nyk", "nykaa_id"), "flipkart": ("fk", "flipkart_id"),
               "tira": ("tira", "tira_id"), "purplle": ("pur", "purplle_id")}
        for f in sorted((ROOT / "data" / "catalog").glob("*.csv")):
            store = f.name.split("_")[0]
            if store not in ids:
                continue
            pre, col = ids[store]
            with open(f, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        cls.mrp[f"{pre}-{r[col]}"] = float(r["mrp"])
                    except (KeyError, ValueError):
                        pass

    def test_known_pairs(self):
        got = {x.product_id for x in self.m.matches("pur-288836") if x.size_known}
        if "pur-288836" in self.m.feats:
            self.assertIn("fk-FRNFWSHFY7K9HG4S", got)

    def test_mrp_agreement(self):
        agree = total = 0
        for pid in [p for p in self.m.feats if p.startswith("pur-")]:
            for x in self.m.matches(pid):
                a, b = self.mrp.get(pid), self.mrp.get(x.product_id)
                if not x.size_known or not a or not b:
                    continue
                d = abs(a - b) / max(a, b)
                if 0.06 < d < 0.2:          # MRP revisions: neither proof nor disproof
                    continue
                total += 1
                agree += d <= 0.06
        self.assertGreater(total, 500)
        self.assertGreaterEqual(agree / total, 0.90, f"{agree}/{total} exact matches agree on MRP")


if __name__ == "__main__":
    unittest.main()
