"""Price-check bot: DM a product link (or a product name), get every tracked store's
current price for that exact product, cheapest first.

    python -m skd.bot                      # run the bot (long polling, Ctrl+C to stop)
    python -m skd.bot --ask "<link or name>"   # print the reply offline, no Telegram

Phase 1 scope: only products in data/watchlist.csv. Cross-store matches come from
skd.match (same brand + size + pack). Links we can't resolve are logged in bot.db as
requests, so the most-asked missing products can be added to the watchlist.

Prices come from prices.db, which the GitHub Actions run rewrites every 3 h on the
`data` branch. The bot keeps its own copy (data/bot_prices.db) and refreshes it from
origin/data every --refresh-hours, so it never touches the tracker's DB.
"""
import argparse
import html
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import db, match, posts, run, telegram

ROOT = run.ROOT
STORE_NAMES = {**posts.STORE_NAMES, "tira": "Tira", "maccaron": "Maccaron"}
STORE_HOSTS = {"nykaa.com": "nykaa", "flipkart.com": "flipkart", "purplle.com": "purplle",
               "tirabeauty.com": "tira", "maccaron.in": "maccaron", "amazon.in": "amazon"}
# Short/share links we expand (Location header only, one request per hop, no page body).
SHORT_HOSTS = ("dl.flipkart.com", "fkrt.it", "fkrt.co", "amzn.in", "amzn.to", "a.co",
               "ekaro.in", "earnkaro.com", "onelink.me", "nykaa.me", "page.link")
EARNKARO_HOSTS = ("ekaro.in", "earnkaro.com")
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
RATE_LIMIT = (10, 60)          # at most 10 messages per chat per 60 s

BOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS queries (
    ts INTEGER NOT NULL, chat_id INTEGER, kind TEXT NOT NULL,   -- link | search | pick
    text TEXT, product_id TEXT, found INTEGER NOT NULL
);
"""

HELP = ("Send me a skincare product link from Nykaa, Purplle, Flipkart, Tira, Maccaron or "
        "Amazon, or just type a product name (e.g. <i>minimalist niacinamide 10% 30ml</i>).\n\n"
        "I'll show the current price of that exact product (same size) on every store I track, "
        "cheapest first.")


# ---------- links -> product ids ----------

def _store_of(host):
    host = host.lower().removeprefix("www.").removeprefix("m.")
    for h, store in STORE_HOSTS.items():
        if host == h or host.endswith("." + h):
            return store
    return None


def url_keys(url):
    """Lookup keys for a store URL, most specific first (e.g. ['nykaa:15020676', 'nykaa:15022069'])."""
    u = urllib.parse.urlsplit(url.strip())
    store = _store_of(u.hostname or "")
    if not store:
        return []
    q = urllib.parse.parse_qs(u.query)
    path = urllib.parse.unquote(u.path).rstrip("/")
    keys = []
    if store == "nykaa":
        keys += [f"nykaa:{s}" for s in q.get("skuId", []) + q.get("skuid", [])]
        keys += [f"nykaa:{m}" for m in re.findall(r"/p/(\d+)", path)]
    elif store == "flipkart":
        keys += [f"flipkart:{p.upper()}" for p in q.get("pid", [])]
        keys += [f"flipkart:{m}" for m in re.findall(r"/p/(itm[0-9a-z]+)", path, re.I)]
    elif store == "amazon":
        keys += [f"amazon:{m.upper()}" for m in
                 re.findall(r"/(?:dp|gp/product|gp/aw/d|d)/([A-Z0-9]{10})(?:[/?]|$)", path + "/", re.I)]
    elif store == "purplle":
        keys += [f"purplle:{m}" for m in re.findall(r"/product/(\d+)$", path)]
        slug = path.rsplit("/", 1)[-1]
        if slug and not slug.isdigit():
            keys.append(f"purplle:{slug.lower()}")
    elif store == "tira":
        m = re.search(r"/product/([^/]+)$", path)
        if m:
            keys.append(f"tira:{m.group(1).lower()}")
    elif store == "maccaron":
        keys += [f"maccaron:{m}" for m in re.findall(r"/products/[^/]+/(\d+)$", path)]
    return keys


def _id_keys(p):
    """Keys implied by the product id itself (nyk-123 -> nykaa:123)."""
    pre, _, rest = p.product_id.partition("-")
    store = {"nyk": "nykaa", "fk": "flipkart", "pur": "purplle", "mac": "maccaron"}.get(pre)
    keys = [f"{store}:{rest.upper() if store == 'flipkart' else rest}"] if store else []
    if p.store == "amazon" and p.asin:
        keys.append(f"amazon:{p.asin.upper()}")
    return keys


def build_index(products):
    idx = {}
    for p in products:
        for k in _id_keys(p) + url_keys(p.url):
            idx.setdefault(k, p.product_id)
    return idx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def expand_short_link(url, opener=None, hops=4):
    """Follow a share/short link's redirects by reading only the Location header, until it
    lands on a store we know. Returns the final URL, or None."""
    opener = opener or urllib.request.build_opener(_NoRedirect)
    for _ in range(hops):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if _store_of(host) and not host.startswith("dl."):
            return url
        if not any(host == h or host.endswith("." + h) for h in SHORT_HOSTS):
            return None
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0 (SkinKareDealz price-check bot)"})
        try:
            with opener.open(req, timeout=10) as r:
                loc = r.headers.get("Location")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if 300 <= e.code < 400 else None
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not loc:
            return None
        url = urllib.parse.urljoin(url, loc)
    return url if _store_of(urllib.parse.urlsplit(url).hostname or "") else None


# ---------- the price-check engine ----------

class LivePrices:
    """Live checkout price for a store page, fetched when someone asks.

    The catalogue says which page is the same product; this reads that one page now, so the
    reply matches the cart. Purplle only for now (its brand listings understate the checkout
    price). Cached for `ttl` seconds, at most one request per `gap` seconds per store, and a
    refusal pauses that store for an hour. On any failure the tracked price is used."""

    def __init__(self, fetchers=None, ttl=1800, gap=1.0, clock=time.time, sleep=time.sleep):
        from .sources import purplle
        self.fetchers = fetchers if fetchers is not None else {"purplle": (purplle.fetch_product,
                                                                          (purplle.Blocked,))}
        self.ttl, self.gap, self.clock, self.sleep = ttl, gap, clock, sleep
        self.cache, self.last, self.paused = {}, {}, {}

    def get(self, p):
        """(ts, price, in_stock, 'live') or None."""
        if p.store not in self.fetchers:
            return None
        now = self.clock()
        hit = self.cache.get(p.product_id)
        if hit and now - hit[0] < self.ttl:
            return hit
        if self.paused.get(p.store, 0) > now:
            return None
        fetch, blocked = self.fetchers[p.store]
        wait = self.last.get(p.store, 0) + self.gap - now
        if wait > 0:
            self.sleep(wait)
        self.last[p.store] = self.clock()
        try:
            it = fetch(p.product_id)
        except blocked:
            self.paused[p.store] = self.clock() + 3600
            return None
        except Exception:
            return None
        row = (int(self.clock()), it["price"], 1 if it["in_stock"] else 0, "live")
        self.cache[p.product_id] = row
        return row


def current_price(cat, conn, p, now):
    """Live price when we can get one, else the tracked one (None if stale)."""
    live = cat.live.get(p) if cat.live else None
    return live or run.latest_check(conn, p.product_id, now)


class Catalog:
    """Watchlist + matcher + link index, rebuilt when watchlist.csv changes."""

    def __init__(self, watchlist_path, live=None):
        self.path = Path(watchlist_path)
        self.mtime = None
        self.live = live
        self.reload()

    def reload(self):
        mtime = self.path.stat().st_mtime
        if mtime == self.mtime:
            return
        products = run.load_watchlist(self.path)
        self.products = {p.product_id: p for p in products}
        self.matcher = match.Matcher(products, match.load_mrp(self.path.parent / "catalog"))
        self.index = build_index(products)
        self.mtime = mtime

    def lookup(self, url):
        for k in url_keys(url):
            if k in self.index:
                return self.index[k]
        return None


def _title(p):
    size = p.size if p.size and not re.search(re.escape(p.size), p.name, re.I) else ""
    name = p.name if p.name.lower().startswith(p.brand.lower()) else f"{p.brand} {p.name}"
    name = re.split(r"\s+\|\s*", name)[0]          # drop Flipkart/Purplle SEO tails
    return name + (f" · {size}" if size else "")


def _size_text(cat, pid):
    f = cat.matcher.feats[pid]
    others = [cat.products[m.product_id] for m in cat.matcher.matches(pid)] or [cat.products[pid]]
    unit = "ml" if any("ml" in (o.size + o.name).lower() for o in others) else "g"
    return f"{f.size:g} {unit}" if f.size else ""


def _link(p, cfg):
    aff = posts.affiliate_link(p, cfg.get("amazon_tag", ""))
    return (aff, True) if aff else (p.url, False)


def _ago(seconds):
    h = seconds / 3600
    return "just now" if h < 1 else f"{int(h)} h ago" if h < 48 else f"{int(h // 24)} days ago"


def _low_note(conn, product_id, price, ts, days=30):
    """'lowest in 30 days' when we have >= 7 days of history and this is at or below all of it."""
    hist = [h for h in db.history(conn, product_id, ts - days * 86400) if h[0] < ts]
    if not hist or ts - hist[0][0] < 7 * 86400:
        return ""
    low = min(h[1] for h in hist)
    if price <= low:
        return f"lowest in {days} days"
    return f"{days}-day low {posts.inr(low)}"


def _price_line(cat, conn, q, cfg, now, label):
    """('▫️ <a>label</a> — ₹x', price or None, used_affiliate, used_earnkaro)."""
    e = html.escape
    link, aff = _link(q, cfg)
    cur = current_price(cat, conn, q, now)
    if cur and cur[2] and cur[1]:
        what, price = posts.inr(cur[1]), cur[1]
    else:
        what, price = ("out of stock" if cur else "no recent price"), None
    ek = aff and any(h in link for h in EARNKARO_HOSTS)
    return f'<a href="{e(link, quote=True)}">{e(label)}</a> — {what}', price, aff, ek


def compare_unsized(cat, conn, product_id, cfg, now):
    """The listing that was sent shows no size, so we can't say which size it is or
    rank it. Show its own price, then the same product by size on the other stores."""
    e = html.escape
    p = cat.products[product_id]
    store = STORE_NAMES.get(p.store, p.store)
    by_size = cat.matcher.sizes_elsewhere(product_id)
    line, _, used_aff, used_ek = _price_line(cat, conn, p, cfg, now, store)
    out = [f"🔎 <b>{e(_title(p))}</b>", "", f"▫️ {line} <i>(size not listed on {e(store)})</i>"]
    if by_size:
        unit = "ml" if "ml" in " ".join(cat.products[m.product_id].size + cat.products[m.product_id].name
                                       for ms in by_size.values() for m in ms).lower() else "g"
        out += ["", f"{e(store)} doesn't say which size this is, so here it is by size elsewhere:"]
        for size, ms in by_size.items():
            parts = []
            for m in ms:
                q = cat.products[m.product_id]
                line, price, aff, ek = _price_line(cat, conn, q, cfg, now, STORE_NAMES.get(q.store, q.store))
                parts.append((price is None, price or 0, line))
                used_aff |= aff and price is not None
                used_ek |= ek and price is not None
            parts.sort()
            out.append(f"<b>{size:g} {unit}</b>: " + " · ".join(x[2] for x in parts))
        out.append("\n<i>Check the size on the product page before comparing.</i>")
    else:
        out.append("\nI haven't found this product on the other stores I track.")
    if used_ek:
        out.append("<i>Open the link in your browser (not the store app) to complete the purchase.</i>")
    if used_aff:
        out.append(f"<i>{e(posts.DISCLOSURE)}</i>")
    return "\n".join(out)


def compare(cat, conn, product_id, cfg, now=None):
    """HTML reply comparing one product across stores."""
    now = now or int(time.time())
    p = cat.products[product_id]
    feats = cat.matcher.feats[product_id]
    if feats.size is None and not feats.combo:
        return compare_unsized(cat, conn, product_id, cfg, now)
    found = cat.matcher.matches(product_id)
    exact = [product_id] + [m.product_id for m in found if m.size_known]
    unsized = [m.product_id for m in found if not m.size_known]

    priced, other = [], []
    for pid in exact:
        q = cat.products[pid]
        cur = current_price(cat, conn, q, now)
        if cur and cur[2] and cur[1]:
            priced.append((cur[1], q, cur[0]))
        else:
            other.append((q, "out of stock" if cur else "no recent price"))
    priced.sort(key=lambda x: (x[0], x[1].store != p.store))

    e = html.escape
    out = [f"🔎 <b>{e(_title(p))}</b>" + (f" · {_size_text(cat, product_id)}" if feats.size_from_mrp else "")]
    if feats.size_from_mrp:
        out.append(f"<i>{e(STORE_NAMES.get(p.store, p.store))} doesn't list the size; its MRP "
                   f"({posts.inr(cat.matcher.mrp[product_id])}) matches the {_size_text(cat, product_id)} "
                   f"on other stores.</i>")
    used_aff = used_ek = False
    if priced:
        best_price, best, best_ts = priced[0]
        out.append(f"💰 Cheapest: <b>{posts.inr(best_price)} on {STORE_NAMES.get(best.store, best.store)}</b>")
        out.append("")
        for i, (price, q, ts) in enumerate(priced):
            link, aff = _link(q, cfg)
            used_aff |= aff
            used_ek |= aff and any(h in link for h in EARNKARO_HOSTS)
            note = _low_note(conn, q.product_id, price, ts)
            mark = "✅" if i == 0 else "▫️"
            store = STORE_NAMES.get(q.store, q.store)
            if q.product_id != product_id and cat.matcher.feats[q.product_id].size_from_mrp:
                note = "size worked out from MRP" + (f" · {note}" if note else "")
            out.append(f'{mark} <a href="{e(link, quote=True)}">{e(store)}</a> — {posts.inr(price)}'
                       + (f" · <i>{e(note)}</i>" if note else ""))
    else:
        out.append("No current in-stock price for this one right now.")
    for q, why in other:
        link, _ = _link(q, cfg)
        out.append(f'▫️ <a href="{e(link, quote=True)}">{e(STORE_NAMES.get(q.store, q.store))}</a> — {why}')
    if unsized:
        names = ", ".join(f'<a href="{e(_link(cat.products[x], cfg)[0], quote=True)}">'
                          f'{e(STORE_NAMES.get(cat.products[x].store, cat.products[x].store))}</a>'
                          for x in unsized)
        out.append(f"\nAlso listed on {names} — size not shown there, so not compared.")
    if len(exact) == 1 and not unsized:
        out.append("\nI haven't found this exact size on the other stores I track.")
    if priced:
        newest, oldest = _ago(now - max(t for _, _, t in priced)), _ago(now - min(t for _, _, t in priced))
        when = newest if newest == oldest else f"{newest} to {oldest}"
        out.append(f"\n<i>Prices checked {when}; "
                   f"they can change, so confirm at checkout.</i>")
    if used_ek:
        out.append("<i>Open the link in your browser (not the store app) to complete the purchase.</i>")
    if used_aff:
        out.append(f"<i>{e(posts.DISCLOSURE)}</i>")
    return "\n".join(out)


def search_reply(cat, conn, text, now=None):
    """(html, buttons): up to 5 distinct products for a typed name."""
    now = now or int(time.time())
    hits, seen, rows = cat.matcher.search(text, limit=60), set(), []
    want_combo = re.search(r"combo|kit|duo|trio|set|bundle|pack of", text, re.I)
    for score, pid in hits:
        if pid in seen or score < 0.5 or (cat.matcher.feats[pid].combo and not want_combo):
            continue
        group = {pid} | {m.product_id for m in cat.matcher.matches(pid)}
        seen |= group
        p = cat.products[pid]
        f = cat.matcher.feats[pid]
        name = re.split(r"\s+\|\s*|\s+with\s+|,", _title(p).split(" · ")[0], maxsplit=1, flags=re.I)[0]
        name = (name[:40] + "…") if len(name) > 41 else name
        size = f"{f.size:g}{'ml' if 'ml' in (p.size + p.name).lower() else 'g'}" if f.size else ""
        size += f" ×{f.pack}" if f.pack > 1 else ""
        label = " · ".join(x for x in (name, size, STORE_NAMES.get(p.store, p.store)) if x)
        rows.append([{"text": label, "callback_data": f"p:{pid}"}])
        if len(rows) == 5:
            break
    if not rows:
        return ("I couldn't find that in the products I track. Try the brand and product name, "
                "or send a link.", None)
    return "Which one? Tap to compare prices across stores:", {"inline_keyboard": rows}


def answer(cat, conn, text, cfg, now=None, expand=expand_short_link):
    """Reply for one incoming message: (kind, product_id or None, html, reply_markup)."""
    text = (text or "").strip()
    if (not text or text.startswith("/start") or text.startswith("/help")
            or re.fullmatch(r"(hi+|hello|hey+|hii+|start|help|namaste)[!. ]*", text, re.I)):
        return "help", None, HELP, None
    urls = URL_RE.findall(text)
    if urls:
        url = urls[0].rstrip(".,)")
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not _store_of(host) or host.startswith("dl."):
            url = expand(url) or url
        pid = cat.lookup(url)
        if pid:
            return "link", pid, compare(cat, conn, pid, cfg, now), None
        if not _store_of(urllib.parse.urlsplit(url).hostname or ""):
            return "link", None, ("I can read links from Nykaa, Purplle, Flipkart, Tira, Maccaron "
                                  "and Amazon. If this is a share link, try opening it and sending the "
                                  "full page address."), None
        return "link", None, ("I don't track this product yet. I've noted it, and the most-asked "
                              "products get added first. You can also type its name to see what I have."), None
    body, kb = search_reply(cat, conn, text, now)
    return "search", None, body, kb


# ---------- running the bot ----------

def refresh_prices(dest, log):
    """Copy prices.db from origin/data (what GitHub Actions last saved) into dest."""
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "data"], cwd=ROOT, check=True, timeout=120)
        blob = subprocess.run(["git", "show", "origin/data:prices.db"], cwd=ROOT, check=True,
                              capture_output=True, timeout=120).stdout
    except (subprocess.SubprocessError, OSError) as ex:
        log(f"refresh: failed ({ex}); keeping the current copy")
        return False
    if len(blob) < 4096 or not blob.startswith(b"SQLite format 3"):
        log("refresh: origin/data:prices.db doesn't look like a database; keeping the current copy")
        return False
    tmp = Path(str(dest) + ".tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, dest)
    log(f"refresh: prices.db updated ({len(blob) // 1024} KB)")
    return True


class Bot:
    def __init__(self, cfg, cat, prices_path, state_path, api=telegram.call, log=print):
        self.cfg, self.cat, self.prices_path, self.api, self.log = cfg, cat, prices_path, api, log
        self.state = sqlite3.connect(str(state_path))
        self.state.executescript(BOT_SCHEMA)
        self.recent = {}

    def _get(self, key, default=None):
        row = self.state.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key, value):
        self.state.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, str(value)))
        self.state.commit()

    def _call(self, method, **params):
        return self.api(self.cfg["telegram_token"], method, params)

    def _limited(self, chat_id, now):
        n, window = RATE_LIMIT
        hits = [t for t in self.recent.get(chat_id, []) if now - t < window]
        hits.append(now)
        self.recent[chat_id] = hits
        return len(hits) > n

    def _send(self, chat_id, text, markup=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}}
        if markup:
            params["reply_markup"] = markup
        self._call("sendMessage", **params)

    def handle(self, upd, now=None):
        now = now or int(time.time())
        conn = sqlite3.connect(f"file:{self.prices_path}?mode=ro", uri=True)
        try:
            if "callback_query" in upd:
                cq = upd["callback_query"]
                chat_id = cq["message"]["chat"]["id"]
                self._call("answerCallbackQuery", callback_query_id=cq["id"])
                pid = (cq.get("data") or "").removeprefix("p:")
                if pid in self.cat.products and not self._limited(chat_id, now):
                    self._send(chat_id, compare(self.cat, conn, pid, self.cfg, now))
                    self._log(now, chat_id, "pick", pid, pid, True)
                return
            msg = upd.get("message") or {}
            chat = msg.get("chat") or {}
            if chat.get("type") != "private" or "text" not in msg:
                return
            chat_id = chat["id"]
            if self._limited(chat_id, now):
                if len(self.recent[chat_id]) == RATE_LIMIT[0] + 1:
                    self._send(chat_id, "That's a lot at once. Give me a minute 🙂")
                return
            kind, pid, body, kb = answer(self.cat, conn, msg["text"], self.cfg, now)
            self._send(chat_id, body, kb)
            if kind != "help":
                self._log(now, chat_id, kind, msg["text"][:500], pid, bool(pid) or kb is not None)
        finally:
            conn.close()

    def _log(self, ts, chat_id, kind, text, pid, found):
        self.state.execute("INSERT INTO queries VALUES (?,?,?,?,?,?)",
                           (ts, chat_id, kind, text, pid, int(found)))
        self.state.commit()

    def poll_once(self, timeout=50):
        offset = int(self._get("offset", 0))
        updates = self.api(self.cfg["telegram_token"], "getUpdates",
                           {"offset": offset, "timeout": timeout,
                            "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 15)
        for upd in updates:
            try:
                self.handle(upd)
            except Exception as ex:          # one bad update must not stop the bot
                self.log(f"update {upd.get('update_id')}: {type(ex).__name__}: {ex}")
            self._set("offset", upd["update_id"] + 1)
        return len(updates)


def main(argv=None):
    ap = argparse.ArgumentParser(description="SkinKareDealz price-check bot")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--watchlist", default=str(ROOT / "data" / "watchlist.csv"))
    ap.add_argument("--prices", default=str(ROOT / "data" / "bot_prices.db"),
                    help="the bot's own copy of prices.db (default %(default)s)")
    ap.add_argument("--state", default=str(ROOT / "data" / "bot.db"),
                    help="bot state + query log (default %(default)s)")
    ap.add_argument("--refresh-hours", type=float, default=1.0,
                    help="re-copy prices.db from origin/data this often; 0 = never")
    ap.add_argument("--ask", metavar="TEXT", help="print the reply to TEXT and exit (no Telegram)")
    a = ap.parse_args(argv)
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", file=sys.stderr, flush=True)
    sys.stdout.reconfigure(encoding="utf-8")

    cfg = run.load_config(a.config)
    prices = Path(a.prices)
    if a.refresh_hours and (not prices.exists() or time.time() - prices.stat().st_mtime > a.refresh_hours * 3600):
        refresh_prices(prices, log)
    if not prices.exists():
        sys.exit(f"no price database at {prices}; run once with network access to copy it from origin/data")
    cat = Catalog(a.watchlist, live=LivePrices())

    if a.ask is not None:
        conn = sqlite3.connect(f"file:{prices}?mode=ro", uri=True)
        kind, pid, body, kb = answer(cat, conn, a.ask, cfg)
        print(body)
        for row in (kb or {}).get("inline_keyboard", []):
            print(f"  [{row[0]['text']}]  -> {row[0]['callback_data']}")
        return

    if not cfg.get("telegram_token"):
        sys.exit("telegram_token is missing (config.toml or TELEGRAM_TOKEN)")
    bot = Bot(cfg, cat, prices, a.state, log=log)
    me = telegram.call(cfg["telegram_token"], "getMe")
    telegram.call(cfg["telegram_token"], "setMyCommands",
                  {"commands": [{"command": "help", "description": "How to use the price check"}]})
    log(f"@{me['username']} is running with {len(cat.products)} products. Ctrl+C to stop.")
    last_refresh, backoff = time.time(), 5
    while True:
        try:
            bot.poll_once()
            backoff = 5
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as ex:              # network blips, Telegram 5xx
            log(f"poll: {type(ex).__name__}: {ex}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        if a.refresh_hours and time.time() - last_refresh > a.refresh_hours * 3600:
            refresh_prices(prices, log)
            last_refresh = time.time()
        try:
            cat.reload()
        except OSError as ex:
            log(f"watchlist reload: {ex}")


if __name__ == "__main__":
    main()
