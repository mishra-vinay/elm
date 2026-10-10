# Bajaj Finance — previous-day high/low break-and-pullback (protocol, written before results)

Same rule as the silver study (`docs/silver-options-protocol.md`), applied to the **cash-market share** `BAJFINANCE.NS`, traded intraday (MIS) both long and short, flat by the close. No options.

**Data:** Yahoo hourly bars (09:15, 10:15 … 15:15 IST; ~7 per session; ~2 years kept) for the one-year test; 15-minute and 5-minute bars (~60 days) as a resolution check only. Session = regular market hours.
**Windows:** 1Y = 10 Oct 2025 → 9 Oct 2026 (headline), 6M = 10 Apr → 9 Oct 2026, prior year = 10 Oct 2024 → 9 Oct 2025 (extra out-of-period check).

**Rule (unchanged from silver):** break = a bar trades beyond the previous session's high (low); pullback = a *later* bar trades back `p` inside the level; enter at that level (open if gapped); exit at +target, −stop, or the session close. Same-bar ambiguity resolved against the trader (conservative primary; optimistic as a bound). Break bars never fill the pullback.

**Primary spec = the silver parameters, unchanged:** p = 1%, target = 2%, stop = 1%, intraday.
Caveat fixed in advance: Bajaj Finance's typical daily range is smaller than silver's, so 2%/1% may be rarely reached. Volatility-scaled variants (p ∈ {0, 0.5, 1}%, target ∈ {1, 1.5, 2, 3}%, stop ∈ {0.5, 1}%/none) are **exploratory only**.

**Costs:** 0.10% round trip (STT 0.025% on the sell leg, stamp, exchange and GST ≈ 0.035%, slippage 0.02% per side, brokerage ₹40 on a ~₹2 lakh position ≈ 0.02%). Sensitivity: 0% and 0.20%.

**Pass/fail (1Y window, all must hold):**
1. Mean net return per trade > 0 with the day-clustered 95% bootstrap interval above 0.
2. Beats random-entry timing (same exits, random bar and direction, 1,000 draws), one-sided p < 0.05.
3. Still positive after removing the 3 best trades.
4. Positive in the 6M window and in the prior year.
5. Result stands under the conservative same-bar rule (primary) — and is not an artefact of side: reported separately for longs and shorts.

Benchmark: buy-and-hold of the stock over the same window, for context only.
