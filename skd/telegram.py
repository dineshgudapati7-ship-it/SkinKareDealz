"""Official Telegram Bot API over urllib (no SDK).

send_message: post to the channel (the bot must be a channel admin with "Post messages").
call: any Bot API method with a JSON body, used by the price-check bot (skd.bot)."""
import json
import urllib.error
import urllib.parse
import urllib.request


def send_message(token, chat_id, text, opener=urllib.request.urlopen):
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with opener(req, timeout=30) as r:
        resp = json.load(r)
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram error: {resp.get('description')}")
    return resp["result"]["message_id"]


def call(token, method, params=None, timeout=30, opener=urllib.request.urlopen):
    """POST a Bot API method as JSON and return its result (raises on ok=false)."""
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params or {}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with opener(req, timeout=timeout) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:      # Telegram puts the reason in the error body
        try:
            resp = json.load(e)
        except ValueError:
            raise RuntimeError(f"Telegram {method}: HTTP {e.code}") from None
    if not resp.get("ok"):
        raise RuntimeError(f"Telegram {method}: {resp.get('description')}")
    return resp["result"]
