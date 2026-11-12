"""End-to-end: watchlist + Keepa (stubbed) + manual prices -> deals -> outbox."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skd import run
from skd.sources import keepa

NOW = 1_800_000_000
DAY = 86400


def kt(unix):  # unix -> keepa minutes
    return unix // 60 - keepa.KEEPA_EPOCH_MIN


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "watchlist.csv").write_text(
            "product_id,name,brand,size,store,asin,url,affiliate_url,key_ingredient,skin_types\n"
            "amz-drop,Sunscreen SPF 50,Minimalist,50g,amazon,B0DROP,,,,oily\n"
            "amz-flat,Niacinamide 10%,Minimalist,30ml,amazon,B0FLAT,,,Niacinamide,\n"
            "nyk-drop,Barrier Cream,Re'equil,50g,nykaa,,,https://ekaro.in/abc,Ceramides,dry\n"
            "nyk-nolink,Cleanser,CeraVe,236ml,nykaa,,,,,\n", encoding="utf-8")
        rows = ["product_id,date,price"]
        for d in range(45, 0, -1):
            day = (NOW - d * DAY)
            from datetime import datetime, timezone, timedelta
            ds = datetime.fromtimestamp(day, timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M")
            rows += [f"nyk-drop,{ds},899", f"nyk-nolink,{ds},1100"]
        from datetime import datetime, timezone, timedelta
        today = datetime.fromtimestamp(NOW - 3600, timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d %H:%M")
        rows += [f"nyk-drop,{today},649", f"nyk-nolink,{today},799"]
        (self.tmp / "manual.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (self.tmp / "config.toml").write_text('keepa_key = "k"\namazon_tag = "skd-21"\n', encoding="utf-8")

        # Keepa: B0DROP was 499 for 90 days, dropped to 349 two hours ago. B0FLAT flat 599.
        self.products = [
            {"asin": "B0DROP", "stats": {"current": [34900, 36000]},
             "csv": [[kt(NOW - 90 * DAY), 49900, kt(NOW - 7200), 34900]]},
            {"asin": "B0FLAT", "stats": {"current": [59900, -1]},
             "csv": [[kt(NOW - 90 * DAY), 59900]]},
        ]

    def run_pipeline(self, *extra):
        args = ["--config", str(self.tmp / "config.toml"), "--db", str(self.tmp / "p.db"),
                "--watchlist", str(self.tmp / "watchlist.csv"), "--manual", str(self.tmp / "manual.csv"),
                "--outbox", str(self.tmp / "outbox"), "--now", str(NOW), "--no-nykaa", *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(keepa, "fetch_products", return_value=self.products), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            run.main(args)
        return out.getvalue(), err.getvalue()

    def test_dry_run_finds_real_drops_only(self):
        out, err = self.run_pipeline("--dry-run")
        self.assertIn("DEAL amz-drop (new low)", out)
        self.assertIn("lowest in 60 days", out)
        self.assertIn("https://www.amazon.in/dp/B0DROP?tag=skd-21", out)
        self.assertIn("DEAL nyk-drop", out)
        self.assertIn("https://ekaro.in/abc", out)
        self.assertNotIn("amz-flat", out)
        self.assertIn("nyk-nolink: deal", err)          # flagged, not posted
        self.assertFalse((self.tmp / "outbox").exists())  # dry run writes nothing

    def test_real_run_queues_whatsapp_and_does_not_repeat(self):
        _, err = self.run_pipeline()
        self.assertIn("2 deals posted", err)
        self.assertIn("telegram: not configured", err)
        queued = list((self.tmp / "outbox" / "whatsapp").glob("*.txt"))
        self.assertEqual(len(queued), 1)
        text = queued[0].read_text(encoding="utf-8")
        self.assertEqual(text.count("#ad"), 2)
        _, err2 = self.run_pipeline()                    # same prices, next run
        self.assertIn("0 deals posted", err2)
        self.assertIn("already posted", err2)


if __name__ == "__main__":
    unittest.main()
