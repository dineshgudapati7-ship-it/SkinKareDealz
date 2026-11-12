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

    def run_pipeline(self, fk_fetch, tira_fetch, now=NOW):
        args = ["--config", str(self.tmp / "none.toml"), "--db", str(self.tmp / "p.db"),
                "--watchlist", str(self.tmp / "watchlist.csv"), "--manual", str(self.tmp / "m.csv"),
                "--outbox", str(self.tmp / "outbox"), "--brands-file", str(self.tmp / "brands.csv"),
                "--now", str(now), "--dry-run", "--no-purplle", "--no-maccaron", "--no-nykaa"]
        err = io.StringIO()
        with mock.patch.object(flipkart, "fetch_brand", side_effect=fk_fetch) as f, \
                mock.patch.object(tira, "fetch_brand", side_effect=tira_fetch) as t, \
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


if __name__ == "__main__":
    unittest.main()
