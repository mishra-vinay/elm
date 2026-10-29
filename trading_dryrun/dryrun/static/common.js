// Shared by every page: helpers, header with navigation and status badges, login, instrument search, activity log.
const $ = id => document.getElementById(id);
const fmt = (x, d = 2) => x == null ? "–" : Number(x).toLocaleString("en-IN", {minimumFractionDigits: d, maximumFractionDigits: d});
const cls = x => x == null ? "" : x > 0 ? "pos" : x < 0 ? "neg" : "";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
let authMode = "none";
// poll on a timer, but not while the tab is hidden (saves the server and the laptop battery); refresh at once when it comes back
function every(fn, ms) {
  setInterval(() => { if (!document.hidden) fn(); }, ms);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) fn(); });
}
const known = {};

async function api(path, opts) {
  const r = await fetch(path, Object.assign({headers: {"Content-Type": "application/json"}}, opts));
  if (r.status === 401) { showLogin(); throw new Error("login required"); }
  const text = await r.text(); let data = null; try { data = JSON.parse(text); } catch (e) {}
  if (!r.ok && r.status !== 409) throw new Error((data && (data.detail || data.message)) || text);
  return {status: r.status, data};
}

const NAV = [["runs", "/", "Dry runs"], ["ticks", "/ticks", "Live ticks"], ["depth", "/depth", "Depth"], ["depthres", "/depth-research", "Depth research"], ["backtest", "/backtest", "Backtest"], ["strategies", "/strategies", "Strategies"], ["market", "/market-data", "Market data"], ["settings", "/settings", "Settings"]];
function renderHeader(active) {
  $("hdr").innerHTML = `<header><h1>Dry-run trading</h1>
    <nav>${NAV.map(([k, h, t]) => `<a href="${h}" class="${k === active ? "on" : ""}">${t}</a>`).join("")}</nav>
    <span class="badge" id="modeBadge">…</span><span class="badge ok" title="Real-order trading is not part of this phase">DRY RUN · no orders are ever sent</span>
    <span class="badge" id="feedBadge">feed …</span><span class="badge" id="runsBadge"></span>
    <span class="muted small" id="clock"></span><span class="small neg" id="dbErr"></span><span style="flex:1"></span>
    <a class="small" href="/api/export/trades.csv">Download trades (CSV)</a></header>
    <div id="loginBox"><section><h2>Login</h2>
      <div id="pwForm"><label>Password</label><input type="password" id="pw"><p><button class="primary" onclick="login()">Log in</button></p></div>
      <div id="otpForm" style="display:none"><label>E-mail</label><input id="otpEmail" type="email">
        <p><button onclick="otpRequest()">Send code</button></p><label>6-digit code</label><input id="otpCode" inputmode="numeric">
        <p><button class="primary" onclick="otpVerify()">Verify</button></p></div>
      <div class="small neg" id="loginMsg"></div></section></div>`;
}
function showLogin() {
  $("app").style.display = "none"; $("loginBox").style.display = "block";
  $("pwForm").style.display = authMode === "password" ? "block" : "none"; $("otpForm").style.display = authMode === "email_otp" ? "block" : "none";
}
async function login() { try { await api("/api/auth/login", {method: "POST", body: JSON.stringify({password: $("pw").value})}); location.reload(); } catch (e) { $("loginMsg").textContent = e.message; } }
async function otpRequest() { try { await api("/api/auth/otp/request", {method: "POST", body: JSON.stringify({email: $("otpEmail").value})}); $("loginMsg").textContent = "If that address is allowed, a code was sent."; } catch (e) { $("loginMsg").textContent = e.message; } }
async function otpVerify() { try { await api("/api/auth/otp/verify", {method: "POST", body: JSON.stringify({email: $("otpEmail").value, code: $("otpCode").value})}); location.reload(); } catch (e) { $("loginMsg").textContent = e.message; } }

async function refreshStatus() {
  try {
    const {data: s} = await api("/api/status");
    $("clock").textContent = s.now + " IST"; $("modeBadge").textContent = s.mode === "sim" ? "SIMULATED FEED" : "Dhan live data";
    $("modeBadge").className = "badge " + (s.mode === "sim" ? "warn" : "ok");
    const f = s.feed; $("feedBadge").textContent = f.fatal ? "feed error" : f.connected ? `feed connected · ${f.subscribed} subscribed` + (f.last_message_age != null ? ` · ${f.last_message_age}s` : "") : "feed disconnected";
    $("feedBadge").className = "badge " + (f.fatal ? "bad" : f.connected ? "ok" : "warn");
    $("runsBadge").textContent = `${s.active_runs} active run${s.active_runs === 1 ? "" : "s"}`;
    $("dbErr").textContent = s.writer_errors ? `Database writer errors: ${s.writer_errors} (see the server log)` : "";
  } catch (e) { if (e.message !== "login required") $("feedBadge").textContent = "dashboard offline"; }
}

let searchTimer;
function wireSearch(inputId, listId, onPick) {
  $(inputId).addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = $(inputId).value; if (q.length < 2) return;
      if (known[q]) { onPick(known[q], [known[q]]); return; }                  // a label chosen from the dropdown
      try {
        const {data} = await api("/api/instruments/search?q=" + encodeURIComponent(q));
        data.forEach(i => known[i.label] = i);
        $(listId).innerHTML = data.map(i => `<option value="${esc(i.label)}"></option>`).join("");
        const hit = data.find(i => i.label === q); onPick(hit || (data.length === 1 ? data[0] : null), data);
      } catch (e) {}
    }, 250);
  });
}

let actFilter = "all";
function initActivity() {
  $("activity").innerHTML = `<h2>Activity log</h2><div class="row small" id="actFilters" style="margin-bottom:8px">
    <button data-f="all" class="primary">All</button><button data-f="run">Runs</button><button data-f="feed">Market feed</button>
    <button data-f="data">Data</button><button data-f="risk">Risk</button><button data-f="app">App</button><button data-f="errors">Errors</button>
    <span class="muted">newest first · refreshes every 3 s</span></div>
    <div class="tbl" style="max-height:360px;overflow:auto"><table id="actT"><thead><tr><th>Time</th><th>Source</th><th>Message</th></tr></thead><tbody></tbody></table></div>`;
  $("actFilters").addEventListener("click", e => {
    const f = e.target.dataset && e.target.dataset.f; if (!f) return;
    actFilter = f; document.querySelectorAll("#actFilters button").forEach(b => b.className = b.dataset.f === f ? "primary" : ""); refreshActivity();
  });
  refreshActivity(); every(refreshActivity, 3000);
}
async function refreshActivity() {
  try {
    const {data} = await api("/api/activity?limit=200&source=" + actFilter);
    $("actT").querySelector("tbody").innerHTML = data.map(r => `<tr><td class="nw">${String(r.ts).replace("T", " ").slice(0, 19)}</td><td class="nw">${r.source}</td><td class="${r.level === "error" ? "neg" : r.level === "warn" ? "warnc" : ""}">${esc(r.message)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">Nothing logged yet.</td></tr>';
  } catch (e) {}
}

// page = "runs" | "ticks" | "backtest" | "market" | "settings"; onReady runs once the user is logged in (or no login is needed)
async function boot(page, onReady) {
  renderHeader(page);
  const a = await (await fetch("/api/auth/status")).json(); authMode = a.mode;
  if (!a.authenticated) { showLogin(); return; }
  refreshStatus(); every(refreshStatus, 2000);
  if ($("activity")) initActivity();             // not every page shows the activity log
  onReady();
}

// ---- strategy form shared by the Dry runs and Backtest pages. Strategies and their settings come from /api/strategies, so nothing here names a strategy.
const STRAT = {list: [], none: true, onChange: null};
const curStrat = () => STRAT.list.find(x => x.name === ($("s_name") || {}).value);
function fieldHtml(f) {
  const id = "so_" + f.key, tip = f.help ? ` title="${esc(f.help)}"` : "";
  if (f.type === "select") return `<div><label${tip}>${esc(f.label)}</label><select id="${id}">${(f.choices || []).map(([v, t]) => `<option value="${esc(v)}"${String(v) === String(f.default) ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></div>`;
  return `<div><label${tip}>${esc(f.label)}</label><input id="${id}" type="${f.type === "number" ? "number" : "text"}"${f.step ? ` step="${f.step}"` : ""} value="${esc(f.default ?? "")}"></div>`;
}
function renderStrategyFields() {
  const spec = curStrat(); $("s_fields").innerHTML = spec ? spec.fields.map(fieldHtml).join("") : "";
  if ($("s_blurb")) $("s_blurb").textContent = spec ? spec.blurb || "" : "";
  if (STRAT.onChange) STRAT.onChange();
}
async function initStrategyForm(onChange) {
  STRAT.onChange = onChange; const {data} = await api("/api/strategies"); STRAT.list = data.strategies; STRAT.none = !STRAT.list.length;
  const box = $("stratBox");
  if (STRAT.none) { box.innerHTML = `<div class="help"><b>${esc(data.message || "No strategy is installed yet.")}</b><br>The feed, ticks, market data and settings pages work as usual. Once a strategy is added, its settings appear here.</div>`; if (onChange) onChange(); return; }
  box.innerHTML = `<div class="row" style="margin:2px 0 8px;align-items:flex-end"><div style="min-width:260px"><label>Strategy</label><select id="s_name">${STRAT.list.map(x => `<option value="${esc(x.name)}">${esc(x.label)}</option>`).join("")}</select></div></div><div class="small muted" id="s_blurb" style="margin:0 0 8px;max-width:900px"></div><div class="grid" id="s_fields"></div>`;
  $("s_name").addEventListener("change", renderStrategyFields);
  const changed = () => { if (STRAT.onChange) STRAT.onChange(); };
  box.addEventListener("input", changed); box.addEventListener("change", changed);
  renderStrategyFields();
}
function strategyParams() {
  const spec = curStrat(); if (!spec) return null; const options = {};
  spec.fields.forEach(f => { const v = $("so_" + f.key).value; options[f.key] = f.type === "number" ? (v === "" ? null : Number(v)) : v; });
  return {strategy: spec.name, options};
}
const optionsText = p => { const o = p.options && Object.keys(p.options).length ? p.options : null; return (p.strategy || "–") + (o ? ": " + Object.entries(o).map(([k, v]) => `${k}=${v}`).join(", ") : ""); };
function renderPreview(box, d) {
  if (d.error) { box.innerHTML = `<span class="neg">${esc(d.error)}</span>`; return; }
  if (!d.summary && !(d.cards || []).length) { box.innerHTML = '<span class="muted">This strategy has no calculation preview.</span>'; return; }
  box.innerHTML = (d.summary ? `<div class="small">${d.summary}</div>` : "") + `<div class="calcgrid">${(d.cards || []).map(c => `<div class="calcCard"><div><b>${esc(c.title)}</b></div><dl class="kvlist small" style="margin-top:4px">${c.rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl></div>`).join("")}</div>` +
    (d.notes || []).map(n => `<div class="small muted" style="margin-top:6px">${n}</div>`).join("");
}
