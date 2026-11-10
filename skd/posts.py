"""Affiliate links and message text for Telegram (HTML) and WhatsApp (plain markup)."""
import html
from dataclasses import dataclass

STORE_NAMES = {"amazon": "Amazon", "nykaa": "Nykaa", "flipkart": "Flipkart",
               "purplle": "Purplle", "brand": "brand site"}
DISCLOSURE = "#ad · affiliate link — I may earn a small commission at no extra cost to you"


@dataclass
class Product:
    product_id: str
    name: str
    brand: str
    size: str
    store: str
    asin: str = ""
    url: str = ""
    affiliate_url: str = ""
    key_ingredient: str = ""
    skin_types: str = ""


def affiliate_link(p: Product, amazon_tag: str) -> str | None:
    """Amazon: build from ASIN + tag. Others: the link you converted in
    EarnKaro/GrabCash and pasted into watchlist.csv. None = can't post yet."""
    if p.store == "amazon" and p.asin and amazon_tag:
        return f"https://www.amazon.in/dp/{p.asin}?tag={amazon_tag}"
    return p.affiliate_url or None


def inr(x: float) -> str:
    """Indian digit grouping: 123456 -> ₹1,23,456."""
    n = int(round(x))
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-" if n < 0 else "") + "₹" + s


def _lines(p, price, verdict):
    title = f"{p.brand} {p.name}" + (f" ({p.size})" if p.size else "")
    store = STORE_NAMES.get(p.store, p.store.title())
    if verdict.lowest_in_days:
        headline = f"{inr(price)} on {store} — lowest in {verdict.lowest_in_days} days"
    else:
        headline = f"{inr(price)} on {store}"
    detail = None
    if verdict.avg_30d and verdict.pct_below_avg and verdict.pct_below_avg > 0:
        detail = f"30-day avg {inr(verdict.avg_30d)} ({verdict.pct_below_avg:.0%} lower)"
    tags = []
    if p.key_ingredient:
        tags.append(f"🧪 {p.key_ingredient}")
    if p.skin_types:
        tags.append(f"Skin: {p.skin_types}")
    return title, headline, detail, " · ".join(tags)


def telegram_text(p, price, verdict, link):
    title, headline, detail, tags = _lines(p, price, verdict)
    e = html.escape
    out = [f"🔥 <b>{e(title)}</b>", e(headline)]
    if detail:
        out.append(e(detail))
    if tags:
        out.append(e(tags))
    out += [f'🛒 <a href="{e(link, quote=True)}">Buy now</a>', f"<i>{e(DISCLOSURE)}</i>"]
    return "\n".join(out)


def whatsapp_text(p, price, verdict, link):
    title, headline, detail, tags = _lines(p, price, verdict)
    out = [f"🔥 *{title}*", headline]
    if detail:
        out.append(detail)
    if tags:
        out.append(tags)
    out += [f"🛒 {link}", f"_{DISCLOSURE}_"]
    return "\n".join(out)
