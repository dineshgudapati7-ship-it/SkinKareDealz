# SkinKareDealz

A price tracker that finds *real* skincare deals. It posts them to a Telegram channel automatically and queues WhatsApp-ready text for you to paste into your WhatsApp Channel by hand.

Pure Python 3.11+ standard library. No installs needed.

## How a deal is decided (`skd/deals.py`)
MRP is ignored. A product counts as a deal only when it is **in stock**, has **at least 7 days of history**, and meets one of these:
- it is **≥20% below its time-weighted 30-day average**, or
- it is **below every price in the previous 60 days** (only claimed once there are ≥30 days of history).

To avoid repeat posts, the same price drop isn't posted twice. After a post there is also a 72-hour cooldown, unless the price falls another 5%.

## Setup
1. Copy `config.example.toml` to `config.toml` and fill in:
   - your Keepa API key
   - your Amazon Associates tag
   - a Telegram bot token from @BotFather, with the bot added to your channel as an admin who can post
2. Fill the `asin` column in `data/watchlist.csv`. The ASIN is the `B0…` code in the Amazon product URL or SiteStripe.
3. **Non-Amazon products** (Nykaa, brand sites):
   - Add a row with `store=nykaa` (or `brand`, `purplle`).
   - Convert the product URL in EarnKaro or GrabCash and paste the result into `affiliate_url`.
   - Log prices in `data/manual_prices.csv` (`product_id,date,price`; write `OOS` when out of stock).

## Run
```
python -m skd.run --dry-run   # preview posts; sends and records nothing
python -m skd.run             # post to Telegram + append to outbox/whatsapp/YYYY-MM-DD.txt
python -m unittest discover -s tests -t .
```
Schedule `python -m skd.run` every 2–3 hours with Windows Task Scheduler, or from n8n with an "Execute Command" node.

## WhatsApp
Meta has no official API for posting to Channels, so WhatsApp posting stays manual. Copy the posts from `outbox/whatsapp/<today>.txt` into your Channel. They use WhatsApp's own formatting (`*bold*`, `_italic_`).

## Building the watchlist from brand catalogues
`python -m skd.catalog` reads `data/brands.csv` (tiers 2–5 by default). It pulls every Purplle product for those brands, with one row per size or pack, and makes 1 request every 2 s. It stops on HTTP 403/429.

It saves a dated snapshot with prices in `data/catalog/` and upserts `store=purplle` rows into `data/watchlist.csv`. Rerunning it adds new launches and keeps the affiliate links, ingredients and skin types you've filled in. Brands with an empty `purplle_slugs` aren't on Purplle; source those from Amazon later.

## Purplle price tracking
Every `python -m skd.run` also fetches the Purplle brand pages for the brands in your watchlist and records a price for each `pur-…` product. That's about 160 requests at 1 every 2 s, roughly 5 minutes. Skip it with `--no-purplle`.

`track.cmd` runs this in record-only mode (`--dry-run`) and appends the output to `logs/track.log`. Schedule it every 3 hours with Windows Task Scheduler.

## Running on GitHub Actions
`.github/workflows/track.yml` runs the tracker every 6 hours (IST 05:47, 11:47, 17:47, 23:47; GitHub's cron often starts 10–30 min late). It replaces the Windows task. Each run takes about 6 minutes, roughly 720 of the 2,000 free minutes a month for a private repo. **Maccaron is not fetched in the cloud.** It returns 403 to GitHub's datacenter IPs, while the same page returns 200 from a home connection.

- **State:** `prices.db` lives on the `data` branch as a single commit, force-pushed after each run. The run fails rather than starting an empty DB, and it refuses to save a DB that has fewer rows than it restored. To get a local copy: `git fetch origin data && git show origin/data:prices.db > data/prices.db`.
- **Manual run:** Actions tab → "price tracking" → Run workflow.
- **Going live:** add the repo secret `TELEGRAM_TOKEN`, add the variables `TELEGRAM_CHAT_ID` and `AMAZON_TAG` (Settings → Secrets and variables → Actions), then remove `--dry-run` from the workflow.

## Maccaron
`data/brands.csv` → `maccaron_prefixes` gives the product-URL prefix for each brand on maccaron.in (9 of our brands are sold there). The catalogue reads the public sitemap, then reads each product page's schema.org data: name, price, stock and EAN barcode when one exists. It writes `store=maccaron` rows (`mac-<id>`) with an `ean` column.

The pipeline fetches each watched Maccaron page at most once every 6 hours, about 15 minutes per pass. Skip it with `--no-maccaron`.

Tira and Nykaa both answer scripted requests with 403 (bot protection), so they are deliberately not fetched.

## Data sources
- **Amazon:** the Keepa API. On the first run it backfills about 90 days of price history, so deal detection works from day one.
- **Brand sites:** not scraped. They block automated requests with HTTP 429. Use network feeds or manual prices for these.
