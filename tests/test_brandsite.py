"""Official brand sites: parsers (fixtures shaped like the real pages, 2026-11-13), the
polite fetcher (robots.txt, stop at the first 403/429), the catalogue and the run."""
import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path

from skd import catalog, db, run
from skd.sources import brandsite

NOW = 1_800_000_000
TODAY = date(2026, 11, 13)


def next_page(variants):
    state = {"props": {"pageProps": {"initialState": {"productDetailReducer": {"product": {
        "magentoId": 1, "name": "Parent", "type": "configurable", "price": 1068,
        "configurableProductLinks": variants}}}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(state)}</script>'


TRIO = {"magentoId": 4956, "name": "Ultimate Nourishment Cleansing Bar Trio-3pc", "price": 1335,
        "size": "3pc", "isInStock": True, "sku": "TBSCOMBO-57",
        "customAttributes": [{"attribute_code": "special_price", "value": "1068.000000"},
                             {"attribute_code": "special_from_date", "value": "2025-08-21 00:00:00"}]}
TEA_TREE = {"magentoId": 564, "name": "Tea Tree Body Wash", "price": 895, "size": "250ML",
            "isInStock": False, "sku": "BS-38338001",
            "customAttributes": [{"attribute_code": "ean", "value": "5028197828745"},
                                 {"attribute_code": "ondc_special_price", "value": "761.000000"}]}
# JSON-LD says InStock; the page (and isInStock) said Out of Stock.
TEA_TREE_LD = ('<script type="application/ld+json">{"@type": "Product", "name": "Tea Tree Body Wash", '
               '"sku": "BS-38338001", "offers": {"@type": "AggregateOffer", "availability": '
               '"https://schema.org/InStock", "highPrice": 895.0, "lowPrice": 895.0}}</script>')
KAMA = ('<script type="application/ld+json">{"@context":"https://schema.org/","@type":"Product",'
        '"name":"Hand Cream","brand":"Kama Ayurveda","sku":"ITM00100","price":1250,"offers":'
        '{"@type":"AggregateOffer","lowPrice":1250,"highPrice":1250,"priceCurrency":"INR",'
        '"offerCount":"1","availability":"https://schema.org/InStock"}}</script>')
FOREST = ('<title>Advanced Eternal Youth Formula Date &amp; Litchi 15g| Forest Essentials</title>'
          '<meta itemprop="sku" content="1067"/><meta itemprop="price" content="2095.00" />'
          '<meta itemprop="availability" content="https://schema.org/InStock" />')
FEED = ("id,title,link,availability,price,sale_price,brand,gtin\n"
        "WOW_FW_RETINOL,Retinol Face Wash,https://www.buywow.in/products/retinol-face-wash-tube,"
        "in stock,399 INR,,WOW,8906105615281\n"
        "WOW_HNC_VITC,Vitamin C Hand Cream,https://www.buywow.in/products/vitamin-c-hand-nail-cream,"
        "out of stock,349 INR,299 INR,WOW,8906105615236\n")
SHOPIFY = json.dumps({"title": "Retinol Night Serum", "variants": [
    {"id": 11, "title": "30ml", "price": 66500, "compare_at_price": 110000, "available": True, "barcode": None},
    {"id": 12, "title": "50ml", "price": 89900, "compare_at_price": None, "available": False,
     "barcode": "8906110000001"}]})


class Parsers(unittest.TestCase):
    def test_body_shop_variant_uses_special_price_not_mrp(self):
        [v] = brandsite.parse_page(next_page([TRIO]), "u", TODAY)
        self.assertEqual((v["key"], v["size"], v["mrp"], v["price"], v["in_stock"]),
                         ("4956", "3pc", 1335.0, 1068.0, True))

    def test_body_shop_stock_from_variant_wins_over_jsonld_and_ondc_ignored(self):
        [v] = brandsite.parse_page(TEA_TREE_LD + next_page([TEA_TREE]), "u", TODAY)
        self.assertEqual((v["price"], v["in_stock"], v["ean"]), (895.0, False, "5028197828745"))

    def test_expired_special_price_is_ignored(self):
        old = dict(TRIO, customAttributes=TRIO["customAttributes"] +
                   [{"attribute_code": "special_to_date", "value": "2026-01-01 00:00:00"}])
        self.assertEqual(brandsite.parse_page(next_page([old]), "u", TODAY)[0]["price"], 1335.0)

    def test_jsonld_single_offer(self):
        self.assertEqual(brandsite.parse_page(KAMA, "u"), [
            {"key": "itm00100", "name": "Hand Cream", "size": "", "url": "u", "mrp": None,
             "price": 1250.0, "in_stock": True, "ean": ""}])

    def test_jsonld_price_range_is_refused(self):
        ranged = KAMA.replace('"highPrice":1250', '"highPrice":1335').replace('"price":1250,', "")
        self.assertEqual(brandsite.parse_page(ranged, "u"), [])

    def test_microdata_name_keeps_size(self):
        [v] = brandsite.parse_page(FOREST, "u")
        self.assertEqual((v["key"], v["name"], v["price"], v["in_stock"]),
                         ("1067", "Advanced Eternal Youth Formula Date & Litchi 15g", 2095.0, True))

    def test_feed_sale_price_and_stock(self):
        a, b = brandsite.parse_feed(FEED)
        self.assertEqual((a["key"], a["price"], a["in_stock"], a["ean"]),
                         ("wow_fw_retinol", 399.0, True, "8906105615281"))
        self.assertEqual((b["mrp"], b["price"], b["in_stock"]), (349.0, 299.0, False))

    def test_shopify_variants_in_rupees(self):
        a, b = brandsite.parse_shopify(SHOPIFY, "https://x.com/products/serum")
        self.assertEqual((a["key"], a["size"], a["price"], a["mrp"], a["url"]),
                         ("11", "30ml", 665.0, 1100.0, "https://x.com/products/serum?variant=11"))
        self.assertEqual((b["price"], b["in_stock"], b["ean"]), (899.0, False, "8906110000001"))

    def test_parse_site(self):
        self.assertEqual(brandsite.parse_site("page:https://www.thebodyshop.in/"),
                         ("page", "https://www.thebodyshop.in"))
        self.assertIsNone(brandsite.parse_site("-"))
        self.assertIsNone(brandsite.parse_site(""))
        self.assertEqual(brandsite.site_key("https://www.buywow.in"), "buywow")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(pages, log):
    def open_(req, timeout=None):
        log.append(req.full_url)
        body = pages.get(req.full_url)
        if isinstance(body, int):
            raise urllib.error.HTTPError(req.full_url, body, "x", {}, None)
        if body is None:
            raise urllib.error.HTTPError(req.full_url, 404, "x", {}, None)
        return FakeResp(body.encode())
    return open_


class Fetcher(unittest.TestCase):
    def test_sitemap_index_robots_and_gap(self):
        B = "https://shop.example"
        pages = {f"{B}/robots.txt": "User-agent: *\nDisallow: /products/private\n",
                 f"{B}/sitemap.xml": f"<sitemapindex><sitemap><loc>{B}/sitemap_products_1.xml?from=1&amp;to=9</loc>"
                                     f"</sitemap><sitemap><loc>{B}/sitemap_pages_1.xml</loc></sitemap></sitemapindex>",
                 f"{B}/sitemap_products_1.xml?from=1&to=9":
                     f"<urlset><url><loc>{B}/products/serum</loc></url><url><loc>{B}/products/private</loc></url>"
                     f"<url><loc>{B}/</loc></url></urlset>",
                 f"{B}/products/serum.js": SHOPIFY}
        log, sleeps = [], []
        s = brandsite.Site("shopify", B, opener=opener(pages, log), sleep=sleeps.append)
        urls = s.sitemap_urls()
        self.assertEqual(urls, [f"{B}/products/serum", f"{B}/products/private"])
        self.assertEqual(len(s.items(urls[0])), 2)
        self.assertEqual(s.items(urls[1]), [])                     # robots.txt disallows it
        self.assertNotIn(f"{B}/products/private.js", log)
        self.assertNotIn(f"{B}/sitemap_pages_1.xml", log)
        self.assertEqual(sleeps, [3.0] * (len(log) - 1))              # >= 3 s between all requests

    def test_429_raises_blocked(self):
        B = "https://shop.example"
        s = brandsite.Site("shopify", B, opener=opener({f"{B}/robots.txt": "", f"{B}/products/a.js": 429}, []),
                           sleep=lambda s: None)
        with self.assertRaises(brandsite.Blocked):
            s.items(f"{B}/products/a")

    def test_outage_stops_site_after_five_failures(self):
        B = "https://shop.example"
        state = {"n": 0}

        def down(req, timeout=None):
            if req.full_url.endswith("robots.txt"):
                return FakeResp(b"")
            state["n"] += 1
            if state["n"] == 2:                       # one success resets the count
                return FakeResp(b"<html></html>")
            raise urllib.error.URLError("getaddrinfo failed")
        s = brandsite.Site("page", B, opener=down, sleep=lambda s: None)
        errors = 0
        with self.assertRaises(brandsite.Blocked):
            for i in range(20):
                try:
                    s.items(f"{B}/p{i}")
                except urllib.error.URLError:
                    errors += 1
        self.assertEqual(errors, 1 + 4)               # 1 before the success, then 4 more; the 5th raises Blocked

    def test_review_pages_dropped_from_sitemap(self):
        B = "https://www.thebodyshop.in"
        pages = {f"{B}/robots.txt": "",
                 f"{B}/sitemap.xml": f"<urlset><url><loc>{B}/tea-tree-wash/p/tbs1</loc></url>"
                                     f"<url><loc>{B}/tea-tree-wash/reviews</loc></url></urlset>"}
        s = brandsite.Site("page", B, opener=opener(pages, []), sleep=lambda s: None)
        self.assertEqual(s.sitemap_urls(), [f"{B}/tea-tree-wash/p/tbs1"])

    def test_list_shaped_data_is_not_a_crash(self):
        nd = '<script id="__NEXT_DATA__" type="application/json">'              '{"props":{"pageProps":{"initialState":{"productDetailReducer":{"product":[]}}}}}</script>'
        ld = '<script type="application/ld+json">{"@type":"Product","sku":"x","offers":[[1]]}</script>'
        self.assertEqual(brandsite.parse_page(nd, "u"), [])
        self.assertEqual(brandsite.parse_page(ld, "u"), [])


class FakeSite:
    """Stands in for brandsite.Site: {url or None: items | Exception}."""
    calls = []

    def __init__(self, kind, base, delay=None, data=None):
        self.kind, self.base, self.key = kind, base, brandsite.site_key(base)
        self.data = data

    def sitemap_urls(self):
        return [u for u in self.data if u]

    def items(self, url=None):
        FakeSite.calls.append((self.key, url))
        got = self.data[url]
        if isinstance(got, Exception):
            raise got
        return got


def item(key, price, url="u", in_stock=True, size=""):
    return {"key": key, "name": f"P{key}", "size": size, "url": url, "mrp": None, "price": price,
            "in_stock": in_stock, "ean": ""}


DATA = {
    "https://www.buywow.in": {None: [item("wow_a", 399.0), item("wow_b", 299.0, in_stock=False)]},
    "https://discoverpilgrim.com": {"https://discoverpilgrim.com/products/s": [
        item("11", 665.0, "https://discoverpilgrim.com/products/s?variant=11", size="30ml"),
        item("12", 899.0, "https://discoverpilgrim.com/products/s?variant=12", size="50ml")]},
    "https://thedermaco.com": {"https://thedermaco.com/products/x": brandsite.Blocked("thedermaco.com returned 429")},
}


def factory(kind, base, delay=None):
    return FakeSite(kind, base, delay, DATA[base])


BRANDS = ("tier,brand,brand_site\n3,WOW Skin Science,feed:https://www.buywow.in\n"
          "2,Pilgrim,shopify:https://discoverpilgrim.com\n2,The Derma Co,shopify:https://thedermaco.com\n"
          "2,Cetaphil,-\n")


class Catalog(unittest.TestCase):
    def test_build_brand(self):
        brands = list(__import__("csv").DictReader(io.StringIO(BRANDS)))
        msgs = []
        snap, watch, fields, missing = catalog.build_brand(brands, 3.0, msgs.append, site_factory=factory)
        self.assertEqual([w["product_id"] for w in watch],
                         ["brd-buywow-wow_a", "brd-buywow-wow_b", "brd-discoverpilgrim-11",
                          "brd-discoverpilgrim-12"])
        self.assertEqual({w["store"] for w in watch}, {"brand"})
        self.assertEqual(watch[2]["size"], "30ml")
        self.assertEqual(missing, ["The Derma Co", "Cetaphil"])
        self.assertTrue(any("STOP" in m and "429" in m for m in msgs))
        self.assertEqual(fields, catalog.BRAND_FIELDS)


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "brands.csv").write_text(BRANDS, encoding="utf-8")
        (self.tmp / "watchlist.csv").write_text(
            "product_id,name,brand,size,store,asin,url,affiliate_url,key_ingredient,skin_types,ean\n"
            "brd-buywow-wow_a,A,WOW Skin Science,,brand,,https://www.buywow.in/products/a,,,,\n"
            "brd-buywow-wow_b,B,WOW Skin Science,,brand,,https://www.buywow.in/products/b,,,,\n"
            "brd-discoverpilgrim-11,S,Pilgrim,30ml,brand,,https://discoverpilgrim.com/products/s?variant=11,,,,\n"
            "brd-discoverpilgrim-12,S,Pilgrim,50ml,brand,,https://discoverpilgrim.com/products/s?variant=12,,,,\n"
            "brd-discoverpilgrim-13,S,Pilgrim,100ml,brand,,https://discoverpilgrim.com/products/s?variant=13,,,,\n"
            "brd-thedermaco-1,X,The Derma Co,,brand,,https://thedermaco.com/products/x,,,,\n",
            encoding="utf-8")
        FakeSite.calls = []

    def ingest(self, now=NOW):
        conn = db.connect(self.tmp / "p.db")
        msgs = []
        run.ingest_brand(conn, run.load_watchlist(self.tmp / "watchlist.csv"), self.tmp / "brands.csv",
                         now, msgs.append, site_factory=factory)
        return conn, "\n".join(msgs)

    def test_prices_each_site_once_and_block_is_contained(self):
        conn, log = self.ingest()
        rows = dict(((r[0]), r[1:]) for r in conn.execute(
            "SELECT product_id, price, in_stock FROM price_checks WHERE source='brand'"))
        self.assertEqual(rows, {"brd-buywow-wow_a": (399.0, 1), "brd-buywow-wow_b": (None, 0),
                                "brd-discoverpilgrim-11": (665.0, 1), "brd-discoverpilgrim-12": (899.0, 1),
                                "brd-discoverpilgrim-13": (None, 0)})        # size no longer offered
        # one feed read, one page for all three Pilgrim sizes, one refused Derma Co page
        self.assertEqual(sorted(FakeSite.calls, key=str),
                         sorted([("buywow", None), ("discoverpilgrim", "https://discoverpilgrim.com/products/s"),
                                 ("thedermaco", "https://thedermaco.com/products/x")], key=str))
        self.assertIn("STOP", log)
        self.assertIn("stopped: thedermaco", log)
        self.assertEqual(run.latest_check(conn, "brd-buywow-wow_a", NOW)[1:], (399.0, 1, "brand"))

    def test_recent_prices_are_not_refetched(self):
        conn, _ = self.ingest()
        conn.close()
        FakeSite.calls = []
        self.ingest(NOW + 3600)
        self.assertEqual(FakeSite.calls, [("thedermaco", "https://thedermaco.com/products/x")])


if __name__ == "__main__":
    unittest.main()
