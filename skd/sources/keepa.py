"""Amazon.in prices via the Keepa API (https://keepa.com/#!api).

Keepa format notes (verify against the docs with your own key):
  * domain 10 = amazon.in
  * prices are integers in the smallest currency unit (paise); -1 = no offer
  * csv[i] is a flat list [keepaTime, price, keepaTime, price, ...]
    i = 0 AMAZON (sold by Amazon), 1 NEW (lowest 3rd-party new)
  * keepaTime is minutes since 2011-01-01 UTC
  * with stats=N, product["stats"]["current"][i] holds the latest price
We prefer the "sold by Amazon" price (fewer counterfeit worries) and fall
back to the lowest new offer.
"""
import json
import urllib.parse
import urllib.request

KEEPA_DOMAIN_IN = 10
KEEPA_EPOCH_MIN = 21564000  # minutes between 1970-01-01 and 2011-01-01
AMAZON, NEW = 0, 1


def keepa_time_to_unix(kt: int) -> int:
    return (kt + KEEPA_EPOCH_MIN) * 60


def _rupees(v):
    return None if v is None or v < 0 else v / 100


def fetch_products(api_key, asins, history_days=90, opener=urllib.request.urlopen):
    """Return raw Keepa product dicts for up to 100 ASINs (1 token each)."""
    q = urllib.parse.urlencode({
        "key": api_key, "domain": KEEPA_DOMAIN_IN, "asin": ",".join(asins),
        "stats": 30, "days": history_days,
    })
    with opener(f"https://api.keepa.com/product?{q}", timeout=60) as r:
        data = json.load(r)
    if "error" in data:
        raise RuntimeError(f"Keepa error: {data['error']}")
    return data.get("products", [])


def current_price(product):
    """(price_in_rupees | None, in_stock) for one Keepa product."""
    cur = (product.get("stats") or {}).get("current") or []
    for idx in (AMAZON, NEW):
        if idx < len(cur):
            p = _rupees(cur[idx])
            if p is not None:
                return p, True
    return None, False


def price_history(product):
    """[(unix_ts, rupees | None)] change points, preferring the Amazon series."""
    csv = product.get("csv") or []
    for idx in (AMAZON, NEW):
        series = csv[idx] if idx < len(csv) else None
        if series:
            return [(keepa_time_to_unix(series[i]), _rupees(series[i + 1]))
                    for i in range(0, len(series) - 1, 2)]
    return []
