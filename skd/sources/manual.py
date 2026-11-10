"""Prices you record yourself (Nykaa, brand sites) until a network feed exists.

data/manual_prices.csv columns: product_id,date,price
  date: YYYY-MM-DD or YYYY-MM-DD HH:MM (local time, Asia/Kolkata)
  price: rupees, or blank / OOS for out of stock
"""
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))


def _parse_ts(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s.strip(), fmt).replace(tzinfo=IST).timestamp())
        except ValueError:
            pass
    raise ValueError(f"bad date: {s!r}")


def read(path):
    """Yield (product_id, ts, price | None, in_stock)."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = (row.get("price") or "").strip()
            oos = raw == "" or raw.upper() == "OOS"
            yield (row["product_id"].strip(), _parse_ts(row["date"]),
                   None if oos else float(raw), not oos)
