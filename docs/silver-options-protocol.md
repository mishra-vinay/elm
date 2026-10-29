# Silver options study — protocol (written before any result was computed)

**Strategy as given:** mark the previous session's high/low. Buy setup: price breaks above the high,
pulls back (e.g. 100 → 99/98), enter long, exit at +2–3%. Sell setup: price breaks below the low,
pulls back (95 → 96), enter short, exit at −2–3%.

**Interpretation fixed in advance (the request leaves these open)**
- Session = MCX day 09:00–23:30 IST, Monday–Friday. Previous-session high/low from that window.
- Underlying proxy = COMEX silver (Yahoo `SI=F`) hourly bars × USDINR, scaled ×1.20 to the ~₹2.28 lakh/kg MCX level.
  Free historical MCX option prices do not exist, so option P&L is **modelled** (Black-76, ATM, IV = 20-session realised vol).
- "Break" = a bar's high (low) exceeds the previous high (low). "Pullback" = a **later** bar trades back `p` beyond the level
  (buy: to H·(1−p); sell: to L·(1+p)). Entry at that level (open if gapped through).
- Buy setup → buy ATM call; sell setup → buy ATM put (option buyer; no short options).
- Exit at +target on the underlying, stop at −1%, or flat at session close.
- Same-bar ambiguity: **conservative** (stop first; no target in the entry bar) is the primary; optimistic reported as a bound.

**Primary spec:** p = 1%, target = 2%, stop = 1%, intraday exit, 1 lot SILVERM (5 kg), costs: option half-spread 1.5% per side,
CTT 0.1% of sale premium, ₹40 brokerage; underlying round-trip cost 0.10%.
**Exploratory grid (not for selection):** p ∈ {0, 1, 2}%, target ∈ {2, 3}%, stop ∈ {1, 1.5, none}, carry overnight, IV × {0.8, 1, 1.25}.

**Periods:** 6M = 10-Apr-2026 → 9-Oct-2026, 1Y = 10-Oct-2025 → 9-Oct-2026; 2Y (extra) = 10-Oct-2024 →.

**Pass/fail (all must hold in the 1Y window for "supported"; 6M reported separately):**
1. Underlying, primary spec, net of costs: mean > 0 and day-clustered 95% bootstrap CI lower bound > 0.
2. Modelled option return on premium, base costs: mean > 0 and CI lower bound > 0.
3. Still > 0 under the conservative intrabar rule (it is the primary, so this is the same test as 1–2).
4. Beats random-entry timing (same exits, random bar and direction, 1,000 draws) at one-sided p < 0.05.
5. Remains positive after removing the 3 best trades.
6. Positive in the 6M window as well.

Number of trials is stated in the report; with ~30 grid cells, cells that pass individually are not evidence.
