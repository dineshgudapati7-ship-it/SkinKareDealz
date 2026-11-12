"""Build the watchlist from brand catalogues.

    python -m skd.catalog                 # tiers 2-5 from data/brands.csv
    python -m skd.catalog --tiers 2 --brands "Minimalist,Plum"
    python -m skd.catalog --stores nykaa  # opt-in; needs scrapling, 1 request / 3 s
    python -m skd.catalog --stores flipkart,tira   # same method
    (Tira: top 12 per brand page + every sitemap product of our brands, one page each;
     products already in the watchlist are skipped, so only the first run is long, ~2.5 h)

For every brand with Purplle slugs it fetches all products (variants expanded),
saves a dated snapshot with prices to data/catalog/, and merges the products
into data/watchlist.csv as store=purplle rows (product_id = pur-<purplle id>).
Existing rows keep their affiliate_url / key_ingredient / skin_types.
"""
import argparse
import csv
import sys
import time
from datetime import date
from pathlib import Path

from .sources import flipkart, maccaron, nykaa, purplle, tira

ROOT = Path(__file__).resolve().parent.parent
WATCH_FIELDS = ["product_id", "name", "brand", "size", "store", "asin", "url",
                "affiliate_url", "key_ingredient", "skin_types", "ean"]
PURPLLE_FIELDS = ["tier", "brand", "purplle_id", "name", "size", "url", "mrp", "price", "in_stock",
                  "offer", "offer_ids"]
MACCARON_FIELDS = ["tier", "brand", "maccaron_id", "name", "url", "price", "in_stock", "ean"]
NYKAA_FIELDS = ["tier", "brand", "nykaa_id", "sku", "name", "size", "type", "url", "mrp", "price",
                "in_stock"]
FLIPKART_FIELDS = ["tier", "brand", "flipkart_id", "name", "size", "url", "mrp", "price", "in_stock"]
TIRA_FIELDS = ["tier", "brand", "tira_id", "name", "size", "url", "mrp", "price", "in_stock"]
NOT_ON_NYKAA = "-"          # nykaa_paths value: searched, no brand page found


def read_brands(path, tiers, only=None):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r["tier"] in tiers and (not only or r["brand"] in only)]


def merge_watchlist(path, new_rows):
    """Upsert rows by product_id; returns (added, updated, total)."""
    path = Path(path)
    existing = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))
    index = {r["product_id"]: r for r in existing}
    added = updated = 0
    for n in new_rows:
        cur = index.get(n["product_id"])
        if cur:
            changed = any(cur.get(k) != n[k] for k in ("name", "brand", "size", "url")
                          if not (k == "size" and not n[k]))
            # An empty size never wipes one we filled in (e.g. skd.nykaa_sizes).
            cur.update({k: n[k] for k in ("name", "brand", "size", "store", "url", "ean")
                        if k in n and not (k == "size" and not n[k] and cur.get("size"))})
            updated += changed
        else:
            row = {k: "" for k in WATCH_FIELDS} | n
            existing.append(row)
            index[n["product_id"]] = row
            added += 1
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=WATCH_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(existing)
    return added, updated, len(existing)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--brands-file", default=str(ROOT / "data" / "brands.csv"))
    ap.add_argument("--watchlist", default=str(ROOT / "data" / "watchlist.csv"))
    ap.add_argument("--snapshot-dir", default=str(ROOT / "data" / "catalog"))
    ap.add_argument("--tiers", default="2,3,4,5")
    ap.add_argument("--brands", help="comma-separated brand names to limit to")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    ap.add_argument("--stores", default="purplle,maccaron")
    ap.add_argument("--no-tira-sitemap", action="store_true",
                    help="Tira: brand pages only (top 12 per brand), skip the sitemap product pages")
    a = ap.parse_args(argv)
    log = lambda m: print(m, file=sys.stderr)

    only = {b.strip() for b in a.brands.split(",")} if a.brands else None
    brands = read_brands(a.brands_file, set(a.tiers.split(",")), only)
    stores = {s.strip() for s in a.stores.split(",")}
    builders = {"purplle": build_purplle, "maccaron": build_maccaron,
                "nykaa": lambda b, d, l: build_nykaa(b, d, l, a.brands_file),
                "flipkart": build_flipkart,
                "tira": lambda b, d, l: build_tira(b, d, l, a.brands_file,
                                                   None if a.no_tira_sitemap else a.watchlist)}
    snap_dir = Path(a.snapshot_dir)
    snap_dir.mkdir(parents=True, exist_ok=True)
    for store, build in builders.items():
        if store not in stores:
            continue
        log(f"== {store}")
        snap_rows, watch_rows, fields, missing = build(brands, a.delay, log)
        snap = snap_dir / f"{store}_{date.today():%Y-%m-%d}.csv"
        if snap.exists():           # a partial (--brands) rerun the same day: keep the other brands' rows
            ran = {b["brand"] for b in brands}
            with snap.open(newline="", encoding="utf-8") as f:
                snap_rows = [r for r in csv.DictReader(f) if r.get("brand") not in ran] + snap_rows
        with snap.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(snap_rows)
        added, updated, total = merge_watchlist(a.watchlist, watch_rows)
        log(f"{store} snapshot: {len(snap_rows)} rows -> {snap}")
        log(f"watchlist: +{added} new, {updated} updated, {total} total rows")
        if missing:
            log(f"not on {store} ({len(missing)}): {', '.join(missing)}")


def build_purplle(brands, delay, log):
    snap_rows, watch_rows, missing = [], [], []
    for b in brands:
        slugs = [s for s in (b.get("purplle_slugs") or "").split("|") if s]
        if not slugs:
            missing.append(b["brand"])
            continue
        seen = set()
        for slug in slugs:
            try:
                items = purplle.fetch_brand(slug, delay=delay, log=log)
            except purplle.Blocked as e:
                log(f"STOP: {e} — not retrying.")
                return snap_rows, watch_rows, PURPLLE_FIELDS, missing
            for it in items:
                if it["purplle_id"] in seen:
                    continue
                seen.add(it["purplle_id"])
                snap_rows.append({"tier": b["tier"], "brand": b["brand"], **it})
                watch_rows.append({"product_id": f"pur-{it['purplle_id']}", "name": it["name"],
                                   "brand": b["brand"], "size": it["size"],
                                   "store": "purplle", "url": it["url"]})
        log(f"{b['brand']}: {len(seen)} products")
    return snap_rows, watch_rows, PURPLLE_FIELDS, missing


def build_maccaron(brands, delay, log):
    by_prefix = {}
    missing = []
    for b in brands:
        prefixes = [s for s in (b.get("maccaron_prefixes") or "").split("|") if s]
        if not prefixes:
            missing.append(b["brand"])
        for p in prefixes:
            by_prefix[p] = b
    snap_rows, watch_rows = [], []
    if not by_prefix:
        return snap_rows, watch_rows, MACCARON_FIELDS, missing
    try:
        urls = maccaron.product_urls(by_prefix)
        log(f"sitemap: {sum(map(len, urls.values()))} product URLs for {len(by_prefix)} brands")
        for prefix, links in urls.items():
            b, kept = by_prefix[prefix], 0
            for url in links:
                p = maccaron.fetch_product(url)
                time.sleep(delay)
                if not p:
                    continue                      # discontinued page / no product data
                pid = maccaron.product_id(url)
                kept += 1
                snap_rows.append({"tier": b["tier"], "brand": b["brand"], "maccaron_id": pid,
                                  "url": url, **p})
                watch_rows.append({"product_id": f"mac-{pid}", "name": p["name"], "brand": b["brand"],
                                   "size": "", "store": "maccaron", "url": url, "ean": p["ean"]})
            log(f"{b['brand']}: {kept}/{len(links)} live products")
    except maccaron.Blocked as e:
        log(f"STOP: {e} — not retrying.")
    return snap_rows, watch_rows, MACCARON_FIELDS, missing

def save_nykaa_paths(brands_file, resolved, column="nykaa_paths"):
    """Write {brand: 'path|path' or '-'} into a column (default nykaa_paths) of brands.csv."""
    with open(brands_file, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        fields, rows = list(r.fieldnames), list(r)
    if column not in fields:
        fields.append(column)
    for row in rows:
        if row["brand"] in resolved:
            row[column] = resolved[row["brand"]]
    with open(brands_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def build_nykaa(brands, delay, log, brands_file=None):
    """Brand pages on nykaa.com, slow and steady; the whole store stops at the first refusal."""
    delay = max(delay, nykaa.MIN_DELAY)
    snap_rows, watch_rows, missing, resolved = [], [], [], {}
    first = True
    try:
        for b in brands:
            paths = (b.get("nykaa_paths") or "").strip()
            if not paths:
                if not first:
                    time.sleep(delay)
                first = False
                found = nykaa.find_brand_path(b["brand"])
                paths = resolved[b["brand"]] = found or NOT_ON_NYKAA
                log(f"{b['brand']}: brand page {'-> ' + found if found else 'not found'}")
            if paths == NOT_ON_NYKAA:
                missing.append(b["brand"])
                continue
            seen = set()
            for path in paths.split("|"):
                if not first:
                    time.sleep(delay)
                first = False
                for it in nykaa.fetch_brand(path, delay=delay, log=log):
                    if it["nykaa_id"] in seen:
                        continue
                    seen.add(it["nykaa_id"])
                    snap_rows.append({"tier": b["tier"], "brand": b["brand"], **it})
                    watch_rows.append({"product_id": f"nyk-{it['nykaa_id']}", "name": it["name"],
                                       "brand": b["brand"], "size": it["size"],
                                       "store": "nykaa", "url": it["url"]})
            log(f"{b['brand']}: {len(seen)} products")
    except nykaa.Blocked as e:
        log(f"STOP: {e} — not retrying.")
    finally:
        if resolved and brands_file:
            save_nykaa_paths(brands_file, resolved)
    return snap_rows, watch_rows, NYKAA_FIELDS, missing


def build_flipkart(brands, delay, log):
    """Search each brand on flipkart.com (slow and steady); stops at the first refusal."""
    delay = max(delay, nykaa.MIN_DELAY)
    snap_rows, watch_rows, missing = [], [], []
    try:
        for i, b in enumerate(brands):
            if i:
                time.sleep(delay)
            items = flipkart.fetch_brand(b["brand"], delay=delay, log=log)
            if not items:
                missing.append(b["brand"])
            for it in items:
                snap_rows.append({"tier": b["tier"], "brand": b["brand"], **it})
                watch_rows.append({"product_id": f"fk-{it['flipkart_id']}", "name": it["name"],
                                   "brand": b["brand"], "size": it["size"],
                                   "store": "flipkart", "url": it["url"]})
    except nykaa.Blocked as e:
        log(f"STOP: {e} — not retrying.")
    return snap_rows, watch_rows, FLIPKART_FIELDS, missing


def build_tira(brands, delay, log, brands_file=None, watchlist=None):
    """Tira products for our brands; stops at the first refusal.

    1. Each brand page /brand/<slug>: its top 12 products, one request per brand.
    2. If `watchlist` is given: every product sitemap URL that starts with one of the brand's
       slugs and isn't in the watchlist yet, one product page each. The page confirms the brand
       (a prefix like "simple-" also matches other brands' products)."""
    delay = max(delay, nykaa.MIN_DELAY)
    snap_rows, watch_rows, missing, resolved = [], [], [], {}
    found = set()                                    # brands with at least one product
    seen_ids = set()

    def add(b, it):
        if it["tira_id"] in seen_ids:
            return
        seen_ids.add(it["tira_id"])
        found.add(b["brand"])
        snap_rows.append({"tier": b["tier"], "brand": b["brand"], **it})
        watch_rows.append({"product_id": f"tira-{it['tira_id']}", "name": it["name"],
                           "brand": b["brand"], "size": it.get("size", ""), "store": "tira", "url": it["url"]})
    try:
        slugs = None
        for b in brands:
            known = (b.get("tira_slug") or "").strip()
            if known == NOT_ON_NYKAA:
                continue
            if known:
                candidates = [known]
            else:
                if slugs is None:
                    slugs = tira.brand_slugs()
                    time.sleep(delay)
                listed = tira.slug_for(slugs, b["brand"])
                candidates = [listed] if listed else tira.guess_slugs(b["brand"])
            rows = slug = None
            for cand in candidates:
                time.sleep(delay)
                try:
                    got = tira.fetch_brand(cand, log=log)
                except tira.NotFound:
                    continue
                if known or tira.is_brand(got, b["brand"]):
                    rows, slug = got, cand
                    break
            if not known:
                resolved[b["brand"]] = slug or NOT_ON_NYKAA
            for it in rows or []:
                add(b, it)
        if watchlist:
            _tira_sitemap_products(brands, resolved, watchlist, delay, log, add)
    except nykaa.Blocked as e:
        log(f"STOP: {e} — not retrying.")
    finally:
        if resolved and brands_file:
            save_nykaa_paths(brands_file, resolved, column="tira_slug")
    missing = [b["brand"] for b in brands if b["brand"] not in found]
    return snap_rows, watch_rows, TIRA_FIELDS, missing


def _tira_sitemap_products(brands, resolved, watchlist, delay, log, add):
    known_urls = set()
    if Path(watchlist).exists():
        with open(watchlist, newline="", encoding="utf-8") as f:
            known_urls = {r["url"] for r in csv.DictReader(f) if r.get("store") == "tira"}
    time.sleep(delay)
    urls = tira.product_sitemap_urls(delay=delay, log=log)
    for b in brands:
        slug = (resolved.get(b["brand"]) or b.get("tira_slug") or "").strip()
        prefixes = list(dict.fromkeys(([slug] if slug and slug != NOT_ON_NYKAA else []) + tira.guess_slugs(b["brand"])))
        todo = [u for u in tira.urls_for_brand(urls, prefixes) if u not in known_urls]
        kept = 0
        for u in todo:
            time.sleep(delay)
            try:
                it = tira.fetch_product(u)
            except tira.NotFound:
                continue
            if it["brand_slug"] not in prefixes and nykaa._norm(it["brand_name"]) != nykaa._norm(b["brand"]):
                continue                             # a different brand that shares the prefix
            add(b, {k: it[k] for k in TIRA_FIELDS if k in it})
            kept += 1
        if todo:
            log(f"  tira {b['brand']}: {kept} of {len(todo)} new sitemap products")


if __name__ == "__main__":
    main()
