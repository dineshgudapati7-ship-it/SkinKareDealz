"""Flipkart products for a brand, from the state JSON in its public search pages.

Fetched with Scrapling's Fetcher, the same "slow and steady" method as Nykaa, chosen by the
owner on 2026-11-12: at least MIN_DELAY seconds between requests, and a full stop on the
first refusal. Never add proxies or retries.

Page: /search?q=<brand>&page=N (robots.txt allows it, but not sort= or facets=, so we use
neither). About 40 products per page, from window.__INITIAL_STATE__. A search for the brand
name also returns other brands' products, so only titles that start with the brand name
are kept. The search stops after GIVE_UP_AFTER pages in a row with no match.

Page 1 has the full price block (finalPrice / mrp). Later pages only have
`prices: [{strikeOff: true, value: MRP}, {strikeOff: false, value: price}]`.
"""
import json
import re
import time

from .nykaa import Blocked, MIN_DELAY, _default_get, _norm

BASE = "https://www.flipkart.com"
GIVE_UP_AFTER = 2
_STATE_START = "window.__INITIAL_STATE__"
_UNDEF = re.compile(r"([:\[,]\s*)undefined\b")


def fetch_state(url, get=_default_get):
    status, _, html = get(url)
    i = html.find(_STATE_START)
    if status != 200 or i < 0:
        raise Blocked(f"flipkart returned {status} (state={'yes' if i >= 0 else 'no'}) for {url}")
    state, _ = json.JSONDecoder().raw_decode(_UNDEF.sub(r"\1null", html[html.find("{", i):]))
    return state


def _products(state):
    out = []

    def walk(o):
        if isinstance(o, dict):
            info = o.get("productInfo")
            v = info.get("value") if isinstance(info, dict) else None
            if isinstance(v, dict) and isinstance(v.get("pricing"), dict) and v.get("id"):
                out.append(v)
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(state)
    return out


def _prices(pricing):
    final = (pricing.get("finalPrice") or {}).get("value")
    mrp = (pricing.get("mrp") or {}).get("value")
    listed = pricing.get("prices") or []
    if final is None:
        live = [p.get("value") for p in listed if not p.get("strikeOff") and p.get("value") is not None]
        final = min(live) if live else None
    if mrp is None:
        struck = [p.get("value") for p in listed if p.get("strikeOff") and p.get("value") is not None]
        mrp = max(struck) if struck else final
    return (float(mrp) if mrp is not None else None), (float(final) if final is not None else None)


def parse_products(state, brand):
    """Rows for this brand only (title starts with the brand name, or productBrand matches)."""
    want = _norm(brand)
    for v in _products(state):
        titles = v.get("titles") or {}
        name = (titles.get("title") or "").strip()
        if not (_norm(v.get("productBrand")) == want or _norm(name).startswith(want)):
            continue
        mrp, price = _prices(v["pricing"])
        yield {
            "flipkart_id": v["id"],
            "name": name,
            "size": (titles.get("subtitle") or "").strip(),
            "url": BASE + (v.get("baseUrl") or f"/p/{v['id']}"),
            "mrp": mrp,
            "price": price,
            "in_stock": (v.get("availability") or {}).get("displayState") == "IN_STOCK",
        }


def has_more(state):
    return bool((((state.get("pageDataV4") or {}).get("page") or {}).get("pageData") or {}).get("hasMorePages"))


def fetch_brand(brand, delay=MIN_DELAY, max_pages=15, get=_default_get, log=print, sleep=time.sleep):
    """All of one brand's products found by searching its name. Raises Blocked on a refusal."""
    delay = max(delay, MIN_DELAY)
    q = brand.replace("&", "%26").replace(" ", "%20")
    seen, out, misses = set(), [], 0
    for page in range(1, max_pages + 1):
        if page > 1:
            sleep(delay)
        state = fetch_state(f"{BASE}/search?q={q}&page={page}", get)
        new = 0
        for row in parse_products(state, brand):
            if row["flipkart_id"] not in seen:
                seen.add(row["flipkart_id"])
                out.append(row)
                new += 1
        misses = 0 if new else misses + 1
        if misses >= GIVE_UP_AFTER or not has_more(state):
            break
    log(f"  flipkart {brand}: {len(out)} products ({page} pages)")
    return out


# ---------- product pages (live checks from the price-check bot) ----------
# /p/<itm...>?pid=<FSN> is allowed by robots.txt. The page's state JSON has, under
# multiWidgetState.pageDataResponse.pageContext.fdpEventTracking.events.psi:
#   fsn (the product id), ppd.finalPrice / ppd.mrp, pls.availabilityStatus, nb.nonBuyable.
# ppd.nepPrice is a lower new-customer price; we never use it.

def _find_psi(o):
    if isinstance(o, dict):
        if "fsn" in o and isinstance(o.get("ppd"), dict):
            return o
        for v in o.values():
            r = _find_psi(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_psi(v)
            if r:
                return r
    return None


def parse_product_page(state, pid):
    """{'mrp', 'price', 'in_stock'} for product `pid`; ValueError if the page is for
    something else or has no price block."""
    psi = _find_psi(state)
    if not psi:
        raise ValueError("flipkart page without a price block")
    if str(psi.get("fsn", "")).upper() != str(pid).upper():
        raise ValueError(f"flipkart page is for {psi.get('fsn')}, not {pid}")
    ppd = psi["ppd"]
    price = ppd.get("finalPrice") or ppd.get("fsp")
    pls, nb = psi.get("pls") or {}, psi.get("nb") or {}
    in_stock = (pls.get("availabilityStatus") == "IN_STOCK" and not nb.get("nonBuyable")
                and price is not None)
    return {"mrp": float(ppd["mrp"]) if ppd.get("mrp") else None,
            "price": float(price) if in_stock else None, "in_stock": in_stock}


def fetch_product(url, pid, get=_default_get):
    """Live offer for one product page. Raises Blocked on a refusal."""
    return parse_product_page(fetch_state(url, get), pid)
