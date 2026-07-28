#!/usr/bin/env python3
"""
Trade journal for Kalshi monthly-rain learning bets.

Records each bet WITH the live market context at entry (bid/ask/MTD/needs), so
later you can grade calibration: did the rungs you bought resolve at the rate
their entry price implied? That's the only way to know if any signal is real.

This does NOT place orders -- you place them in the Kalshi app, then log here.

Log a bet (snapshots live context automatically):
  python3 log_bet.py --series KXRAINNYCM --threshold 4 --side yes \
                     --price 91 --count 1 --note "exp1: settlement convergence"

Show the journal:
  python3 log_bet.py --show

Settle a row once the month closes (fill result + realized pnl in cents):
  python3 log_bet.py --settle <row_id> --result yes    # or: no
"""

import argparse
import csv
import datetime
import os
import sys

from scan_monthly import load_ladder, fetch_mtd

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades.csv")
FIELDS = ["id", "ts_local", "series", "event", "station", "threshold", "side",
          "price_paid_c", "count", "entry_bid", "entry_ask", "entry_mtd",
          "needs_in", "note", "result", "pnl_c"]


def read_rows():
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="") as f:
        return list(csv.DictReader(f))


def write_rows(rows):
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def log_bet(a):
    etk, rungs, cli = load_ladder(a.series, 0)
    if not rungs:
        sys.exit(f"no ladder for {a.series}")
    rung = next((r for r in rungs if abs(r["x"] - a.threshold) < 1e-6), None)
    if not rung:
        sys.exit(f"no rung at >{a.threshold} in {a.series} "
                 f"(have: {[r['x'] for r in rungs]})")
    mtd, _ = (fetch_mtd(cli) if cli else (None, ""))
    needs = "" if mtd is None else (
        "SETTLED" if mtd > rung["x"] else f"{rung['x'] - mtd:.2f}")
    rows = read_rows()
    rows.append({
        "id": str(len(rows) + 1),
        "ts_local": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "series": a.series, "event": etk, "station": cli or "?",
        "threshold": a.threshold, "side": a.side, "price_paid_c": a.price,
        "count": a.count, "entry_bid": rung["bid"], "entry_ask": rung["ask"],
        "entry_mtd": "" if mtd is None else f"{mtd:.2f}", "needs_in": needs,
        "note": a.note, "result": "", "pnl_c": "",
    })
    write_rows(rows)
    print(f"logged #{len(rows)}: {a.series} >{a.threshold} {a.side} "
          f"{a.count}@{a.price}c  (live {rung['bid']}/{rung['ask']}, "
          f"MTD {rows[-1]['entry_mtd']}, needs {needs})")


def settle(a):
    rows = read_rows()
    row = next((r for r in rows if r["id"] == str(a.settle)), None)
    if not row:
        sys.exit(f"no row id {a.settle}")
    won = (a.result == row["side"])
    price, count = int(row["price_paid_c"]), int(row["count"])
    # YES pays 100c if the event happens; cost was price_paid. NO is symmetric.
    row["pnl_c"] = str((100 - price if won else -price) * count)
    row["result"] = a.result
    write_rows(rows)
    print(f"settled #{a.settle}: {row['result']} -> pnl {row['pnl_c']}c "
          "(excludes Kalshi fees -- subtract those yourself)")


def show():
    rows = read_rows()
    if not rows:
        print("no trades logged yet.")
        return
    print(f"{'id':>3} {'series':<12} {'thr':>4} {'side':<4} {'paid':>4} "
          f"{'cnt':>3} {'bid/ask':>8} {'MTD':>5} {'needs':>7} {'res':>4} "
          f"{'pnl':>6}  note")
    tot = 0
    for r in rows:
        ba = f"{r['entry_bid']}/{r['entry_ask']}"
        pnl = r["pnl_c"]
        if pnl:
            tot += int(pnl)
        print(f"{r['id']:>3} {r['series']:<12} {r['threshold']:>4} "
              f"{r['side']:<4} {r['price_paid_c']:>4} {r['count']:>3} {ba:>8} "
              f"{r['entry_mtd']:>5} {r['needs_in']:>7} {r['result']:>4} "
              f"{pnl:>6}  {r['note']}")
    print(f"\nrealized pnl (settled rows, pre-fees): {tot}c")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series")
    ap.add_argument("--threshold", type=float)
    ap.add_argument("--side", choices=["yes", "no"])
    ap.add_argument("--price", type=int, help="cents you actually paid")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--note", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--settle", type=int, metavar="ROW_ID")
    ap.add_argument("--result", choices=["yes", "no"])
    a = ap.parse_args()
    if a.show:
        show()
    elif a.settle is not None:
        if not a.result:
            sys.exit("--settle needs --result yes|no")
        settle(a)
    elif a.series and a.threshold is not None and a.side and a.price is not None:
        log_bet(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
