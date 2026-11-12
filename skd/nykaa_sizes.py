"""Fill in the size of Nykaa listings whose brand-page entry has none.

About 30% of Nykaa listings are multi-size ("configurable") products. The brand page
shows their default size's price but not which size that is, so the price-check bot
can't compare them. Each product page states it (window.__PRELOADED_STATE__ ->
dataLayer.product.packSize) and lists every size under productPage.product.variants.

    pip install "scrapling[fetchers]"
    python -m skd.nykaa_sizes              # fetch, then write sizes into watchlist.csv
    python -m skd.nykaa_sizes --apply-only # only copy already-fetched sizes in

Same rules as the rest of the Nykaa code: one request at a time, >= nykaa.MIN_DELAY
seconds apart, a single attempt each, and a full stop at the first refusal. Results go
to data/nykaa_sizes.csv after every page, so a later run carries on where a
stop left off.
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

from .sources import nykaa

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["nykaa_id", "size", "mrp", "price", "variants", "fetched"]


def page_size(state):
    """(default size, mrp, price, [(size, child id, mrp, price)]) from a product page state."""
    prod = (state.get("productPage") or {}).get("product") or {}
    dl = (state.get("dataLayer") or {}).get("product") or {}
    variants = [((v.get("packSize") or v.get("variantName") or "").strip(), str(v.get("childId") or ""),
                 v.get("mrp"), v.get("offerPrice")) for v in prod.get("variants") or []]
    size = (dl.get("packSize") or prod.get("packSize") or "").strip()
    if not size:   # fall back to the one variant priced like the page itself
        same = [v for v in variants if (v[2], v[3]) == (prod.get("mrp"), prod.get("offerPrice"))]
        size = same[0][0] if len(same) == 1 else ""
    return size, prod.get("mrp"), prod.get("offerPrice"), variants


def todo(watchlist, done):
    """Unsized single-product Nykaa rows not fetched yet: [(product_id, url)]."""
    from . import match, run
    out = []
    for p in run.load_watchlist(watchlist):
        if p.store != "nykaa" or p.size.strip() or p.product_id[4:] in done:
            continue
        if match.features(p).combo:          # kits and bundles have no single size
            continue
        out.append((p.product_id, p.url))
    return out


def load_done(path):
    if not Path(path).exists():
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["nykaa_id"]: r for r in csv.DictReader(f)}


def fetch(watchlist, out_path, limit=None, delay=nykaa.MIN_DELAY, get=nykaa._default_get,
          log=print, sleep=time.sleep):
    delay = max(delay, nykaa.MIN_DELAY)
    done = load_done(out_path)
    jobs = todo(watchlist, done)[:limit]
    log(f"nykaa sizes: {len(jobs)} pages to read ({len(done)} already done), "
        f"about {len(jobs) * (delay + 1.5) / 60:.0f} min")
    new = not Path(out_path).exists()
    got = 0
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        for i, (pid, url) in enumerate(jobs):
            if i:
                sleep(delay)
            try:
                _, state = nykaa.fetch_state(url, get)
            except nykaa.Blocked as ex:
                log(f"nykaa sizes: stopped at {i}/{len(jobs)}: {ex}")
                break
            except Exception as ex:     # connection reset, timeout...: also a refusal. No retry.
                log(f"nykaa sizes: stopped at {i}/{len(jobs)}: {type(ex).__name__}: {str(ex)[:120]}")
                break
            size, mrp, price, variants = page_size(state)
            w.writerow({"nykaa_id": pid[4:], "size": size, "mrp": mrp, "price": price,
                        "variants": json.dumps(variants), "fetched": int(time.time())})
            f.flush()
            got += bool(size)
            if (i + 1) % 50 == 0:
                log(f"  {i + 1}/{len(jobs)} read, {got} sizes found")
    log(f"nykaa sizes: {got} sizes found this run")


def apply(watchlist, sizes_path, log=print):
    """Write fetched sizes into empty size cells of nyk- rows. Returns rows changed."""
    sizes = {f"nyk-{k}": r["size"] for k, r in load_done(sizes_path).items() if r["size"]}
    with open(watchlist, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        fields, rows = rd.fieldnames, list(rd)
    n = 0
    for r in rows:
        if not r["size"].strip() and r["product_id"] in sizes:
            r["size"] = sizes[r["product_id"]]
            n += 1
    with open(watchlist, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    log(f"watchlist: filled {n} Nykaa sizes")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--watchlist", default=str(ROOT / "data" / "watchlist.csv"))
    ap.add_argument("--out", default=str(ROOT / "data" / "nykaa_sizes.csv"))
    ap.add_argument("--limit", type=int, help="read at most N pages this run")
    ap.add_argument("--apply-only", action="store_true")
    a = ap.parse_args(argv)
    log = lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", file=sys.stderr, flush=True)
    if not a.apply_only:
        fetch(a.watchlist, a.out, a.limit, log=log)
    apply(a.watchlist, a.out, log=log)


if __name__ == "__main__":
    main()
