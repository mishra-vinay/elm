# Dry-run trading system (Phase 1)

Dhan live market data → pluggable strategy engine → **simulated** trades saved in a database, with a small dashboard to watch the feed,
collect market data, run dry runs and backtest. One strategy is installed, **Range thirds** ([study](../docs/range-thirds-study.md): no profitable setting found, so it is for collecting evidence);
others can be added (see [Adding a strategy](#adding-a-strategy)). Built for your own account, single user, on a small server.

**It never sends orders.** There is no order-placement code anywhere in this package, only market-data reads.

> **Read this first.** A good-looking week of dry-run results is not proof of an edge. Judge a strategy on many trades, on data it was not tuned on,
> after realistic costs (the Backtest page shows trade count, t-statistic and first-half / second-half agreement for exactly that reason).

## What is in it

| Piece | File | Notes |
|---|---|---|
| Strategy framework | `dryrun/strategy.py`, `dryrun/strategies/` | The generic trade mechanics (fills, slippage, costs, limits, risk gate) and a registry. The dry run, the replay and the backtest all build the same engine. Installed: `strategies/range_thirds.py`. |
| Risk gate | `dryrun/risk.py` | Independent approval before every entry (stale feed, session window), plus daily-loss and trade-count limits in the engine. Every check is logged. |
| Dhan WebSocket feed | `dryrun/dhan_feed.py` | Binary v2 protocol parser, subscribe, auto-reconnect, token-expiry handling. |
| Dhan REST (data only) | `dryrun/dhan_rest.py` | Daily and intraday history, 85-day chunks, timestamp-format auto-detection. |
| Instrument list | `dryrun/instruments.py` | Downloads Dhan's scrip master (about 25,000 tradable rows kept: indices, stocks, index/stock futures, MCX futures). Search by name, symbol, security id or `SEGMENT:ID`. |
| Previous-day High/Low | `dryrun/levels.py` | Manual entry → stored daily candle → derived from stored intraday candles → fetched from Dhan → ask you. |
| Runner | `dryrun/runner.py` | Feed → candles/ticks → engine → database (writes go through one background thread so the feed never waits on disk). |
| Dashboard + API | `dryrun/app.py`, `dryrun/static/*.html` | Dry runs, Live ticks, Backtest, Market data and Settings pages; plain HTML/JS, no build step. |
| History download / replay | `dryrun/history.py`, `dryrun/replay.py` | Fill the database from Dhan; run the live engine over stored candles. |
| Optional login | `dryrun/auth.py` | None, shared password, or e-mail code via Brevo (scaffolded, off by default). |

## Quick start (no Dhan account needed)

```bash
cd trading_dryrun
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # FEED_MODE=sim is the default
python -m dryrun                # http://127.0.0.1:8000
python -m pytest tests -q       # 50 tests
```

In simulated mode prices are a seeded random walk, market hours are ignored and the page is labelled **SIMULATED FEED**,
so you can try the feed, Live ticks and Market data pages at any time. (Starting a dry run needs a strategy; see below.) The engine will break, pull back, enter, hit the target and save everything within a minute.

## Using real Dhan data

1. **Dhan prerequisites.** A Dhan account, the **Data API** plan (the Trading API is free; data is paid; check the price on
   Dhan's page, your roadmap assumed ₹499 + GST a month), and an access token from *web.dhan.co → My Profile → Access DhanHQ APIs*.
   Tokens last **24 hours**; paste a fresh one into the dashboard's *Dhan access token* box each morning (kept in memory only)
   or update `.env` and restart. Static-IP whitelisting is required only for order APIs, so it does not affect this phase.
2. Put `FEED_MODE=dhan`, `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in `.env`.
3. **Run the first-day check on a trading day:** `python scripts/check_dhan.py`. It prints your data-plan status, whether
   history decodes to the right dates and 09:15–15:30 times, and a few live ticks with the raw exchange time beside your clock.
   I built the feed and history code from Dhan's documentation and tested it against a local fake server, **not against Dhan itself**
   (I could not reach it from my build environment), so this script is the step that proves it on your account.
4. Start the app, open the dashboard, enter an instrument, click **Start dry run**.

Dhan limits: 5 WebSocket connections per user, 5,000 instruments per connection, 100 per subscription message; the server
pings every 10 s and drops a silent client after 40 s (handled). Error codes 806/807/808/809/810 (no data plan, token expired or invalid)
stop the retry loop and show *feed error* on the dashboard instead of hammering the server.

> **About "tick-by-tick".** This is the broker's WebSocket feed (last-traded-price updates in Quote mode, which add volume and day
> high/low), not the exchange's complete order-book feed, which is a separate paid product. It is plenty for a High/Low rule, but
> measure its latency and gaps during the dry run (the dashboard shows seconds since the last message, and every run records data coverage).

## Adding a strategy

Two strategies ship (`range_thirds`, `prev_day_zone30`), plus a hidden research version of the first. A strategy is a small class that says *when to enter and where the stop and target are*; everything else (one open position,
slippage, a limit target and market stop, round-trip costs, the daily loss limit, trade-count limits, the risk gate, square-off, the run summary, signals
and trades saved to the database, the replay and the online backtest) is shared in `StrategyEngine`.

1. Create `dryrun/strategies/my_strategy.py`: subclass `StrategyEngine`, implement `evaluate(ts, price)` (it is called on every tick, after any open position
   has been managed; return a list of `Event`s and open a trade with `self.signal(side, ts, price, level, reason, exits)`), optionally `levels()` (chart lines),
   `status_text()` and `describe_event()`. Keep its numbers in a `RULES` dict at the top of the file (there is no settings form: to change a number, edit the file). Register it with
   `register(StrategySpec(name, label, engine, blurb="one plain-English paragraph", preview=..., describe=...))`.
2. Import it in `dryrun/strategies/__init__.py`.
3. Restart the service. The strategy appears in the Dry runs and Backtest pages with its `blurb` under the dropdown, and the calculation panel comes from `preview`. (`hidden=True` keeps a research strategy out of the dropdowns.)

`dryrun/strategies/range_thirds.py` is a complete, short example (RULES, a geometry function, one engine class), and `tests/demo_strategy.py` a smaller one used only by the tests. Replay from the command line:
`python -m dryrun.replay --symbol BAJFINANCE --strategy NAME --option KEY=VALUE --interval 1m --from ... --to ...`.

Every signal (taken or skipped, and why: "a position is already open", "max trades per day reached", "risk: stale feed…") is saved with its time, price and level.

## Monitoring the feed (Live ticks page) and tick hygiene

* **Live ticks page (`/ticks`)**: per instrument, packets per second, silence time, distinct vs duplicate vs out-of-order vs bad packets,
  the shortest exchange-to-arrival delay, and how many microseconds *our* code spends per packet. The feed panel shows connection state,
  reconnects, the websocket round trip to Dhan and the database writer queue. All counters are in memory, so the page is nearly free.
* **Packet filtering** (`runner._process_packet`): identical repeats and packets older than one already processed are skipped (the quote feed repeats
  snapshots on every depth change, about 90% of packets in a live check). A jump of more than 20% is held until the next *distinct* tick: if it agrees,
  the move was real and both are used; if the price falls back, the jump is dropped as a bad print. A new trading day forgets yesterday's price.
* **Stale-feed check is per instrument** and counts every packet, even skipped duplicates, as proof the feed is alive.
* **Restart safety**: a run that was live when the process died is marked `interrupted`, and any open simulated trade is closed at the run's last stored price
  with exit reason `interrupted` (flagged, P&L from that price).
* **Keep-alive**: the websocket pings Dhan every 15 s and reconnects if there is no reply within 20 s (`DhanFeed(ping_interval, ping_timeout)`).
* **Stored ticks** (Settings page): only distinct ticks are stored. Raw ticks are deleted after *N* days (default 3, saved in the database, overrides
  `TICK_RETENTION_DAYS`). Download any day as CSV first; delete a day or everything older than *N* days from the same table.

## Position sizing: the maximum loss decides the quantity

Nobody enters a quantity. Both strategies carry `RULES["max_loss_rs"]` (Rs 10,000) and size every trade: `units = floor(max loss / (stop distance + exit slippage + round-trip cost) / lot size) x lot size`
(`dryrun/sizing.py`; lot sizes of the MCX contracts in use are in `LOT_SIZES`, other instruments are sized in single units). If even one lot would lose more than the maximum, the trade is not taken.
The trade list shows the quantity chosen. A stop-out then costs at most the maximum loss when the stop fills near its level; a gap far through the stop can lose more. Set `max_loss_rs` to `None`
in a strategy file to use a run's fixed quantity instead.

## Range thirds (installed strategy)

All of it is in `dryrun/strategies/range_thirds.py` (the numbers in `RULES`, the rule in `RangeThirdsEngine`); nothing is configured on a page. With D = previous High - Low, third = D/3 and the
"0.8" = 0.8% of the previous High (BUY) or Low (SELL): BUY entry = High - (third - 0.8), stop = entry - 0.8, target = entry + third + (third - 0.8) (= High + third); SELL entry = Low + (third - 0.8),
stop = entry + 0.8, target = entry - third - (third - 0.8). Fixed prices from yesterday's High/Low; the risk is exactly the 0.8. Approach entry (before the level breaks). Around it: no entry in the first
5 minutes (candle), none from 18:00 (an armed side is cancelled then), one trade a day in total (counted when the entry fills; the other side is cancelled), a quantity worked out from the Rs 10,000 maximum loss, the day-open filter (no trade at all unless the day opens
between the previous Low and High), and the runner's session square-off (MCX 23:20, NSE 15:15). Nothing is carried overnight. Same rule as `tradingview/range_thirds_strategy.pine`.

`range_thirds_research.py` (hidden from the dropdowns) is the earlier all-options version: the D/6, D/3, D/3 rule, after-break entry, windows as settings. The study in `../docs/range-thirds-study.md`,
`python -m dryrun.strategies.range_thirds_backtest` (a sweep and `--verify` tool) and `scripts/verify_backtest.py` use it (strategy name `range_thirds_research`).

## Previous-day 30% opening zones (installed strategy `prev_day_zone30`)

The session's **opening price** picks the side, once a day: open in the upper 30% of yesterday's range (PDH - 0.3 D to PDH) = BUY; open in the lower 30%
(PDL to PDL + 0.3 D) = SELL; an open outside both zones (or a gap beyond PDH/PDL) = no trade. One trade a day, win or lose.
* **Entry timing:** "no entry in the first candle" means the entry cannot be at the opening price itself. It is a market order at the first price 5 minutes (`RULES["skip_minutes"]`) after the
  session open, i.e. at the open of the second candle (= the close of the first). A run started later than `RULES["grace_minutes"]` (30) after that moment skips the day instead of entering late.
  The opening price is the real session open: a run started mid-session reads it from the day's first stored candle or Dhan's 1-minute candles.
* **Stop and target:** the stop is the middle price of yesterday's High and Low, (PDH + PDL) / 2 (always on the losing side: a BUY enters above it, a SELL below it), and the target is 1.3 times the
  stop distance from the actual entry price (`RULES["target_x_stop"]` = 1.3). No new entry from 18:00 (`RULES["last_entry"]`); the runner's normal session square-off applies (MCX 23:20, NSE 15:15),
  nothing is carried overnight. The quantity comes from the Rs 10,000 maximum loss.
* Pine version for TradingView: `tradingview/opening_zone30_strategy.pine`.

## 30% opening buy at center (installed strategy `prev_day_zone30_buy_center`)

Reads the day like the 30% opening zones strategy but buys at the centre of yesterday's range instead of at the open, and only buys. The day's real open must be in the top 30% of yesterday's range
(PDH - 0.3 D to PDH); any other open is no trade. From the end of the first candle a BUY order rests at the centre, (PDH + PDL) / 2, and fills when price comes down to it (at once if price is already
below it). Stop = PDL; target = entry + 1.4 x the stop distance (risk 1 : reward 1.4; from the centre that is the centre + 0.7 D). If price never comes down, nothing is bought; the order is cancelled
from 18:00. One trade a day; the quantity comes from the Rs 10,000 maximum loss; the session square-off applies. Everything is in `dryrun/strategies/prev_day_zone30_buy_center.py` (`RULES` and the
engine). There is no Pine version.

## Market shift strategy (installed strategy `market_shift`)

BUY only, on 5-minute candles; it does not use yesterday's High and Low. The rules and numbers are in `dryrun/strategies/market_shift.py` (`RULES` and the engine); nothing is configured on a page.

* **Day open level:** the open of the first 5-minute candle. **Market shift:** a completed candle closes above it. **Confirmation sequence:** the following candles while each has a higher high and a higher low (no minimum or maximum length).
  The last candle of the sequence is the **reference candle**: its high is the breakout level, its close the reference entry price.
* A candle that closes below the day open level cancels the setup (a waiting order too); the next close above starts a new, independent setup.
* After price breaks above the reference high, a BUY waits at the reference close. If price comes back, the trade is taken; a lower fill (a gap through the price) is taken at that lower price and the difference is added to the target.
  If the target is reached first there is no trade.
* **Stop:** walk back from the candle before the shift candle while each earlier low is not higher than the lowest so far; the stop is the lowest low found (also compared with the shift and sequence candles). **Target:** reference entry + (reference entry - stop).
* Several setups a day, one position at a time (the per-day trade limit is not applied to it). No quantity rule: the run's fixed quantity is used, so a backtest reads in points. Backtest on 1-minute or 5-minute candles.
* **Market shift SELL** (`market_shift_sell`, `dryrun/strategies/market_shift_sell.py`) is the exact mirror: a candle closing BELOW the day open level, a falling sequence, the lowest candle as reference (its low is the breakout level), a SELL waiting at the reference close,
  stop = the highest high of the backward walk, a higher fill subtracts the difference from the target. Both sides share one engine (`MarketShiftEngine`, comparisons written with a sign), so they cannot drift apart.
* The **Strategies** page (menu) describes every installed strategy step by step; each strategy supplies its text as `GUIDE` in its own file.

## Database (SQLite now, PostgreSQL later)

`DATABASE_URL=sqlite:///data/dryrun.db` by default; the same models run on PostgreSQL (`postgresql+psycopg://…`).

| Table | Contents |
|---|---|
| `instruments` | The scrip-master rows (segment, security id, symbol, name, lot size, expiry). |
| `candles` | Daily and 1/5/15/25/60-minute candles, from Dhan history **and** from your own live ticks (1-minute, `source=live`). |
| `ticks` | Every accepted live tick: time, price, exchange time (raw), cumulative volume, day high/low. Deleted after `TICK_RETENTION_DAYS` (30). |
| `daily_levels` | The High/Low used for each trading date and where it came from. |
| `runs` | One row per dry run: parameters, levels, status, ticks received, first/last tick, feed disconnects, **`ran_ok`**, error, and a JSON `summary` with performance and data coverage. |
| `signals` | Breaks and entry signals, taken or skipped, with reasons. |
| `trades` | Simulated trades: entry, target, stop, exit, reason, gross, cost, net, net %. |
| `api_logs`, `risk_logs` | Feed events and reconnects; every risk check and rejection. |

*Did the run work?* `ran_ok` is true when ticks arrived and nothing failed; `summary.data_coverage` is the share of minutes in the run
that had at least one tick, and `feed_disconnects` counts reconnects. *How did it perform?* `summary` holds trades, wins, win rate, targets/stops/square-offs,
gross P&L, costs and net P&L. Download all trades from the dashboard (**Download trades (CSV)**).

**Saving history for later research.** The **Market data collection** panel can (a) keep recording live ticks and 1-minute candles for any instrument
without running the strategy and (b) download history from Dhan: daily candles from inception and intraday candles for up to about 5 years
(Dhan's stated limit; 90 days per request, handled in chunks). Or from a shell: `python -m dryrun.history --symbol BAJFINANCE --days 1825`.
Disk planning, as an estimate only: a busy instrument produces on the order of 5–30 MB of ticks a day; 1-minute candles are about 375 rows a day per equity.

**Backtest page layout.** Data set, strategy (with a one-paragraph description) and Run are all that is visible. One collapsed *Settings* panel holds the period, slippage and cost and the
starting capital. There is no quantity box: each strategy works out the quantity of every trade from its maximum loss (`RULES["max_loss_rs"]`, Rs 10,000), see `dryrun/sizing.py`. The page's script is `static/backtest.js`. Sessions with far fewer candles than a full exchange session are always skipped (the result
lists how many); there is no control for it.
**Backtest page report.** Like TradingView's Strategy Tester: total P&L in rupees and % of the starting capital (set quantity and capital on the form), max drawdown,
profitable trades, profit factor, an equity + drawdown chart, and a **day chart** (candles, the strategy's lines, entry/exit markers) for any tested day; click a trade to open its day.
**Fills:** a candle only gives four prices (open/low/high/close), so the replay plays an extra tick at every strategy line a leg of the path crosses
(the levels, plus the open position's stop and target): a pending order fills at the level the price crossed, not at the candle's extreme, and a stop exits at the stop
(plus slippage). A gap between candles stays a gap. This matters most on 5- and 15-minute data.

**Checking the backtest.** `python scripts/verify_backtest.py --db sqlite:///data/dryrun.db --symbol BAJFINANCE --segment NSE_EQ` (or `--instrument-id N [--from DATE]`) re-implements the
current Range thirds rule with a different mechanism (a dense tick path per candle instead of the engine's inserted level ticks) and compares it trade by trade with the Backtest page's engine,
and checks the candles (OHLC sanity, gaps, flat candles), every trade's invariants, the daily levels, look-ahead (cutting the data off earlier must not change earlier trades), the summary
statistics, and how much the result depends on the unknown high/low order inside a candle. Run it against a copy of the database on a quiet machine. Last run (19 Oct 2026): all checks pass on
Bajaj Finance, Reliance and crude oil mini 1-minute candles. A backtest's complete-session test compares each day with a full exchange session (375 bars NSE, 870 MCX at 1 minute), not with the data's median.

**Same engine for backtests.** `python -m dryrun.replay --symbol BAJFINANCE --interval 1m --from 2026-01-01 --to 2026-10-09` runs the live
engine over stored candles (open→low→high→close inside each bar for up bars, open→high→low→close for down bars, so use 1-minute data).

**Simulated data is kept out of real analysis.** Candles saved while `FEED_MODE=sim` are tagged `source=sim` and are never used as previous-day levels. Still, before switching to Dhan for good, start from a clean database (`sudo systemctl stop dryrun && rm -f /opt/dryrun/data/dryrun.db* && sudo systemctl start dryrun`), so test runs don't clutter your history.

## Market data collection: what each button does

* **Download history** — pulls candles from Dhan into `candles` (daily from inception; 1/5/15/25/60-minute for the number of days you enter, up to about 5 years).
  It is **incremental**: a second click fetches only what is newer than what you already have (the last stored day is fetched again to complete it), so it is quick and never creates duplicates.
  Dhan's official candles also replace any candle the app built itself from live ticks. The tick box *keep updated after close* adds the instrument to the list that is refreshed automatically every weekday after 16:05 IST;
  **Update all now** does the same on demand.
* **Collect live ticks / Stop collecting** — subscribes the instrument to the live feed and saves every price update (`ticks`) and a 1-minute candle (`source=live`) **without running the strategy**.
  The choice is remembered, so collection resumes after a restart. Starting a dry run does the same for its own instrument automatically.
* **Reload instrument list** — only refreshes the list of names and ids (stocks, indices, futures; no options). It never touches stored candles.
* **Delete** (per row in *Stored candles*) — removes that instrument's candles for that interval. Runs, signals and trades are never deleted by it.
* **Previous-day High/Low** next morning: manual entry → the stored daily candle of the last trading day → if that candle is older than the previous weekday, a fresh fetch from Dhan → levels derived from stored real intraday candles. The run's timeline shows which source and which date were used.

## Dashboard (three pages, activity log on each)

* **Dry runs** (`/`) — start a run (search box, settings, previous-day High/Low filled in automatically, optional run name), see **Active runs** (summary table plus a card per run with chart, "what is happening now", timeline and Stop) and **Finished runs** (status, ran OK, settings, trades, win %, net P&L, coverage, details).
  You can run **several at once**, up to 20: different instruments, or the *same* instrument with different settings and names to compare them. **Stop all** squares everything off.
* **Market data** (`/market-data`) — collect live ticks, download history (incremental), keep instruments updated after the close, reload the instrument list, see what is recorded and stored, delete stored candles.
* **Settings** (`/settings`) — Dhan connection (mode, masked client id, token status and expiry read from the token itself), **Test connection and data plan**, **Update token** (optionally saved to the server's `.env` so it survives a restart), read-only configuration and session times.
* **Activity log** (bottom of every page) — runs, market feed, data jobs, risk rejections, app events and errors, newest first, with filters.

Header on every page: feed status, number of active runs, clock (IST), and a CSV download of all simulated trades.

A run is for **one trading day**: it squares off at the session's square-off time and is finalised. A run started when the market is closed is scheduled for the next session. If the app restarts mid-run, that run is marked *interrupted* (stored ticks remain) rather than silently resumed with lost state.
Real-order live trading is **not part of this phase**; the page says so and nothing in the app can place an order.

## Speed

`python scripts/benchmark.py` builds a throw-away database (600k ticks, about 470k one-minute candles) and times what each page asks for plus the live-tick path. Typical results on a 2026 laptop-class CPU: every page call 1-15 ms (the Market-data summary, the heaviest query, is cached for 20 s),
about 70,000 ticks per second through the strategy and candles for 5 active runs, and about 40,000 rows per second into the database; real feeds deliver far less (tens of ticks per second per busy instrument). Peak memory about 150 MB, so a 1 GB instance is enough.
Other measures: responses are gzipped, pages stop polling while the browser tab is hidden, the instrument list (35 MB) is downloaded at most once per 20 hours at start-up, and history queries load run instruments in a single query.

## Login

* **Default: none.** The app then listens on `127.0.0.1` only and **refuses to start** on any other address without a login. Reach it through an SSH tunnel.
* **Password:** set `DASHBOARD_PASSWORD` (and `SECRET_KEY`). Five wrong tries lock that client out for 5 minutes. Cookies are `HttpOnly`, `SameSite=Strict`, and `Secure` over HTTPS.
  A password sent over plain HTTP can be read on the network; use the SSH tunnel or put HTTPS in front.
* **E-mail code (later):** `AUTH_MODE=email_otp` with `BREVO_API_KEY`, `BREVO_SENDER_EMAIL`, `ALLOWED_EMAILS`. Codes are 6 digits, single-use, expire in 5 minutes, locked after 5 wrong tries;
  unknown addresses get the same response as allowed ones. Written and unit-tested against a fake HTTP client; **not yet tried against Brevo**.

## Deploying on AWS (Mumbai, `ap-south-1`)

This app needs very little: one process, SQLite, a handful of instruments. A 1 GB instance (`t3.micro`/`t2.micro`) is enough to start.

1. Create an AWS account, choose region **Asia Pacific (Mumbai) ap-south-1**, and check *Billing → Free Tier* for what your account actually includes.
2. Launch an **Ubuntu 24.04** instance of a Free-Tier-eligible type (`t3.micro` or `t2.micro`, whichever the console marks eligible), 30 GB gp3 storage.
3. Security group: allow **SSH (22) from your own IP only**. Do **not** open port 8000.
4. Copy and set up:
   ```bash
   scp -i key.pem -r trading_dryrun ubuntu@<public-ip>:~/
   ssh -i key.pem ubuntu@<public-ip> 'bash ~/trading_dryrun/deploy/aws_ec2_setup.sh'
   ssh -i key.pem ubuntu@<public-ip>      # then:  nano /opt/dryrun/.env   (FEED_MODE=dhan, credentials)
   sudo systemctl start dryrun && journalctl -u dryrun -f
   ```
5. After a code update (`git pull` in the checkout): `bash trading_dryrun/deploy/update.sh` copies it over, restarts, and prints the log if it fails to start. If the service will not start, `sudo journalctl -u dryrun -n 30 --no-pager` shows why.
6. From your own computer: `ssh -i key.pem -L 8000:localhost:8000 ubuntu@<public-ip>` and open <http://localhost:8000>.
7. A daily SQLite backup runs at 16:30 on weekdays (`/opt/dryrun/data/backups`, 14 days kept). Copy it off the machine too (S3 or your computer).

Docker alternative: `docker compose -f deploy/docker-compose.yml up -d --build` (needs `DASHBOARD_PASSWORD` in `.env`; the port is published on the host's loopback only).

**Costs, honestly.** AWS changed its free offer for accounts created from 15 July 2025: new accounts get a credit-based Free plan rather than the older 12-month 750-hour allowance,
and a public IPv4 address has been billed (about US$0.005/hour, roughly ₹300/month) since February 2024. I could only confirm these from third-party summaries
([Infratally](https://infratally.com/articles/aws-free-tier-2026/), [AWS pricing page](https://aws.amazon.com/ec2/pricing/on-demand/)), so treat the Billing console as the source of truth. As you asked, if something is not free, use the paid
service: expect a few hundred rupees a month on AWS after credits, plus the **Dhan Data API fee, which is not free**. Set an AWS budget alert before leaving anything running.
Do not attach a NAT gateway (not free-tier and a common surprise bill).

## Security notes

* Credentials live only in `.env` (mode 600, git-ignored) or process memory. Never put them in code. For anything beyond this phase use AWS Secrets Manager.
* The token endpoint and every API route require login when a login is enabled; with none, the app only listens on loopback.
* No inbound ports other than SSH, restricted to your IP.

## Known limits (be aware before trusting a result)

* **Not proven against live Dhan yet.** The packet layout, request codes and endpoints come from Dhan's docs; the first real session is the test (`scripts/check_dhan.py`).
  The docs do not state the epoch base of the exchange timestamps, so the strategy and candles use **your server's clock** (kept in IST), not the exchange time. Keep the server clock accurate (Ubuntu's `systemd-timesyncd` does).
* The docs also leave the Market Status packet undocumented (ignored here) and don't mention rate limits for history calls (the client pauses 0.25 s between calls).
* A 24-hour token means a daily manual step until you automate renewal (Dhan offers a TOTP-based token endpoint).
* Dry-run fills are simulated at the signal tick plus slippage; real fills would be worse in fast markets and the queue position is unknown.
* Option contracts are not loaded (futures, stocks and indices only); lot sizes shown are Dhan's `LOT_SIZE` column, and for some MCX contracts that column may not equal the trading unit, so check before sizing.
* Exchange holidays are not hard-coded: on a holiday a real-feed run simply receives no ticks and is finalised with `ran_ok = false`.
* One process, one feed connection, no failover between brokers (deliberately; add it only after order reconciliation exists).

## Not in this phase (by design)

Order placement, broker failover, PostgreSQL (supported by `DATABASE_URL`, untested here), React front end, WhatsApp/e-mail alerts, multi-user accounts. The roadmap order in your plan stands: dry run → monitoring → paper trading → controlled live.

## Market-depth recorder (research, separate from the dry-run service)

`scripts/depth_recorder.py` stores every Dhan Full-packet update for one instrument (last price, volume, total buy/sell
quantity, OI and the 5 bid + 5 offer levels with quantity and order count) in `data/depth.db`, plus the raw 162-byte packet.
Table `depth` holds the rows, `events` the connects/errors, and the view `depth_view` adds the per-update volume
change, 5-level bid/offer quantity, imbalance and spread. Defaults to NATGASMINI Nov futures (`MCX_COMM:574320`, the SecurityId from Dhan's scrip master, not the app's row id in the
dashboard's instrument search).

    python3 -m venv ~/depth-venv && ~/depth-venv/bin/pip install websockets      # once
    ~/depth-venv/bin/python scripts/depth_recorder.py                            # 09:00-23:35 IST, needs DHAN_* in .env
    ~/depth-venv/bin/python scripts/depth_recorder.py --summary                  # what has been recorded
