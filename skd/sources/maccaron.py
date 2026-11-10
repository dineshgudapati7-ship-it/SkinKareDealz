"""Maccaron (maccaron.in) products via its public sitemap + product-page JSON-LD.

robots.txt allows /en/products/ and publishes /sitemap.xml. Product URLs look like
  https://maccaron.in/en/products/<brand-prefix>_<slug>/<id>
so a brand's products are found by URL prefix. Brand pages don't carry prices,
so each product page is fetched (slowly) and its schema.org Product read:
name, price, availability and sku (an EAN barcode for many products).
"""
import json
import re
import time
import urllib.error
import urllib.request

BASE = "https://maccaron.in"
UA = "Mozilla/5.0 (compatible; SkinKareDealz-catalog/0.1)"
_LOC = re.compile(r"<loc>([^<]+)</loc>")
_LDJSON = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
_URL = re.compile(r"/en/products/([a-z0-9-]+)_[^/]+/(\d+)$")
_EAN = re.compile(r"^\d{8}$|^\d{12,14}$")


class Blocked(RuntimeError):
    pass


def _get(url, opener=urllib.request.urlopen):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"maccaron returned {e.code} for {url}") from e
        if e.code in (404, 410):
            return None
        raise


def product_urls(prefixes, delay=1.0, opener=urllib.request.urlopen):
    """{prefix: [product urls]} from the product sitemaps."""
    wanted = set(prefixes)
    out = {p: [] for p in wanted}
    index = _get(f"{BASE}/sitemaps/products-index.xml", opener) or ""
    for sm in _LOC.findall(index):
        for url in _LOC.findall(_get(sm.strip(), opener) or ""):
            m = _URL.search(url.strip())
            if m and m.group(1) in wanted:
                out[m.group(1)].append(url.strip())
        time.sleep(delay)
    return out


def product_id(url):
    m = _URL.search(url)
    return m.group(2) if m else None


def parse_product(html):
    """Product dict from a product page's JSON-LD, or None."""
    for block in _LDJSON.findall(html or ""):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        if d.get("@type") != "Product":
            continue
        offers = d.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        try:
            price = float(offers.get("price"))
        except (TypeError, ValueError):
            price = None
        sku = str(d.get("gtin13") or d.get("sku") or "").strip()
        return {
            "name": (d.get("name") or "").strip(),
            "price": price,
            "in_stock": str(offers.get("availability", "")).endswith("InStock"),
            "ean": sku if _EAN.match(sku) else "",
        }
    return None


def fetch_product(url, opener=urllib.request.urlopen):
    """Parsed product, or None if the page is gone / has no product data."""
    return parse_product(_get(url, opener))
