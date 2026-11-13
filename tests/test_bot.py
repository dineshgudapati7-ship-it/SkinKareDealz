"""Price-check bot: link parsing, replies and the Telegram update loop (fake API)."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from skd import bot, db

NOW = 1_800_000_000
DAY = 86400

WATCHLIST = """product_id,name,brand,size,store,asin,url,affiliate_url,key_ingredient,skin_types,ean
pur-288836,Minimalist Niacinamide 10% with Matmarine + Zinc + Acetyl Glucosamine Regulates Sebum & Evens Tone,Minimalist,30 ml,purplle,,https://www.purplle.com/product/288836,,,,
fk-FRNFWSHFY7K9HG4S,Minimalist 10% Niacinamide Face Serum With Matmarine + Zinc for Oily Skin,Minimalist,30 ml,flipkart,,https://www.flipkart.com/minimalist-10-niacinamide/p/itm50b8203a314d8?pid=FRNFWSHFY7K9HG4S,,,,
nyk-15020675,Minimalist 10% Niacinamide Face Serum With Matmarine + Zinc For Reducing Oil & Blemishes,Minimalist,30ml,nykaa,,https://www.nykaa.com/minimalist-10percent-niacinamide/p/15022069?skuId=15020675,https://ekaro.in/enkr123,,,
tira-7532238,Minimalist 10% Niacinamide Face Serum With Matmarine + Zinc For Reducing Oil & Blemishes (30ml),Minimalist,,tira,,https://www.tirabeauty.com/product/minimalist-10-niacinamide-face-serum---30ml-klghrqnheu6u,,,,
min-niacinamide10-amz,10% Niacinamide Face Serum Matmarine Zinc,Minimalist,30ml,amazon,B08F9MF314,https://www.amazon.in/dp/B08F9MF314,,,,
nyk-2793282,Dot & Key Vitamin C + E Super Bright Gel Moisturizer For Face Visibly Boosts Glow,Dot & Key,,nykaa,,https://www.nykaa.com/dot-key-vitamin-c-e-super-bright-moisturizer/p/2793282,,,,
pur-305333,Dot & Key Vitamin C + E Super Bright Gel Moisturizer For Face Visibly Boosts Glow For All Skin,Dot & Key,25 ml,purplle,,https://www.purplle.com/product/305333,,,,
pur-372607,Dot & Key Vitamin C + E Super Bright Gel Moisturizer For Face Visibly Boosts Glow For All Skin,Dot & Key,60 ml,purplle,,https://www.purplle.com/product/372607,,,,
pur-1,Plum Green Tea Oil-Free Moisturizer,Plum,50 ml,purplle,,https://www.purplle.com/plum-green-tea-oil-free-moisturizer-50ml,,,,
"""
CFG = {"telegram_token": "T", "amazon_tag": "skd-21"}


class Links(unittest.TestCase):
    def test_url_keys(self):
        self.assertEqual(bot.url_keys("https://www.nykaa.com/x/p/15022069?skuId=15020675"),
                         ["nykaa:15020675", "nykaa:15022069"])
        self.assertEqual(bot.url_keys("https://www.flipkart.com/x/p/itm50b8?pid=frnfwshfy7k9hg4s&lid=1"),
                         ["flipkart:FRNFWSHFY7K9HG4S", "flipkart:itm50b8"])
        self.assertEqual(bot.url_keys("https://www.amazon.in/Minimalist-Niacinamide/dp/B08F9MF314/ref=sr_1_3?x=1"),
                         ["amazon:B08F9MF314"])
        self.assertEqual(bot.url_keys("https://m.purplle.com/product/288836"), ["purplle:288836"])
        self.assertEqual(bot.url_keys("https://www.tirabeauty.com/product/abc-30ml-xyz?src=share"),
                         ["tira:abc-30ml-xyz"])
        self.assertEqual(bot.url_keys("https://evil.example/p/123"), [])

    def test_short_link_reads_location_only_and_stays_on_known_hosts(self):
        hops = {"https://dl.flipkart.com/s/abc": "https://www.flipkart.com/x/p/itm1?pid=P1"}

        class Resp:
            def __init__(self, loc):
                self.headers = {"Location": loc}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class Opener:
            seen = []

            def open(self, req, timeout):
                self.seen.append(req.get_method())
                return Resp(hops.get(req.full_url))

        o = Opener()
        self.assertEqual(bot.expand_short_link("https://dl.flipkart.com/s/abc", opener=o),
                         "https://www.flipkart.com/x/p/itm1?pid=P1")
        self.assertEqual(o.seen, ["HEAD"])
        self.assertIsNone(bot.expand_short_link("https://bit.ly/zzz", opener=o))   # not a store shortener


class Replies(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "watchlist.csv").write_text(WATCHLIST, encoding="utf-8")
        self.cat = bot.Catalog(self.tmp / "watchlist.csv")
        self.conn = db.connect(self.tmp / "prices.db")
        for d in range(20, 0, -1):          # 20 days of history at 549 on Purplle
            db.record_price(self.conn, "pur-288836", NOW - d * DAY, 549, True, "purplle-page")
        db.record_price(self.conn, "pur-288836", NOW - 3600, 491, True, "purplle-page")
        db.record_price(self.conn, "fk-FRNFWSHFY7K9HG4S", NOW - 3600, 569, True, "flipkart")
        db.record_price(self.conn, "nyk-15020675", NOW - 3600, 499, True, "nykaa")
        db.record_price(self.conn, "tira-7532238", NOW - 3600, None, False, "tira")
        db.record_price(self.conn, "nyk-2793282", NOW - 3600, 426, True, "nykaa")
        db.record_price(self.conn, "pur-305333", NOW - 3600, 263, True, "purplle-page")
        db.record_price(self.conn, "pur-372607", NOW - 3600, 366, True, "purplle-page")
        db.record_price(self.conn, "pur-1", NOW - 10 * DAY, 300, True, "purplle-page")   # stale
        self.conn.commit()

    def ask(self, text):
        return bot.answer(self.cat, self.conn, text, CFG, NOW, expand=lambda u: None)

    def test_link_compares_every_store_cheapest_first(self):
        kind, pid, body, kb = self.ask("check this https://www.flipkart.com/minimalist/p/itm50b8203a314d8?pid=FRNFWSHFY7K9HG4S")
        self.assertEqual((kind, pid), ("link", "fk-FRNFWSHFY7K9HG4S"))
        self.assertIn("Cheapest: <b>₹491 on Purplle</b>", body)
        self.assertLess(body.index("₹491"), body.index("₹499"))
        self.assertLess(body.index("₹499"), body.index("₹569"))
        self.assertIn("lowest in 30 days", body)
        self.assertIn("Tira</a> — out of stock", body)
        self.assertIn("Amazon</a> — no recent price", body)

    def test_affiliate_disclosure_only_when_affiliate_link_used(self):
        body = self.ask("https://www.nykaa.com/x/p/15022069?skuId=15020675")[2]
        self.assertIn('href="https://ekaro.in/enkr123"', body)
        self.assertIn("#ad", body)
        self.assertIn("browser", body)                     # EarnKaro: buy in the browser
        body = bot.compare(self.cat, self.conn, "pur-1", CFG, NOW)
        self.assertNotIn("#ad", body)

    def test_listing_without_size_is_shown_by_size_not_ranked(self):
        body = self.ask("https://www.nykaa.com/dot-key-vitamin-c-e-super-bright-moisturizer/p/2793282?productId=2")[2]
        self.assertNotIn("Cheapest", body)                 # can't rank an unknown size
        self.assertIn("size not listed on Nykaa", body)
        self.assertIn("<b>25 ml</b>", body)
        self.assertIn("<b>60 ml</b>", body)
        self.assertLess(body.index("₹263"), body.index("₹366"))

    def test_stale_price_is_not_shown(self):
        body = bot.compare(self.cat, self.conn, "pur-1", CFG, NOW)
        self.assertNotIn("₹300", body)
        self.assertIn("no recent price", body)

    def test_unknown_link_and_other_sites(self):
        self.assertIn("don't track", self.ask("https://www.nykaa.com/new/p/999")[2])
        self.assertIn("I can read links", self.ask("https://example.com/item/1")[2])

    def test_search_groups_one_product_once(self):
        kind, pid, body, kb = self.ask("minimalist niacinamide 10% 30ml")
        self.assertEqual(kind, "search")
        self.assertEqual(len(kb["inline_keyboard"]), 1)    # four stores, one product
        self.assertTrue(kb["inline_keyboard"][0][0]["callback_data"].startswith("p:"))

    def test_greeting_gets_help(self):
        self.assertEqual(self.ask("hi")[0], "help")
        self.assertEqual(self.ask("/start")[0], "help")

    def test_html_is_escaped(self):
        body = bot.compare(self.cat, self.conn, "nyk-15020675", CFG, NOW)
        self.assertIn("&amp;", body)
        self.assertNotIn("& Blemishes", body)


class Live(unittest.TestCase):
    """Live checks: replace the tracked price, cached, paused after a refusal."""

    def setUp(self):
        r = Replies()
        r.setUp()
        self.r = r
        self.calls = []
        self.t = [NOW]
        self.mode = {"purplle": "ok", "flipkart": "ok", "tira": "ok"}

        class Refused(Exception):
            pass
        self.Refused = Refused

        class Gone(LookupError):
            pass

        def fetcher(store, price):
            def fetch(p):
                self.calls.append(p.product_id)
                m = self.mode[store]
                if m == "refuse":
                    raise Refused()
                if m == "reset":
                    raise ConnectionError("stream reset")
                if m == "gone":
                    raise Gone()
                if m == "other":
                    raise ValueError("page is for another product")
                return {"price": price, "mrp": 699.0, "in_stock": True}
            return fetch

        sleeps = self.sleeps = []
        self.live = bot.LivePrices({
            "purplle": bot.LiveSource(fetcher("purplle", 549.0), (Refused,), gap=1.0, pause=3600),
            "flipkart": bot.LiveSource(fetcher("flipkart", 455.0), (Refused,), gap=3.0,
                                       pause=12 * 3600, strict=True),
            "tira": bot.LiveSource(fetcher("tira", 520.0), (Refused,), gap=3.0, pause=12 * 3600,
                                   strict=True, gone=(Gone,)),
        }, clock=lambda: self.t[0], sleep=sleeps.append)
        r.cat.live = self.live

    def reply(self, pid="pur-288836"):
        return bot.compare(self.r.cat, self.r.conn, pid, CFG, NOW)

    def test_live_prices_win_and_are_cached(self):
        body = self.reply()
        self.assertIn("Purplle</a> — ₹549", body)          # tracked 491 replaced
        self.assertIn("Flipkart</a> — ₹455", body)         # tracked 569 replaced
        self.assertIn("Tira</a> — ₹520", body)             # tracked "out of stock" replaced
        self.assertIn("Cheapest: <b>₹455 on Flipkart</b>", body)
        n = len(self.calls)
        self.reply()
        self.assertEqual(len(self.calls), n)               # second reply used the cache

    def test_nykaa_and_amazon_never_fetched(self):
        self.reply()
        self.assertFalse([c for c in self.calls if c.startswith(("nyk-", "min-"))])

    def test_scrapling_stores_keep_3s_gap(self):
        self.reply()
        self.live.cache.clear()
        self.reply()                                       # same clock: must wait the gap
        self.assertIn(3.0, self.sleeps)

    def test_refusal_falls_back_and_pauses(self):
        self.mode["purplle"] = "refuse"
        self.assertIn("₹491", self.reply())                # tracked price
        self.t[0] += 60
        self.live.cache.clear()
        before = self.calls.count("pur-288836")
        self.reply()
        self.assertEqual(self.calls.count("pur-288836"), before)   # paused

    def test_connection_reset_pauses_strict_store_12h(self):
        self.mode["flipkart"] = "reset"
        self.assertIn("Flipkart</a> — ₹569", self.reply())         # tracked price
        self.assertGreater(self.live.paused["flipkart"], NOW + 11 * 3600)

    def test_delisted_is_out_of_stock_not_a_pause(self):
        self.mode["tira"] = "gone"
        self.assertIn("Tira</a> — out of stock", self.reply())
        self.assertNotIn("tira", self.live.paused)

    def test_wrong_product_page_is_ignored_not_a_pause(self):
        self.mode["flipkart"] = "other"
        self.assertIn("Flipkart</a> — ₹569", self.reply())
        self.assertNotIn("flipkart", self.live.paused)


class Loop(unittest.TestCase):
    def setUp(self):
        r = Replies()
        r.setUp()
        self.tmp, self.cat = r.tmp, r.cat
        r.conn.close()
        self.calls = []
        self.updates = []

        def api(token, method, params=None, timeout=30):
            self.calls.append((method, params))
            if method == "getUpdates":
                out, self.updates = self.updates, []
                return out
            return True

        self.bot = bot.Bot(CFG, self.cat, self.tmp / "prices.db", self.tmp / "bot.db",
                           api=api, log=lambda m: None)

    def msg(self, uid, text, chat_type="private", chat_id=42):
        return {"update_id": uid, "message": {"chat": {"id": chat_id, "type": chat_type}, "text": text}}

    def sent(self):
        return [p for m, p in self.calls if m == "sendMessage"]

    def test_reply_offset_and_request_log(self):
        self.updates = [self.msg(7, "https://www.purplle.com/product/288836"),
                        self.msg(8, "https://www.nykaa.com/unknown/p/1")]
        self.assertEqual(self.bot.poll_once(), 2)
        out = self.sent()
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["chat_id"], 42)
        self.assertEqual(out[0]["parse_mode"], "HTML")
        self.assertTrue(out[0]["link_preview_options"]["is_disabled"])
        self.assertEqual(self.bot._get("offset"), "9")
        rows = sqlite3.connect(self.tmp / "bot.db").execute(
            "SELECT kind, product_id, found FROM queries ORDER BY ts, rowid").fetchall()
        self.assertEqual(rows, [("link", "pur-288836", 1), ("link", None, 0)])

    def test_ignores_groups_and_channels(self):
        self.updates = [self.msg(1, "https://www.purplle.com/product/288836", chat_type="group")]
        self.bot.poll_once()
        self.assertEqual(self.sent(), [])

    def test_button_tap_sends_comparison(self):
        self.updates = [{"update_id": 3, "callback_query": {
            "id": "cb1", "data": "p:pur-288836", "message": {"chat": {"id": 42}}}}]
        self.bot.poll_once()
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "cb1"}), self.calls)
        self.assertIn("Cheapest", self.sent()[0]["text"])

    def test_rate_limit(self):
        self.updates = [self.msg(i, "hi") for i in range(1, 15)]
        self.bot.poll_once()
        self.assertEqual(len(self.sent()), bot.RATE_LIMIT[0] + 1)   # 10 replies + one "slow down"

    def test_bad_update_does_not_stop_the_loop(self):
        self.updates = [{"update_id": 5, "callback_query": {"id": "x"}},     # malformed
                        self.msg(6, "hi")]
        self.bot.poll_once()
        self.assertEqual(len(self.sent()), 1)
        self.assertEqual(self.bot._get("offset"), "7")


if __name__ == "__main__":
    unittest.main()
