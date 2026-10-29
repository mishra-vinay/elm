"""Test the order-book/volume ideas on recorded depth data.

    python3 scripts/depth_research.py --db data/depth.db                 # NATGASMINI Nov (Dhan id 574320)
    python3 scripts/depth_research.py --db depth.db --out docs/depth-research.md

Pull the server file first (stop nothing, SQLite copes with readers):
    scp ubuntu@3.106.207.13:/opt/dryrun/data/depth.db* .
Needs only the Python standard library. Writes a markdown report and a CSV of every simulated trade.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json

from dryrun.depth_research import RULES, run_all, to_json, write_report      # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/depth.db")
    ap.add_argument("--security-id", default="574320")
    ap.add_argument("--bar-seconds", type=int, default=10)
    ap.add_argument("--tick", type=float, default=0.1, help="price tick of the instrument (NATGASMINI 0.1)")
    ap.add_argument("--fee-pct", type=float, default=0.06, help="round-trip fee in percent of price (futures convention used elsewhere in this repo)")
    ap.add_argument("--stop-ticks", type=float, default=8.0)
    ap.add_argument("--out", default="data/depth_research_report.md")
    ap.add_argument("--json", default="", help="also write the results (without trades) as JSON, used by the dashboard page")
    a = ap.parse_args()
    if not Path(a.db).exists():
        sys.exit("no such file: %s" % a.db)
    res = run_all(a.db, a.security_id, a.bar_seconds, a.tick, a.fee_pct, a.stop_ticks)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    csv_path = str(Path(a.out).with_suffix(".trades.csv"))
    text = write_report(res, a.out, csv_path)
    if a.json:
        Path(a.json).write_text(json.dumps(to_json(res), allow_nan=False))
    print(text if not a.json else "report written")
    print("report: %s\ntrades: %s\n%d variants, %d day(s) recorded" % (a.out, csv_path, len(RULES), len(res["days"])))


if __name__ == "__main__":
    main()
