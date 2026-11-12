"""Nykaa prices flow into price_checks; gated to every N hours; a refusal stops only Nykaa."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skd import db, run
from skd.sources import nykaa

NOW = 1_800_000_000


def item(pid, price, in_stock=True):
    return {"nykaa_id": pid, "sku": "", "name": "n", "size": "", "type": "simple", "url": "u",
            "mrp": price, "price": price, "in_stock": in_stock}


class NykaaIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "watchlist.csv").write_text(
            "product_id,name,brand,size,store,asin,url,affiliate_url,key_ingredient,skin_types\n"
            "nyk-1,Sunscreen,Minimalist,,nykaa,,u,,,\n"
            "nyk-2,Serum,Minimalist,,nykaa,,u,,,\n"
            "nyk-3,Cream,Clinique,,nykaa,,u,,,\n", encoding="utf-8")
        (self.tmp / "brands.csv").write_text(
            "tier,brand,purplle_slugs,maccaron_prefixes,nykaa_paths\n"
            "2,Minimalist,,,brands/minimalist/c/16420\n"
            "2,Clinique,,,luxe/brands/clinique/c/3814\n"
            "2,Plum,,,brands/plum/c/2131\n"
            "2,Vicco,,,-\n", encoding="utf-8")

    def run_pipeline(self, fetch, now=NOW):
        args = ["--config", str(self.tmp / "none.toml"), "--db", str(self.tmp / "p.db"),
                "--watchlist", str(self.tmp / "watchlist.csv"), "--manual", str(self.tmp / "m.csv"),
                "--outbox", str(self.tmp / "outbox"), "--brands-file", str(self.tmp / "brands.csv"),
                "--now", str(now), "--dry-run", "--no-purplle", "--no-maccaron"]
        err = io.StringIO()
        with mock.patch.object(nykaa, "fetch_brand", side_effect=fetch) as m, \
                mock.patch.object(run.time, "sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            run.main(args)
        return m, sleep, err.getvalue()

    def test_records_watched_brands_only_and_sleeps_between_brands(self):
        pages = {"brands/minimalist/c/16420": [item("1", 379.0), item("2", 549.0, False), item("77", 1.0)],
                 "luxe/brands/clinique/c/3814": [item("3", 2500.0)]}
        m, sleep, err = self.run_pipeline(lambda path, **kw: pages[path])
        self.assertEqual(sorted(c.args[0] for c in m.call_args_list), sorted(pages))  # not plum, not "-"
        sleep.assert_called_once_with(nykaa.MIN_DELAY)
        self.assertIn("priced 3/3", err)
        conn = db.connect(self.tmp / "p.db")
        rows = conn.execute("SELECT product_id, price, in_stock, source FROM price_checks ORDER BY product_id").fetchall()
        self.assertEqual(rows, [("nyk-1", 379.0, 1, "nykaa"), ("nyk-2", None, 0, "nykaa"), ("nyk-3", 2500.0, 1, "nykaa")])
        self.assertEqual(run.latest_check(conn, "nyk-1", NOW)[1], 379.0)
        conn.close()

    def test_gated_until_every_hours_passed(self):
        fetch = lambda path, **kw: [item("1", 379.0)]
        self.run_pipeline(fetch)
        m, _, err = self.run_pipeline(fetch, now=NOW + 3 * 3600)
        m.assert_not_called()
        self.assertIn("nykaa: skipped (last fetched 3.0h ago)", err)
        m, _, _ = self.run_pipeline(fetch, now=NOW + run.NYKAA_EVERY_HOURS * 3600)
        self.assertTrue(m.called)

    def test_block_keeps_earlier_prices_and_run_finishes(self):
        def fetch(path, **kw):
            if "clinique" in path:
                raise nykaa.Blocked("nykaa returned 502")
            return [item("1", 379.0)]
        paths_first = "tier,brand,purplle_slugs,maccaron_prefixes,nykaa_paths\n" \
                      "2,Minimalist,,,brands/minimalist/c/16420\n2,Clinique,,,luxe/brands/clinique/c/3814\n"
        (self.tmp / "brands.csv").write_text(paths_first, encoding="utf-8")
        _, _, err = self.run_pipeline(fetch)
        self.assertIn("nykaa: STOP", err)
        self.assertIn("done:", err)
        conn = db.connect(self.tmp / "p.db")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM price_checks WHERE source='nykaa'").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
