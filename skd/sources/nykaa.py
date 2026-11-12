"""Nykaa brand catalogues from the state JSON embedded in its public brand pages.

Nykaa sits behind Akamai and answers plain scripts (urllib, curl) with "Access
Denied". Fetching here goes through Scrapling's Fetcher, which presents a real
Chrome TLS fingerprint. The owner chose this on 2026-11-11, on two conditions:
**slow and steady**, meaning a fixed gap of at least MIN_DELAY seconds between
requests, and a stop at the first refusal. Never add proxies or retries that
get around a refusal.

    pip install "scrapling[fetchers]"      # only needed for Nykaa

Brand page: /brands/<slug>/c/<id> (or /luxe/brands/... for Nykaa Luxe brands)?page_no=N&sort=popularity. Each page holds 20
listings in window.__PRELOADED_STATE__.categoryListing.listingData. `totalFound`
is the brand total, and `stopFurtherCall` is set on the last page. A search for
the brand name redirects to its brand page, and that is how brand paths are found.

A size-variant ("configurable") listing shows its default size's price. Other
sizes sometimes appear as their own "simple" listings, which share the parent's
slug.
"""
import json
import re
import time

BASE = "https://www.nykaa.com"
MIN_DELAY = 3.0
_STATE = re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)
_BRAND_PATH = re.compile(r"^(luxe/)?brands/[^/?#]+/c/\d+$")   # luxe brands live under /luxe/


class Blocked(RuntimeError):
    pass


def _default_get(url):
    from scrapling.fetchers import Fetcher          # lazy: only Nykaa needs it
    page = Fetcher.get(url, impersonate="chrome", timeout=30)
    return page.status, page.url, page.html_content


def fetch_state(url, get=_default_get):
    """(final_url, state dict). Raises Blocked on anything that isn't a normal page."""
    status, final_url, html = get(url)
    if status != 200 or "Access Denied" in html[:2000]:
        raise Blocked(f"nykaa returned {status} for {url}")
    m = _STATE.search(html)
    if not m:
        raise Blocked(f"nykaa page without product data (challenge page?) for {url}")
    return final_url, json.loads(m.group(1))


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and"))


def find_brand_path(brand, get=_default_get):
    """'brands/<slug>/c/<id>' for a brand name, or None if Nykaa doesn't redirect to it."""
    url = f"{BASE}/search/result/?q={brand.replace(' ', '%20').replace('&', '%26')}"
    _, state = fetch_state(url, get)
    ld = (state.get("categoryListing") or {}).get("listingData") or {}
    path = (ld.get("canonical") or ld.get("url") or "").strip("/")
    if ld.get("listingType") != "brand" or not _BRAND_PATH.match(path):
        return None
    if _norm(ld.get("brandName")) != _norm(brand):
        return None                                  # redirected to a different brand
    return path


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_products(listing):
    """One dict per listing id from categoryListing.listingData."""
    for p in listing.get("products") or []:
        pid = str(p.get("id") or p.get("productId") or "").strip()
        if not pid:
            continue
        slug = (p.get("slug") or "").strip("/")
        url = f"{BASE}/{slug}" if slug else f"{BASE}/p/{pid}"
        if slug and not slug.endswith(f"/p/{pid}"):  # a size listed under its parent's page
            url += f"?skuId={pid}"
        yield {
            "nykaa_id": pid,
            "sku": p.get("sku") or "",
            "name": (p.get("name") or "").strip(),
            "size": (p.get("packSize") or "").strip(),
            "type": p.get("type") or "",
            "url": url,
            "mrp": _num(p.get("mrp")),
            "price": _num(p.get("price")),
            "in_stock": bool(p.get("inStock")),
        }


def fetch_brand(path, delay=MIN_DELAY, max_pages=60, get=_default_get, log=print, sleep=time.sleep):
    """All listings on one brand path, de-duplicated by id. Raises Blocked on a refusal."""
    delay = max(delay, MIN_DELAY)
    seen, out = set(), []
    for page in range(1, max_pages + 1):
        if page > 1:
            sleep(delay)
        _, state = fetch_state(f"{BASE}/{path}?page_no={page}&sort=popularity", get)
        ld = (state.get("categoryListing") or {}).get("listingData") or {}
        if page == 1:
            log(f"  {path}: {ld.get('totalFound')} listings")
        for row in parse_products(ld):
            if row["nykaa_id"] not in seen:
                seen.add(row["nykaa_id"])
                out.append(row)
        if ld.get("stopFurtherCall") or not ld.get("products"):
            break
    return out
