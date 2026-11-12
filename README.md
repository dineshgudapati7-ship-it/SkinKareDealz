# SkinKareDealz

A price tracker that finds *real* skincare deals. It posts them to a Telegram channel automatically and queues WhatsApp-ready text for you to paste into your WhatsApp Channel by hand.

Pure Python 3.11+ standard library. No installs needed, except Scrapling for the opt-in Nykaa catalogue (see Nykaa below).

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
**The checkout price comes from product pages.** Purplle's brand listings often show less than the product page and the cart. On 2026-11-12, 12 of 20 sampled products differed (₹366 listed vs ₹425 in the cart), and no listing field predicts which. So every run also reads up to 600 product pages (`/product/<id>`, allowed by robots.txt), stalest first, each at most once per 20 h, and records them as `source=purplle-page` (`--purplle-pages`, 0 = off). The listing prices are still stored as `source=purplle`, but only for diagnosis: they never count as the current price or as history.

Every `python -m skd.run` also fetches the Purplle brand pages for the brands in your watchlist and records a price for each `pur-…` product. That's about 160 requests at 1 every 2 s, roughly 5 minutes. Skip it with `--no-purplle`.

`track.cmd` runs this in record-only mode (`--dry-run`) and appends the output to `logs/track.log`. Schedule it every 3 hours with Windows Task Scheduler.

## Running on GitHub Actions
`.github/workflows/track.yml` runs the tracker every 3 hours (IST 02:47, 05:47, 08:47 and so on; GitHub's cron often starts 10–30 min late). It replaces the Windows task. Each run takes about 6 minutes, roughly 1,440 of the 2,000 free minutes a month for a private repo. If the watchlist grows a lot, check the usage under Settings → Billing. **Maccaron is not fetched in the cloud.** It returns 403 to GitHub's datacenter IPs, while the same page returns 200 from a home connection.

- **State:** `prices.db` lives on the `data` branch as a single commit, force-pushed after each run. The run fails rather than starting an empty DB, and it refuses to save a DB that has fewer rows than it restored. To get a local copy: `git fetch origin data && git show origin/data:prices.db > data/prices.db`.
- **Manual run:** Actions tab → "price tracking" → Run workflow.
- **Going live:** add the repo secret `TELEGRAM_TOKEN`, add the variables `TELEGRAM_CHAT_ID` and `AMAZON_TAG` (Settings → Secrets and variables → Actions), then remove `--dry-run` from the workflow.

## Maccaron
`data/brands.csv` → `maccaron_prefixes` gives the product-URL prefix for each brand on maccaron.in (9 of our brands are sold there). The catalogue reads the public sitemap, then reads each product page's schema.org data: name, price, stock and EAN barcode when one exists. It writes `store=maccaron` rows (`mac-<id>`) with an `ean` column.

The pipeline fetches each watched Maccaron page at most once every 6 hours, about 15 minutes per pass. Skip it with `--no-maccaron`.

## Nykaa
Nykaa's bot protection (Akamai) blocks plain scripts, robots.txt included. On 2026-11-11 the owner chose to fetch it anyway with [Scrapling](https://github.com/D4Vinci/Scrapling), whose `Fetcher` presents a real Chrome TLS fingerprint. The conditions are **slow and steady**: at most 1 request every 3 s (`nykaa.MIN_DELAY`, never lower), a full stop at the first refusal, and no proxies or retries to get around a block. Doing this can put Nykaa's own affiliate program (NAP) at risk.

```
pip install "scrapling[fetchers]"
python -m skd.catalog --stores nykaa
```
For each brand, the first run searches Nykaa once and stores the brand-page path in `data/brands.csv` → `nykaa_paths`. `-` means no brand page was found; edit the cell by hand to fix it. It then reads the brand pages, 20 listings each, and saves `data/catalog/nykaa_<date>.csv` with prices. It upserts `store=nykaa` rows (`nyk-<id>`) into the watchlist. A size-variant listing shows its default size's price. Other sizes appear as their own rows when Nykaa lists them separately, with `?skuId=` URLs.

**Price tracking:** `python -m skd.run` re-reads the brand pages of every brand that has `nyk-…` rows in the watchlist, and records a price for each one (`source=nykaa`). That's about 180 pages at 1 every 3 s, roughly 15 minutes on a GitHub runner. To save Actions minutes it runs at most every 24 h (`--nykaa-every`). Skip it with `--no-nykaa`. A refusal (any non-200, or a page with no product data) stops Nykaa for that run and keeps the prices already recorded.

## Flipkart and Tira
On 2026-11-12 the owner extended the same Scrapling method to these two stores, with the same rules: at least 3 s between requests, one attempt per request, a full stop at the first refusal, and no proxies.

```
python -m skd.catalog --stores flipkart,tira
```
- **Flipkart** (`fk-<pid>`): searches `/search?q=<brand>&page=N`. robots.txt allows this, but not `sort=` or `facets=`, so neither is used. Only titles that start with the brand name are kept, and a brand stops after 2 pages with no match. That gave 3,202 products from 38 brands. **Many listings come from third-party resellers** (e.g. La Roche-Posay, imported Kiehl's), and some are non-skincare (Patanjali). Check the seller before posting. Tracked by `skd.run` at most every 24 h (`--flipkart-every`, `--no-flipkart`), about 225 pages and roughly 15 minutes.
- **Tira** (`tira-<uid>`), from two sources:
  - **Brand pages** `/brand/<slug>` server-render only the **top 12 products** ("Load more" uses Tira's app-token API, which we don't use). Slugs come from the brand sitemap or are guessed from the name and accepted only if the page's products are that brand; they're stored in `brands.csv` → `tira_slug` (`-` = no brand page).
  - **Product sitemaps** (~65 files, ~27k products) find the rest: URLs that start with a brand's slug are opened one product page each, and the page must confirm the brand. Pages are ~1.4 MB, so the first full catalogue run takes about 2.5 h (~2,700 products for our brands). Later runs skip URLs already in the watchlist. `--no-tira-sitemap` = brand pages only.
  - **Price tracking:** every `skd.run` reads the brand pages (~40 requests), then up to `--tira-pages` product pages (default 150, about 9 min) for the watched Tira products whose price is oldest, recorded as `source=tira-page` (counts as current for 26 h). A product priced in the last 20 h is skipped (`--tira-page-age`), so each page is fetched about once a day at most, and the whole list is refreshed in rotation; a full rotation needs about 2,700 pages, about 2.5 h of fetching. A delisted product is recorded out of stock. Any refusal stops Tira for that run. `--no-tira` skips both.

## Data sources
- **Amazon:** the Keepa API. On the first run it backfills about 90 days of price history, so deal detection works from day one.
- **Brand sites:** not scraped. They block automated requests with HTTP 429. Use network feeds or manual prices for these.

## Price-check bot (`skd/bot.py`)
People DM the bot a product link from Nykaa, Purplle, Flipkart, Tira, Maccaron or Amazon, or type a product name. The bot replies with the current price of **that exact product** (same size and pack) on every store we track, cheapest first. Typed names get up to 5 buttons to pick from.

```
python -m skd.bot                          # run it (long polling; Ctrl+C stops)
python -m skd.bot --ask "<link or name>"   # print the reply offline, no Telegram
```
- **Token:** the same `telegram_token` the channel poster uses. The bot only answers private chats.
- **Prices:** copied from `origin/data:prices.db` into `data/bot_prices.db` every hour (`--refresh-hours`). The tracker's own DB is never touched. Stale prices are hidden, using the same freshness windows as `skd.run` (`FRESH_HOURS`), so a reply never quotes an old price as current.
- **Matching (`skd/match.py`):** the brand, product type, active %, SPF and pack count must be identical, and combos never match. Size has to be equal, with g and ml treated as the same unit. Then the name words are compared. A listing with no size (about 30% of Nykaa rows) is shown as a link only, "size not shown there, so not compared", because those listings carry the default size's price. **Check:** the same SKU has the same printed MRP everywhere, and about 94% of exact matches agree on it (`tests/test_match.py::RealCatalogue`). Most of the rest are reseller or old-stock MRPs.
- **Links:** we use the watchlist's `affiliate_url` when there is one, or the Amazon tag; otherwise the plain store page. The `#ad` disclosure appears only when an affiliate link is actually in the reply. EarnKaro links add "open in your browser", because app purchases aren't tracked.
- **Requests:** every query is logged in `data/bot.db` → `queries`. `found=0` rows are links we don't track yet, so you can see which products to add:
  `sqlite3 data/bot.db "SELECT text, COUNT(*) n FROM queries WHERE found=0 GROUP BY text ORDER BY n DESC"`
- **Short links** (`dl.flipkart.com/s/…`, `amzn.in`, EarnKaro): the bot sends one HEAD request per hop and reads only the redirect target, never the page. Only known store/shortener hosts are followed.
- **Hosting:** it has to run on an always-on machine (it's a loop, not a cron job). Running it on the Windows PC is fine to start.
- **Live prices:** for Purplle rows the bot reads the product page when someone asks (cached 30 min, at most 1 request/s, paused for an hour after a refusal). Otherwise it uses the tracked price. Other stores use tracked prices only.
- **Nykaa sizes:** about 30% of Nykaa listings are multi-size products with no size in the brand listing. `python -m skd.nykaa_sizes` reads each one's product page (Scrapling, ≥3 s, stop at the first refusal) into `data/nykaa_sizes.csv` and fills the watchlist's empty size cells. A later catalogue run never blanks a filled size. Where that hasn't run, the bot works the size out from the MRP if exactly one size elsewhere has the same MRP.
