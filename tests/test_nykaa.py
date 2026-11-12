"""Nykaa source + catalog builder, against fake pages (no network)."""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skd import catalog
from skd.sources import nykaa


def page(listing, status=200):
    html = f"<html><script>window.__PRELOADED_STATE__ = {json.dumps({'categoryListing': {'listingData': listing}})}</script></html>"
    return status, "https://www.nykaa.com/x", html


def prod(pid, slug_id=None, size=None, price=100, in_stock=True, ptype="simple"):
    return {"id": pid, "sku": f"SKU{pid}", "name": f"Serum {pid} ", "packSize": size, "mrp": 120,
            "price": price, "inStock": in_stock, "type": ptype, "slug": f"serum/p/{slug_id or pid}"}


class Parse(unittest.TestCase):
    def test_rows_and_variant_url(self):
        rows = list(nykaa.parse_products({"products": [prod("1", size="30ml"), prod("2", slug_id="1")]}))
        self.assertEqual([r["nykaa_id"] for r in rows], ["1", "2"])
        self.assertEqual(rows[0]["url"], "https://www.nykaa.com/serum/p/1")
        self.assertEqual(rows[1]["url"], "https://www.nykaa.com/serum/p/1?skuId=2")  # size under parent page
        self.assertEqual((rows[0]["name"], rows[0]["size"], rows[0]["price"]), ("Serum 1", "30ml", 100.0))
        self.assertEqual(rows[1]["size"], "")


class Fetch(unittest.TestCase):
    def test_paginates_until_stop_and_dedupes(self):
        pages = [page({"totalFound": 3, "products": [prod("1"), prod("2")]}),
                 page({"totalFound": 3, "products": [prod("2"), prod("3")], "stopFurtherCall": True})]
        get = mock.Mock(side_effect=pages)
        sleep = mock.Mock()
        rows = nykaa.fetch_brand("brands/x/c/1", delay=0, get=get, log=lambda m: None, sleep=sleep)
        self.assertEqual([r["nykaa_id"] for r in rows], ["1", "2", "3"])
        self.assertIn("page_no=2", get.call_args_list[1].args[0])
        sleep.assert_called_once_with(nykaa.MIN_DELAY)          # delay never below the floor

    def test_refusals_raise_blocked(self):
        for resp in [(403, "u", "nope"), (200, "u", "<H1>Access Denied</H1>"), (200, "u", "<html>challenge</html>")]:
            with self.assertRaises(nykaa.Blocked):
                nykaa.fetch_state("u", get=lambda url, r=resp: r)

    def test_find_brand_path(self):
        ok = page({"listingType": "brand", "canonical": "brands/minimalist/c/16420", "brandName": "Minimalist"})
        other = page({"listingType": "brand", "canonical": "brands/simple-x/c/9", "brandName": "Simple X"})
        cat = page({"listingType": "category", "canonical": "skin/c/8377", "brandName": ""})
        self.assertEqual(nykaa.find_brand_path("Minimalist", get=lambda u: ok), "brands/minimalist/c/16420")
        luxe = page({"listingType": "brand", "canonical": "luxe/brands/kiehl-s/c/2371", "brandName": "Kiehl's"})
        self.assertEqual(nykaa.find_brand_path("Kiehl's", get=lambda u: luxe), "luxe/brands/kiehl-s/c/2371")
        self.assertIsNone(nykaa.find_brand_path("Simple", get=lambda u: other))
        self.assertIsNone(nykaa.find_brand_path("Simple", get=lambda u: cat))


class Build(unittest.TestCase):
    def test_resolves_saves_paths_and_stops_on_block(self):
        with tempfile.TemporaryDirectory() as d:
            bf = Path(d) / "brands.csv"
            bf.write_text("tier,brand,purplle_slugs\n2,A,a\n2,B,b\n2,C,c\n", encoding="utf-8")
            brands = catalog.read_brands(bf, {"2"})
            find = mock.Mock(side_effect=["brands/a/c/1", None, nykaa.Blocked("403")])
            fetch = mock.Mock(return_value=[{"nykaa_id": "7", "sku": "", "name": "N", "size": "50ml",
                                             "type": "simple", "url": "u", "mrp": 1, "price": 1, "in_stock": True}])
            with mock.patch.object(nykaa, "find_brand_path", find), mock.patch.object(nykaa, "fetch_brand", fetch), \
                    mock.patch.object(catalog.time, "sleep"):
                snap, watch, _, missing = catalog.build_nykaa(brands, 0, lambda m: None, bf)
            self.assertEqual([w["product_id"] for w in watch], ["nyk-7"])
            self.assertEqual(watch[0]["store"], "nykaa")
            self.assertEqual(missing, ["B"])
            with bf.open(encoding="utf-8") as f:
                rows = {r["brand"]: r["nykaa_paths"] for r in csv.DictReader(f)}
            self.assertEqual(rows, {"A": "brands/a/c/1", "B": "-", "C": ""})  # C never resolved: blocked


if __name__ == "__main__":
    unittest.main()
