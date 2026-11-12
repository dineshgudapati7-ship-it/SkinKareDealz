"""Purplle prices flow into price_checks and count as a fresh 'current' price."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skd import db, run
from skd.sources import purplle

NOW = 1_800_000_000


def item(pid, price, in_stock=True):
    return {"purplle_id": pid, "name": "n", "size": "", "url": "u", "mrp": price,
            "price": price, "in_stock": in_stock, "offer": "", "offer_ids": ""}


class PurplleIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "watchlist.csv").write_text(
            "product_id,name,brand,size,store,asin,url,affiliate_url,key_ingredient,skin_types\n"
            "pur-1,Sunscreen,Minimalist,50 gm,purplle,,u,,,\n"
            "pur-2,Serum,Minimalist,30 ml,purplle,,u,,,\n"
            "pur-9,Gone,Minimalist,,purplle,,u,,,\n", encoding="utf-8")
        (self.tmp / "brands.csv").write_text(
            "tier,brand,purplle_slugs\n2,Minimalist,minimalist\n2,Plum,plum\n", encoding="utf-8")

    def run_pipeline(self, fetch):
        args = ["--config", str(self.tmp / "none.toml"), "--db", str(self.tmp / "p.db"),
                "--watchlist", str(self.tmp / "watchlist.csv"), "--manual", str(self.tmp / "m.csv"),
                "--outbox", str(self.tmp / "outbox"), "--brands-file", str(self.tmp / "brands.csv"),
                "--now", str(NOW), "--dry-run", "--purplle-pages", "0"]
        err = io.StringIO()
        with mock.patch.object(purplle, "fetch_brand", side_effect=fetch) as m, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            run.main(args)
        return m, err.getvalue()

    def test_records_prices_for_watched_products_only(self):
        m, err = self.run_pipeline(lambda slug, **kw: [item("1", 379.0), item("2", 549.0, False), item("77", 10.0)])
        self.assertEqual([c.args[0] for c in m.call_args_list], ["minimalist"])   # plum not watched
        self.assertIn("priced 2/3", err)
        conn = db.connect(self.tmp / "p.db")
        rows = conn.execute("SELECT product_id, price, in_stock, source FROM price_checks ORDER BY product_id").fetchall()
        self.assertEqual(rows, [("pur-1", 379.0, 1, "purplle"), ("pur-2", None, 0, "purplle")])
        # Listing prices are kept for diagnosis but never count as the current price.
        self.assertIsNone(run.latest_check(conn, "pur-1", NOW))

    def test_block_stops_purplle_but_run_finishes(self):
        def blocked(slug, **kw):
            raise purplle.Blocked("purplle returned 429")
        _, err = self.run_pipeline(blocked)
        self.assertIn("STOP", err)
        self.assertIn("done:", err)


if __name__ == "__main__":
    unittest.main()


class PurpllePages(unittest.TestCase):
    """The checkout price comes from product pages, stalest first, in rotation."""

    def setUp(self):
        from skd.posts import Product
        self.conn = db.connect(":memory:")
        self.products = [Product(product_id=f"pur-{i}", name="n", brand="b", size="", store="purplle")
                         for i in range(1, 6)]
        db.record_price(self.conn, "pur-1", NOW - 2 * 3600, 400, True, "purplle-page")   # fresh: skipped
        db.record_price(self.conn, "pur-2", NOW - 30 * 3600, 400, True, "purplle-page")  # stale
        db.record_price(self.conn, "pur-3", NOW - 3600, 300, True, "purplle")            # listed in stock

    def sweep(self, fetch, max_pages=10):
        log = []
        run.ingest_purplle_pages(self.conn, self.products, NOW, log.append, max_pages=max_pages,
                                 fetch=fetch, sleep=lambda s: None)
        return log

    def test_order_skip_fresh_and_record(self):
        seen = []

        def fetch(pid):
            seen.append(pid)
            if pid == "pur-5":
                raise purplle.NotFound("gone")
            return {"price": 425.0, "mrp": 495.0, "in_stock": True}

        self.sweep(fetch)
        self.assertNotIn("pur-1", seen)
        self.assertEqual(seen[0], "pur-3")                  # in stock by listing comes first
        self.assertEqual(run.latest_check(self.conn, "pur-3", NOW)[1:], (425.0, 1, "purplle-page"))
        self.assertEqual(run.latest_check(self.conn, "pur-5", NOW)[2], 0)

    def test_stops_at_first_refusal(self):
        seen = []

        def fetch(pid):
            seen.append(pid)
            raise purplle.Blocked("403")

        log = self.sweep(fetch)
        self.assertEqual(len(seen), 1)
        self.assertTrue(any("STOP" in m for m in log))

    def test_page_parser(self):
        html = ('x availability:$R[493]={mrp:"495",ourPrice:425,offerPrice:"425",specialofferPrice:"425",'
                'specialofferText:"",stockStatus:"1",discount:14} "offers":{"@type":"Offer",'
                '"availability":"https://schema.org/InStock","price":"425","priceCurrency":"INR"}')
        self.assertEqual(purplle.parse_product_page(html), {"mrp": 495.0, "price": 425.0, "in_stock": True})
        oos = html.replace('stockStatus:"1"', 'stockStatus:"0"').replace('offerPrice:"425"', 'offerPrice:"0"')
        self.assertFalse(purplle.parse_product_page(oos)["in_stock"])
        ld_only = '"offers":{"@type":"Offer","availability":"https://schema.org/InStock","price":"304"}'
        self.assertEqual(purplle.parse_product_page(ld_only)["price"], 304.0)
        with self.assertRaises(purplle.NotFound):
            purplle.parse_product_page("<html>nothing</html>")
