"""Official brand (D2C) sites, read only where they serve a plain, polite client.

data/brands.csv -> brand_site holds "<kind>:<base url>" (or "-": no Indian D2C store):

  feed     a public product feed the site publishes for machines (WOW: /feeds/product-list-csv,
           linked from its llms.txt). One request prices the whole catalogue.
  shopify  /products/<handle>.js: every variant with price, compare-at price, stock, barcode.
  page     product HTML. Tried in order: Next.js __NEXT_DATA__ variants (The Body Shop),
           schema.org JSON-LD Product (Kama Ayurveda), itemprop microdata (Forest Essentials).

Products are found through each site's sitemap. robots.txt is honoured per URL.

Probe, 2026-11-13: 20 of the 21 Shopify brand stores (Minimalist, Derma Co, Plum, Foxtale ...)
answer 429 to the first product request, JSON and HTML alike. That is bot protection, so a
domain stops at its first 403/429 for the rest of the run: no retries, no browser-like
headers, no proxies. Pilgrim's Shopify store did answer.

Checked against the live pages in Chrome on 2026-11-13:
- WOW Retinol Face Wash: feed 399, page ₹399.
- Kama Hand Cream: JSON-LD 1250, page ₹1250.00.
- Forest Essentials Date & Litchi 15g: microdata 2095, page ₹2,095.
- The Body Shop Trio-3pc: the variant's `price` is the MRP (₹1,335). The checkout price is its
  `special_price` (₹1,068, "Flat 20% off").
- The Body Shop Tea Tree Body Wash: the JSON-LD said InStock, but the page said Out of Stock, as
  did the variant's isInStock. So variant data wins over JSON-LD.
"""
import csv
import html as htmllib
import io
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import date

UA = "Mozilla/5.0 (compatible; SkinKareDealz-catalog/0.1)"
MIN_DELAY = 3.0
MAX_NET_FAILS = 5       # consecutive connection errors (DNS, reset, timeout) before a site stops
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_LDJSON = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S)
_NEXT = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)
_EAN = re.compile(r"^\d{8}$|^\d{12,14}$")


class Blocked(RuntimeError):
    pass


def _ssl_context():
    # Python's Windows trust store rejects several of these sites ("certificate has expired")
    # while curl/Chrome accept them; certifi's bundle (installed with Scrapling) does too.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_CTX = _ssl_context()


def _get(url, opener=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        if opener:
            r = opener(req, timeout=30)
        else:
            r = urllib.request.urlopen(req, timeout=30, context=_CTX)
        with r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            raise Blocked(f"{urllib.parse.urlsplit(url).hostname} returned {e.code}") from e
        if e.code in (404, 410):
            return None
        raise


def parse_site(value):
    """'shopify:https://x.com' -> ('shopify', 'https://x.com'); '-' or '' -> None."""
    kind, _, base = (value or "").strip().partition(":")
    if kind not in ("feed", "shopify", "page") or not base.startswith("https://"):
        return None
    return kind, base.rstrip("/")


def site_key(base):
    """Short stable id for a site: https://www.buywow.in -> buywow."""
    host = urllib.parse.urlsplit(base).hostname or ""
    host = host.removeprefix("www.")
    return host.split(".")[0]


def _num(v):
    try:
        f = float(str(v).replace(",", "").split()[0])
    except (TypeError, ValueError, IndexError):
        return None
    return f if f > 0 else None


# ---------- discovery ----------

class Site:
    """One brand site: robots rules, a request gap, and a hard stop on the first refusal."""

    def __init__(self, kind, base, delay=MIN_DELAY, opener=None, sleep=time.sleep):
        self.kind, self.base = kind, base.rstrip("/")
        self.key = site_key(base)
        self.delay, self.opener, self.sleep = max(delay, MIN_DELAY), opener, sleep
        self._last = None
        self.robots = None
        self._net_fails = 0

    def get(self, url):
        if self.robots is None:
            self.robots = urllib.robotparser.RobotFileParser()
            self.robots.parse((self._fetch(f"{self.base}/robots.txt") or "").splitlines())
        if not self.robots.can_fetch(UA, url):
            return None
        return self._fetch(url)

    def _fetch(self, url):
        if self._last is not None:
            self.sleep(self.delay)
        self._last = True
        try:
            body = _get(url, self.opener)
        except urllib.error.HTTPError:
            self._net_fails = 0
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # Our connection or theirs is down: stop instead of spending hundreds of slow requests on it.
            self._net_fails += 1
            if self._net_fails >= MAX_NET_FAILS:
                raise Blocked(f"{urllib.parse.urlsplit(url).hostname} unreachable "
                              f"{self._net_fails} times in a row ({e})") from e
            raise
        self._net_fails = 0
        return body

    def sitemap_urls(self):
        """Product page URLs from /sitemap.xml (index: only its product sitemaps)."""
        top = self.get(f"{self.base}/sitemap.xml") or ""
        locs = [u.replace("&amp;", "&") for u in _LOC.findall(top)]
        if "<sitemapindex" in top:
            out = []
            for sm in locs:
                if "product" in sm.lower():
                    out += [u.replace("&amp;", "&") for u in _LOC.findall(self.get(sm) or "")]
            locs = out
        if self.kind == "shopify":
            locs = [u for u in locs if "/products/" in u]
        # The Body Shop lists every product twice: the page and its /reviews page.
        locs = [u for u in locs if not u.rstrip("/").endswith("/reviews")]
        return list(dict.fromkeys(u for u in locs if u.startswith("http")))

    # ---------- per kind ----------

    def items(self, url=None):
        """Products (dicts, one per size/variant). feed: the whole catalogue; else one page."""
        if self.kind == "feed":
            return parse_feed(self.get(f"{self.base}/feeds/product-list-csv") or "")
        if self.kind == "shopify":
            raw = self.get(url.split("?")[0].rstrip("/") + ".js")
            return parse_shopify(raw, url) if raw else []
        html = self.get(url)
        return parse_page(html, url) if html else []


def parse_feed(text):
    """Google-Merchant-style CSV (id, title, link, availability, price, sale_price, gtin)."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        mrp, sale = _num(r.get("price")), _num(r.get("sale_price"))
        if not r.get("id") or not (mrp or sale):
            continue
        gtin = (r.get("gtin") or "").strip()
        out.append({"key": r["id"].strip().lower(), "name": (r.get("title") or "").strip(),
                    "size": "", "url": (r.get("link") or "").strip(), "mrp": mrp,
                    "price": sale or mrp,
                    "in_stock": (r.get("availability") or "").strip().lower() == "in stock",
                    "ean": gtin if _EAN.match(gtin) else ""})
    return out


def parse_shopify(raw, url):
    try:
        d = json.loads(raw)
    except ValueError:
        return []
    out = []
    variants = d.get("variants") or []
    for v in variants:
        title = (v.get("title") or "").strip()
        bc = str(v.get("barcode") or "").strip()
        price = v.get("price")
        cmp_ = v.get("compare_at_price")
        out.append({"key": str(v["id"]), "name": (d.get("title") or "").strip(),
                    "size": "" if title == "Default Title" else title,
                    "url": f"{url.split('?')[0]}?variant={v['id']}" if len(variants) > 1 else url,
                    "mrp": cmp_ / 100 if cmp_ else None,
                    "price": price / 100 if price else None,
                    "in_stock": bool(v.get("available")), "ean": bc if _EAN.match(bc) else ""})
    return out


def parse_page(html, url, today=None):
    return _next_data(html, url, today or date.today()) or _jsonld(html, url) or _microdata(html, url)


def _attrs(o):
    return {a.get("attribute_code"): a.get("value") for a in o.get("customAttributes") or []
            if isinstance(a, dict)}


def _next_data(html, url, today):
    m = _NEXT.search(html or "")
    if not m:
        return []
    try:
        prod = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["productDetailReducer"]["product"]
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(prod, dict):
        return []
    variants = prod.get("configurableProductLinks") or [prod]
    out = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        a = _attrs(v)
        mrp = _num(v.get("price"))
        price = mrp
        special = _num(a.get("special_price"))
        start, end = (a.get("special_from_date") or "")[:10], (a.get("special_to_date") or "")[:10]
        if special and (not start or start <= today.isoformat()) and (not end or today.isoformat() <= end):
            price = special
        stock = v.get("isInStock")
        if stock is None:
            stock = (v.get("stockItem") or {}).get("is_in_stock")
        ean = str(a.get("ean") or "").strip()
        out.append({"key": str(v.get("magentoId") or v.get("sku")), "name": (v.get("name") or "").strip(),
                    "size": str(v.get("size") or "").strip(), "url": url, "mrp": mrp, "price": price,
                    "in_stock": bool(stock), "ean": ean if _EAN.match(ean) else ""})
    return [o for o in out if o["key"] not in ("None", "")]


def _jsonld(html, url):
    for block in _LDJSON.findall(html or ""):
        try:
            d = json.loads(block)
        except ValueError:
            continue
        for it in d if isinstance(d, list) else [d]:
            if not isinstance(it, dict) or it.get("@type") != "Product":
                continue
            offers = it.get("offers") or {}
            if isinstance(offers, list):
                if len(offers) != 1:
                    return []         # several prices and no way to tell sizes apart
                offers = offers[0]
            if not isinstance(offers, dict):
                return []
            lo, hi = _num(offers.get("lowPrice")), _num(offers.get("highPrice"))
            if lo and hi and lo != hi:
                return []             # a price range (several sizes): not one product
            price = _num(offers.get("price")) or lo
            key = str(it.get("sku") or "").strip()
            if not price or not key:
                return []
            ean = str(it.get("gtin13") or "").strip()
            return [{"key": key.lower(), "name": (it.get("name") or "").strip(), "size": "", "url": url,
                     "mrp": None, "price": price,
                     "in_stock": str(offers.get("availability", "")).endswith("InStock"),
                     "ean": ean if _EAN.match(ean) else ""}]
    return []


_ITEMPROP = re.compile(r'<meta\s+itemprop="(price|availability|sku)"\s+content="([^"]*)"')
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S)


def _microdata(html, url):
    props = {}
    for k, v in _ITEMPROP.findall(html or ""):
        props.setdefault(k, v)
    price, pid = _num(props.get("price")), props.get("sku")
    if not price or not pid:
        return []
    t = _TITLE.search(html)        # "Advanced Eternal Youth Formula Date &amp; Litchi 15g| Forest Essentials"
    name = htmllib.unescape(t.group(1)).split("|")[0].strip() if t else ""
    return [{"key": pid.strip().lower(), "name": name, "size": "", "url": url, "mrp": None,
             "price": price, "in_stock": props.get("availability", "").endswith("InStock"), "ean": ""}]
