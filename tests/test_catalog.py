import csv
import tempfile
import unittest
from pathlib import Path

from skd import catalog
from skd.sources import purplle

BODY = {"has_more": 0, "total_items": 2, "items": [
    {"type": "product", "id": "1", "url": "brand-spf-50g", "name": "Brand SPF 50 Sunscreen, 50 gm",
     "offers_pd": {"cart_item_title": "Free gift on orders above 499+", "title": "Free gift on499+", "offer_ids": [33963, 7]},
     "price": "399", "our_price": "379", "offer_price": "379", "stock_status": "1",
     "multiple_variants": [{"items": [
         {"product_id": "1", "option_display_value": "50 gm", "price": "399", "offer_price": "379", "stock_status": "1"},
         {"product_id": "2", "option_display_value": "30 gm", "price": "249", "offer_price": "237", "stock_status": "0"}]}]},
    {"type": "product", "id": "3", "url": "brand-cleanser", "name": "Brand Cleanser",
     "price": "300", "our_price": "270", "offer_price": "", "stock_status": "1"},
    {"type": "banner", "id": "x"},
]}


class ParseTest(unittest.TestCase):
    def test_variants_expand_and_sizes_strip(self):
        rows = list(purplle.parse_items(BODY))
        self.assertEqual([r["purplle_id"] for r in rows], ["1", "2", "3"])
        self.assertEqual(rows[1]["name"], "Brand SPF 50 Sunscreen")
        self.assertEqual(rows[1]["size"], "30 gm")
        self.assertEqual(rows[1]["url"], "https://www.purplle.com/product/2")
        self.assertEqual(rows[0]["url"], "https://www.purplle.com/brand-spf-50g")
        self.assertFalse(rows[1]["in_stock"])
        self.assertEqual(rows[2]["price"], 270.0)   # falls back to our_price
        self.assertEqual(rows[2]["name"], "Brand Cleanser")

    def test_store_offer_applies_to_all_variants(self):
        rows = list(purplle.parse_items(BODY))
        self.assertEqual(rows[1]["offer"], "Free gift on orders above 499+")
        self.assertEqual(rows[1]["offer_ids"], "33963|7")
        self.assertEqual((rows[2]["offer"], rows[2]["offer_ids"]), ("", ""))


class MergeTest(unittest.TestCase):
    def test_upsert_keeps_manual_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "w.csv"
            with p.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=catalog.WATCH_FIELDS)
                w.writeheader()
                w.writerow({"product_id": "pur-1", "name": "old", "brand": "B", "size": "50 gm",
                            "store": "purplle", "url": "u", "affiliate_url": "AFF"})
            new = [{"product_id": "pur-1", "name": "new", "brand": "B", "size": "50 gm", "store": "purplle", "url": "u"},
                   {"product_id": "pur-2", "name": "two", "brand": "B", "size": "", "store": "purplle", "url": "v"}]
            self.assertEqual(catalog.merge_watchlist(p, new), (1, 1, 2))
            rows = list(csv.DictReader(p.open(encoding="utf-8")))
            self.assertEqual(rows[0]["name"], "new")
            self.assertEqual(rows[0]["affiliate_url"], "AFF")


if __name__ == "__main__":
    unittest.main()
