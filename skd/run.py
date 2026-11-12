"""One pipeline run: ingest prices -> find real deals -> post.

    python -m skd.run --dry-run      # print posts, send/record nothing
    python -m skd.run                # post to Telegram + write WhatsApp queue

Schedule it every 2-3 hours (Windows Task Scheduler, cron, or n8n "Execute Command").
Secrets can come from env vars: KEEPA_KEY, AMAZON_TAG, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID.
"""
import argparse
import csv
import os
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import db, deals, posts, telegram
from .sources import flipkart, keepa, maccaron, manual, nykaa, purplle, tira

ROOT = Path(__file__).resolve().parent.parent
FRESH_HOURS = {"keepa": 6, "manual": 24, "purplle": 6, "maccaron": 8, "nykaa": 25, "flipkart": 25, "tira": 8, "tira-page": 26}   # older "current" prices are ignored


def load_config(path):
    cfg = {}
    if Path(path).exists():
        with open(path, "rb") as f:
            cfg = tomllib.load(f)
    env = {"keepa_key": "KEEPA_KEY", "amazon_tag": "AMAZON_TAG",
           "telegram_token": "TELEGRAM_TOKEN", "telegram_chat_id": "TELEGRAM_CHAT_ID"}
    for k, var in env.items():
        if os.environ.get(var):
            cfg[k] = os.environ[var]
    return cfg


def load_watchlist(path):
    fields = posts.Product.__dataclass_fields__
    with open(path, newline="", encoding="utf-8") as f:
        return [posts.Product(**{k: (v or "").strip() for k, v in row.items() if k in fields})
                for row in csv.DictReader(f) if (row.get("product_id") or "").strip()]


def ingest_keepa(conn, cfg, products, now, log):
    key = cfg.get("keepa_key")
    by_asin = {p.asin: p for p in products if p.store == "amazon" and p.asin}
    if not key or not by_asin:
        log(f"keepa: skipped ({'no key' if not key else 'no ASINs in watchlist'})")
        return
    asins = list(by_asin)
    for i in range(0, len(asins), 100):
        for kp in keepa.fetch_products(key, asins[i:i + 100]):
            p = by_asin.get(kp.get("asin"))
            if not p:
                continue
            for ts, price in keepa.price_history(kp):        # idempotent backfill
                db.record_price(conn, p.product_id, ts, price, price is not None, "keepa-history")
            price, in_stock = keepa.current_price(kp)
            db.record_price(conn, p.product_id, now, price, in_stock, "keepa")
    log(f"keepa: fetched {len(asins)} ASINs")


def ingest_purplle(conn, products, brands_file, now, log, delay=2.0):
    """Re-read the Purplle brand listings and record a price for every watched pur-<id>."""
    watched = {p.product_id: p for p in products if p.store == "purplle"}
    if not watched:
        log("purplle: skipped (no purplle products in watchlist)")
        return
    wanted = {p.brand for p in watched.values()}
    slugs = []
    if Path(brands_file).exists():
        with open(brands_file, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["brand"] in wanted:
                    slugs += [s for s in (r.get("purplle_slugs") or "").split("|") if s]
    seen = 0
    for slug in slugs:
        try:
            items = purplle.fetch_brand(slug, delay=delay, log=lambda m: None)
        except purplle.Blocked as e:
            log(f"purplle: STOP — {e}; keeping what was fetched so far")
            break
        except Exception as e:                      # one bad brand shouldn't kill the run
            log(f"purplle: {slug} failed ({e})")
            continue
        for it in items:
            pid = f"pur-{it['purplle_id']}"
            if pid in watched:
                ok = it["in_stock"] and it["price"] is not None
                db.record_price(conn, pid, now, it["price"] if ok else None, ok, "purplle")
                seen += 1
        conn.commit()
    log(f"purplle: priced {seen}/{len(watched)} watched products from {len(slugs)} brand pages")


MACCARON_EVERY_HOURS = 6     # one page per product, so refresh less often than Purplle


def ingest_maccaron(conn, products, now, log, delay=2.0, force=False, every_hours=MACCARON_EVERY_HOURS):
    """Fetch each watched Maccaron product page (at most every `every_hours`)."""
    watched = [p for p in products if p.store == "maccaron" and p.url]
    if not watched:
        log("maccaron: skipped (no maccaron products in watchlist)")
        return
    last = conn.execute("SELECT MAX(ts) FROM price_checks WHERE source='maccaron'").fetchone()[0]
    if not force and last and now - last < (every_hours - 0.5) * 3600:
        log(f"maccaron: skipped (last fetched {(now - last) / 3600:.1f}h ago)")
        return
    priced = gone = 0
    for i, p in enumerate(watched):
        try:
            item = maccaron.fetch_product(p.url)
        except maccaron.Blocked as e:
            log(f"maccaron: STOP — {e}; keeping what was fetched so far")
            break
        except Exception as e:
            log(f"maccaron: {p.product_id} failed ({e})")
            item = None
        if item is None:
            gone += 1
        else:
            ok = item["in_stock"] and item["price"] is not None
            db.record_price(conn, p.product_id, now, item["price"] if ok else None, ok, "maccaron")
            priced += 1
        if i % 50 == 49:
            conn.commit()
        if delay:
            time.sleep(delay)
    conn.commit()
    log(f"maccaron: priced {priced}/{len(watched)} watched products ({gone} pages gone)")


NYKAA_EVERY_HOURS = 24      # ~180 pages, ~15 min on a GitHub runner: once a day fits the Actions budget


def ingest_nykaa(conn, products, brands_file, now, log, delay=nykaa.MIN_DELAY, force=False,
                 every_hours=NYKAA_EVERY_HOURS):
    """Re-read Nykaa brand pages (slow and steady) and record a price for every watched nyk-<id>.
    The first refusal stops Nykaa for this run; prices fetched before it are kept."""
    watched = {p.product_id: p for p in products if p.store == "nykaa"}
    if not watched:
        log("nykaa: skipped (no nykaa products in watchlist)")
        return
    last = conn.execute("SELECT MAX(ts) FROM price_checks WHERE source='nykaa'").fetchone()[0]
    if not force and last and now - last < (every_hours - 0.5) * 3600:
        log(f"nykaa: skipped (last fetched {(now - last) / 3600:.1f}h ago)")
        return
    wanted = {p.brand for p in watched.values()}
    paths = []
    if Path(brands_file).exists():
        with open(brands_file, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["brand"] in wanted:
                    paths += [s for s in (r.get("nykaa_paths") or "").split("|") if s and s != "-"]
    seen = pages = 0
    for i, path in enumerate(paths):
        if i and delay:
            time.sleep(max(delay, nykaa.MIN_DELAY))
        try:
            items = nykaa.fetch_brand(path, delay=delay, log=lambda m: None)
        except nykaa.Blocked as e:
            log(f"nykaa: STOP — {e}; keeping what was fetched so far")
            break
        except Exception as e:                      # one bad brand shouldn't kill the run
            log(f"nykaa: {path} failed ({e})")
            continue
        pages += 1
        for it in items:
            pid = f"nyk-{it['nykaa_id']}"
            if pid in watched:
                ok = it["in_stock"] and it["price"] is not None
                db.record_price(conn, pid, now, it["price"] if ok else None, ok, "nykaa")
                seen += 1
        conn.commit()
    log(f"nykaa: priced {seen}/{len(watched)} watched products from {pages}/{len(paths)} brand pages")


FLIPKART_EVERY_HOURS = 24   # ~225 search pages, ~15 min


def _gated(conn, source, now, every_hours, force, log):
    last = conn.execute("SELECT MAX(ts) FROM price_checks WHERE source=?", (source,)).fetchone()[0]
    if not force and last and now - last < (every_hours - 0.5) * 3600:
        log(f"{source}: skipped (last fetched {(now - last) / 3600:.1f}h ago)")
        return True
    return False


def _ingest_by_brand(conn, products, store, prefix, id_key, jobs, fetch, now, log, delay):
    """Shared loop for the Scrapling stores: one fetch per job (brand), stop at the first refusal."""
    watched = {p.product_id for p in products if p.store == store}
    seen = done = 0
    blocked = False
    for i, job in enumerate(jobs):
        if i and delay:
            time.sleep(max(delay, nykaa.MIN_DELAY))
        try:
            items = fetch(job)
        except nykaa.Blocked as e:
            log(f"{store}: STOP — {e}; keeping what was fetched so far")
            blocked = True
            break
        except Exception as e:
            log(f"{store}: {job} failed ({e})")
            continue
        done += 1
        for it in items:
            pid = f"{prefix}-{it[id_key]}"
            if pid in watched:
                ok = it["in_stock"] and it["price"] is not None
                db.record_price(conn, pid, now, it["price"] if ok else None, ok, store)
                seen += 1
        conn.commit()
    log(f"{store}: priced {seen}/{len(watched)} watched products from {done}/{len(jobs)} brands")
    return blocked


def _brand_column(brands_file, wanted, column):
    out = []
    if Path(brands_file).exists():
        with open(brands_file, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r["brand"] in wanted:
                    out += [s for s in (r.get(column) or "").split("|") if s and s != "-"]
    return out


def ingest_flipkart(conn, products, now, log, delay=nykaa.MIN_DELAY, force=False,
                    every_hours=FLIPKART_EVERY_HOURS):
    brands = sorted({p.brand for p in products if p.store == "flipkart"})
    if not brands:
        log("flipkart: skipped (no flipkart products in watchlist)")
        return
    if _gated(conn, "flipkart", now, every_hours, force, log):
        return
    _ingest_by_brand(conn, products, "flipkart", "fk", "flipkart_id", brands,
                     lambda b: flipkart.fetch_brand(b, delay=delay, log=lambda m: None), now, log, delay)


TIRA_PAGES = 150   # product pages per run (~9 min at 1 / 3 s); see ingest_tira


def ingest_tira(conn, products, brands_file, now, log, delay=nykaa.MIN_DELAY, max_pages=TIRA_PAGES):
    """Brand pages (top 12 per brand) every run, then up to max_pages product pages for the
    watched Tira products whose last price is oldest, so the whole Tira list is refreshed in
    rotation. A refusal anywhere stops Tira for this run."""
    watched = [p for p in products if p.store == "tira"]
    if not watched:
        log("tira: skipped (no tira products in watchlist)")
        return
    slugs = _brand_column(brands_file, {p.brand for p in watched}, "tira_slug")
    if _ingest_by_brand(conn, products, "tira", "tira", "tira_id", slugs,
                        lambda s: tira.fetch_brand(s, log=lambda m: None), now, log, delay):
        return
    if max_pages > 0:
        ingest_tira_pages(conn, watched, now, log, delay, max_pages)


def ingest_tira_pages(conn, watched, now, log, delay, max_pages):
    last = dict(conn.execute("SELECT product_id, MAX(ts) FROM price_checks "
                             "WHERE source IN ('tira','tira-page') GROUP BY product_id").fetchall())
    due = sorted((p for p in watched if p.url and last.get(p.product_id, 0) < now),
                 key=lambda p: last.get(p.product_id, 0))[:max_pages]
    priced = gone = 0
    for p in due:
        time.sleep(max(delay, nykaa.MIN_DELAY))     # also after the last brand page
        try:
            it = tira.fetch_product(p.url)
        except tira.NotFound:
            db.record_price(conn, p.product_id, now, None, False, "tira-page")   # delisted
            gone += 1
            continue
        except nykaa.Blocked as e:
            log(f"tira-page: STOP — {e}; keeping what was fetched so far")
            break
        except Exception as e:
            log(f"tira-page: {p.url} failed ({e})")
            continue
        ok = it["in_stock"] and it["price"] is not None
        db.record_price(conn, p.product_id, now, it["price"] if ok else None, ok, "tira-page")
        priced += 1
        if priced % 25 == 0:
            conn.commit()
    conn.commit()
    oldest = min((last.get(p.product_id, 0) for p in watched), default=0)
    age = f"{(now - oldest) / 86400:.1f} days" if oldest else "never priced"
    log(f"tira-page: priced {priced}, {gone} gone, of {len(due)} due ({len(watched)} watched; "
        f"oldest price before this run: {age})")


def latest_check(conn, product_id, now):
    row = conn.execute(
        "SELECT ts, price, in_stock, source FROM price_checks WHERE product_id=? "
        "AND source IN ('keepa','manual','purplle','maccaron','nykaa','flipkart','tira','tira-page') ORDER BY ts DESC LIMIT 1", (product_id,)).fetchone()
    if not row or now - row[0] > FRESH_HOURS[row[3]] * 3600:
        return None
    return row


def find_deals(conn, products, now, rules=deals.Rules()):
    found = []
    for p in products:
        cur = latest_check(conn, p.product_id, now)
        if not cur:
            continue
        ts, price, in_stock, _ = cur
        if not in_stock:
            found.append((p, price, deals.Verdict(False, "out of stock")))
            continue
        # Look back far enough to find where the current price run began.
        lookback = ts - (rules.low_window_days + 120) * deals.DAY
        pts = [h for h in db.history(conn, p.product_id, lookback) if h[0] <= ts]
        before_run, run_start = deals.split_current_run(pts, price)
        run_start = run_start or ts
        win_start = run_start - rules.low_window_days * deals.DAY
        past = [h for h in before_run if h[0] >= win_start]
        carry = db.last_before(conn, p.product_id, win_start)
        v = deals.evaluate(price, True, ts, past, carry,
                           db.last_post(conn, p.product_id), rules, run_start=run_start)
        found.append((p, price, v))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--db", default=str(ROOT / "data" / "prices.db"))
    ap.add_argument("--watchlist", default=str(ROOT / "data" / "watchlist.csv"))
    ap.add_argument("--manual", default=str(ROOT / "data" / "manual_prices.csv"))
    ap.add_argument("--outbox", default=str(ROOT / "outbox"))
    ap.add_argument("--brands-file", default=str(ROOT / "data" / "brands.csv"))
    ap.add_argument("--no-purplle", action="store_true", help="skip the Purplle price fetch")
    ap.add_argument("--purplle-delay", type=float, default=2.0, help=argparse.SUPPRESS)
    ap.add_argument("--no-maccaron", action="store_true", help="skip the Maccaron price fetch")
    ap.add_argument("--maccaron-delay", type=float, default=2.0, help=argparse.SUPPRESS)
    ap.add_argument("--maccaron-every", type=float, default=MACCARON_EVERY_HOURS,
                    help="refetch Maccaron at most every N hours (default %(default)s)")
    ap.add_argument("--no-nykaa", action="store_true", help="skip the Nykaa price fetch")
    ap.add_argument("--nykaa-delay", type=float, default=nykaa.MIN_DELAY, help=argparse.SUPPRESS)
    ap.add_argument("--nykaa-every", type=float, default=NYKAA_EVERY_HOURS,
                    help="refetch Nykaa at most every N hours (default %(default)s)")
    ap.add_argument("--no-flipkart", action="store_true", help="skip the Flipkart price fetch")
    ap.add_argument("--flipkart-every", type=float, default=FLIPKART_EVERY_HOURS,
                    help="refetch Flipkart at most every N hours (default %(default)s)")
    ap.add_argument("--no-tira", action="store_true", help="skip the Tira price fetch")
    ap.add_argument("--tira-pages", type=int, default=TIRA_PAGES,
                    help="Tira product pages per run, stalest first (default %(default)s; 0 = brand pages only)")
    ap.add_argument("--now", type=int, help=argparse.SUPPRESS)  # for tests
    a = ap.parse_args(argv)
    log = lambda m: print(m, file=sys.stderr)

    cfg = load_config(a.config)
    now = a.now or int(time.time())
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(a.db)
    products = load_watchlist(a.watchlist)

    n = 0
    for pid, ts, price, ok in manual.read(a.manual):
        db.record_price(conn, pid, ts, price, ok, "manual")
        n += 1
    log(f"manual: {n} rows")
    ingest_keepa(conn, cfg, products, now, log)
    conn.commit()
    if not a.no_purplle:
        ingest_purplle(conn, products, a.brands_file, now, log, a.purplle_delay)
    if not a.no_maccaron:
        ingest_maccaron(conn, products, now, log, a.maccaron_delay, every_hours=a.maccaron_every)
    if not a.no_nykaa:
        ingest_nykaa(conn, products, a.brands_file, now, log, a.nykaa_delay, every_hours=a.nykaa_every)
    if not a.no_flipkart:
        ingest_flipkart(conn, products, now, log, a.nykaa_delay, every_hours=a.flipkart_every)
    if not a.no_tira:
        ingest_tira(conn, products, a.brands_file, now, log, a.nykaa_delay, a.tira_pages)

    can_telegram = cfg.get("telegram_token") and cfg.get("telegram_chat_id")
    wa_lines, posted = [], 0
    for p, price, v in find_deals(conn, products, now):
        if not v.is_deal:
            log(f"  -  {p.product_id}: {v.reason}")
            continue
        link = posts.affiliate_link(p, cfg.get("amazon_tag", ""))
        if not link:
            log(f"  !  {p.product_id}: deal ({v.reason}) but no affiliate link — add one to watchlist.csv")
            continue
        tg = posts.telegram_text(p, price, v, link)
        wa = posts.whatsapp_text(p, price, v, link)
        if a.dry_run:
            print(f"\n=== DEAL {p.product_id} ({v.reason}) ===\n--- Telegram (HTML) ---\n{tg}\n--- WhatsApp ---\n{wa}")
            continue
        if can_telegram:
            telegram.send_message(cfg["telegram_token"], cfg["telegram_chat_id"], tg)
            db.record_post(conn, p.product_id, now, price, "telegram")
        wa_lines.append(wa)
        db.record_post(conn, p.product_id, now, price, "whatsapp-queue")
        conn.commit()
        posted += 1

    if wa_lines:
        out = Path(a.outbox) / "whatsapp"
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"{datetime.now():%Y-%m-%d}.txt"
        with f.open("a", encoding="utf-8") as fh:
            for w in wa_lines:
                fh.write(f"----- {datetime.now():%H:%M} -----\n{w}\n\n")
        log(f"whatsapp: {len(wa_lines)} posts queued in {f}")
    if not a.dry_run and not can_telegram:
        log("telegram: not configured, skipped")
    log(f"done: {posted} deals posted")
    conn.close()


if __name__ == "__main__":
    main()
