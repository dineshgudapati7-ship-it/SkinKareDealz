"""SQLite storage: one row per price check, plus a log of what was posted."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_checks (
    product_id  TEXT    NOT NULL,
    ts          INTEGER NOT NULL,          -- unix seconds (UTC)
    price       REAL,                      -- rupees; NULL when out of stock
    in_stock    INTEGER NOT NULL,
    source      TEXT    NOT NULL,          -- keepa | keepa-history | manual
    PRIMARY KEY (product_id, ts, source)
);
CREATE INDEX IF NOT EXISTS idx_checks_product_ts ON price_checks(product_id, ts);

CREATE TABLE IF NOT EXISTS posts (
    product_id  TEXT    NOT NULL,
    ts          INTEGER NOT NULL,
    price       REAL    NOT NULL,
    channel     TEXT    NOT NULL           -- telegram | whatsapp-queue
);
"""


# Recorded for diagnosis only: Purplle's brand-listing price is often below what the product
# page and cart charge, so it is never history or a current price (see run.ingest_purplle_pages).
LISTING_ONLY = ("purplle",)
_NOT_LISTING = f"source NOT IN ({','.join(repr(s) for s in LISTING_ONLY)})"


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def record_price(conn, product_id, ts, price, in_stock, source):
    conn.execute(
        "INSERT OR IGNORE INTO price_checks VALUES (?,?,?,?,?)",
        (product_id, int(ts), price, 1 if in_stock else 0, source),
    )


def history(conn, product_id, since_ts):
    """[(ts, price)] for in-stock checks since since_ts, oldest first."""
    rows = conn.execute(
        "SELECT ts, price FROM price_checks WHERE product_id=? AND ts>=? "
        f"AND in_stock=1 AND price IS NOT NULL AND {_NOT_LISTING} ORDER BY ts",
        (product_id, int(since_ts)),
    )
    return rows.fetchall()


def last_before(conn, product_id, ts):
    """Most recent in-stock (ts, price) strictly before ts, or None."""
    return conn.execute(
        "SELECT ts, price FROM price_checks WHERE product_id=? AND ts<? "
        f"AND in_stock=1 AND price IS NOT NULL AND {_NOT_LISTING} ORDER BY ts DESC LIMIT 1",
        (product_id, int(ts)),
    ).fetchone()


def last_post(conn, product_id):
    """(ts, price) of the most recent post for this product, or None."""
    return conn.execute(
        "SELECT ts, price FROM posts WHERE product_id=? ORDER BY ts DESC LIMIT 1",
        (product_id,),
    ).fetchone()


def record_post(conn, product_id, ts, price, channel):
    conn.execute("INSERT INTO posts VALUES (?,?,?,?)", (product_id, int(ts), price, channel))
