"""Post to a Telegram channel via the official Bot API (sendMessage, HTML mode).
The bot must be added to the channel as an admin with "Post messages"."""
import json
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
