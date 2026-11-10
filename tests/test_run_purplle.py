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
                "--now", str(NOW), "--dry-run"]
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
        self.assertEqual(run.latest_check(conn, "pur-1", NOW)[1], 379.0)

    def test_block_stops_purplle_but_run_finishes(self):
        def blocked(slug, **kw):
            raise purplle.Blocked("purplle returned 429")
        _, err = self.run_pipeline(blocked)
        self.assertIn("STOP", err)
        self.assertIn("done:", err)


if __name__ == "__main__":
    unittest.main()
