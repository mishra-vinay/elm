// Backtest page: pick a stored data set and a strategy, run it through the same engine as the dry runs, and show the report
// (rupee P&L cards, equity and drawdown chart, a day chart with the trades, statistics and the trade list). Shared helpers are in common.js.
let DATA = [], lastResult = null, JOB = null, lastDay = null;
const lab = c => `${c.name || c.symbol} (${c.segment}) · ${c.interval} · ${c.count.toLocaleString()} bars · ${c.days} days · ${String(c.from).slice(0, 10)} → ${String(c.to).slice(0, 10)}`;
async function loadSets() {
  const {data} = await api("/api/data/summary");
  DATA = data.candles.filter(c => c.interval !== "D").sort((a, b) => (a.symbol + a.interval).localeCompare(b.symbol + b.interval));
  $("ds").innerHTML = DATA.map((c, i) => `<option value="${i}">${esc(lab(c))}</option>`).join("") || '<option value="">No intraday candles stored yet: use Market data → Download history</option>';
  if (DATA.length) dsChanged();
}
function dsChanged() { const c = DATA[$("ds").value]; if (!c) return; $("d_from").value = String(c.from).slice(0, 10); $("d_to").value = String(c.to).slice(0, 10); }
function params() {
  const n = id => $(id).value === "" ? null : Number($(id).value), sp = strategyParams();
  if (!sp) throw new Error("No strategy is installed yet.");
  return {...sp, slippage_pct: n("p_slip") ?? 0, cost_pct: n("p_cost") ?? 0, max_trades_per_day: 2, daily_loss_limit_pct: 2.0};
}
async function runBt() {
  const c = DATA[$("ds").value]; if (!c) { $("btMsg").textContent = "pick a data set first"; return; }
  $("runBtn").disabled = true; $("btMsg").className = "small muted"; $("btMsg").textContent = "running…";
  try {
    const {data} = await api("/api/backtest", {method: "POST", body: JSON.stringify({instrument_id: c.instrument_id, interval: c.interval, date_from: $("d_from").value || null, date_to: $("d_to").value || null, capital: Number($("p_cap").value) || 100000, params: params()})});
    JOB = data.id;
    for (;;) {
      await new Promise(r => setTimeout(r, 700));
      const {data: j} = await api("/api/backtest/" + data.id);
      if (j.status === "running") { $("btMsg").textContent = `running… ${Math.round(j.elapsed)} s`; continue; }
      if (j.status === "error") throw new Error(j.error);
      $("btMsg").textContent = `finished in ${j.result.seconds} s`; show(j.result); break;
    }
  } catch (e) { $("btMsg").className = "small neg"; $("btMsg").textContent = e.message; }
  $("runBtn").disabled = false;
}
const pct = (v, d = 3) => v == null ? "–" : (v > 0 ? "+" : "") + Number(v).toFixed(d) + "%";
const rs = (v, sign = true) => { if (v == null) return "–"; const a = Math.abs(v), d = a < 1000 ? 2 : 0;
  return (v < 0 ? "−" : sign && v > 0 ? "+" : "") + "₹" + a.toLocaleString("en-IN", {minimumFractionDigits: d, maximumFractionDigits: d}); };
const kpi = (l, v, c, sub, tip = "") => `<div class="kpi" title="${esc(tip)}"><span>${l}</span><b class="${c}">${v}</b><small>${sub}</small></div>`;
const stat = (l, v, c = "", tip = "") => `<div class="stat" title="${esc(tip)}"><span>${l}</span><b class="${c}">${v}</b></div>`;
// the quantity is chosen by the strategy for every trade (from its maximum loss): show the range actually used
const qtyText = tl => { const q = [...new Set(tl.map(t => t.qty).filter(v => v != null))].sort((a, b) => a - b); return !q.length ? "" : q.length === 1 ? `quantity ${q[0]} per trade` : `quantity chosen by the strategy: ${q[0]} to ${q[q.length - 1]} per trade`; };
function show(r) {
  lastResult = r; $("res").style.display = "";
  const p = r.params;
  $("resTitle").textContent = `${r.instrument} · ${r.interval} · ${r.from_date} → ${r.to_date}`;
  const spec = (STRAT.list || []).find(x => x.name === p.strategy);
  $("resSub").textContent = `${spec ? spec.label : p.strategy} · ${Object.keys(p.options || {}).length ? "settings: " + Object.entries(p.options).map(([k, v]) => `${k}=${v}`).join(", ") + " · " : "rules fixed in the strategy file · "}${qtyText(r.trade_list)} · slippage ${p.slippage_pct}% per side · cost ${p.cost_pct}%`;
  const sig = r.trades >= 30 && r.t_stat != null && r.t_stat > 2 && r.mean > 0;
  const cls2 = r.mean == null ? "meh" : sig ? "good" : r.mean > 0 ? "meh" : "bad";
  const msg = r.mean == null ? "No trades were taken in this data." : sig ? "Positive on average and statistically significant. Still check that both halves agree, that costs are realistic, and test other data before trusting it." :
    r.mean > 0 ? "Positive on average, but not statistically significant: this could easily be luck." : "Negative on average after costs.";
  $("verdict").innerHTML = `<div class="verdict ${cls2}"><b>${msg}</b>${r.warnings.map(w => "<br>• " + esc(w)).join("")}${r.dropped_sessions.length ? `<br>• ${r.dropped_sessions.length} incomplete session(s) skipped.` : ""}</div>`;
  let run = 0; r.trade_list.forEach(t => { run += t.net_pnl; t.cum = run; });
  $("kpi").innerHTML = r.net_pnl == null ? "" : [
    kpi("Total P&L", rs(r.net_pnl), cls(r.net_pnl), `${pct(r.net_pnl_pct, 2)} of ₹${Number(r.capital).toLocaleString("en-IN")}`, "net of slippage and costs, for the quantity entered"),
    kpi("Max drawdown", r.max_drawdown_rs ? rs(-r.max_drawdown_rs, false) : "₹0", r.max_drawdown_rs ? "neg" : "", `${r.max_drawdown_rs_pct}% of peak equity`, "largest fall from a peak of the closed-trade equity"),
    kpi("Profitable trades", r.win_rate == null ? "–" : r.win_rate + "%", "", `${r.wins} of ${r.trades}`),
    kpi("Profit factor", r.profit_factor ?? "–", "", "gross profit ÷ gross loss"),
    kpi("Average trade", r.trades ? rs(r.net_pnl / r.trades) : "–", cls(r.net_pnl), pct(r.mean, 3) + " net per trade")].join("");
  $("cards").innerHTML = [stat("Trades", r.trades, "", `${r.sessions} sessions tested, ${r.sessions_with_trade} with a trade`), stat("Win rate", r.win_rate == null ? "–" : r.win_rate + "%"),
    stat("Average trade (net)", pct(r.mean), cls(r.mean), "mean of net % per trade, after slippage and costs"), stat("Total (net, summed)", pct(r.total, 2), cls(r.total)),
    stat("t-statistic", r.t_stat ?? "–", "", "mean / standard error; below 2 in size means not distinguishable from zero"), stat("Profit factor", r.profit_factor ?? "–", "", "gross profit / gross loss"),
    stat("Avg win / avg loss", `${pct(r.avg_win, 2)} / ${pct(r.avg_loss, 2)}`), stat("Max drawdown", "-" + r.max_drawdown + "%", "neg", "largest peak-to-trough fall of the cumulative result"),
    stat("First half average", pct(r.first_half_mean), cls(r.first_half_mean), `trades before ${r.split_date}`), stat("Second half average", pct(r.second_half_mean), cls(r.second_half_mean), `trades from ${r.split_date}`),
    stat("Sharpe (per session)", r.sharpe_sessions ?? "–", "", "annualised from the daily net result of every tested session")].join("");
  const tbl = (id, rows, h) => $(id).innerHTML = `<thead><tr><th>${h}</th><th class="num">Trades</th><th class="num">Win %</th><th class="num">Avg</th><th class="num">Total</th></tr></thead><tbody>${rows.map(x => `<tr><td>${esc(x.key)}</td><td class="num">${x.trades}</td><td class="num">${x.win_rate}</td><td class="num ${cls(x.mean)}">${pct(x.mean, 2)}</td><td class="num ${cls(x.total)}">${pct(x.total, 1)}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">–</td></tr>'}</tbody>`;
  tbl("tSide", r.by_side, "Side"); tbl("tExit", r.by_exit, "Exit"); tbl("tMonth", r.by_month, "Month");
  const shown = r.trade_list.slice(-500);
  $("tradeNote").textContent = r.trade_list.length > shown.length ? `(latest ${shown.length} of ${r.trade_list.length})` : `(${r.trade_list.length})`;
  $("tTrades").innerHTML = `<thead><tr><th>Date</th><th>Side</th><th class="num">Qty</th><th>In</th><th>Out</th><th class="num">Entry</th><th class="num">Exit</th><th>Exit reason</th><th class="num">Net ₹</th><th class="num">Net %</th><th class="num">Cumulative ₹</th></tr></thead><tbody>${shown.map(t => `<tr class="pick" onclick="pickDay('${t.date}')" title="open this day on the chart"><td class="nw">${t.date}</td><td>${t.side}</td><td class="num">${t.qty ?? ""}</td><td>${t.entry_ts}</td><td>${t.exit_ts}</td><td class="num">${fmt(t.entry)}</td><td class="num">${fmt(t.exit)}</td><td>${t.exit_reason}</td><td class="num ${cls(t.net_pnl)}">${rs(t.net_pnl)}</td><td class="num ${cls(t.net_pct)}">${pct(t.net_pct, 2)}</td><td class="num ${cls(t.cum)}">${rs(t.cum, false)}</td></tr>`).join("")}</tbody>`;
  drawEq(r); setupDays(r);
}
function drawEq(r) {
  const cv = $("eq"), dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight; cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext("2d"); g.scale(dpr, dpr); const css = getComputedStyle(document.documentElement), col = n => css.getPropertyValue(n).trim();
  const pts = (r.equity_rs || []).map(([t, v]) => [Date.parse(t.replace(" ", "T")), v]);
  g.font = "11px system-ui";
  if (!pts.length) { g.fillStyle = col("--muted"); g.fillText("no trades", 10, h / 2); return; }
  const t0 = Date.parse(r.from_date), t1 = Date.parse(r.to_date) + 86400000, L = 70, R = 10, pw = w - L - R, X = t => L + (t - t0) / (t1 - t0) * pw;
  const eqTop = 8, eqBot = Math.round(h * 0.66), ddTop = eqBot + 14, ddBot = h - 20;
  const steps = [[t0, 0]]; pts.forEach(([t, v]) => { steps.push([t, steps[steps.length - 1][1]], [t, v]); }); steps.push([t1, pts[pts.length - 1][1]]);
  const vals = steps.map(p => p[1]), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), pad = (hi - lo) * 0.08 || 1, Y = v => eqBot - (v - (lo - pad)) / ((hi + pad) - (lo - pad)) * (eqBot - eqTop);
  const up = pts[pts.length - 1][1] >= 0, line = up ? col("--green") : col("--red");
  g.strokeStyle = col("--line"); g.fillStyle = col("--muted");
  for (let k = 0; k <= 4; k++) { const v = lo - pad + (hi + pad - (lo - pad)) * k / 4; g.beginPath(); g.moveTo(L, Y(v)); g.lineTo(w - R, Y(v)); g.stroke(); g.fillText(rs(v, false).replace(/\.\d+$/, ""), 4, Y(v) + 3); }
  g.setLineDash([4, 3]); g.strokeStyle = col("--ink2"); g.beginPath(); g.moveTo(L, Y(0)); g.lineTo(w - R, Y(0)); g.stroke(); g.setLineDash([]);
  g.beginPath(); steps.forEach(([t, v], i) => i ? g.lineTo(X(t), Y(v)) : g.moveTo(X(t), Y(v))); g.lineTo(X(t1), Y(0)); g.lineTo(X(t0), Y(0)); g.closePath();
  g.globalAlpha = 0.12; g.fillStyle = line; g.fill(); g.globalAlpha = 1;
  g.strokeStyle = line; g.lineWidth = 1.8; g.beginPath(); steps.forEach(([t, v], i) => i ? g.lineTo(X(t), Y(v)) : g.moveTo(X(t), Y(v))); g.stroke(); g.lineWidth = 1;
  g.fillStyle = col("--ink2"); g.fillText("Equity (cumulative net P&L)", L + 4, eqTop + 11);
  // drawdown panel: how far below the previous equity peak the closed-trade equity is, over time
  let peak = 0; const dd = [[t0, 0]]; pts.forEach(([t, v]) => { peak = Math.max(peak, v); dd.push([t, dd[dd.length - 1][1]], [t, peak - v]); }); dd.push([t1, dd[dd.length - 1][1]]);
  const mdd = Math.max(...dd.map(p => p[1])) || 1, DY = v => ddTop + v / mdd * (ddBot - ddTop);
  g.strokeStyle = col("--line"); g.beginPath(); g.moveTo(L, ddTop); g.lineTo(w - R, ddTop); g.stroke();
  g.beginPath(); dd.forEach(([t, v], i) => i ? g.lineTo(X(t), DY(v)) : g.moveTo(X(t), DY(v))); g.lineTo(X(t1), ddTop); g.lineTo(X(t0), ddTop); g.closePath();
  g.globalAlpha = 0.22; g.fillStyle = col("--red"); g.fill(); g.globalAlpha = 1;
  g.fillStyle = col("--ink2"); g.fillText("Drawdown", L + 4, ddTop + 11); g.fillStyle = col("--muted"); g.fillText(mdd > 1 || r.max_drawdown_rs ? rs(-mdd, false).replace(/\.\d+$/, "") : "", 4, ddBot);
  for (let k = 0; k <= 4; k++) { const t = t0 + (t1 - t0) * k / 4; g.fillText(new Date(t).toISOString().slice(0, 10), Math.min(X(t) - (k === 4 ? 66 : k ? 30 : 0), w - 70), h - 4); }
  $("eqNote").textContent = `${pts.length} closed trades. Equity counts closed trades only (steps at each exit), in rupees, after slippage and costs. ${qtyText(r.trade_list)}.`;
}
function setupDays(r) {
  const days = Object.keys(r.day_levels || {}).sort(), by = {};
  r.trade_list.forEach(t => { (by[t.date] = by[t.date] || []).push(t); });
  $("daySel").innerHTML = days.map(d => { const ts = by[d] || [], sum = ts.reduce((a, t) => a + t.net_pnl, 0);
    return `<option value="${d}">${d} · ${ts.length ? ts.length + " trade" + (ts.length > 1 ? "s" : "") + " · " + rs(sum) : "no trade"}</option>`; }).join("");
  const withTrade = days.filter(d => by[d]); $("daySel").value = withTrade.length ? withTrade[withTrade.length - 1] : days[days.length - 1];
  loadDay();
}
function pickDay(d) { $("daySel").value = d; loadDay(); $("daySel").scrollIntoView({behavior: "smooth", block: "center"}); }
function stepDay(k) { const s = $("daySel"), i = s.selectedIndex + k; if (i >= 0 && i < s.options.length) { s.selectedIndex = i; loadDay(); } }
async function loadDay() {
  const d = $("daySel").value; if (!d || !JOB) return; $("dayMsg").textContent = "loading…";
  try { const {data} = await api(`/api/backtest/${JOB}/day?date=${d}`); lastDay = data; $("dayMsg").textContent = ""; drawDay(data); }
  catch (e) { $("dayMsg").className = "small neg"; $("dayMsg").textContent = e.message; }
}
function drawDay(v) {
  const box = $("dayChart"), cs = v.candles;
  if (!cs.length) { box.innerHTML = '<div class="muted small" style="padding:10px">No candles stored for that day.</div>'; $("dayTrades").innerHTML = ""; return; }
  const css = getComputedStyle(document.documentElement), col = n => css.getPropertyValue("--" + n).trim() || css.getPropertyValue("--ink2").trim();
  const W = Math.max(360, box.clientWidth || 900), H = 460, L = 58, R = 150, T = 14, B = 26, pw = W - L - R, ph = H - T - B;
  let lo = Math.min(...cs.map(c => c[3])), hi = Math.max(...cs.map(c => c[2])); const span = (hi - lo) || 1, c0lo = lo, c0hi = hi;
  (v.levels || []).forEach(l => { if (/^prev/.test(l.name) || (l.value > c0lo - span * 0.7 && l.value < c0hi + span * 0.7)) { lo = Math.min(lo, l.value); hi = Math.max(hi, l.value); } });     // always include prev High / prev Low, plus the lines near the price (not far-away stops)
  lo -= span * 0.06; hi += span * 0.06;
  const y = p => T + (hi - p) / (hi - lo) * ph, step = pw / cs.length, x = i => L + (i + 0.5) * step, idx = new Map(cs.map((c, i) => [c[0], i]));
  const ix = dt => { const k = dt.slice(11, 16); if (idx.has(k)) return idx.get(k); let b = 0; for (let i = 0; i < cs.length; i++) if (cs[i][0] <= k) b = i; return b; };
  const f2 = n => Number(n).toLocaleString("en-IN", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  let s = "";
  for (let k = 0; k <= 5; k++) { const p = lo + (hi - lo) * k / 5; s += `<line x1="${L}" x2="${L + pw}" y1="${y(p)}" y2="${y(p)}" stroke="${col("line")}"/><text x="${L - 4}" y="${y(p) + 3}" font-size="10" text-anchor="end" fill="${col("muted")}">${f2(p)}</text>`; }
  let lastX = -999; cs.forEach((c, i) => { if (x(i) - lastX >= 70) { lastX = x(i); s += `<text x="${x(i)}" y="${H - 8}" font-size="10" text-anchor="middle" fill="${col("muted")}">${c[0]}</text>`; } });
  const bw = Math.max(1, step * 0.7);
  cs.forEach((c, i) => { const c0 = c[4] >= c[1] ? col("green") : col("red"); s += `<line x1="${x(i)}" x2="${x(i)}" y1="${y(c[2])}" y2="${y(c[3])}" stroke="${c0}"/><rect x="${x(i) - bw / 2}" y="${Math.min(y(c[1]), y(c[4]))}" width="${bw}" height="${Math.max(1, Math.abs(y(c[1]) - y(c[4])))}" fill="${c0}"/>`; });
  const hidden = [];
  (v.levels || []).forEach(l => { if (l.value <= lo || l.value >= hi) { hidden.push(`${l.name} ${f2(l.value)}`); return; }
    const c0 = col(l.color || "muted"), solid = /^prev/.test(l.name);
    s += `<line x1="${L}" x2="${L + pw}" y1="${y(l.value)}" y2="${y(l.value)}" stroke="${c0}" stroke-width="${solid ? 1.6 : 1}" ${solid ? "" : 'stroke-dasharray="5 4"'}/><text x="${L + pw + 4}" y="${y(l.value) - 3}" font-size="10" fill="${c0}">${esc(l.name)} ${f2(l.value)}</text>`; });
  v.trades.forEach(t => {
    const a = ix(t.entry_dt), b = ix(t.exit_dt), win = t.net_pnl > 0, c0 = win ? col("green") : col("red"), buy = t.side === "BUY", ex = x(a), ey = y(t.entry), xx = x(b), xy = y(t.exit);
    const rail = (p, c1) => p == null || p <= lo || p >= hi ? "" : `<line x1="${ex}" x2="${Math.max(xx, ex + 8)}" y1="${y(p)}" y2="${y(p)}" stroke="${c1}" stroke-width="2" stroke-dasharray="2 3"/>`;
    const tip = `${t.side} ${t.entry_dt.slice(11, 16)} @ ${f2(t.entry)} → ${t.exit_reason} ${t.exit_dt.slice(11, 16)} @ ${f2(t.exit)} · ${rs(t.net_pnl)} (${pct(t.net_pct, 2)})`;
    s += `<g><title>${esc(tip)}</title>${rail(t.target, col("green"))}${rail(t.stop, col("red"))}<line x1="${ex}" y1="${ey}" x2="${xx}" y2="${xy}" stroke="${c0}" stroke-width="2" stroke-dasharray="6 3"/>` +
      (buy ? `<path d="M${ex} ${ey - 1} l-6 11 h12 z" fill="${col("blue")}"/>` : `<path d="M${ex} ${ey + 1} l-6 -11 h12 z" fill="${col("orange")}"/>`) +
      `<circle cx="${xx}" cy="${xy}" r="5" fill="${c0}" stroke="${col("surface")}" stroke-width="1.5"/>` +
      `<text x="${ex + 8}" y="${buy ? ey + 20 : ey - 12}" font-size="11" font-weight="600" fill="${col("ink")}">${t.side} ${f2(t.entry)}</text>` +
      `<text x="${xx + 8}" y="${xy + (xy < ey ? -8 : 16)}" font-size="11" fill="${c0}">${esc(t.exit_reason)} ${rs(t.net_pnl)}</text></g>`;
  });
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Candles, levels and trades of ${v.date}">${s}</svg>`;
  $("dayTrades").innerHTML = `<b>${esc(v.symbol)} · ${v.interval} · ${v.date}</b> · ` + (v.trades.length ? v.trades.map(t =>
    `<span class="nw">${t.side} in ${t.entry_dt.slice(11, 16)} @ ${f2(t.entry)} → ${esc(t.exit_reason)} ${t.exit_dt.slice(11, 16)} @ ${f2(t.exit)}: <b class="${cls(t.net_pnl)}">${rs(t.net_pnl)}</b> (${pct(t.net_pct, 2)})</span>`).join(" · ") : "no trade this day") +
    (hidden.length ? `<div class="muted">Lines outside the view: ${esc(hidden.join(", "))}</div>` : "");
}
let rzT = null; window.addEventListener("resize", () => { clearTimeout(rzT); rzT = setTimeout(() => { if (lastResult) drawEq(lastResult); if (lastDay) drawDay(lastDay); }, 150); });
function dl() {
  if (!lastResult) return; const rows = [["date", "side", "qty", "entry_time", "exit_time", "entry", "exit", "exit_reason", "net_rs", "net_pct", "cumulative_rs"], ...lastResult.trade_list.map(t => [t.date, t.side, t.qty, t.entry_ts, t.exit_ts, t.entry, t.exit, t.exit_reason, t.net_pnl, t.net_pct, Math.round(t.cum * 100) / 100])];
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([rows.map(r => r.join(",")).join("\n")], {type: "text/csv"})); a.download = `backtest_${lastResult.symbol}_${lastResult.interval}.csv`; a.click();
}
boot("backtest", async () => { await initStrategyForm(() => { $("runBtn").disabled = STRAT.none; }); loadSets(); });
