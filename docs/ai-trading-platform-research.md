# AI-Powered Automated Trading Platform for NSE/BSE — Research & Recommendation

*Prepared 8 Oct 2026. Scope: self-operated automated trading of equities, futures and options in India via broker APIs.*

**How to read the evidence tags**

| Tag | Meaning |
|---|---|
| **[V]** | Found in web sources during this research (mostly broker/vendor/news pages — **not** primary SEBI/NSE circulars, which I did not retrieve). Re-check before relying on it. |
| **[?]** | Sources conflict, or only one weak source. Treat as unconfirmed. |
| **[E]** | My estimate or engineering judgement. Not sourced. |

> **This is not investment, tax or legal advice, and no strategy here is guaranteed to be profitable.** Win rates and R:R figures in §3 are *typical profiles*, not measured Indian-market results — no trustworthy public dataset exists for most of these strategies. Every number must be re-derived on your own data after costs.

---

## 1. Executive Summary

### 1.1 Recommended architecture
A **modular monolith in Python**, event-driven internally, deployed as a few containers on one hardened VM in Mumbai. Latency-insensitive by design (100 ms–1 s decisions); we are not competing with colocated HFT.

```
 Broker WebSocket ─► Market-Data Ingestor ─► Redis Streams ─► Feature/Indicator Engine
 (ticks, depth, OI)        │                                          │
                           ▼                                          ▼
                    TimescaleDB (ticks,                 Regime Classifier ─► Strategy Selector
                    candles, chain snapshots)                                      │
 News/Events ─► NLP/Event Engine ─► risk-off flags ────────────────────────────────►│
                                                                                    ▼
                                    Strategy Engine (rules)  ─► Signal ─► AI Confidence Scorer
                                                                                    │ (accept / reject / resize)
                                                                                    ▼
                                         ┌────────────  RISK ENGINE (deterministic, can veto anything) ◄── kill switch
                                         ▼
                                  Order Mgmt System ─► Broker Adapter (Dhan | Zerodha | Sim) ─► Exchange
                                         │                     ▲
                                         ▼                     │ order/position postbacks
                                  Position Manager (SL / trail / partials / time stops)
                                         │
              Postgres (orders, trades, P&L) · Prometheus/Grafana · Telegram/Email · Dashboard
```

Five design rules that matter more than any model:
1. **AI advises; deterministic code decides.** Models output probabilities and flags. The Risk Engine is plain, tested code with veto power over every order, and the AI has no path to the broker.
2. **Every position has a broker-resident protective stop** placed immediately on fill, so a crashed server doesn't leave naked exposure.
3. **Same strategy code runs in backtest, paper and live** — only the broker adapter changes (`SimBroker` vs `LiveBroker`).
4. **Broker-agnostic adapter interface** from day one (switching or running two brokers is a config change).
5. **Capital protection before returns**: hard limits, staged rollout, automatic de-risking.

### 1.2 Recommended broker API
**Primary: Dhan. Secondary/failover: Zerodha Kite Connect.** Decision gate: a 2-week measured bake-off in paper mode (latency, rejects, WebSocket drops, 429s).
- Dhan: trading API free; Data API ₹499 + tax/month **[V]** and includes real-time WebSocket, historical intraday (up to 5 yrs, active contracts only), 20/200-level depth, **option-chain** and **expired-option data** **[V]** — the last is rare and valuable for backtesting options.
- Zerodha: orders free, ₹500/month for data (live + historical) **[V]**; the most mature ecosystem and documentation **[E]**. If you weigh track record over features, flip the order — the adapter layer makes this cheap.
- I found **no independent, current latency/reliability benchmark**; AlgoTest publishes a "Broker Speedtest" worth consulting, but run your own.

### 1.3 Recommended cloud
**AWS Mumbai (`ap-south-1`)**, DR in Hyderabad (`ap-south-2`) later. Graviton (ARM) instances, Elastic IP as the whitelisted static IP, Docker Compose (no Kubernetes). Oracle Always-Free ARM (4 OCPU / 24 GB) is fine for dev/paper but has capacity and no-SLA risk **[V]** — not for live capital. SEBI/NSE rules also require retail-algo hosting on Indian servers **[V]**.

### 1.4 Recommended AI components
In order of value-for-risk: (1) **market-regime classifier** (HMM/GMM + gradient boosting) → strategy gating; (2) **meta-labelling confidence scorer** (LightGBM, calibrated) that accepts/rejects/resizes signals from rule-based strategies; (3) **volatility forecaster** (HAR-RV/GARCH) for option buy-vs-sell decisions; (4) **news/event engine** (FinBERT baseline + LLM structured extraction) used as a **risk-off veto**, not a signal generator. Reinforcement learning and pattern-CNNs: research sandbox only.

### 1.5 Recommended trading strategies (staged)
- **Phase 1–2 (automate first):** ORB and VWAP-pullback on index futures/ATM options *via underlying signal*; EMA/SuperTrend trend filter; swing momentum + sector rotation in cash equities (lowest cost drag).
- **Phase 2–3:** *Defined-risk* option income only — credit spreads and iron condors with regime and event filters.
- **Research only (not for live capital in this roadmap):** gamma scalping, naked short straddles/strangles, RL.

### 1.6 Risk management framework
Five layers — pre-trade checks → per-trade stops → portfolio limits → system-health gates → kill switch — with default limits of **0.5–1 % risk/trade, 2 % daily loss halt, 5 % weekly, 10 % drawdown de-risk, 15 % hard stop + human review** (§7). Staged capital rollout: paper → 1 lot → 25 % → 50 % → 100 %.

### 1.7 Development roadmap
| Phase | Weeks | Outcome |
|---|---|---|
| 1 — Foundation & paper trading | 1–10 | Data capture, broker adapter, OMS, risk engine, simulator, 2 rule-based strategies on paper |
| 2 — Small live + AI gating | 11–24 | Live with 1 lot, regime classifier + confidence scorer in shadow then active, alerts/dashboard, 4–5 strategies |
| 3 — Scale & options structures | 25–40 | Defined-risk option strategies, vol forecasting, DR/HA, analytics, strategy lifecycle automation |

### 1.8 Estimated infrastructure cost (monthly, ₹88/USD assumed, ex-GST) **[E]**
| Tier | Approx. monthly | Notes |
|---|---|---|
| Minimum | **₹1,500–3,500** | 1 small ARM VM (or Oracle free tier for paper), broker data ₹500, free tooling |
| Recommended | **₹8,000–17,000** (typically ~₹10–12k) | t4g.large-class VM, 200 GB disk, news/LLM budget, optional tick vendor |
| Production | **₹55,000–95,000** | 2 VMs across AZs, managed/HA DB, vendor tick data, DR, monitoring |

### 1.9 Estimated development effort **[E]**
~**14–20 person-months** end to end (Phase 1 ≈ 3–4, Phase 2 ≈ 5–7, Phase 3 ≈ 6–9). A solo senior quant-engineer: ~9–12 months to Phase 2 complete. Two engineers (one platform, one quant/ML): ~5–6 months to a supervised live system. Paper-trading soak time is *calendar* time, not effort — it cannot be compressed.

### 1.10 Key risks and limitations
1. **The base rate is brutal:** SEBI found ~91 % of individual equity-derivatives traders lost money in FY25 — net losses ≈ ₹1.06 lakh crore **[V]**. Automation removes emotion; it does not create edge.
2. **Costs:** STT on futures rose to 0.05 % and on option sales to 0.15 % of premium from 1 Apr 2026 **[V]**; flat per-order brokerage multiplies on multi-leg structures.
3. **Regulation moves:** static-IP whitelisting, order tagging, daily forced logout, 10 OPS threshold since 1 Apr 2026 **[V]**. Running this for anyone *other than yourself/family* triggers algo-provider empanelment and possibly RA/RIA obligations **[V]** — get legal advice before offering it to others.
4. **Overfitting / model risk**, **operational risk** (API outages, auth expiry, rate limits), **tail events** (gaps, circuits, expiry-day volatility).

---

## 2. Reality Check: Evidence, Costs, Regulation

### 2.1 What the data says about retail derivatives outcomes
- FY25: ~91 % of individual F&O traders lost money; aggregate net loss ≈ ₹1,05,603 crore (up from ₹74,812 crore in FY24); average loss ≈ ₹1.1 lakh per trader; sample ≈ 96 lakh traders at the top 13 brokers **[V]** ([Business Standard](https://www.business-standard.com/amp/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html), [Value Research](https://www.valueresearchonline.com/stories/225398/average-trader-lost-rs-1-1-lakh-fo-fy25/)).
- A later SEBI analysis (reported) found ~91 % of first-year participants net losers, rising to ~94–96 % in later years; only ~0.5 % were profitable in *every* year FY22–FY26 **[V/?]** — a single secondary summary, verify against SEBI.
- FY26 reported aggregate retail equity-F&O losses ≈ ₹91,685 crore but a *higher* average loss per person (~₹1.2 lakh) **[?]**.

**Implication:** the platform's job is to (a) enforce discipline, (b) test ideas rigorously, and (c) *fail small*. Design the project around surviving a no-edge outcome, then capturing edge if it exists.

### 2.2 Transaction-cost drag (illustrative, ₹100 Nifty option, 1 lot = 65) **[E]**
| Item | ≈ ₹ |
|---|---|
| Brokerage (₹20 × 2 orders) | 40 |
| STT (0.15 % of ₹6,500 premium on sell, from Apr 2026 **[V]**) | 10 |
| Exchange charges (~0.035 % × ₹13,000 turnover) | 5 |
| GST on brokerage + exchange charges | 8 |
| SEBI/stamp | 1 |
| **Round trip (before slippage)** | **≈ 64 (~1 % of premium)** |

Add slippage (0.5–1 ₹/side on market-ish exits ≈ ₹30–65/side) and friction is ~2–3 % of premium — ~10 % of a 25 %-premium stop. An iron condor open+close is 8 orders ≈ ₹160 brokerage alone. **Backtests without a realistic cost/slippage model are fiction.**

### 2.3 Regulatory baseline (SEBI/NSE retail-algo framework) — **[V]**, verify on sebi.gov.in / nseindia.com
| Requirement | Detail |
|---|---|
| Effective | Full framework in force from **1 Apr 2026** (after extensions from Aug/Oct 2025) |
| Static IP | Mandatory for API users; registered with the broker; changeable ~once/week; optional secondary IP; sharing only with family. Cloud/VPS IPs are acceptable, home dynamic IPs are not |
| Threshold | ≤ **10 orders/sec/exchange**: no individual strategy registration, but orders are tagged as algo (generic Algo ID). > 10 OPS: register via broker. Vendors/platforms face stricter registration/empanelment |
| Sessions | OAuth + 2FA; all API sessions forcibly logged out daily → **daily re-authentication must be designed in** |
| Orders | Market orders need **market protection** (non-zero) |
| Audit | Logs retained 5 years; every order traceable to key/IP/user |
| Hosting | Retail algos on Indian servers |
| Consequence | Non-compliant API orders can be blocked; API access terminated; penalties |

**Design response:** cap internal order rate at ~5 OPS (well under 10), tag/log every order with a unique client tag, retain all logs ≥ 5 yrs, add a pre-market auth checklist (alert if not authenticated by 08:45 IST), and use a registered static IP (AWS Elastic IP).

**Personal use vs. offering to others:** the above assumes *you trade your own/family accounts*. Managing others' money or selling signals/algos is a different regulatory regime (algo-provider empanelment, Research Analyst/Investment Adviser rules). Out of scope — seek counsel.

### 2.4 Market-structure facts that affect strategy design **[V]**
- One weekly index-options expiry per exchange: **Nifty weekly on NSE (Tuesday expiry since Sept 2025)**; BankNifty/FinNifty/Midcap Select are monthly-only; Sensex on BSE (expiry day reported inconsistently — Thursday per most sources **[?]**).
- Lot sizes: Nifty **65** (from 75), BankNifty 30, FinNifty 60, Midcap Select 120; revised periodically — **load lot sizes from the instrument master daily, never hard-code**.
- Upfront premium collection for buyers (Feb 2025); no calendar-spread margin benefit on expiry day; extra 2 % ELM on short index options on expiry day; intraday position-limit monitoring (≥ 4 snapshots/day).
- STT hike (above). F&O income is generally non-speculative business income; intraday equity is speculative income — **get a CA** (tax audit thresholds, ITR-3).

---

## 3. Strategy Research

Conventions: *ATR* = ATR(14) on the signal timeframe. *R* = initial risk (entry→stop) in ₹. *Risk %* = fraction of account equity risked per trade (default 0.5–1 %). **Win-rate / R:R columns are typical profiles [E]**: they show the *shape* of each strategy's payoff, which matters more than the headline win rate. Always judge by **expectancy = (win% × avg win) − (loss% × avg loss) − costs**.

**Sizing formulae (used throughout)**
- Futures/equity: `qty = floor(equity × risk% / (stop_distance × lot_or_share))`
- Long options: `lots = floor(equity × risk% / ((entry − stop_premium) × lot_size))`
- Spreads/condors: `lots = floor(equity × risk% / max_loss_per_lot)` (defined risk makes this exact)
- Account too small for ≥ 1 lot at the target risk % → **skip the trade**, never round up. (Realistically, index-options automation at ≤ 1 % risk needs roughly ₹5–10 lakh **[E]**.)

### 3.1 Trend following
| Strategy | Entry | Exit & stop | Target | Sizing | Typical win % / R:R **[E]** |
|---|---|---|---|---|---|
| **EMA crossover** (9/21 on 15m; or 20/50 on 1h) | Fast crosses above slow on bar close; ADX(14) > 20; price > 200 EMA & session VWAP; volume ≥ 1× 20-bar avg (mirror for shorts) | Opposite cross, or 2.5×ATR chandelier trail. Initial SL 1.5–2×ATR or beyond last swing | No fixed target; book 50 % at 2R, trail rest | ATR-based, 0.5–1 % | 30–40 % / 2–3 : 1. Whipsaws in ranges → regime gate required |
| **VWAP trend/pullback** | Uptrend (higher-TF trend up, VWAP slope up, price > VWAP); pullback to VWAP (±0.25 ATR); rejection candle; volume dry-up on pullback | Close below VWAP − 0.25 ATR; SL under pullback low | 1.5–2R or day-high extension | 0.5–1 % | 45–55 % / 1.2–2 : 1 |
| **SuperTrend** (10, 3) on 15m–1h | Line flips; ADX > 20 filter; HTF agreement | Opposite flip; the SuperTrend line *is* the trailing SL | Trail only | 0.5–1 % | 35–45 % / ~2 : 1; poor in chop |
| **Momentum breakout** (Donchian 20/55-day) | Close > N-day high, volume ≥ 1.5× avg, relative strength > Nifty, not within results window | 10-day low or 3×ATR trail; initial SL 2×ATR | Trail; optional 3R partial | 0.5 % per name, ≤ 15 names | 35–45 % / 2.5–4 : 1 |

### 3.2 Intraday
| Strategy | Entry | Exit & stop | Target | Sizing | Typical win % / R:R **[E]** |
|---|---|---|---|---|---|
| **Opening Range Breakout** (15-min OR; test 5/30 variants) | 5-min close beyond OR high/low after 09:30; volume ≥ 1.3× avg; skip if OR > ~1.2× avg daily ATR-fraction or on extreme VIX; **max 1 trade/underlying/day** | SL at opposite OR edge or midpoint; **hard time exit 15:10** | 1.5–2× OR width or trail | 0.5–1 % (future/ATM option via underlying levels) | 40–50 % / 1.5–2 : 1 |
| **VWAP reversal** (mean reversion) | Range regime (ADX < 18); price ≥ 2σ from session VWAP (or ≥ 1.5 ATR); reversal candle + momentum divergence | SL beyond extreme + 0.3 ATR; exit at VWAP | VWAP (partial at 50 % of distance) | 0.5 % | 55–65 % / 0.8–1.2 : 1. **Disaster on trend days** → regime gate mandatory |
| **Gap strategies** | *Gap-and-go:* gap > 0.5 % with strong pre-open and holds above open for 15 min. *Gap-fill:* gap with normal VIX, fails to extend in first 15 min → fade toward prior close | Go: SL below gap-day first-15-min low. Fill: SL beyond opening extreme | Go: 1.5–2R/trail. Fill: prior close | 0.5 % | Go 40–50 % / 1.5–2 : 1; Fill 55–65 % / ~1 : 1 |
| **Volume breakout** | Break of prior-day high/low or day range on volume ≥ 2× avg; futures: OI rising with price | 1×ATR(5m) trail; SL back inside range | 2R | 0.5 % | 40–50 % / 1.5–2 : 1; liquid names only |

### 3.3 Options (index; load lots from instrument master)
> Option *buying* has positive convexity but needs a real directional edge to beat theta and ~2–3 % friction. Option *selling* has a high win rate, **negative skew** and tail risk — one bad day can erase months. **Automate only defined-risk structures.**

| Strategy | Entry | Exit & stop | Target | Sizing | Typical win % / R:R **[E]** |
|---|---|---|---|---|---|
| **Option buying** (directional) | Underlying signal from ORB/trend; ATM/1-ITM; IV percentile < ~60; avoid last-day theta | SL 25–35 % of premium *or* underlying-level stop; **time stop 45–60 min** if no progress | 1 : 2 (book half at 1R) | Premium-risk formula above | 30–40 % / 2–3 : 1 pre-cost |
| **Option selling** (short strangle/straddle) — *research / defined-risk only* | IV rank > 60–70 or IV − forecast RV > threshold; stable/falling VIX; no event day; 0.15–0.20 delta shorts; **buy far-OTM wings** | SL 1.5–2× credit per leg or net loss = 1R; delta-neutral adjust if |net Δ| > limit; time exit by 15:15 | 50–70 % of credit | Max loss ≤ 1 % equity | 65–80 % / 0.3–0.6 : 1 (negative skew) |
| **Credit spreads** (bull put / bear call) | Directional bias from trend/regime; short 0.20–0.30 delta, long 0.05–0.10 delta; width 100–200 pts (Nifty) | SL at 2× credit or short-strike breach; exit by 1 DTE | 50 % of credit | `floor(risk/max_loss)` | 65–75 % / 0.3–0.5 : 1 |
| **Iron condor** | Range regime (ADX < 20, low forecast RV), IVP > 50, no RBI/Budget/results/major global event; shorts ≈ 1–1.3 SD (0.12–0.20 delta), wings 100–200 pts | 50 % profit take; stop at 2× credit or strike breach; close before expiry day if monthly | 50 % of credit | Max loss ≤ 1 % equity | 70–80 % / 0.25–0.5 : 1 |
| **Long straddle/strangle** (event vol) | Buy when implied event move < forecast realised move (cheap IV) before scheduled catalysts | Exit right after event (vol crush); SL 30–40 % premium | 2 : 1+ | 0.5 % | 30–40 % / 2 : 1+ |
| **Gamma scalping** — *research only* | Long ATM straddle + delta-hedge with futures at Δ thresholds | Stop on theta budget exhausted | Realised vol > implied | ≤ 0.5 % theta budget/day | Not meaningful. At retail costs (STT on futures 0.05 % **[V]**, brokerage per hedge, slippage, lot sizes) usually negative; needs sub-second monitoring |

### 3.4 Swing trading (cash equities — lowest cost drag, no expiry pressure)
| Strategy | Entry | Exit & stop | Target | Sizing | Typical profile **[E]** |
|---|---|---|---|---|---|
| **Multi-day trend following** | Daily: price > 200 DMA, 50 > 200; enter on pullback to 20/50 EMA or breakout; liquidity filter (₹ turnover) | Close < 20/50 EMA or 3×ATR chandelier; initial SL 2–3×ATR | Trail | 0.5 % risk/name, 10–20 names, ≤ 25 % per sector | 35–45 % / 2.5–3.5 : 1 |
| **Momentum screening** (12-1 month, Nifty 200/500) | Rank by 6–12-month return skipping last month, vol-adjusted + relative strength; top decile; monthly rebalance; **market filter: Nifty > 200 DMA else cash/liquid ETF** | Drops out of top 20–30 % rank, or −2.5×ATR | Rebalance-driven | Inverse-vol weights | ~50–55 % per position; portfolio Sharpe plausibly 0.7–1.0; low turnover → cost-efficient. Benchmark vs NSE momentum index |
| **Sector rotation** | Rank sector indices (Bank, IT, Pharma, FMCG, Auto, Metal, Realty, Energy…) on composite 1/3/6-month relative strength vs Nifty + breadth (% stocks > 50 DMA); hold top 2–3 via sector ETFs/constituents | Sector leaves top half or index < 50 DMA; 8–10 % position stop | Monthly rebalance | Equal weight, ≤ 30 % per sector | ~55 % / ~1.5 : 1; modest turnover |

### 3.5 AI-based strategies (details in §5)
| Strategy | How it is used | Realistic expectation **[E]** |
|---|---|---|
| **ML prediction** | Gradient boosting predicts P(trade hits target before stop within N bars) — *meta-labelling* on top of rule-based signals; triple-barrier labels | 51–55 % directional accuracy is already "good"; most models die after costs. Value is mostly in **filtering bad trades** |
| **Pattern recognition** | Rule-based + CNN pattern detectors as *features* | Weak standalone evidence; never trade patterns alone |
| **Volatility forecasting** | HAR-RV/GARCH/LSTM forecast of next-day/5-day realised vol vs implied → sell vol when IV ≫ forecast RV, buy when IV ≪ | Vol is far more forecastable than direction; best AI ROI for options |
| **Reinforcement learning** | Execution/hedging/sizing policies in a simulator | Research sandbox only: instability, sim-to-real gap, overfitting |
| **Regime detection** | HMM/GMM on realised vol, ADX, India VIX level/change, breadth, gap stats → 3–4 regimes | A *gate*, not a P&L strategy; highest-leverage AI component |

### 3.6 Regime → strategy allocation (the AI Selector's starting table)
| Regime | Enable | Disable / reduce |
|---|---|---|
| Trend (up/down), normal vol | EMA/SuperTrend, ORB, VWAP-pullback, momentum, option buying, aligned credit spreads | VWAP reversal, iron condors |
| Range, low vol | VWAP reversal, iron condor, defined-risk strangle | Breakouts, option buying (theta) |
| High vol / event window | Size × 0.5; long-vol only; or stand aside | All short-vol; tight-stop breakouts |
| Crash / illiquid / circuit risk | Flat; kill-switch armed | Everything new |

---

## 4. Market Data Layer

### 4.1 Live market data
| Source | Use | Notes |
|---|---|---|
| **Broker WebSocket** (Dhan/Zerodha) — primary | Ticks, quotes, depth, OI for ≤ a few hundred instruments | Sufficient for a non-HFT system. Dhan: 20/200-level NSE depth **[V]**. Zerodha: ~3 connections, ~3,000 instruments each **[E]** — verify. Plan for reconnect, heartbeats and gap-fill |
| **Direct NSE/BSE feeds** | Not for retail | Exchange data is licensed, not owned; colocation/direct feeds are for members |
| **Licensed tick vendors** (TrueData, Global Datafeeds) | Independent second feed, historical tick/1-min incl. options | Authorized NSE/BSE/MCX vendors; pricing not published in my sources — anecdotal ₹1,500–2,500/month, older quotes ₹1,450–2,600/segment/month **[?]**. Get written quotes and licence terms (personal vs commercial) |
| **NSE website scraping** | Avoid as a dependency | Unofficial, rate-limited, ToS risk **[V]** |

### 4.2 Options data
- **Option chain (strike-wise LTP, bid/ask, OI, volume, IV, Greeks):** from the broker API. Dhan exposes option chain + expired options **[V]**; with Zerodha you assemble it from the instrument master + quotes **[E]**.
- **Derive your own analytics** (don't trust vendor Greeks blindly): implied vol via Black-76 using the matching-expiry future as the forward; skew; term structure; ATM straddle price → implied move; **PCR** = Σ put OI ÷ Σ call OI (use ATM ± N strikes, track change not level); max-pain; OI build-up/unwind classification (price↑/OI↑ long build-up, etc.); **India VIX** + IV percentile/rank.
- **Record from day 1:** snapshot the full chain every 1–5 s (or at least 1 min) into your own store. This is the cheapest way to own proprietary options history.

### 4.3 News, events & sentiment
| Need | Options | Notes |
|---|---|---|
| Corporate announcements / earnings | NSE/BSE announcement feeds via a licensed vendor or scraper (e.g. marketplace scrapers, ~US$50 per 1,000 records for one **[V]**); earnings calendar from exchange board-meeting intimations | Dedupe on announcement ID; check ToS before scraping |
| RBI / macro calendar | RBI MPC dates are pre-published (FY27: Apr 6–8, Jun 3–5, Aug 3–5, **Oct 5–7**, Dec 2–4, Feb 3–5 '27 **[V]**); RBI press releases; MOSPI/DBIE for CPI/IIP; US events from FRED/Trading Economics-type calendars | **Maintain a curated YAML event calendar** (MPC, Budget, CPI, FOMC, expiry, results) — deterministic, free, auditable |
| Global cues | GIFT Nifty, US index futures, Brent, DXY, USDINR, US 10Y — via broker/free quote APIs | Pre-market regime inputs |
| General news | Licensed APIs with entity tagging (verify licence before storing/redistributing); RSS for low-stakes use | Many "free" tiers forbid commercial/automated use |
| Flows | FII/DII provisional EOD data (NSE) | EOD only |
| Sentiment | FinBERT (ProsusAI) as baseline; LLM for structured extraction | FinBERT is English-trained — expect weaker accuracy on Hinglish/India-specific wording **[E]**; validate on a labelled sample of NSE headlines |

---

## 5. AI & Decision Engine

### 5.1 Principle
Rule-based strategies generate candidate signals. AI answers **three narrow questions**: *What regime are we in? Is this signal likely to work right now? Is there an event that should stop us?* This is *meta-labelling* (López de Prado): far more robust than asking a model to predict price.

### 5.2 Modules
| Module | Model | Inputs → Output |
|---|---|---|
| **Regime classifier** | HMM/GMM (unsupervised) + LightGBM classifier on labelled regimes | Realised vol (multi-window), ADX, India VIX level & change, breadth, gap stats, trend slope → {trend↑, trend↓, range-low-vol, high-vol/event} + probability |
| **Strategy selector** | Rule matrix (§3.6) first; later a contextual bandit with rolling per-strategy/per-regime expectancy | Regime + recent strategy performance → enabled set + size multiplier |
| **Confidence scorer** | LightGBM/XGBoost, **probability-calibrated** (isotonic/Platt) | Signal features (strategy id, distance to key levels, volume z-score, spread, time-of-day, regime, VIX, event proximity) → P(win), expected R |
| **Volatility forecaster** | HAR-RV baseline, GARCH variants; LSTM only if it beats HAR out-of-sample | Intraday/daily RV → forecast RV vs IV → VRP signal |
| **Trend detector** | Multi-timeframe slope + ADX + Kalman/regression channel | Trend direction/strength feature |
| **Risk predictor** | Quantile regression/EVT for next-day move & gap risk | Tail-risk estimate → size multiplier, event de-risking |
| **News/event engine** | FinBERT scores + LLM extraction of {event type, affected symbols, direction, materiality, confidence} | Risk-off flag, no-trade windows, optional small score tilt |

### 5.3 Trade decision pipeline (every candidate passes all gates)
```
Signal ─► 1 Regime allows this strategy?            no → reject
        ─► 2 Event/news veto active?                 yes → reject
        ─► 3 Liquidity & spread OK? Cost-adjusted EV > 0?  no → reject
        ─► 4 Confidence scorer: calibrated P(win) ≥ threshold (start 0.58–0.62) & EV_net > 0   no → reject
        ─► 5 Risk Engine pre-trade checks (limits, margin, exposure)                           no → reject
        ─► 6 Size = base_risk × regime_mult × confidence_mult (capped), then execute
```
Every rejection is logged with its reason — rejected-trade analytics tell you whether the filters add value (compare filtered vs unfiltered outcomes in shadow mode).

### 5.4 Validation discipline (where most AI trading projects fail)
- **Purged, embargoed time-series CV** and **combinatorial purged CV**; walk-forward re-training; no random splits.
- Report **Deflated Sharpe Ratio** and **Probability of Backtest Overfitting** when you've tried many variants; keep a log of every experiment (the number of trials matters).
- **Calibration check** (reliability curves) — a 0.65 score must really win ~65 %.
- **Shadow mode** ≥ 4–8 weeks: model runs live, logs decisions, doesn't act.
- **Drift monitoring** (PSI on features, rolling calibration error, rolling expectancy); auto-fallback to rules-only if drift or degradation trips a threshold.
- **Champion/challenger** with fixed promotion criteria.
- LLMs: pin the model version, constrain outputs to a JSON schema, log prompts/outputs, never let LLM output place orders. LLMs do not predict prices — use them to convert text into structured features and vetoes.

### 5.5 Tooling **[E]**
Python 3.12; Polars/pandas; LightGBM, scikit-learn; `arch`/statsmodels (GARCH/HAR), hmmlearn; PyTorch only where justified; MLflow (experiment/model registry); training offline on a laptop or spot instance. **No GPU in production** — tree models and FinBERT inference on a few hundred headlines/day run fine on CPU.

---

## 6. Broker Integration Layer

### 6.1 Comparison (reported facts are **[V]**/**[?]** from vendor pages and community forums; "Judgement" is mine)
| | **Zerodha Kite Connect** | **Dhan (DhanHQ)** | **Angel One SmartAPI** | **Upstox** | **Fyers** |
|---|---|---|---|---|---|
| Orders API cost | Free **[V]** | Free **[V]** | Free **[V]** | Free **[V]** | Free **[V]** |
| Data cost | ₹500/mo (live + historical) **[V]** | ₹499 + tax/mo **[V]** | Free **[V]** | Free (Plus plan for depth-30) **[V]** | Free **[V]** |
| Order rate | ~10/s reported **[V]** | n/a in my sources | 20/s, 500/min, 1,000/hr (forum table) **[?]** | 50/s (marketing) vs older 25/s **[?]** | 10/s, 200/min; daily 10k→~100k **[?]** |
| WebSocket | Mature; multi-connection **[E]** | Live feed + 20/200-depth (NSE); up to 50 instr/conn on 20-depth **[V]** | 3 connections/client **[V]** | V3: 2 connections normal plan; caps 2,000–5,000 keys by mode **[V/?]** | 5 recommended; symbol caps conflict (200 vs 5,000) **[?]** |
| Option chain / Greeks | Assemble yourself **[E]** | Native chain + expired options **[V]** | Basic **[E]** | Option Greeks feed **[V]** | Available **[E]** |
| Historical for backtests | Live contracts only **[E]** | 5 yrs intraday (active contracts); expired options **[V]** | Limited **[E]** | Available; limits unclear | Available; **no paper sandbox [V]** |
| Docs/ecosystem | Largest, most mature **[E]** | Modern, improving; one user-reported option-chain JSON bug **[V]** | Large; users report sporadic blocks **[V]** | Improving (v3); no positions WebSocket **[V]** | Decent; community adapters for NautilusTrader **[V]** |
| **Judgement** | Reliability/maturity benchmark | Best data-per-rupee for options automation | Cost-free but verify stability | Good feed, check limits | Solid, rate-limit sensitive |

### 6.2 Recommendation and rollout
1. **Primary Dhan, secondary Zerodha** (flip if you prioritise track record). Open both accounts early; static-IP registration and API onboarding take time.
2. Build a thin **`BrokerAdapter` interface** (place/modify/cancel, positions, orders, margins, instrument master, tick stream, postbacks) with implementations `DhanBroker`, `KiteBroker`, `SimBroker`. OpenAlgo (open-source, 30+ broker connectors **[V]**) is a useful reference or fallback, but adds a dependency in your critical path — prefer your own thin layer.
3. **2-week bake-off in paper mode**: measure order-ack latency (p50/p95/p99), reject rate, WebSocket disconnects, tick staleness, 429s, option-chain accuracy vs a second source.
4. **Daily auth:** forced daily logout means a morning login step. Automate what the broker permits (token refresh flows); keep a human-in-the-loop step if the broker's terms require interactive 2FA; alert if not authenticated by 08:45.

### 6.3 Execution-layer rules
- **Idempotent client order tags** (also satisfies algo tagging); never blind-retry after a timeout — *query order status first*.
- Order state machine: `NEW → SENT → ACK → PARTIAL → FILLED | REJECTED | CANCELLED`, driven by postbacks with polling reconciliation.
- Limit orders with a protection buffer by default; market orders only with non-zero market protection. Where SL-M isn't allowed (e.g., some option contracts), use SL-Limit with a buffer — check per broker/segment.
- Place the **protective stop immediately on fill**; maintain stop and target as OCO-like logic in your Position Manager, with the exchange-resident stop as the fallback.
- Reconcile positions/orders/margins against the broker every few seconds and on every fill; any mismatch → freeze new entries and alert.
- Respect freeze quantities (slice big orders), rate limits (internal ≤ ~5 OPS), and market hours/holiday calendar.
- Square off intraday before the broker's auto-square-off (around 15:15–15:20) to avoid forced exits and fees.

---

## 7. Risk Management System

> **Capital protection comes first.** The Risk Engine is independent code (separate process, own tests, minimal dependencies) that every order must pass. No model, strategy or dashboard setting can bypass it.

### 7.1 Layered controls and default limits (example: ₹10 lakh account) **[E]** — tune from paper-trading data
| Layer | Control | Default |
|---|---|---|
| **Pre-trade** | Fat-finger: max qty/lots per order; price within ±x % of LTP; symbol/segment whitelist; margin sufficiency; duplicate-order guard | Max 2 lots/order (start), whitelist = Nifty/BankNifty/liquid F&O |
| | Max open positions / per-underlying exposure / per-sector exposure | 3 concurrent; ≤ 40 % notional per underlying |
| | Net Greeks limits (options book): |Δ|, vega, gamma, short-gamma near expiry | Set per account; hard cap on net short vega |
| | Time filters | No new entries first 5 min, after 14:45, or in event windows |
| **Per-trade** | Initial stop (ATR/structure/premium), trailing, time stop, partial profit | Risk 0.5–1 %; trail activates at +1R; book 50 % at 2R |
| **Daily** | Daily loss limit | −2 %: halt new entries; −3 %: flatten and halt |
| | Consecutive-loss breaker | 4 losses in a row → halt for the day |
| **Weekly/monthly** | Loss limits | −5 % week: size ×0.5 next week; −8 % month: paper-only until review |
| **Drawdown** | Max drawdown protection from equity peak | −10 %: size ×0.5; −15 %: full stop + mandatory human review |
| **System health** | Data staleness (> 3 s on subscribed instruments) → no new entries; broker/WebSocket down; clock drift; reconciliation mismatch; error-rate or reject-loop; 429 storms | Any trip → freeze entries + alert; critical → flatten |
| **Exchange/market** | Circuit/price-band awareness, F&O ban list, expiry-day rules (extra ELM), illiquid-strike filter (spread %, OI minimum) | Auto-reject |
| **Circuit breakers** | Intraday vol shock (e.g., VIX +20 % in 15 min, Nifty −2 %/15 min) | Cut size, tighten stops, or flatten per config |

### 7.2 Kill switch (multi-level)
| Level | Trigger | Action |
|---|---|---|
| L1 Soft | Risk-limit breach, data stale | Block new entries; manage exits only |
| L2 Reduce | Drawdown/vol-shock | Cut position size, tighten stops |
| L3 Flatten | Daily-loss −3 %, reconciliation failure, unknown position | Cancel all orders, close all positions, halt |
| L4 Disconnect | Security event / runaway orders | Revoke token/session, halt process; manual restart only |

Triggers: automatic **and** manual (Telegram command with confirmation + dashboard button + a physical fallback — **keep the broker's mobile app logged in and know how to use the broker's own kill switch**). A separate lightweight "watchdog" process should be able to issue L3 even if the main engine hangs.

### 7.3 Trade management
- **Dynamic SL:** ATR-based, volatility-scaled; tighten as IV collapses or time to expiry shrinks.
- **Trailing:** chandelier (high − k×ATR) or structure-based (higher-lows); for options, trail on premium *and* underlying level.
- **Partial profit:** 50 % at 1.5–2R, move stop to breakeven (+costs), trail rest.
- **Time stops:** exit if no progress in N bars (theta for long options; mean-reversion trades).
- **Portfolio hedging:** cap net delta by hedging with index futures or buying wings; keep short-option books defined-risk; scale down ahead of events instead of "hoping".
- **Sizing governance:** fixed-fractional risk; fractional Kelly ≤ 0.25 only after a long live record; reduce size after drawdown (anti-martingale), never increase after losses.

### 7.4 Operational failure runbook
| Failure | Automated response |
|---|---|
| WebSocket drop | Reconnect with backoff; REST poll fallback; no new entries while stale |
| Process crash | Supervisor restart; rebuild state from broker; broker-resident stops protect meanwhile |
| Broker API outage | Alert; attempt secondary adapter for *exit-only*; human takes over via broker app |
| VM failure | Alert via external heartbeat; manual failover (Phase 2), warm standby (Phase 3) |
| Auth expired mid-day | Alert; entries blocked; existing stops remain at exchange |

---

## 8. Backtesting & Simulation

### 8.1 Four modes, one codebase
| Mode | Data | Broker | Purpose |
|---|---|---|---|
| Historical backtest | Stored bars/ticks/chains | `SimBroker` (fees, slippage, latency, partial fills, lot sizes, market protection) | Hypothesis testing, parameter sweeps |
| Walk-forward / OOS | Rolling train → test windows | `SimBroker` | Robustness, decay estimate |
| Paper (forward) | **Live** stream | `SimBroker` fed by live ticks; fills at the *next* tradable price with conservative slippage | Realism check, ops shakedown. Most Indian brokers lack a real paper API (e.g., Fyers has no sandbox **[V]**) so build your own |
| Live | Live stream | `LiveBroker` | Real, staged capital |

### 8.2 Frameworks
| Tool | Use | Verdict |
|---|---|---|
| **vectorbt** | Vectorised research, big parameter sweeps (seconds) **[V]** | Use for idea screening; not for execution realism |
| **Own event-driven core** (same strategy classes as live) | Order-level realism, partial fills, intrabar stop ordering | **Recommended**; ~3–5 weeks to build |
| **NautilusTrader** | Rust-core event-driven engine with live/backtest parity; community Fyers adapter **[V]**; you supply connectivity/data | Strong candidate if you want to buy rather than build; steeper learning curve, no first-party Dhan/Zerodha adapters in my sources — evaluate in Phase 3 |
| backtrader | Legacy; reports of install issues on newer Python and low maintenance **[V/?]** | Avoid for new work |
| OpenAlgo | Broker-agnostic execution layer + vectorbt-based backtests **[V]** | Reference/optional; verify what has shipped |
| AlgoTest / StockMock-style hosted tools | Fast options-structure backtests **[V]** | Good sanity cross-check |

### 8.3 Historical data
| Data | Source |
|---|---|
| Equities/futures intraday (live contracts) | Broker historical APIs (Zerodha/Dhan) |
| **Expired index options** | Dhan expired-option data **[V]**; vendors (TrueData/GDFL) |
| Tick history | TrueData, Global Datafeeds (licensed) **[V]** |
| EOD derivatives & cash | NSE/BSE bhavcopy (free; note format changes — handle schema versions) **[E]** |
| India VIX, index history | NSE |
| Corporate actions | Exchange announcements; adjust prices consistently |
| **Your own recordings** | Start capturing ticks + option-chain snapshots on day 1 |

**Data hygiene:** survivorship bias (use point-in-time constituents and delisted names), corporate-action adjustment, expiry-roll handling, holiday/special-session calendar, lot-size changes over time, bad ticks, and timestamp alignment (exchange vs receive time).

### 8.4 Methodology
1. Realistic costs: brokerage per order, STT (current rates by date), exchange/SEBI/stamp/GST, **slippage model** (spread-based + size/impact; harsher for illiquid strikes and market-on-open).
2. Intrabar ambiguity: if both stop and target fall in one bar, assume the *stop* hit first unless tick data says otherwise.
3. Latency: fill at next tick/bar after signal + broker latency.
4. Walk-forward with parameter stability checks; prefer broad plateaus over sharp optima.
5. Monte-Carlo trade resampling for drawdown distributions; stress tests on 2008-style, 2020 and budget/election-day gaps.
6. Minimum evidence: ≥ 200–300 trades OOS per strategy, multiple regimes covered.

### 8.5 Promotion gates (backtest → paper → live) **[E]**
| Gate | Criteria |
|---|---|
| Backtest → Paper | OOS net Sharpe ≥ ~1.0, profit factor ≥ 1.3, max DD within budget, stable across parameter neighbourhood, DSR/PBO acceptable |
| Paper → Live (1 lot) | ≥ 8–12 weeks and ≥ 50–100 paper trades; realised slippage within tolerance of model; zero unresolved Sev-1 incidents; kill-switch drill passed |
| Live scale-up | 25 % → 50 % → 100 % of target size only after each stage meets expectancy within confidence band of backtest |
| Demotion | Rolling-60-trade expectancy < 0, or drawdown > 1.5× backtest max DD → back to paper |

---

## 9. Infrastructure & Deployment

### 9.1 Components
| Component | Tech **[E]** | Notes |
|---|---|---|
| Trading engine (ingest, features, strategies, OMS, position manager) | Python 3.12 asyncio, Pydantic | One process family; supervised |
| Risk engine + watchdog | Separate small process | Own tests, minimal deps |
| AI engine | LightGBM/sklearn/arch; FinBERT on CPU; LLM via API | Online inference small; training offline |
| Scheduler | systemd timers/APScheduler + own NSE calendar | Pre-market login check, EOD jobs, retraining |
| Hot state | Redis 7 (Streams) | Pub/sub, positions cache |
| Database | PostgreSQL 16 + TimescaleDB | Ticks/candles/chain snapshots (compressed), orders, trades, P&L, audit log |
| Cold storage | Parquet on S3 | Research archives, 5-year log retention |
| Dashboard | Grafana (ops) + small FastAPI/React or Streamlit control UI | **Behind VPN/SSO; never public** |
| Alerts | Telegram bot (primary), SES/SMTP email, optional WhatsApp | See §10 |
| Observability | Prometheus, Grafana, Loki/CloudWatch, Sentry, external heartbeat (e.g., Healthchecks.io) | Dead-man's-switch is essential |
| Secrets | AWS Secrets Manager/SSM; never in repo | Rotate keys; least privilege |
| Packaging | Docker + Compose; GitHub Actions CI | Kubernetes is unnecessary at this scale |

### 9.2 Cloud choice
| Provider | Fit | Notes |
|---|---|---|
| **AWS** (`ap-south-1` Mumbai; `ap-south-2` Hyderabad DR) | **Recommended** | Mature, Elastic IP for the whitelisted static IP, strong managed services. c7g.large ≈ $0.049/hr, t4g.large ≈ $0.045/hr on-demand in Mumbai **[V]** |
| Oracle Cloud | Dev/paper only | Always-Free ARM (4 OCPU/24 GB) is attractive, but "out of capacity" is common, home region is permanent, single AD in India, free tier has no SLA **[V]** |
| Google Cloud (Mumbai/Delhi) | Fine technically | Choose if you already run on GCP |
| Azure (Pune/Chennai) | Fine technically | Same |

Broker APIs are internet-facing retail endpoints; cross-provider latency differences are noise for this system. Pick on reliability, tooling and cost.

### 9.3 Sizing **[E]**
| | Minimum | Recommended | Production |
|---|---|---|---|
| Use | Dev / paper / ≤ 100 instruments | Live, ≤ ~500 instruments, 4–6 strategies | Live capital, HA, 2 underlyings × full chains + equities |
| Compute | 1 VM: 2 vCPU / 4 GB (t4g.medium-class) | 1 VM: 2–4 vCPU / 8–16 GB (t4g.large / c7g.xlarge) | 2 VMs across AZs: 4 vCPU / 16 GB each (c7g.xlarge/m7g.xlarge) + warm standby |
| Storage | 100 GB gp3 | 250–500 GB gp3 + S3 | 1 TB+ gp3, Multi-AZ/managed Postgres or replicated Timescale, S3 lifecycle |
| Database | Postgres + Timescale on the same VM | Same VM, separate volume, nightly snapshots | Dedicated DB node(s) with replication & PITR |
| Network | Elastic IP (whitelisted) | Same | Two static IPs (primary + secondary registered with broker) |
| AI/ML | CPU | CPU | CPU; GPU only for offline research |

Storage logic: persist 1-min/1-s aggregates for the whole universe (tiny) and raw ticks only for a watchlist (~1–3 GB/day uncompressed for a few hundred instruments, ~5–10× smaller compressed) **[E]**.

### 9.4 Reliability practices
Daily snapshots + tested restore; infrastructure as code (Terraform) once past Phase 1; unattended-upgrades only in maintenance windows (never during 08:30–15:45 IST); NTP/chrony; log shipping; restrict inbound to VPN; MFA on cloud and broker accounts; dependency pinning; staging environment that mirrors production with `SimBroker`.

---

## 10. Monitoring & Alerts

### 10.1 Channels
| Channel | Role | Notes |
|---|---|---|
| **Telegram bot** | Primary: all events + two-way commands (`/status`, `/pnl`, `/halt`, `/flatten`) | Free, fast, reliable API; respect ~1 msg/s per chat **[E]**; restrict commands to whitelisted user IDs + confirmation for destructive actions |
| **Email** (SES/SMTP) | Daily/weekly reports, audit trail | Low urgency |
| **Dashboard** (Grafana + control UI) | Live P&L, positions, risk utilisation, system health | Behind VPN |
| **WhatsApp** | Optional mirror of critical alerts | Needs Business API/BSP, approved templates, opt-in. India utility template ≈ ₹0.115/message base **[V]** (+ BSP markup ₹0.10–0.30 **[V]**, +18 % GST); from 1 Oct 2026 utility templates inside an open window and service messages beyond 1,000 free/month are billable **[V]**. Telegram covers 95 % of the need |
| **Phone call/SMS** | Critical only (kill-switch, data stale with open positions) | Twilio/Exotel-type service; escalate if no ack in 2 min |

### 10.2 Alert taxonomy
| Severity | Events | Delivery |
|---|---|---|
| INFO | Trade entry, trade exit, SL hit, target hit, partial booked, daily P&L summary, EOD report | Telegram channel (batched where chatty) |
| WARN | Risk-limit proximity (≥ 70–80 % of daily limit), filter rejects spike, slippage above model, auth near expiry | Telegram + dashboard |
| CRITICAL | Kill switch fired, data feed stale with open positions, reconciliation mismatch, order-reject loop, broker/API down, unexpected position, daily-loss halt | Telegram + phone call/SMS; requires ack |

**Entry alert format:** strategy · symbol/contract · side/qty · entry · SL · target · risk ₹ and % · confidence · regime. **Exit alert:** reason (SL/target/trail/time/manual/kill) · P&L ₹ and R · hold time · slippage vs plan.

### 10.3 Practices
- **Dead-man's switch:** engine pings an external service every minute; silence ⇒ alert. Monitors the monitor.
- Alert deduplication, rate-limiting and escalation; "heartbeat OK" digest at 09:20 and 15:35.
- Metrics (Prometheus): tick latency/staleness, order-ack latency, reject/429 counts, slippage, reconciliation diffs, open risk, P&L, queue depth, model score distributions and calibration.
- Structured JSON logs with order/trade IDs; immutable audit log for 5 years **[V]**.
- Quarterly **kill-switch and failover drills**.

---

## 11. Performance Analytics Framework

### 11.1 Metrics (computed per strategy, per regime, per underlying, per time-of-day, and portfolio-wide)
| Metric | Definition | Use / threshold guide **[E]** |
|---|---|---|
| Win rate | Wins ÷ trades | Meaningless alone; pair with payoff |
| Avg win / avg loss (payoff ratio) | Mean of winners ÷ mean of losers | |
| **Expectancy** | `win% × avgWin − loss% × avgLoss` (₹ and R) | Must be > 0 *net of costs* |
| Profit factor | Gross profit ÷ gross loss | ≥ 1.3 target net |
| Average trade | Net P&L ÷ trades (₹, R) | Must comfortably exceed per-trade friction |
| Sharpe | `(mean daily excess return ÷ σ) × √252`; use a risk-free proxy (≈ short G-sec) | ≥ 1.0 net OOS target |
| Sortino / Calmar (MAR) | Downside-deviation based / CAGR ÷ max DD | Calmar ≥ 1 |
| **Max drawdown** (depth, duration, recovery) | Peak-to-trough on equity | Compare live vs backtest MaxDD |
| Monthly performance | Return table/heatmap, best/worst month, % positive months | |
| Exposure & turnover | Time in market, notional traded | |
| **Cost ratio** | (brokerage + STT + charges + slippage) ÷ gross P&L | > 30–40 % is a red flag |
| Slippage | Fill price − intended price (by strategy/time/liquidity) | Calibrates `SimBroker` |
| Reject analytics | Outcomes of AI/risk-rejected signals vs accepted | Proves filters add value |
| Model health | Calibration error, PSI, rolling AUC/Brier | Triggers fallback |
| Overfitting guards | Deflated Sharpe, PBO, trial count | For every promoted strategy |
| Benchmarks | Nifty 50 buy-and-hold, liquid-fund/G-sec, equal-risk baseline | Alpha vs beta |

### 11.2 Strategy comparison scorecard (auto-generated weekly)
Rows = strategies; columns = trades, win %, payoff, expectancy (R), PF, Sharpe, MaxDD, cost ratio, slippage, rolling-60 expectancy, status (`research | paper | live-1lot | live-scaled | demoted`), capital allocated. Capital allocation follows rolling risk-adjusted performance with caps (e.g., ≤ 40 % risk budget per strategy).

### 11.3 Decay & lifecycle rules
- **Demote** when rolling-60-trade expectancy < 0, or live DD > 1.5× backtest MaxDD, or slippage > 2× model.
- **Retire** after two consecutive failed re-validations.
- Weekly review report (auto), monthly strategy committee (you), quarterly full re-validation on fresh OOS data.

### 11.4 Reporting
Daily EOD report (P&L, trades, risk usage, rejects, incidents), weekly scorecard, monthly deep-dive, and a **tax-ready trade ledger** export (turnover per the applicable F&O/intraday rules) for your CA.

---

## 12. Roadmap, Cost, Effort

### 12.1 Phases
**Phase 1 — Foundation & paper (weeks 1–10)**
- Repo, CI, environments; AWS VPC, Elastic IP, secrets, Docker Compose.
- Open Dhan + Zerodha API access; static-IP registration; daily-auth flow.
- `BrokerAdapter` + `SimBroker`; market-data ingestor with reconnect/gap-fill; Timescale schema; **start recording ticks/chains**.
- OMS state machine, reconciliation, Risk Engine v1 (pre-trade, daily loss, kill switch L1–L3), Position Manager (SL/target/trail/time).
- Backtest engine with realistic cost model; 2 strategies (ORB + VWAP-pullback) and 1 trend filter; Telegram alerts; basic Grafana.
- **Exit criteria:** 4+ weeks stable paper run; kill-switch drill passed; paper vs backtest slippage reconciled.

**Phase 2 — Small live + AI gating (weeks 11–24)**
- Live with 1 lot, capped daily loss (e.g., ≤ 0.5–1 % of a small dedicated capital allocation).
- Regime classifier (shadow → active), meta-label confidence scorer (shadow → active), event calendar + FinBERT/LLM veto.
- Add swing momentum + sector rotation (cash), EMA/SuperTrend; option chain analytics (IV/PCR/OI) and long-option execution via underlying signal.
- Analytics v1 (scorecard, daily/weekly reports), dashboard, watchdog, dead-man's switch, runbooks.
- **Exit criteria:** 8–12 weeks live at 1 lot with expectancy within the backtest band; no Sev-1 incidents.

**Phase 3 — Scale & options structures (weeks 25–40)**
- Defined-risk credit spreads/iron condors with event/regime filters; multi-leg execution & leg-risk handling; Greeks-based portfolio limits.
- Vol forecaster (HAR/GARCH) driving option buy-vs-sell; drift monitoring; champion/challenger.
- HA/DR (warm standby, Hyderabad DR), secondary-broker exit-only failover, IaC, data-vendor second feed.
- Staged scale-up 25 % → 100 % of target capital; quarterly re-validation process; RL/gamma research sandbox (non-production).

### 12.2 Estimated monthly infrastructure cost (₹88/USD, ex-GST) **[E]**
| Line | Minimum | Recommended | Production |
|---|---|---|---|
| Compute | ₹0 (Oracle free) – ₹1,500 | ₹2,900 (t4g.large) – ₹6,300 | ₹12,600 (2× c7g.xlarge) + standby ₹6,000 |
| Storage + snapshots + S3 | ₹500 | ₹2,000 | ₹6,000 |
| Static IPv4 / network | ₹320 | ₹320 | ₹700 |
| Managed/HA database | – | – | ₹15,000–25,000 |
| Broker data API | ₹500 | ₹500–1,000 (both brokers) | ₹1,000 |
| Tick/historical vendor | – | ₹0–2,500 | ₹5,000–25,000 |
| News/LLM APIs | ₹0–500 | ₹2,000–5,000 | ₹5,000–15,000 |
| Monitoring/alerts (Sentry, heartbeat, SMS, WhatsApp) | ₹0 | ₹0–500 | ₹2,000–5,000 |
| **Total (sum of line-item ranges)** | **₹1,500–3,500** | **₹8,000–17,000** | **₹55,000–95,000** |

Excludes trading costs, taxes and developer time. Prices come from third-party snapshots **[V]**/estimates **[E]** — confirm with the AWS calculator and vendor quotes.

### 12.3 Estimated development effort **[E]**
| Phase | Person-months | Calendar |
|---|---|---|
| 1 | 3–4 | ~10 weeks (2 engineers) |
| 2 | 5–7 | ~14 weeks |
| 3 | 6–9 | ~16 weeks |
| **Total** | **14–20** | ~9–10 months with 2 engineers; ~12–15 months solo |

Largest risk to schedule is not coding but **paper/live soak time** and data work.

---

## 13. Key Risks and Limitations
| Risk | Why it matters | Mitigation |
|---|---|---|
| **No edge / base rate** | ~91 % of retail derivative traders lose **[V]**; most published "win rates" are marketing | Defined-risk only, strict promotion gates, fail small, expect most ideas to be rejected |
| **Overfitting / backtest bias** | Easiest way to lose money with AI | Purged CV, DSR/PBO, trial logging, shadow mode |
| **Costs & taxes** | STT hike, per-order brokerage, slippage, wide option spreads | Cost-aware EV filter; prefer low-turnover strategies; CA input |
| **Regulatory change** | Static IP, algo tags, OPS limit, daily logout; further SEBI/NSE changes | Track circulars; compliance module; stay ≤ 5 OPS; logs retained 5 yrs |
| **Scope creep into advice/ management of others' money** | Triggers algo-provider/RA/IA regimes | Keep personal/family; legal advice before expanding |
| **Short-vol tail risk** | Gaps, circuits, event days wipe out months of premium | Wings, event calendar, size caps, expiry-day rules |
| **Operational** | API outages, auth expiry, rate limits, VM failure, WebSocket gaps | Reconciliation, broker-resident stops, watchdog, secondary broker, runbooks |
| **Data risk** | Bad ticks, missing OI, vendor Greeks errors, scraping breakage | Dual sources, own IV calc, sanity checks |
| **Model/LLM risk** | Drift, hallucination, silent API model changes | Calibration monitoring, pinned versions, schema-constrained outputs, no order authority |
| **Security** | API keys + broker account = money | Secrets manager, IP restriction, MFA, VPN-only dashboards, least privilege, no keys in logs |
| **Human override** | Disabling safeguards after a loss | Limits changeable only via change-controlled config with cool-off delay |
| **Small-capital granularity** | Lot sizes force over-risking below ~₹5–10 lakh **[E]** | Skip trades that exceed risk budget; use cash-equity strategies first |
| **Limits of this research** | Based on web summaries, not primary SEBI/NSE/broker documents; some figures conflict **[?]** | Verify every [V]/[?] item against primary sources before committing money |

---

## 14. Decisions Needed From You
1. **Capital and risk tolerance** (starting capital, max acceptable drawdown) — drives lot sizing and which strategies are even feasible.
2. **Personal/family use only?** (If not, regulatory scope changes materially.)
3. **Primary broker**: accept Dhan-primary/Zerodha-secondary, or prefer track-record-first (flip)?
4. **Instruments first**: cash equities + index futures/options via underlying signals (recommended) vs. options-structures-first.
5. **Team and timeline**: solo vs. 2 engineers (affects the roadmap in §12).
6. **Data budget**: broker data only vs. adding a licensed tick/historical vendor in Phase 1.

---

## Appendix A — Sources (secondary; verify against primary documents)

**Regulation / market structure**
- [SEBI algo trading changes — April 2026 (Zerodha "In the Money")](https://inthemoneybyzerodha.substack.com/p/sebi-algo-trading-changes-april-2026)
- [New NSE rules for retail algo trading (MarketCalls)](https://www.marketcalls.in/market-regulations/new-nse-rules-for-retail-algo-trading-what-retail-traders-algo-platforms-and-brokers-must-now-do.html)
- [SEBI extends retail algo rollout to April 2026 (Business Standard)](https://www.business-standard.com/markets/news/sebi-extends-retail-algo-trading-framework-rollout-to-2026-125093000956_1.html)
- [NSE retail algo circular highlights (Nithin Kamath)](https://nithinkamath.substack.com/p/nses-new-retail-algo-trading-circular)
- [SEBI F&O study — net losses widen in FY25 (Business Standard)](https://www.business-standard.com/amp/markets/news/net-losses-of-traders-in-fo-widens-in-fy25-sebi-study-125070701221_1.html)
- [Average F&O trader lost ₹1.1 lakh in FY25 (Value Research)](https://www.valueresearchonline.com/stories/225398/average-trader-lost-rs-1-1-lakh-fo-fy25/)
- [Budget 2026 STT hike explained (Finnovate)](https://www.finnovate.in/learn/blog/budget-2026-stt-hike-fno-trades-explained)
- [STT changes in Budget 2026 (ICICI Direct)](https://www.icicidirect.com/ilearn/futures-and-options/articles/stt-changes-in-budget-2026-what-f-o-traders-should-know)
- [Nifty expiry moved to Tuesday (Business Standard)](https://www.business-standard.com/markets/news/nse-bids-adieu-to-thursday-expiry-as-dates-swap-come-into-effect-explained-125082800635_1.html)
- [NSE revises lot sizes, Jan 2026 (HDFC Sky)](https://hdfcsky.com/news/nse-revises-market-lot-sizes-for-major-index-derivatives-effective-january-2026)
- [SEBI F&O measures: upfront premium, calendar spread, expiry-day ELM (Upstox community)](https://community.upstox.com/t/an-explanation-of-recently-published-f-o-changes-by-sebi/6841)

**Brokers / data**
- [Kite Connect fee revised to ₹500/month (Zerodha forum)](https://kite.trade/forum/discussion/comment/49597)
- [Zerodha: how to sign up for Kite Connect](https://support.zerodha.com/category/trading-and-markets/kite-web-and-mobile/kite-api/articles/how-do-i-sign-up-for-kite-connect)
- [Zerodha makes trading API free, bundles historical data (MarketCalls)](https://www.marketcalls.in/algo-trading/zerodha-makes-trading-api-free-for-personal-use-bundles-historical-data-with-connect-api.html)
- [DhanHQ Data API subscription](https://dhan.co/support/platforms/dhanhq-api/how-does-the-dhanhq-data-api-subscription-work/) · [Dhan 20-depth](https://dhanhq.co/docs/v2/20-market-depth) · [Dhan releases](https://dhanhq.co/docs/v2/releases/)
- [Angel One SmartAPI rate-limit forum thread](https://smartapi.angelone.in/smartapi/forum/post/16460)
- [Upstox Market Data Feed V3](https://upstox.com/developer/api-documentation/v3/get-market-data-feed) · [Upstox trading API](https://upstox.com/trading-api/)
- [Fyers API v3 introduction](https://fyers.in/community/t/introducing-fyers-api-version-3-v3-0-0-a-major-update-for-improved-algo-trading/12193) · [Fyers data-websocket limits](https://support.fyers.in/portal/en/kb/articles/is-there-a-limit-to-the-number-of-symbols-i-can-track-using-the-data-websocket-in-api-v3)
- [TrueData market data APIs](https://truedata.in/market-data-apis) · [Tick-data vendor discussion (TradingQnA)](https://tradingqna.com/t/need-reliable-tick-by-tick-data-for-indian-markets-suggestions/194950)
- [AlgoTest — best brokers for algo trading in India](https://algotest.in/blog/best-brokers-for-algo-trading-in-india.md) · [AlgoTest Broker Speedtest](https://algotest.in/blog/broker-speedtest-algotest.md)

**Tooling / infra / alerts**
- [Python backtesting library comparison 2026 (AlgoLab)](https://algolab.co.kr/blog/python-backtesting-library-comparison-2026) · [nautilus-fyers adapter](https://pypi.org/project/nautilus-fyers/) · [OpenAlgo v1.0 (MarketCalls)](https://www.marketcalls.in/openalgo/introducing-openalgo-v1-0-the-ultimate-open-source-algorithmic-trading-framework-for-indian-markets.html)
- [AWS ap-south-1 pricing](https://aws-pricing.com/ap-south-1.html) · [c7g.large](https://aws-pricing.com/c7g.large.html) · [t4g.large](https://sparecores.com/server/aws/t4g.large)
- [Oracle Always Free resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm)
- [WhatsApp Business API pricing India 2026](https://baat.ai/blog/whatsapp-api-pricing-india) · [Oct 2026 pricing change](https://m.aisensy.com/blog/whatsapp-service-message-pricing-update/)
- [RBI FY27 MPC calendar](https://www.outlookmoney.com/banking/rbi-publishes-fy27-mpc-schedule-april-meeting-to-decide-first-rate-move)
- [FinBERT paper](https://arxiv.org/pdf/1908.10063) · [NSE/BSE announcements scraper (Apify)](https://apify.com/nexgendata/nse-bse-announcements)
