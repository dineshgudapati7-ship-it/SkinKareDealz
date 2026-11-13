"""Flipkart + Tira sources and their tracker steps, against fake pages (no network)."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skd import db, run
from skd.sources import flipkart, nykaa, tira

NOW = 1_800_000_000


def fk_page(products, more=True):
    state = {"pageDataV4": {"page": {"pageData": {"hasMorePages": more},
                                     "data": {"1": [{"widget": {"data": {"products": [
                                         {"productInfo": {"value": p}} for p in products]}}}]}}}}
    return 200, "u", f"<script>window.__INITIAL_STATE__ = {json.dumps(state).replace('null', 'undefined')};</script>"


def fk(pid, title, brand=None, full=True, stock="IN_STOCK"):
    pricing = ({"finalPrice": {"value": 236}, "mrp": {"value": 249}} if full
               else {"prices": [{"strikeOff": True, "value": 999}, {"strikeOff": False, "value": 949}]})
    return {"id": pid, "productBrand": brand, "titles": {"title": title, "subtitle": "30 ml"},
            "pricing": pricing, "availability": {"displayState": stock}, "baseUrl": f"/x/p/itm?pid={pid}"}


def tira_page(items, total=204):
    state = {"reduxData": {"catalog": {"product_lists": {"data": {"items": items, "page": {"item_total": total}}}}}}
    return 200, "u", f"<script>window.APP_DATA = {json.dumps(state)}</script>"


def tira_product(uid, name="Minimalist Serum", brand="minimalist", brand_name="Minimalist", sellable=True):
    details = {"uid": uid, "name": name, "slug": f"p-{uid}",
               "grouped_attributes": [{"details": [{"key": "Net Weight", "value": "30 ml"}]}],
               "brand": {"name": brand_name, "action": {"page": {"query": {"brand": [brand]}}}}}
    meta = {"sellable": sellable, "price": {"effective": {"min": 569}, "marked": {"min": 599}}}
    state = {"reduxData": {"catalog": {"product_details": details, "product_meta": meta}}}
    return f"<script>window.APP_DATA = {json.dumps(state)}</script>"


class FlipkartParse(unittest.TestCase):
    def test_brand_filter_and_both_price_shapes(self):
        _, _, html = fk_page([fk("A", "Minimalist Serum", "Minimalist"), fk("B", "Plum Serum", "Plum"),
                              fk("C", "Minimalist Sunscreen", None, full=False, stock="OUT_OF_STOCK")])
        st = flipkart.fetch_state("u", get=lambda u: (200, "u", html))
        rows = list(flipkart.parse_products(st, "Minimalist"))
        self.assertEqual([r["flipkart_id"] for r in rows], ["A", "C"])
        self.assertEqual((rows[0]["price"], rows[0]["mrp"], rows[0]["in_stock"]), (236.0, 249.0, True))
        self.assertEqual((rows[1]["price"], rows[1]["mrp"], rows[1]["in_stock"]), (949.0, 999.0, False))
        self.assertEqual(rows[0]["url"], "https://www.flipkart.com/x/p/itm?pid=A")

    def test_gives_up_after_pages_without_brand(self):
        pages = [fk_page([fk("A", "Minimalist A")]), fk_page([fk("Z", "Other Z")]), fk_page([fk("Y", "Other Y")]),
                 fk_page([fk("B", "Minimalist B")])]
        get = mock.Mock(side_effect=pages)
        rows = flipkart.fetch_brand("Minimalist", get=get, log=lambda m: None, sleep=lambda s: None)
        self.assertEqual([r["flipkart_id"] for r in rows], ["A"])
        self.assertEqual(get.call_count, 3)

    def test_refusal(self):
        with self.assertRaises(nykaa.Blocked):
            flipkart.fetch_state("u", get=lambda u: (403, "u", ""))
        with self.assertRaises(nykaa.Blocked):
            flipkart.fetch_state("u", get=lambda u: (200, "u", "<html>captcha</html>"))


class TiraParse(unittest.TestCase):
    def test_brand_page(self):
        _, _, html = tira_page([{"uid": 7, "name": " Serum ", "slug": "serum-7", "sellable": True,
                                 "price": {"effective": {"min": 569}, "marked": {"min": 599}}}])
        rows, total = tira.parse_brand_page(html)
        self.assertEqual(total, 204)
        self.assertEqual(rows[0], {"tira_id": "7", "name": "Serum", "size": "",
                                   "url": "https://www.tirabeauty.com/product/serum-7",
                                   "mrp": 599.0, "price": 569.0, "in_stock": True})

    def test_brand_slugs_and_refusals(self):
        xml = ("<urlset><url><loc>https://www.tirabeauty.com/brand/minimalist</loc></url>"
               "<url><loc>https://www.tirabeauty.com/brand/dot-key</loc></url></urlset>")
        self.assertEqual(tira.brand_slugs(get=lambda u: (200, "u", xml)), {"minimalist": "minimalist", "dotkey": "dot-key"})
        with self.assertRaises(nykaa.Blocked):
            tira.fetch_html("u", get=lambda u: (403, "u", ""))
        with self.assertRaises(nykaa.Blocked):
            tira.parse_brand_page("<html>no data</html>")


class TiraProducts(unittest.TestCase):
    def test_product_page(self):
        it = tira.parse_product_page(tira_product(9))
        self.assertEqual(it, {"tira_id": "9", "name": "Minimalist Serum", "size": "30 ml",
                              "url": "https://www.tirabeauty.com/product/p-9", "mrp": 599.0, "price": 569.0,
                              "in_stock": True, "brand_slug": "minimalist", "brand_name": "Minimalist"})
        empty = '<script>window.APP_DATA = {"reduxData": {"catalog": {"product_details": {}}}}</script>'
        with self.assertRaises(tira.NotFound):          # rendered, but no product: delisted
            tira.parse_product_page(empty)
        with self.assertRaises(nykaa.Blocked):          # no state at all: challenge page
            tira.parse_product_page("<html>no data</html>")

    def test_sitemaps(self):
        B = "https://www.tirabeauty.com"
        pages = {f"{B}/sitemap.xml": f"<loc>{B}/sitemap/products/1/page.sitemap.xml</loc>"
                                     f"<loc>{B}/sitemap/products/2/page.sitemap.xml</loc><loc>{B}/sitemap/x.xml</loc>",
                 f"{B}/sitemap/products/1/page.sitemap.xml": f"<loc>{B}/product/plum-a-1</loc><loc>{B}/product/simple-b-2</loc>",
                 f"{B}/sitemap/products/2/page.sitemap.xml": f"<loc>{B}/product/plum-a-1</loc><loc>{B}/product/plumeria-c</loc>"}
        sleeps = []
        urls = tira.product_sitemap_urls(get=lambda u: (200, u, pages[u]), delay=0, sleep=sleeps.append,
                                         log=lambda m: None)
        self.assertEqual(urls, [f"{B}/product/plum-a-1", f"{B}/product/simple-b-2", f"{B}/product/plumeria-c"])
        self.assertEqual(sleeps, [nykaa.MIN_DELAY, nykaa.MIN_DELAY])   # never faster than MIN_DELAY
        self.assertEqual(tira.urls_for_brand(urls, ["plum"]), [f"{B}/product/plum-a-1"])
        self.assertEqual(tira.urls_for_brand(urls, ["", None]), [])

    def test_build_tira_sitemap_products(self):
        from skd import catalog
        B = "https://www.tirabeauty.com/product/"
        with tempfile.TemporaryDirectory() as d:
            bf = Path(d) / "brands.csv"
            bf.write_text("tier,brand,tira_slug\n2,Simple,simple\n2,Dr. Sheth's,-\n", encoding="utf-8")
            wl = Path(d) / "watchlist.csv"
            wl.write_text(f"product_id,store,url\ntira-5,tira,{B}simple-known-5\n", encoding="utf-8")
            urls = [B + "simple-known-5", B + "simple-face-wash-6", B + "simple-joys-toy-7", B + "dr-sheths-gel-8"]
            pages = {B + "simple-face-wash-6": tira.parse_product_page(tira_product(6, "Simple Face Wash", "simple", "Simple")),
                     B + "simple-joys-toy-7": tira.parse_product_page(tira_product(7, "Toy", "simple-joys", "Simple Joys")),
                     B + "dr-sheths-gel-8": tira.parse_product_page(tira_product(8, "Gel", "dr-sheth-s", "Dr. Sheth's"))}
            brand_rows = [{"tira_id": "6", "name": "Simple Face Wash", "size": "", "url": B + "simple-face-wash-6",
                           "mrp": 1, "price": 1, "in_stock": True}]
            got = []
            with mock.patch.object(tira, "fetch_brand", return_value=brand_rows), \
                    mock.patch.object(tira, "product_sitemap_urls", return_value=urls), \
                    mock.patch.object(tira, "fetch_product", side_effect=lambda u: got.append(u) or pages[u]), \
                    mock.patch.object(catalog.time, "sleep"):
                snap, watch, _, missing = catalog.build_tira(catalog.read_brands(bf, {"2"}), 0, lambda m: None, bf, wl)
            self.assertNotIn(B + "simple-known-5", got)                 # already watched: not refetched
            self.assertEqual([w["product_id"] for w in watch], ["tira-6", "tira-8"])   # 6 once; 7 is another brand
            self.assertEqual(watch[1]["brand"], "Dr. Sheth's")           # no brand page, found by name
            self.assertEqual(missing, [])


class TiraResolve(unittest.TestCase):
    def test_guess_404_and_brand_check(self):
        self.assertEqual(tira.guess_slugs("Dot & Key"), ["dot-and-key", "dot-key"])
        self.assertEqual(tira.guess_slugs("Dr. Sheth's"), ["dr-sheths"])
        with self.assertRaises(tira.NotFound):
            tira.fetch_html("u", get=lambda u: (404, "u", ""))
        self.assertTrue(tira.is_brand([{"name": "Dot & Key Serum"}, {"name": "Dot and Key SPF"}], "Dot & Key"))
        self.assertFalse(tira.is_brand([{"name": "Other Serum"}, {"name": "X Soap"}, {"name": "Dove Soap"}], "Dove"))
        self.assertEqual(nykaa._norm("L'Oréal Paris"), "lorealparis")

    def test_build_tira_guesses_and_saves(self):
        from skd import catalog
        with tempfile.TemporaryDirectory() as d:
            bf = Path(d) / "brands.csv"
            bf.write_text("tier,brand\n2,Dot & Key\n2,Nowhere\n", encoding="utf-8")
            pages = {"dot-key": [{"tira_id": "1", "name": "Dot & Key Serum", "size": "", "url": "u",
                                  "mrp": 1, "price": 1, "in_stock": True}]}

            def fetch(slug, **kw):
                if slug not in pages:
                    raise tira.NotFound(slug)
                return pages[slug]
            with mock.patch.object(tira, "brand_slugs", return_value={}), \
                    mock.patch.object(tira, "fetch_brand", side_effect=fetch), \
                    mock.patch.object(catalog.time, "sleep"):
                _, watch, _, missing = catalog.build_tira(catalog.read_brands(bf, {"2"}), 0, lambda m: None, bf)
            self.assertEqual([w["product_id"] for w in watch], ["tira-1"])
            self.assertEqual(missing, ["Nowhere"])
            with bf.open(encoding="utf-8") as f:
                self.assertEqual({r["brand"]: r["tira_slug"] for r in csv.DictReader(f)},
                                 {"Dot & Key": "dot-key", "Nowhere": "-"})


class Ingest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "watchlist.csv").write_text(
            "product_id,name,brand,size,store,asin,url,affiliate_url,key_ingredient,skin_types\n"
            "fk-A,S,Minimalist,,flipkart,,u,,,\n"
            "tira-7,S,Minimalist,,tira,,u,,,\n"
            "tira-8,S,Plum,,tira,,u,,,\n", encoding="utf-8")
        (self.tmp / "brands.csv").write_text(
            "tier,brand,tira_slug\n2,Minimalist,minimalist\n2,Plum,plum\n", encoding="utf-8")

    def run_pipeline(self, fk_fetch, tira_fetch, now=NOW, product_fetch=None, extra=()):
        args = ["--config", str(self.tmp / "none.toml"), "--db", str(self.tmp / "p.db"),
                "--watchlist", str(self.tmp / "watchlist.csv"), "--manual", str(self.tmp / "m.csv"),
                "--outbox", str(self.tmp / "outbox"), "--brands-file", str(self.tmp / "brands.csv"),
                "--now", str(now), "--dry-run", "--no-purplle", "--no-maccaron", "--no-nykaa", *extra]
        self.product = mock.Mock(side_effect=product_fetch or (lambda u: {"in_stock": True, "price": 500.0}))
        err = io.StringIO()
        with mock.patch.object(flipkart, "fetch_brand", side_effect=fk_fetch) as f, \
                mock.patch.object(tira, "fetch_brand", side_effect=tira_fetch) as t, \
                mock.patch.object(tira, "fetch_product", self.product), \
                mock.patch.object(run.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            run.main(args)
        return f, t, err.getvalue()

    def test_prices_recorded_gating_and_tira_block(self):
        fk_item = {"flipkart_id": "A", "in_stock": True, "price": 236.0}

        def tira_fetch(slug, **kw):
            if slug == "plum":
                raise nykaa.Blocked("tira returned 403")
            return [{"tira_id": "7", "in_stock": True, "price": 569.0}]
        f, t, err = self.run_pipeline(lambda b, **kw: [fk_item], tira_fetch)
        self.assertEqual(f.call_args.args[0], "Minimalist")
        self.assertIn("flipkart: priced 1/1", err)
        self.assertIn("tira: STOP", err)
        self.assertIn("tira: priced 1/2", err)
        conn = db.connect(self.tmp / "p.db")
        rows = conn.execute("SELECT product_id, price, source FROM price_checks ORDER BY product_id").fetchall()
        conn.close()
        self.assertEqual(rows, [("fk-A", 236.0, "flipkart"), ("tira-7", 569.0, "tira")])
        f, t, err = self.run_pipeline(lambda b, **kw: [fk_item], tira_fetch, now=NOW + 3600)
        f.assert_not_called()                       # flipkart gated to every 12 h
        self.assertTrue(t.called)                   # tira runs every time

    def test_brand_page_refusal_skips_product_pages(self):
        def brand(slug, **kw):
            raise nykaa.Blocked("tira returned 403")
        self.run_pipeline(lambda b, **kw: [], brand)
        self.product.assert_not_called()

    def test_product_page_rotation(self):
        wl = self.tmp / "watchlist.csv"
        wl.write_text(wl.read_text(encoding="utf-8") + "tira-9,S,Plum,,tira,,u9,,,\ntira-10,S,Plum,,tira,,u10,,,\n"
                      "tira-11,S,Plum,,tira,,u11,,,\n",
                      encoding="utf-8")
        conn = db.connect(self.tmp / "p.db")
        db.record_price(conn, "tira-9", NOW - 50 * 3600, 400.0, True, "tira-page")     # tira-8: never priced
        db.record_price(conn, "tira-10", NOW - 90 * 3600, 400.0, True, "tira-page")
        db.record_price(conn, "tira-11", NOW - 5 * 3600, 400.0, True, "tira-page")      # too recent
        conn.commit()
        conn.close()

        def brand(slug, **kw):
            return [{"tira_id": "7", "in_stock": True, "price": 569.0}]

        def page(url):
            if url == "u10":
                raise tira.NotFound(url)
            return {"in_stock": True, "price": 450.0}
        _, _, err = self.run_pipeline(lambda b, **kw: [], brand, product_fetch=page, extra=["--tira-pages", "2"])
        # tira-7 was just priced by its brand page; then the stalest two: tira-8 (never), tira-10 (90 h)
        self.assertEqual([c.args[0] for c in self.product.call_args_list], ["u", "u10"])
        self.assertIn("tira-page: priced 1, 1 gone, of 2 due", err)
        # A second run with room for all: only tira-9 (50 h) is due; tira-11 (5 h) is too recent,
        # and tira-7/-8/-10 were just priced.
        _, _, err = self.run_pipeline(lambda b, **kw: [], brand, product_fetch=page, extra=["--tira-pages", "9"])
        self.assertEqual([c.args[0] for c in self.product.call_args_list], ["u9"])
        conn = db.connect(self.tmp / "p.db")
        rows = conn.execute("SELECT product_id, price, in_stock FROM price_checks WHERE source='tira-page' "
                            "AND ts=? ORDER BY product_id", (NOW,)).fetchall()
        self.assertEqual(rows, [("tira-10", None, 0), ("tira-8", 450.0, 1), ("tira-9", 450.0, 1)])
        self.assertIsNotNone(run.latest_check(conn, "tira-8", NOW + 20 * 3600))   # fresh for a day
        conn.close()

    def test_product_page_refusal_stops(self):
        def page(url):
            raise nykaa.Blocked("tira returned 403")
        _, _, err = self.run_pipeline(lambda b, **kw: [], lambda slug, **kw: [], product_fetch=page)
        self.assertEqual(self.product.call_count, 1)
        self.assertIn("tira-page: STOP", err)


if __name__ == "__main__":
    unittest.main()


class FlipkartProductPage(unittest.TestCase):
    STATE = {"multiWidgetState": {"pageDataResponse": {"pageContext": {"fdpEventTracking": {"events": {"psi": {
        "fsn": "FRNFWSHFY7K9HG4S", "ppd": {"fsp": 569, "finalPrice": 569, "mrp": 599, "nepPrice": 430},
        "pls": {"availabilityStatus": "IN_STOCK"}, "nb": {"nonBuyable": False}}}}}}}}

    def test_price_not_new_customer_price(self):
        from skd.sources import flipkart
        self.assertEqual(flipkart.parse_product_page(self.STATE, "frnfwshfy7k9hg4s"),
                         {"mrp": 599.0, "price": 569.0, "in_stock": True})

    def test_out_of_stock_and_wrong_product(self):
        import copy
        from skd.sources import flipkart
        st = copy.deepcopy(self.STATE)
        psi = st["multiWidgetState"]["pageDataResponse"]["pageContext"]["fdpEventTracking"]["events"]["psi"]
        psi["pls"]["availabilityStatus"] = "OUT_OF_STOCK"
        self.assertEqual(flipkart.parse_product_page(st, "FRNFWSHFY7K9HG4S")["in_stock"], False)
        with self.assertRaises(ValueError):
            flipkart.parse_product_page(self.STATE, "OTHERPID")
