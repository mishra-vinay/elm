# Dry-run trading system (Phase 1)

Dhan live market data → previous-day High/Low break-and-pullback rule → **simulated** trades saved in a database,
with a small dashboard to start and watch runs. Built for your own account, single user, on a small server.

**It never sends orders.** There is no order-placement code anywhere in this package, only market-data reads.

> **Read this first.** The rule was backtested on silver futures, silver options and Bajaj Finance shares and showed
> **no edge** in any of them ([silver](../docs/silver-options-study.md), [Bajaj Finance](../docs/bajajfinance-study.md)).
> This system is how you collect your own live evidence on the instruments you care about. The default settings are the
> values that were tested, not recommendations, and a good-looking week of dry-run results is not proof.

## What is in it

| Piece | File | Notes |
|---|---|---|
| Strategy engine | `dryrun/strategy.py` | Pure logic. The dry run **and** the historical replay use the same class. |
| Risk gate | `dryrun/risk.py` | Independent approval before every entry (stale feed, session window), plus daily-loss and trade-count limits in the engine. Every check is logged. |
| Dhan WebSocket feed | `dryrun/dhan_feed.py` | Binary v2 protocol parser, subscribe, auto-reconnect, token-expiry handling. |
| Dhan REST (data only) | `dryrun/dhan_rest.py` | Daily and intraday history, 85-day chunks, timestamp-format auto-detection. |
| Instrument list | `dryrun/instruments.py` | Downloads Dhan's scrip master (about 25,000 tradable rows kept: indices, stocks, index/stock futures, MCX futures). Search by name, symbol, security id or `SEGMENT:ID`. |
| Previous-day High/Low | `dryrun/levels.py` | Manual entry → stored daily candle → derived from stored intraday candles → fetched from Dhan → ask you. |
| Runner | `dryrun/runner.py` | Feed → candles/ticks → engine → database (writes go through one background thread so the feed never waits on disk). |
| Dashboard + API | `dryrun/app.py`, `dryrun/static/index.html` | Plain HTML/JS, no build step. |
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
so you can try the whole flow at any time. Try: type `BAJFINANCE`, set the Pullback/Target to `0.05`/`0.15` and, when
asked, previous High `960.5` / Low `955`. The engine will break, pull back, enter, hit the target and save everything within a minute.

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

## The rule

Previous-session High **H** and Low **L**, from the database or typed in. Settings are per run:

* **Break** — price trades above **H** (or below **L**), optionally by a buffer.
* **Entry** — after a break, price comes back `pullback %` inside the level (below **H** for a buy, above **L** for a sell). That tick is the signal.
  `0` means "retest of the level". One entry per break; trying again needs a fresh break.
* **Exit** — `target %` from entry (limit: fills at the target price), optional `stop %` (market: fills at the tick that crossed it, less slippage),
  or **square-off** at 15:15 (equities/indices/NSE futures) or 23:15 (MCX).
* **Limits** — one position at a time by default, max trades per day, daily loss limit (% of entry value, summed), entries only inside the session.
* **Costs** — slippage per side and a round-trip cost % are charged, so P&L is net, not gross.

Every break and every entry signal is saved with its time, price, level, whether it was **taken or skipped**, and why
("a position is already open", "max trades per day reached", "risk: stale feed…").

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

## Dashboard

1. **Start a dry run** — search box (name, symbol, security id, or `NSE_FNO:48704`-style), the settings above, and manual High/Low fields that appear if none are found.
2. **Active runs** — price chart with previous High/Low, trigger levels, entry/target/stop lines; break status, open position and unrealised %, trades, net P&L, recent signals; **Stop & square off**.
3. **Finished runs** — status, ran OK, ticks, trades, win %, net P&L, data coverage, and a details view with every signal and trade.
4. **Market data collection** — collect ticks, download history, reload the instrument list, stored-candle summary.
5. **Feed log**, Dhan token box, feed status badge (connected, subscribed count, seconds since last message), writer-queue and database-error counters.

A run is for **one trading day**: it squares off at the session's square-off time and is finalised. If the app restarts mid-run, that run is marked
*interrupted* (stored ticks remain) rather than silently resumed with lost state.

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
