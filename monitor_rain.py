#!/usr/bin/env python3
"""
Intraday floor-lock monitor for monthly rain markets.

The edge hypothesis (yours): the CLI climate report posts once a day (morning),
so precip that banks during the day isn't in the official number until tomorrow.
But the SAME airport gauge issues hourly METARs. So a live METAR-derived
month-to-date can cross a bucket threshold HOURS before the market -- which many
traders anchor to the daily report -- reacts. That lag is the floor-lock.

Feedback WITHOUT waiting for settlement: precip only accumulates, so the instant
live-MTD > threshold, that rung is a CERTAIN yes. If the market still asks < 99c,
that's a detected lag you can (a) trade and (b) measure how fast it closes.

    live_MTD = CLI month-to-date (through yesterday) + today's METAR accumulation

Today's accumulation uses precipitationLastHour from the routine hourly METAR
(minute >= 50), one reading per clock-hour, to avoid double-counting the 5-minute
rolling values some stations emit.

Honest caveats:
  * ASOS heated tipping-bucket gauges under-catch in very heavy/frozen precip;
    METAR precip can also be missing (M) -> live_MTD is a floor, may read low.
  * Tiny timing gaps around local midnight / CLI post time.
  * Others may watch METAR too; monthly rain is just less bot-saturated than temp.
  * A lock only appears when it's actively raining near a threshold -- most polls
    find nothing. That's expected; it's an opportunistic monitor.

Usage:
  python3 monitor_rain.py                       # one pass, all cities
  python3 monitor_rain.py --series KXRAINMIAM
  python3 monitor_rain.py --watch 900           # re-poll every 900s (your machine)
"""

import argparse
import json
import time
import urllib.request
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scan_monthly import load_ladder, fetch_mtd
from scan_forecast import COORDS, ALL

UA = {"User-Agent": "kalshi-fc/1.0 (+https://github.com/tbnmichaud-sys/kalshi-scanner)"}
# CLI code -> ASOS METAR station. Default "K"+code; override exceptions here.
ASOS_OVERRIDE = {}

# The CLI month-to-date only updates once a day (morning report). Cache it so a
# looping monitor doesn't re-fetch 2 NWS calls/city on every poll. TTL 3h covers
# the daily refresh with margin; only the METAR obs are polled frequently.
_MTD_CACHE = {}  # cli_code -> (value, note, fetched_at_epoch)


def cached_mtd(cli, ttl=10800):
    now = time.time()
    hit = _MTD_CACHE.get(cli)
    if hit and now - hit[2] < ttl:
        return hit[0], hit[1]
    val, note = fetch_mtd(cli)
    if val is not None:
        _MTD_CACHE[cli] = (val, note, now)
    return val, note


def nget(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return json.load(r)


def today_accumulation(cli_code, tz):
    """Inches of precip banked so far today (local), from routine hourly METAR."""
    sid = ASOS_OVERRIDE.get(cli_code, "K" + cli_code)
    try:
        o = nget(f"https://api.weather.gov/stations/{sid}/observations?limit=60")
    except Exception as e:
        return None, f"{sid}: {type(e).__name__}", 0.0
    local_today = datetime.now(ZoneInfo(tz)).date()
    per_hour = {}  # (hour) -> inches, one routine reading per clock-hour
    for f in o.get("features", []):
        p = f["properties"]
        ts = p.get("timestamp")
        v = (p.get("precipitationLastHour") or {}).get("value")
        if not ts or v is None:
            continue
        dt = datetime.fromisoformat(ts).astimezone(ZoneInfo(tz))
        if dt.date() != local_today or dt.minute < 50:
            continue  # today only; routine hourly ob only (skip 5-min rollups)
        per_hour[dt.hour] = v / 25.4  # keep one per hour
    latest_hr = 0.0  # most recent 1h precip -> "is it raining right now"
    for f in o.get("features", []):  # newest first
        v = (f["properties"].get("precipitationLastHour") or {}).get("value")
        if v is not None:
            latest_hr = v / 25.4
            break
    return sum(per_hour.values()), sid, latest_hr


def poll(series, fee):
    if series not in COORDS:
        return
    lat, lon, tz = COORDS[series]
    etk, rungs, cli = load_ladder(series, 0)
    if not rungs or not cli:
        print(f"{series}: no ladder/station", file=sys.stderr)
        return
    base, _ = cached_mtd(cli)
    if base is None:
        print(f"{series}: CLI MTD unavailable", file=sys.stderr)
        return
    incr, sid, _latest = today_accumulation(cli, tz)
    if incr is None:
        print(f"{series}: METAR unavailable ({sid})", file=sys.stderr)
        incr = 0.0
    live = base + incr
    now = datetime.now(ZoneInfo(tz)).strftime("%H:%M %Z")

    tag = f"  live-MTD {live:.2f}\" (CLI {base:.2f} + today {incr:.2f})"
    print(f"\n{series} {etk} {sid} {now}{tag}")
    for r in rungs:
        if live > r["x"]:
            status = "LOCKED (live)" if base <= r["x"] else "settled(CLI)"
            edge = 100 - r["ask"]
            hot = ""
            if base <= r["x"] and r["ask"] < 99 - fee:
                hot = f"  >>> FLOOR-LOCK: buy YES @ {r['ask']}c for +{edge - fee:.0f}c net"
            print(f"  >{r['x']:<5} {r['bid']:>3}/{r['ask']:<3} vol {r['vol']:>7.0f}"
                  f"  {status}{hot}")
        else:
            gap = r["x"] - live
            near = "  <- one storm away" if gap <= 0.5 else ""
            print(f"  >{r['x']:<5} {r['bid']:>3}/{r['ask']:<3} vol {r['vol']:>7.0f}"
                  f"  needs +{gap:.2f}\"{near}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=ALL)
    ap.add_argument("--fee", type=float, default=2, help="fee buffer, cents")
    ap.add_argument("--watch", type=int, default=0, help="re-poll every N seconds")
    a = ap.parse_args()
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
        print("=" * 68 + f"\nPOLL {stamp}\n" + "=" * 68)
        for s in a.series:
            try:
                poll(s, a.fee)
            except Exception as e:
                print(f"{s}: ERROR {type(e).__name__}: {e}", file=sys.stderr)
        if a.watch <= 0:
            break
        print(f"\n[sleeping {a.watch}s -- Ctrl-C to stop]")
        time.sleep(a.watch)


if __name__ == "__main__":
    main()
