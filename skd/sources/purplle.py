"""Purplle brand catalogues from the JSON that powers its public brand pages.

Endpoint: /neo/merch/items/v2?list_type=brand&list_type_value=<slug>&page=N
(24 listings per page, `has_more` says if another page exists). Brand pages are
allowed by purplle.com/robots.txt; we fetch slowly and stop on any 403/429.

Each listing can carry size/pack variants (`multiple_variants`); every variant
has its own product_id and price, so each becomes its own row.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.purplle.com"
UA = "Mozilla/5.0 (compatible; SkinKareDealz-catalog/0.1)"
_SIZE_TAIL = re.compile(r"[\s,(-]*\d+(\.\d+)?\s*(gm|g|ml|mg|kg|l|n|pcs?|pieces?)\b[\s)]*$", re.I)


class Blocked(RuntimeError):
    pass


def page_url(slug, page):
    q = urllib.parse.urlencode({
        "tenant": "PURPLLE_COM", "sub_tenant": "MAIN_SITE", "userType": 0,
        "list_type": "brand", "list_type_value": slug, "page": page,
        "sort_by": "rel", "elite": 0})
    return f"{BASE}/neo/merch/items/v2?{q}"


def fetch_page(slug, page, opener=urllib.request.urlopen):
    req = urllib.request.Request(page_url(slug, page), headers={"User-Agent": UA})
    try:
        with opener(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"purplle returned {e.code} for {slug} page {page}") from e
        if e.code == 404:          # brand slug no longer listed
            return {"items": [], "has_more": 0, "total_items": 0}
        raise


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_items(body):
    """Yield one dict per product_id (variants expanded) from a listing response."""
    for it in body.get("items") or []:
        if it.get("type") != "product":
            continue
        base_name = (it.get("name") or "").strip()
        # Store promotion shown on the listing (e.g. free gift above ₹499). It is
        # listing-level, so every size variant of the listing gets it too.
        promo = it.get("offers_pd") or {}
        offer = (promo.get("cart_item_title") or promo.get("title") or "").strip() if isinstance(promo, dict) else ""
        offer_ids = "|".join(str(i) for i in (promo.get("offer_ids") or [])) if isinstance(promo, dict) else ""
        variants = [v for m in (it.get("multiple_variants") or []) for v in (m.get("items") or [])]
        if not variants:
            variants = [{"product_id": it.get("id"), "option_display_value": "",
                         "price": it.get("price"), "our_price": it.get("our_price"),
                         "offer_price": it.get("offer_price"),
                         "stock_status": it.get("stock_status")}]
        for v in variants:
            pid = str(v.get("product_id") or "").strip()
            if not pid:
                continue
            size = (v.get("option_display_value") or "").strip()
            own = pid == str(it.get("id"))
            name = base_name if (own and not size) else _SIZE_TAIL.sub("", base_name).rstrip(" ,-")
            yield {
                "purplle_id": pid,
                "name": name,
                "size": size,
                "url": f"{BASE}/product/{it['url']}" if own and it.get("url") else f"{BASE}/product/{pid}",
                "mrp": _num(v.get("price")),
                "price": _num(v.get("offer_price") or v.get("our_price")),
                "in_stock": str(v.get("stock_status")) == "1",
                "offer": offer,
                "offer_ids": offer_ids,
            }


def fetch_brand(slug, delay=2.0, max_pages=60, opener=urllib.request.urlopen, log=print):
    """All products for one brand slug, de-duplicated by product_id."""
    seen, out = set(), []
    for page in range(1, max_pages + 1):
        body = fetch_page(slug, page, opener)
        for row in parse_items(body):
            if row["purplle_id"] not in seen:
                seen.add(row["purplle_id"])
                out.append(row)
        if page == 1:
            log(f"  {slug}: {body.get('total_items')} listings")
        if not body.get("has_more") or not body.get("items"):
            break
        time.sleep(delay)
    return out


# ---------- product pages: the price you actually pay ----------
# The brand-listing JSON above is NOT the checkout price. On 2026-11-12, 12 of 20 sampled
# products listed lower than their product page (e.g. 366 vs 425; the cart showed 425), and
# no listing field (iselite, our_price, specialofferPrice) predicted which. The product page
# /product/<id> carries the offer for that exact size in an `availability` block, plus a
# schema.org Offer. Those are what we record as the current price (source purplle-page).

class NotFound(RuntimeError):
    pass


_AVAIL = re.compile(r"availability:\$R\[\d+\]=\{([^{}]*)")
_KV = re.compile(r'(\w+):(?:"([^"]*)"|(-?[\d.]+))')
_LD_OFFER = re.compile(r'"offers":\{"@type":"Offer","availability":"[^"]*?(\w+)","price":"([\d.]+)"')
PAGE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def parse_product_page(html):
    """{'mrp', 'price', 'in_stock'} for the size the page is for. NotFound if the page holds
    no offer at all."""
    m = _AVAIL.search(html)
    if m:
        kv = {k: (s if s is not None else n) for k, s, n in _KV.findall(m.group(1))}
        price, mrp = _num(kv.get("offerPrice") or kv.get("ourPrice")), _num(kv.get("mrp"))
        in_stock = str(kv.get("stockStatus")) == "1" and bool(price)
    else:
        ld = _LD_OFFER.search(html)
        if not ld:
            raise NotFound("purplle page without an offer")
        price, mrp = _num(ld.group(2)), None
        in_stock = ld.group(1) == "InStock" and bool(price)
    return {"mrp": mrp or None, "price": price if in_stock else None, "in_stock": in_stock}


def fetch_product(product_id, opener=urllib.request.urlopen, timeout=20):
    """Live offer for one pur-<id> / <id>. Blocked on 403/429, NotFound on 404."""
    pid = str(product_id).removeprefix("pur-")
    req = urllib.request.Request(f"{BASE}/product/{pid}", headers={"User-Agent": PAGE_UA})
    try:
        with opener(req, timeout=timeout) as r:
            html = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"purplle returned {e.code} for product {pid}") from e
        if e.code == 404:
            raise NotFound(f"purplle product {pid} not found") from e
        raise
    return parse_product_page(html)
