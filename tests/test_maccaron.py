import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from skd import db, run
from skd.sources import maccaron

PAGE = '''<script type="application/ld+json">{"@type": "BreadcrumbList"}</script>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Product",
 "name": "Supple Preparation Facial Toner 180mL", "sku": "8809115025012",
 "offers": {"@type": "Offer", "price": 1072.5, "availability": "https://schema.org/InStock"}}</script>'''
OOS = PAGE.replace("8809115025012", "A000000180024").replace("InStock", "OutOfStock")
NOW = 1_800_000_000


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Parse(unittest.TestCase):
    def test_product_jsonld(self):
        p = maccaron.parse_product(PAGE)
        self.assertEqual(p, {"name": "Supple Preparation Facial Toner 180mL", "price": 1072.5,
                             "in_stock": True, "ean": "8809115025012"})

    def test_non_ean_sku_dropped_and_oos(self):
        p = maccaron.parse_product(OOS)
        self.assertEqual((p["ean"], p["in_stock"]), ("", False))
        self.assertIsNone(maccaron.parse_product("<html>no data</html>"))

    def test_sitemap_filters_by_brand_prefix(self):
        pages = {
            "https://maccaron.in/sitemaps/products-index.xml": "<loc>https://maccaron.in/sitemaps/products-0.xml</loc>",
            "https://maccaron.in/sitemaps/products-0.xml":
                "<loc>https://maccaron.in/en/products/plum_green-tea-toner/11</loc>"
                "<loc>https://maccaron.in/en/products/plum-goodness_x/12</loc>"
                "<loc>https://maccaron.in/en/products/klairs_toner/6</loc>",
        }
        opener = lambda req, timeout: FakeResp(pages[req.full_url].encode())
        with mock.patch.object(maccaron.time, "sleep"):
            urls = maccaron.product_urls(["plum"], opener=opener)
        self.assertEqual(urls, {"plum": ["https://maccaron.in/en/products/plum_green-tea-toner/11"]})
        self.assertEqual(maccaron.product_id(urls["plum"][0]), "11")


class Ingest(unittest.TestCase):
    def test_records_prices_and_throttles(self):
        tmp = Path(tempfile.mkdtemp())
        conn = db.connect(tmp / "p.db")
        products = [run.posts.Product("mac-11", "Toner", "Plum", "", "maccaron", url="u11"),
                    run.posts.Product("mac-12", "Gone", "Plum", "", "maccaron", url="u12")]
        log = io.StringIO()
        fetch = {"u11": maccaron.parse_product(PAGE), "u12": None}
        with mock.patch.object(maccaron, "fetch_product", side_effect=lambda u: fetch[u]):
            run.ingest_maccaron(conn, products, NOW, lambda m: log.write(m + "\n"), delay=0)
            run.ingest_maccaron(conn, products, NOW + 3600, lambda m: log.write(m + "\n"), delay=0)
        self.assertIn("priced 1/2 watched products (1 pages gone)", log.getvalue())
        self.assertIn("skipped (last fetched 1.0h ago)", log.getvalue())
        self.assertEqual(run.latest_check(conn, "mac-11", NOW + 3600)[1], 1072.5)

    def test_every_hours_controls_refetch(self):
        tmp = Path(tempfile.mkdtemp())
        conn = db.connect(tmp / "p.db")
        products = [run.posts.Product("mac-11", "Toner", "Plum", "", "maccaron", url="u11")]
        log = io.StringIO()
        w = lambda m: log.write(m + "\n")
        with mock.patch.object(maccaron, "fetch_product", return_value=maccaron.parse_product(PAGE)):
            run.ingest_maccaron(conn, products, NOW, w, delay=0, every_hours=12)
            run.ingest_maccaron(conn, products, NOW + 6 * 3600, w, delay=0, every_hours=12)   # GitHub: next 6h run
            run.ingest_maccaron(conn, products, NOW + 12 * 3600 - 1800, w, delay=0, every_hours=12)  # 30 min early
        self.assertIn("skipped (last fetched 6.0h ago)", log.getvalue())
        self.assertEqual(log.getvalue().count("priced 1/1"), 2)


if __name__ == "__main__":
    unittest.main()
