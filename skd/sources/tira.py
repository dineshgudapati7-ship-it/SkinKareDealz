"""Tira (tirabeauty.com): each brand's top products, from its public brand page.

Fetched with Scrapling's Fetcher, the same "slow and steady" method as Nykaa, chosen by the
owner on 2026-11-12: at least MIN_DELAY seconds between requests, and a full stop on the
first refusal. robots.txt allows brand and product pages.

The brand page /brand/<slug> server-renders only its first 12 products, in popularity order,
in window.APP_DATA.reduxData.catalog.product_lists.data. Its "Load more" button calls Tira's
storefront API with an app token, and we deliberately don't use that. So each brand gives
its top 12 products (page.item_total says how many it has in all).

The rest of the catalogue comes from the product sitemaps (/sitemap.xml lists about 65
files of 500 /product/<slug> URLs each, ~27k products in all, ~2,700 for our brands).
The slug starts with the brand name, so URLs are picked by brand slug prefix and each
product page then confirms its brand. A product page carries the same uid as the brand
page, plus price, MRP, stock and the "Net Weight" attribute, in
window.APP_DATA.reduxData.catalog.product_details / product_meta. Pages are ~1.4 MB, so
at 1 request / 3 s a full pass over our brands takes about 2.5 h. The sitemaps don't
list everything (most brand-page top-12 URLs were missing from them on 2026-11-12), so
both sources are used together.

Brand slugs come from /sitemap/brand.sitemap.xml.
"""
import json
import re
import time

from .nykaa import Blocked, MIN_DELAY, _default_get, _norm

BASE = "https://www.tirabeauty.com"
_UNDEF = re.compile(r"([:\[,]\s*)undefined\b")


class NotFound(LookupError):
    pass


def fetch_html(url, get=_default_get):
    status, _, html = get(url)
    if status == 404:
        raise NotFound(url)                           # no such page: not a refusal
    if status != 200 or "Access Denied" in html[:2000]:
        raise Blocked(f"tira returned {status} for {url}")
    return html


def brand_slugs(get=_default_get):
    """{normalised brand name: slug} from the brand sitemap.
    It lists /products/?brand=<slug>, a filter URL that robots.txt disallows. We only take the
    slug from it and fetch the allowed /brand/<slug> page."""
    xml = fetch_html(f"{BASE}/sitemap/brand.sitemap.xml", get)
    slugs = re.findall(r"<loc>\s*https://www\.tirabeauty\.com/(?:brand/|products/\?brand=)([^<\s/?&]+)\s*</loc>", xml)
    return {_norm(s): s for s in slugs}


def slug_for(slugs, brand):
    """Slug for a brand name: 'Dot & Key' matches dot-and-key or dot-key."""
    return slugs.get(_norm(brand)) or slugs.get(_norm(brand.replace("&", "")))


def _catalog_state(html):
    i = html.find("window.APP_DATA")
    if i < 0:
        raise Blocked("tira page without product data (challenge page?)")
    state, _ = json.JSONDecoder().raw_decode(_UNDEF.sub(r"\1null", html[html.find("{", i):]))
    return (state.get("reduxData") or {}).get("catalog") or {}


def parse_brand_page(html):
    """(rows, item_total) from a brand page."""
    data = (_catalog_state(html).get("product_lists") or {}).get("data") or {}
    rows = []
    for it in data.get("items") or []:
        price = it.get("price") or {}
        eff = (price.get("effective") or {}).get("min")
        mrp = (price.get("marked") or {}).get("min")
        rows.append({
            "tira_id": str(it.get("uid")),
            "name": (it.get("name") or "").strip(),
            "size": "",
            "url": f"{BASE}/product/{it.get('slug')}",
            "mrp": float(mrp) if mrp is not None else None,
            "price": float(eff) if eff is not None else None,
            "in_stock": bool(it.get("sellable")),
        })
    return rows, (data.get("page") or {}).get("item_total")


def fetch_brand(slug, get=_default_get, log=print):
    rows, total = parse_brand_page(fetch_html(f"{BASE}/brand/{slug}", get))
    log(f"  tira {slug}: top {len(rows)} of {total}")
    return rows


_LOC = r"<loc>\s*(https://www\.tirabeauty\.com/{}[^<\s]*)\s*</loc>"


def product_sitemap_urls(get=_default_get, delay=MIN_DELAY, sleep=time.sleep, log=print):
    """Every /product/ URL in the product sitemaps, in sitemap order, without duplicates.
    One request per sitemap (~66 in all), MIN_DELAY apart; a refusal raises Blocked."""
    index = fetch_html(f"{BASE}/sitemap.xml", get)
    maps = re.findall(_LOC.format(r"sitemap/products/\d+/page\.sitemap\.xml"), index)
    urls = []
    for m in maps:
        sleep(max(delay, MIN_DELAY))
        urls += re.findall(_LOC.format("product/"), fetch_html(m, get))
    urls = list(dict.fromkeys(urls))
    log(f"  tira sitemaps: {len(urls)} product URLs from {len(maps)} files")
    return urls


def urls_for_brand(urls, slugs):
    """Sitemap URLs whose product slug starts with one of the brand slugs."""
    prefixes = tuple(f"{BASE}/product/{s}-" for s in slugs if s)
    return [u for u in urls if prefixes and u.startswith(prefixes)]


def parse_product_page(html):
    """One product page -> a row like the brand page's, plus brand_slug to check the brand."""
    cat = _catalog_state(html)
    d = cat.get("product_details") or {}
    m = cat.get("product_meta") or {}
    if not d.get("uid"):              # the page rendered but holds no product: gone, not a refusal
        raise NotFound("tira product page without a product")
    price = m.get("price") or {}
    eff = (price.get("effective") or {}).get("min")
    mrp = (price.get("marked") or {}).get("min")
    size = next((a.get("value") for g in d.get("grouped_attributes") or [] for a in g.get("details") or []
                 if a.get("key") == "Net Weight"), "") or ""
    brand = d.get("brand") or {}
    brand_slug = ((((brand.get("action") or {}).get("page") or {}).get("query") or {}).get("brand") or [""])[0]
    return {
        "tira_id": str(d["uid"]),
        "name": (d.get("name") or "").strip(),
        "size": str(size).strip(),
        "url": f"{BASE}/product/{d.get('slug')}",
        "mrp": float(mrp) if mrp is not None else None,
        "price": float(eff) if eff is not None else None,
        "in_stock": bool(m.get("sellable")),
        "brand_slug": brand_slug,
        "brand_name": (brand.get("name") or "").strip(),
    }


def fetch_product(url, get=_default_get):
    """Parsed product page; NotFound for a removed product, Blocked for a refusal."""
    return parse_product_page(fetch_html(url, get))


def guess_slugs(brand):
    """Likely /brand/<slug> values for a brand that isn't in the brand sitemap."""
    import unicodedata
    s = unicodedata.normalize("NFKD", brand).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'’]", "", s)
    out = []
    for amp in ("and", ""):
        g = re.sub(r"[^a-z0-9]+", "-", s.replace("&", f" {amp} ")).strip("-")
        if g not in out:
            out.append(g)
    return out


def is_brand(rows, brand):
    """True if most products on a page start with the brand name (guards a wrong guess)."""
    want = _norm(brand)
    hits = sum(_norm(r["name"]).startswith(want) for r in rows)
    return bool(rows) and hits * 2 >= len(rows)
