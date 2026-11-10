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
                "url": f"{BASE}/{it['url']}" if own and it.get("url") else f"{BASE}/product/{pid}",
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
