"""Research harness for the recorded market depth (data/depth.db). Standard library only (Python 3.9+).

It turns the recorded updates into fixed-size bars (default 10 s), computes signals that use only data up to the end
of the bar, and "trades" them against the recorded best bid/offer: a long enters at the offer and leaves at the bid,
a short the reverse, plus a round-trip fee. So the spread is paid, which is where most thin edges disappear.

Every rule and parameter is fixed in RULES below BEFORE looking at results. The report splits the recorded days in
half (first half = train, second half = test; one day only: first/second half of that day) and flags a rule only if it
is positive in both halves, has enough trades, works on most days and clears a high t-statistic, because with ~40 variants
some will look good by chance. Descriptive research, not advice; nothing here places orders.
"""
from __future__ import annotations

import csv
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class Bar:
    day: str
    t: int                       # bucket start, epoch ms
    o: float = 0.0
    h: float = 0.0
    l: float = 0.0
    c: float = 0.0
    vol: int = 0
    buyv: int = 0
    sellv: int = 0
    imb: float = 0.0             # (bid5 - ask5) / (bid5 + ask5) at the end of the bar
    bid_first: float = 0.0
    ask_first: float = 0.0
    bid_last: float = 0.0
    ask_last: float = 0.0
    n: int = 0                   # updates in the bar (0 = carried forward, no update)


def load_bars(db: str, sid: str, bar_s: int = 10) -> dict:
    """{day: [dense bars]} from depth.db for one security id."""
    con = sqlite3.connect(db)                       # read-only use; a WAL file needs a normal open to read
    cur = con.execute("SELECT recv_ms, substr(recv_ts,1,10), ltp, volume, b1p, a1p, "
                      "(b1q+b2q+b3q+b4q+b5q), (a1q+a2q+a3q+a4q+a5q) FROM depth WHERE sid=? ORDER BY id", (str(sid),))
    ms = bar_s * 1000
    days: dict = defaultdict(dict)
    vm_day, prev = {}, {}
    for recv_ms, day, ltp, vol, bp, ap, b5, a5 in cur:
        if not ltp or ltp <= 0 or not bp or not ap or bp <= 0 or ap <= 0:
            continue
        vm_prev = vm_day.get(day)
        vm = vol if vm_prev is None else max(vm_prev, vol)
        dv = 0 if vm_prev is None else vm - vm_prev
        vm_day[day] = vm
        pp = prev.get(day)
        k = recv_ms // ms
        b = days[day].get(k)
        if b is None:
            b = days[day][k] = Bar(day, k * ms, ltp, ltp, ltp, ltp, bid_first=bp, ask_first=ap)
        b.h, b.l, b.c, b.n = max(b.h, ltp), min(b.l, ltp), ltp, b.n + 1
        b.bid_last, b.ask_last = bp, ap
        b.imb = (b5 - a5) / (b5 + a5) if b5 + a5 else 0.0
        if dv > 0 and pp:
            b.vol += dv
            if ltp >= pp[0]:
                b.buyv += dv
            elif ltp <= pp[1]:
                b.sellv += dv
        prev[day] = (ap, bp)
    con.close()
    out = {}
    for day, bars in days.items():
        ks = sorted(bars)
        dense, last = [], None
        for k in range(ks[0], ks[-1] + 1):
            b = bars.get(k)
            if b is None:                                   # no update in this bar: carry the last quote forward
                b = Bar(day, k * ms, last.c, last.c, last.c, last.c, imb=last.imb, bid_first=last.bid_last, ask_first=last.ask_last,
                        bid_last=last.bid_last, ask_last=last.ask_last)
            dense.append(b)
            last = b
        out[day] = dense
    return dict(sorted(out.items()))


def hhmm(t_ms: int) -> int:
    d = datetime.fromtimestamp(t_ms / 1000, IST)
    return d.hour * 60 + d.minute


@dataclass
class Trade:
    rule: str
    day: str
    side: int
    t_in: int
    t_out: int
    entry: float
    exit: float
    reason: str
    gross_pts: float = 0.0
    net_pct: float = 0.0


# --------------------------------------------------------------------------- rules (fixed before looking at results)
# window = minutes of trailing data; hold = minutes; each variant is a separate test
RULES = []


def _add(name, kind, direction, **kw):
    for hold in (2, 5, 10):
        RULES.append(dict(name="%s, hold %dm" % (name, hold), kind=kind, dir=direction, hold=hold, **kw))


for th in (0.3, 0.5):
    _add("flow-follow: net aggressive volume share >= %.1f over 3 min" % th, "flow", +1, th=th, win=3)
    _add("flow-fade:   net aggressive volume share >= %.1f over 3 min" % th, "flow", -1, th=th, win=3)
for th in (0.25, 0.4):
    _add("queue-with:  5-level imbalance >= %.2f over 1 min (price goes toward the thin side)" % th, "imb", +1, th=th, win=1)
    _add("queue-against: 5-level imbalance >= %.2f over 1 min (contrarian)" % th, "imb", -1, th=th, win=1)
_add("absorption: volume spike, price stuck (<=1 tick), 70%+ one-sided -> fade", "absorb", +1)
_add("spike-follow: volume spike with >=2 tick move -> go with it", "spike", +1)
_add("spike-fade:   volume spike with >=2 tick move -> go against it", "spike", -1)
_add("level-break: near previous High/Low (0.1%) with flow toward the level", "level_break", +1, th=0.2, win=3)
_add("level-reject: near previous High/Low (0.1%) with flow away from the level", "level_reject", +1, th=0.2, win=3)


def _prefix(bars, f):
    p = [0.0]
    for b in bars:
        p.append(p[-1] + f(b))
    return p


def run_rule(rule: dict, bars: list, bar_s: int, tick: float, fee_pct: float, stop_ticks: float, prev_hl=None,
             warm_min: int = 5, last_entry=23 * 60, flat_by=23 * 60 + 20) -> list:
    n = len(bars)
    if n < 20:
        return []
    per_min = 60 // bar_s
    vol_p = _prefix(bars, lambda b: b.vol)
    dlt_p = _prefix(bars, lambda b: b.buyv - b.sellv)
    imb_p = _prefix(bars, lambda b: b.imb)
    vols = [b.vol for b in bars]
    hold_b = rule["hold"] * per_min
    trades, i = [], max(1, int(rule.get("win", 3) * per_min))
    warm_until = hhmm(bars[0].t) + warm_min
    while i < n - 2:
        b = bars[i]
        m = hhmm(b.t)
        side = 0
        if m >= warm_until and m <= last_entry:
            w = int(rule.get("win", 3) * per_min)
            lo = max(0, i + 1 - w)
            kind = rule["kind"]
            if kind in ("flow", "level_break", "level_reject"):
                v = vol_p[i + 1] - vol_p[lo]
                share = (dlt_p[i + 1] - dlt_p[lo]) / v if v >= 50 else 0.0           # needs real volume (>=50 contracts)
                if kind == "flow" and abs(share) >= rule["th"]:
                    side = (1 if share > 0 else -1) * rule["dir"]
                elif kind in ("level_break", "level_reject") and prev_hl and abs(share) >= rule["th"]:
                    hi, lw = prev_hl
                    near_hi, near_lo = b.c >= hi * 0.999, b.c <= lw * 1.001
                    if near_hi or near_lo:
                        toward = (share > 0 and near_hi) or (share < 0 and near_lo)
                        if kind == "level_break":
                            side = (1 if near_hi else -1) if toward else 0
                        else:
                            side = (-1 if near_hi else 1) if not toward else 0
            elif kind == "imb":
                avg = (imb_p[i + 1] - imb_p[lo]) / max(1, i + 1 - lo)
                if abs(avg) >= rule["th"]:
                    side = (1 if avg > 0 else -1) * rule["dir"]
            elif kind in ("absorb", "spike") and i >= 60:
                past = vols[i - 60:i]
                mu = sum(past) / 60
                sd = math.sqrt(sum((x - mu) ** 2 for x in past) / 60)
                if sd > 0 and b.vol > mu + 2 * sd and b.vol >= 30:
                    mv = b.c - b.o
                    if kind == "absorb" and abs(mv) <= tick * 1.01 and b.vol:
                        bs, ss = b.buyv / b.vol, b.sellv / b.vol
                        side = -1 if bs >= 0.7 else (1 if ss >= 0.7 else 0)
                    elif kind == "spike" and abs(mv) >= 2 * tick * 0.99:
                        side = (1 if mv > 0 else -1) * rule["dir"]
        if side == 0:
            i += 1
            continue
        e = bars[i + 1]
        entry = e.ask_first if side > 0 else e.bid_first
        j, reason = min(n - 1, i + 1 + hold_b), "time"
        for k in range(i + 2, j + 1):
            bk = bars[k]
            mark = bk.bid_last if side > 0 else bk.ask_last
            if (mark - entry) * side <= -stop_ticks * tick:
                j, reason = k, "stop"
                break
            if hhmm(bk.t) >= flat_by:
                j, reason = k, "close"
                break
        x = bars[j]
        exit_px = x.bid_last if side > 0 else x.ask_last
        gross = (exit_px - entry) * side
        trades.append(Trade(rule["name"], b.day, side, e.t, x.t, entry, exit_px, reason, gross,
                            100.0 * gross / entry - fee_pct))
        i = j + 1                                           # one position at a time
    return trades


def evaluate(trades: list) -> dict:
    if not trades:
        return {"n": 0}
    r = [t.net_pct for t in trades]
    n = len(r)
    mean = sum(r) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (n - 1)) if n > 1 else 0.0
    wins, losses = [x for x in r if x > 0], [-x for x in r if x < 0]
    per_day = defaultdict(float)
    for t in trades:
        per_day[t.day] += t.net_pct
    return {"n": n, "mean": mean, "total": sum(r), "win": 100.0 * len(wins) / n, "t": mean / (sd / math.sqrt(n)) if sd else 0.0,
            "pf": (sum(wins) / sum(losses)) if losses else float("inf") if wins else 0.0,
            "days": len(per_day), "days_pos": sum(1 for v in per_day.values() if v > 0), "gross_pts": sum(t.gross_pts for t in trades) / n,
            "worst_day": min(per_day.values())}


def split_trades(trades: list, days: list):
    """Train = first half of the recorded days, test = second half; with one day, first and second half of its trades by time."""
    if len(days) >= 2:
        cut = (len(days) + 1) // 2
        tr = set(days[:cut])
        return [t for t in trades if t.day in tr], [t for t in trades if t.day not in tr], "days"
    ts = sorted(trades, key=lambda t: t.t_in)
    h = len(ts) // 2
    return ts[:h], ts[h:], "time (one day only: no out-of-sample)"


def verdict(full: dict, tr: dict, te: dict, mode: str) -> str:
    if mode != "days":
        return "need more days"
    if not full.get("n") or tr.get("n", 0) < 20 or te.get("n", 0) < 20:
        return "too few trades"
    ok = (tr["mean"] > 0 and te["mean"] > 0 and abs(full["t"]) >= 3 and full["days_pos"] >= 0.6 * full["days"])
    return "WORTH A CLOSER LOOK" if ok else "no"


def run_all(db: str, sid: str, bar_s=10, tick=0.1, fee_pct=0.06, stop_ticks=8.0) -> dict:
    data = load_bars(db, sid, bar_s)
    days = list(data)
    prev = {}
    for a, b in zip(days, days[1:]):
        highs = [x.h for x in data[a]]
        lows = [x.l for x in data[a]]
        if len(data[a]) > 600:                               # needs a reasonably complete previous session
            prev[b] = (max(highs), min(lows))
    results = []
    for rule in RULES:
        trades = []
        for d in days:
            if rule["kind"].startswith("level") and d not in prev:
                continue
            trades += run_rule(rule, data[d], bar_s, tick, fee_pct, stop_ticks, prev.get(d))
        tr_t, te_t, mode = split_trades(trades, days)
        full, tr, te = evaluate(trades), evaluate(tr_t), evaluate(te_t)
        results.append({"rule": rule["name"], "full": full, "train": tr, "test": te, "mode": mode, "verdict": verdict(full, tr, te, mode), "trades": trades})
    return {"days": days, "bars": {d: len(v) for d, v in data.items()}, "results": results, "bar_s": bar_s, "tick": tick, "fee_pct": fee_pct,
            "stop_ticks": stop_ticks}


def _f(x, fmt="%.3f"):
    return "-" if x is None else fmt % x


def write_report(res: dict, path: str, trades_csv: Optional[str] = None) -> str:
    rows = res["results"]
    lines = ["# Depth research report", "",
             "Days recorded: %d (%s). Bars: %d s. Costs: spread paid (long in at the offer, out at the bid) + %.2f%% round-trip fee; stop %.0f ticks; tick %.2f."
             % (len(res["days"]), ", ".join(res["days"]) or "none", res["bar_s"], res["fee_pct"], res["stop_ticks"], res["tick"]), "",
             "%d rule variants were tested, so some will look good by chance. A rule is only flagged if it is positive in BOTH halves, has 20+ trades in each, "
             "|t| >= 3 over all trades, and is positive on at least 60%% of the days it traded. With fewer than 2 recorded days nothing can be flagged." % len(rows), ""]
    lines += ["| Rule | Trades | Days (+/traded) | Avg net % | Win % | PF | t | Train avg % (n) | Test avg % (n) | Verdict |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for r in sorted(rows, key=lambda x: -(x["full"].get("t", -99) if x["full"].get("n") else -99)):
        f, tr, te = r["full"], r["train"], r["test"]
        if not f.get("n"):
            lines.append("| %s | 0 | | | | | | | | no trades |" % r["rule"])
            continue
        lines.append("| %s | %d | %d/%d | %s | %.0f | %s | %.1f | %s (%d) | %s (%d) | %s |" % (
            r["rule"], f["n"], f["days_pos"], f["days"], _f(f["mean"]), f["win"], _f(f["pf"], "%.2f"), f["t"],
            _f(tr.get("mean")), tr.get("n", 0), _f(te.get("mean")), te.get("n", 0), r["verdict"]))
    lines += ["", "Reading: 'Avg net %' is the average result per trade after spread and fee. 'Train' and 'Test' are the first and second half of the "
              "recorded days. 'flow-fade' and 'queue-against' are the mirror images of 'flow-follow' and 'queue-with': if the 'with' version loses "
              "consistently the mirror wins before costs, but costs usually remove it."]
    text = "\n".join(lines) + "\n"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if trades_csv:
        with open(trades_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["rule", "day", "side", "t_in", "t_out", "entry", "exit", "reason", "gross_pts", "net_pct"])
            for r in rows:
                for t in r["trades"]:
                    w.writerow([t.rule, t.day, "BUY" if t.side > 0 else "SELL", datetime.fromtimestamp(t.t_in / 1000, IST).strftime("%H:%M:%S"),
                                datetime.fromtimestamp(t.t_out / 1000, IST).strftime("%H:%M:%S"), t.entry, t.exit, t.reason, round(t.gross_pts, 3), round(t.net_pct, 4)])
    return text
