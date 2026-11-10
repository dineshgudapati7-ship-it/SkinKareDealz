"""Decide whether a current price is a *real* deal, using our own price history.

MRP is ignored on purpose: "70% off MRP" is usually marketing, not a drop.

Rules (all must hold):
  * product is in stock
  * enough history to judge (>= MIN_HISTORY_DAYS of coverage)
  * EITHER price <= (1 - DROP_PCT) * time-weighted 30-day average
    OR     price is strictly below the lowest price of the previous 60 days
           (only when history covers >= LOW_WINDOW_MIN_DAYS, so "lowest in 60
           days" is an honest claim)
  * cooldown: not posted in the last COOLDOWN_HOURS, unless the price has
    fallen a further REPOST_EXTRA_DROP_PCT below the last posted price
"""
from dataclasses import dataclass

DAY = 86400


@dataclass(frozen=True)
class Rules:
    drop_pct: float = 0.20
    avg_window_days: int = 30
    low_window_days: int = 60
    min_history_days: int = 7
    low_window_min_days: int = 30
    cooldown_hours: int = 72
    repost_extra_drop_pct: float = 0.05


@dataclass(frozen=True)
class Verdict:
    is_deal: bool
    reason: str
    avg_30d: float | None = None
    low_60d: float | None = None
    pct_below_avg: float | None = None
    lowest_in_days: int | None = None   # set when it's a new N-day low


def time_weighted_average(points, start, end, carry_in=None):
    """Average price over [start, end), each price holding until the next point.

    points: [(ts, price)] sorted ascending, all within [start, end).
    carry_in: (ts, price) observed before `start`, holding at `start`.
    This avoids bias from uneven sampling (Keepa only logs *changes*, while our
    own checks run every few hours).
    """
    seq = []
    if carry_in is not None:
        seq.append((start, carry_in[1]))
    seq.extend(p for p in points if start <= p[0] < end)
    if not seq:
        return None
    total = weighted = 0.0
    for i, (ts, price) in enumerate(seq):
        nxt = seq[i + 1][0] if i + 1 < len(seq) else end
        span = max(0, nxt - ts)
        weighted += price * span
        total += span
    return weighted / total if total else seq[-1][1]


def split_current_run(points, price):
    """Split [(ts, price)] (oldest first, ending with the current check) into
    (history_before_run, run_start_ts). The current "run" is the trailing
    stretch at the current price — e.g. Keepa logs the drop as a change point,
    and that point must not count as history, or a new low compares to itself."""
    i = len(points)
    while i > 0 and points[i - 1][1] == price:
        i -= 1
    run_start = points[i][0] if i < len(points) else None
    return points[:i], run_start


def evaluate(price, in_stock, now, past, carry_in=None, last_post=None, rules=Rules(),
             run_start=None):
    """past: [(ts, price)] observations before the current price run, oldest
    first, covering the low window. carry_in: observation just before that
    window, if any. Windows are measured back from run_start (when the price
    changed to its current value); cooldown is measured from `now`."""
    if not in_stock or price is None:
        return Verdict(False, "out of stock")
    checked_at = now
    now = run_start if run_start is not None else now
    earliest = carry_in[0] if carry_in else (past[0][0] if past else now)
    coverage_days = (now - earliest) / DAY
    if coverage_days < rules.min_history_days:
        return Verdict(False, f"only {coverage_days:.1f} days of history")

    avg_start = now - rules.avg_window_days * DAY
    in_avg = [p for p in past if p[0] >= avg_start]
    before_avg = [p for p in past if p[0] < avg_start]
    avg_carry = before_avg[-1] if before_avg else carry_in
    avg = time_weighted_average(in_avg, avg_start, now, avg_carry)

    low_start = now - rules.low_window_days * DAY
    low_prices = [p[1] for p in past if p[0] >= low_start]
    if carry_in and carry_in[0] < low_start:
        low_prices.append(carry_in[1])  # price was still in effect at window start
    low = min(low_prices) if low_prices else None

    pct = (1 - price / avg) if avg else None
    big_drop = pct is not None and pct >= rules.drop_pct - 1e-9   # 400 vs 500 is exactly 20%
    new_low = (low is not None and price < low
               and coverage_days >= rules.low_window_min_days)

    if not (big_drop or new_low):
        return Verdict(False, "no real drop", avg, low, pct)

    if last_post is not None:
        posted_ts, posted_price = last_post
        same_run = posted_ts >= now                       # already posted this drop
        recent = checked_at - posted_ts < rules.cooldown_hours * 3600
        much_lower = price <= posted_price * (1 - rules.repost_extra_drop_pct)
        if (same_run or recent) and not much_lower:
            return Verdict(False, "already posted" if same_run else "cooldown", avg, low, pct)

    days = min(rules.low_window_days, int(coverage_days)) if new_low else None
    reason = "new low" if new_low else f"{pct:.0%} below 30-day avg"
    return Verdict(True, reason, avg, low, pct, days)
