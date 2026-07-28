#!/usr/bin/env python3
"""
Background floor-lock watcher for monthly rain markets -- notify-only (Phase 1).

Runs forever while you work. Each cycle it:
  1. Polls every city's gauge (no forecast gate -- forecasts miss pop-up
     convection, and the point is to catch surprise rain they didn't call).
     Use --skip to drop perennially-dry cities (e.g. summer LAX/SFO/SEA).
  2. Computes live month-to-date (CLI-through-yesterday + today's accumulation).
  3. Detects a FLOOR-LOCK: a rung whose live-MTD has already crossed the
     threshold (certain YES) that the market still asks < 99c, with enough
     liquidity to fill. That's the CLI-vs-METAR lag = the edge.
  4. Notifies you ONCE per lock (desktop + optional webhook), deduped across
     restarts via a state file.

It only READS public data and NOTIFIES. It never authenticates, never places an
order -- so it cannot touch your Kalshi account. Auto-execution is Phase 2
(separate, opt-in, credential-gated). See place_bet() stub at the bottom.

Notifications:
  * Linux desktop via `notify-send` (if available).
  * Optional phone/chat push: set env KALSHI_NOTIFY_WEBHOOK to an ntfy.sh topic
    URL (https://ntfy.sh/your-topic) or a Slack incoming-webhook URL.

Run in background (survives terminal close):
  nohup python3 watcher.py > watcher.log 2>&1 &
  tail -f watcher.log
Or install the systemd --user unit in watcher.service (see README).

Usage:
  python3 watcher.py                 # defaults: fast 300s, idle 1800s
  python3 watcher.py --once          # single pass then exit (for cron/testing)
  python3 watcher.py --fee 2 --min-size 100 --pop-gate 25
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from scan_monthly import load_ladder, event_month
from scan_forecast import COORDS, ALL
from monitor_rain import cached_mtd, today_accumulation
from log_bet import FIELDS as J_FIELDS, CSV_PATH as J_PATH, read_rows as j_rows

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "watcher_state.json")     # deduped alerts
PREV_PATH = os.path.join(HERE, "watcher_prev.json")       # last cycle's snapshot
WEBHOOK = os.environ.get("KALSHI_NOTIFY_WEBHOOK", "")


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def _get(url, timeout=60):
    req = urllib.request.Request(
        url, headers={"User-Agent": "kalshi-fc/1.0 (+https://github.com/tbnmichaud-sys/kalshi-scanner)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


_ENS_CACHE = {}  # series -> (remainders_inches, fetched_at). Ensembles update
                 # ~4x/day, so cache to avoid re-pulling 71 members each cycle.


def ensemble_remainder(series, ttl=14400):
    """Per-member precip (inches) over the REMAINING days of the month, from the
    GEFS+ICON ensemble. Add to live-MTD to project the final monthly total.
    NOTE: raw ensemble under-forecasts convective extremes (dry-biased tail) --
    the delta it produces is a hypothesis to validate, not a proven edge."""
    hit = _ENS_CACHE.get(series)
    now = time.time()
    if hit and now - hit[1] < ttl:
        return hit[0]
    lat, lon, tz = COORDS[series]
    d = _get(f"https://ensemble-api.open-meteo.com/v1/ensemble?latitude={lat}"
             f"&longitude={lon}&daily=precipitation_sum&models=gfs025,icon_seamless"
             f"&forecast_days=16&timezone={tz.replace('/', '%2F')}")
    daily = d["daily"]
    times = daily["time"]
    cur = times[0][5:7]  # remaining days of the current month
    idx = [i for i, t in enumerate(times) if t[5:7] == cur]
    keys = [k for k in daily if k.startswith("precipitation_sum")]
    rems = [sum((daily[k][i] or 0.0) for i in idx) / 25.4 for k in keys]
    _ENS_CACHE[series] = (rems, now)
    return rems


_FC_TODAY = {}  # series -> (expected_inches_elapsed_today, fetched_at)


def forecast_today(series, ttl=7200):
    """Forecast precip (inches) for the hours of today that have ALREADY elapsed
    -- i.e. what the market would have expected to bank by now. The edge is
    actual-banked minus this: a positive surprise the priced-in forecast missed."""
    hit = _FC_TODAY.get(series)
    now = time.time()
    if hit and now - hit[1] < ttl:
        return hit[0]
    lat, lon, tz = COORDS[series]
    d = _get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
             f"&hourly=precipitation&forecast_days=1&timezone={tz.replace('/', '%2F')}")
    h = d["hourly"]
    now_hour = datetime.now(ZoneInfo(tz)).hour
    exp = 0.0
    for t, p in zip(h["time"], h["precipitation"]):
        if int(t[11:13]) <= now_hour:  # elapsed hours of today only
            exp += (p or 0.0)
    exp /= 25.4
    _FC_TODAY[series] = (exp, now)
    return exp


def city_locks(series, fee, min_size, approach_ask, approach_gap, nextlevel_edge):
    """Analyze one city. Returns a dict with:
      live/base/incr, raining (bool), rate (in/hr now),
      locks     -- Tier 1: rung crossed by live gauge, CLI lagging, still cheap
                   (CERTAIN yes, rare, small payout)
      approaches-- Tier 2: cheap+close+liquid rung WITH rain falling now
                   (SPECULATIVE: bet the storm delivers; you can lose)
      nearest   -- (threshold, gap, ask) of the closest unsettled rung
    """
    _, _, tz = COORDS[series]
    etk, rungs, cli = load_ladder(series, 0)
    if not rungs or not cli:
        return None
    # Floor math is only valid when the event covers the month the CLI MTD
    # describes -- i.e. the current month at the station. Skip future-month
    # events (nearest around rollover, or if the sort ever regresses).
    now_local = datetime.now(ZoneInfo(tz))
    em = event_month(etk)
    if em != (now_local.year, now_local.month):
        return None
    base, _ = cached_mtd(cli)
    if base is None:
        return None
    incr, _sid, rate = today_accumulation(cli, tz)
    incr = incr or 0.0
    raining = rate > 0
    live = base + incr
    locks, approaches, nextlevels, nearest, nearest_model = [], [], [], None, None
    unsettled = []  # every rung above live-MTD, for cycle-over-cycle diffing
    rems = None  # lazy ensemble remainder (fetched once per city, cached)
    for r in rungs:
        if r["x"] >= live:  # not yet crossed by the live gauge
            gap = r["x"] - live
            unsettled.append({"x": r["x"], "ask": r["ask"], "vol": r["vol"],
                              "gap": gap})
            if nearest is None or gap < nearest[1]:
                nearest = (r["x"], gap, r["ask"])
            # Tier 2: within reach, cheap, liquid, and it's raining NOW.
            if (raining and gap <= approach_gap and r["vol"] >= min_size
                    and fee < r["ask"] <= approach_ask):
                approaches.append({"series": series, "event": etk, "x": r["x"],
                                   "ask": r["ask"], "gap": gap, "vol": r["vol"],
                                   "rate": rate})
            # Tier 3: model P(reach this level) from live-MTD + ensemble
            # remainder, vs the market ask. Positive delta = market underprices.
            if r["vol"] >= min_size:
                if rems is None:
                    try:
                        rems = ensemble_remainder(series)
                    except Exception:
                        rems = []
                if rems:
                    p = sum(1 for rm in rems if live + rm > r["x"]) / len(rems)
                    delta = p * 100 - r["ask"]  # cents, buy-YES edge vs model
                    if nearest_model is None or gap < nearest_model[-1]:
                        nearest_model = (r["x"], r["ask"], p, delta, gap)
                    if delta >= nextlevel_edge:
                        nextlevels.append({"series": series, "event": etk,
                                           "x": r["x"], "ask": r["ask"],
                                           "model_p": p, "delta": delta,
                                           "vol": r["vol"]})
        elif base <= r["x"]:
            # Tier 1: crossed by live gauge but NOT yet by the lagging CLI
            # report, still cheap, enough size to fill.
            if r["ask"] < 99 - fee and r["vol"] >= min_size:
                locks.append({"series": series, "event": etk, "x": r["x"],
                              "ask": r["ask"], "bid": r["bid"], "vol": r["vol"],
                              "edge": 100 - r["ask"] - fee, "live": live})
    return {"event": etk, "live": live, "base": base, "incr": incr,
            "raining": raining, "rate": rate, "locks": locks,
            "approaches": approaches, "nextlevels": nextlevels,
            "nearest": nearest, "nearest_model": nearest_model,
            "unsettled": unsettled}


def notify(title, body):
    log(f"NOTIFY: {title} -- {body}")
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-u", "critical", title, body], timeout=5)
        except Exception as e:
            log(f"  notify-send failed: {e}")
    if WEBHOOK:
        try:
            if "slack" in WEBHOOK:
                data = json.dumps({"text": f"*{title}*\n{body}"}).encode()
                req = urllib.request.Request(WEBHOOK, data=data,
                                             headers={"Content-Type": "application/json"})
            else:  # ntfy.sh style: raw body, title in header
                req = urllib.request.Request(WEBHOOK, data=body.encode(),
                                             headers={"Title": title, "Priority": "high"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"  webhook failed: {e}")


def load_seen():
    if os.path.exists(STATE_PATH):
        try:
            return set(json.load(open(STATE_PATH)))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    json.dump(sorted(seen), open(STATE_PATH, "w"))


def load_prev():
    if os.path.exists(PREV_PATH):
        try:
            return json.load(open(PREV_PATH))
        except Exception:
            return {}
    return {}


def save_prev(snap):
    json.dump(snap, open(PREV_PATH, "w"))


def journal_signal(fields):
    """Append a fired signal to the shared trades.csv (log_bet format) as a
    paper entry, assuming a fill at the alert ask. Keeps log_bet.py --show and
    --settle working on auto-logged rows for season-long validation."""
    rows = j_rows()
    row = {k: "" for k in J_FIELDS}
    row.update(fields)
    row["id"] = str(len(rows) + 1)
    # setdefault is a no-op here (every key exists as ""), so fill explicitly.
    if row["count"] in ("", None):
        row["count"] = 1
    if not row["ts_local"]:
        row["ts_local"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    write_header = not os.path.exists(J_PATH)
    with open(J_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=J_FIELDS)
        if write_header:
            w.writeheader()
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", type=int, default=300, help="poll every N s")
    ap.add_argument("--fee", type=float, default=2, help="fee buffer, cents")
    ap.add_argument("--min-size", type=float, default=100, help="min rung volume to flag")
    ap.add_argument("--approach-ask", type=float, default=40,
                    help="Tier2: max ask (cents) for a rung to count as 'cheap'")
    ap.add_argument("--approach-gap", type=float, default=0.5,
                    help="Tier2: max inches short of a rung while raining")
    ap.add_argument("--nextlevel-edge", type=float, default=8,
                    help="Tier3: min cents the model must beat the ask by to flag")
    ap.add_argument("--bank-epsilon", type=float, default=0.05,
                    help="Tier4: min inches banked since last cycle to check for lag")
    ap.add_argument("--stale-tol", type=float, default=1,
                    help="Tier4: max cents a next-level ask may move and still count as 'stale'")
    ap.add_argument("--surprise-eps", type=float, default=0.10,
                    help="Tier4: min inches actual-banked must exceed forecast to be an edge")
    ap.add_argument("--no-journal", action="store_false", dest="journal",
                    help="don't auto-log fired signals to trades.csv (on by default)")
    ap.add_argument("--skip", nargs="*", default=[],
                    help="series to skip, e.g. summer-dry KXRAINLAXM KXRAINSFOM KXRAINSEAM")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()

    watch = [s for s in ALL if s not in a.skip]
    log(f"watcher start. webhook={'set' if WEBHOOK else 'none'} fee={a.fee} "
        f"min_size={a.min_size} watching {len(watch)} cities (no forecast gate)")
    seen = load_seen()
    prevsnap = load_prev()  # {event: {"mtd": live, "asks": {str(x): ask}}}
    while True:
        found = 0
        gnearest = None  # (series, threshold, gap, ask) closest across all cities
        gbest = None     # (series, x, ask, model_p, delta) biggest next-level delta
        newsnap = {}
        for s in watch:
            try:
                res = city_locks(s, a.fee, a.min_size, a.approach_ask,
                                 a.approach_gap, a.nextlevel_edge)
            except Exception as e:
                log(f"{s}: error {type(e).__name__}: {e}")
                continue
            if not res:
                continue
            if res["incr"] > 0:
                st = f"raining {res['rate']:.2f}\"/hr" if res["raining"] else "dry now"
                log(f"  {s}: live-MTD {res['live']:.2f}\" (+{res['incr']:.2f} today, {st})")
            nr = res["nearest"]
            if nr and (gnearest is None or nr[1] < gnearest[2]):
                gnearest = (s, nr[0], nr[1], nr[2])
            nm = res["nearest_model"]
            if nm and (gbest is None or nm[3] > gbest[4]):
                gbest = (s, nm[0], nm[1], nm[2], nm[3])

            for lk in res["locks"]:  # Tier 1 -- certain
                key = f"L:{lk['event']}|{lk['x']}"
                if key in seen:
                    continue
                found += 1
                notify("Kalshi FLOOR-LOCK (certain)",
                       f"{lk['series']} >{lk['x']}\" is SETTLED (live-MTD "
                       f"{lk['live']:.2f}\") but asks {lk['ask']}c -- buy YES for "
                       f"~+{lk['edge']:.0f}c net, vol {lk['vol']:.0f}")
                seen.add(key)
                save_seen(seen)
                if a.journal:
                    journal_signal({"series": lk["series"], "event": lk["event"],
                        "threshold": lk["x"], "side": "yes", "price_paid_c": lk["ask"],
                        "entry_bid": lk["bid"], "entry_ask": lk["ask"],
                        "entry_mtd": f"{lk['live']:.2f}", "needs_in": "SETTLED",
                        "note": f"AUTO Tier1 FLOOR-LOCK +{lk['edge']:.0f}c"})

            for ap_ in res["approaches"]:  # Tier 2 -- speculative
                key = f"A:{ap_['event']}|{ap_['x']}"
                if key in seen:
                    continue
                found += 1
                notify("Kalshi APPROACH (storm active -- NOT certain)",
                       f"{ap_['series']} >{ap_['x']}\" needs +{ap_['gap']:.2f}\" and "
                       f"it's raining {ap_['rate']:.2f}\"/hr now; rung asks "
                       f"{ap_['ask']}c. Speculative: worth it only if you think the "
                       f"storm delivers the rest -- you can lose. vol {ap_['vol']:.0f}")
                seen.add(key)
                save_seen(seen)
                if a.journal:
                    journal_signal({"series": ap_["series"], "event": ap_["event"],
                        "threshold": ap_["x"], "side": "yes", "price_paid_c": ap_["ask"],
                        "entry_ask": ap_["ask"], "entry_mtd": f"{res['live']:.2f}",
                        "needs_in": f"{ap_['gap']:.2f}",
                        "note": f"AUTO Tier2 APPROACH raining {ap_['rate']:.2f}in/hr"})

            for nl in res["nextlevels"]:  # Tier 3 -- predictive delta
                key = f"N:{nl['event']}|{nl['x']}"
                if key in seen:
                    continue
                found += 1
                notify("Kalshi NEXT-LEVEL (model > market -- unproven)",
                       f"{nl['series']} >{nl['x']}\": model P {nl['model_p']*100:.0f}% "
                       f"vs ask {nl['ask']}c (+{nl['delta']:.0f}c delta). Model thinks "
                       f"this level is likelier than priced -- but the ensemble is "
                       f"dry-biased, so treat as a lead to check, not a lock. "
                       f"vol {nl['vol']:.0f}")
                seen.add(key)
                save_seen(seen)
                if a.journal:
                    journal_signal({"series": nl["series"], "event": nl["event"],
                        "threshold": nl["x"], "side": "yes", "price_paid_c": nl["ask"],
                        "entry_ask": nl["ask"], "entry_mtd": f"{res['live']:.2f}",
                        "needs_in": f"{nl['x'] - res['live']:.2f}",
                        "note": (f"AUTO Tier3 model {nl['model_p']*100:.0f}% vs "
                                 f"{nl['ask']}c (+{nl['delta']:.0f}c)")})

            # Tier 4 -- SURPRISE-LAG: an edge only if the banked rain BEAT the
            # forecast (a surprise the priced-in prediction missed) AND the next
            # level's ask hasn't repriced. Rain that merely arrived on schedule ->
            # flat price is CORRECT, not a lag, so it must NOT flag.
            ev = res["event"]
            prev = prevsnap.get(ev)
            if prev is not None and res["incr"] > 0:
                banked = res["live"] - prev.get("mtd", res["live"])
                try:
                    exp_today = forecast_today(s)
                except Exception:
                    exp_today = None
                surprise = None if exp_today is None else res["incr"] - exp_today
                if (banked >= a.bank_epsilon and surprise is not None
                        and surprise >= a.surprise_eps):
                    for u in res["unsettled"]:
                        pa = prev.get("asks", {}).get(str(u["x"]))
                        if pa is None or u["vol"] < a.min_size or u["ask"] >= 90:
                            continue
                        if abs(u["ask"] - pa) <= a.stale_tol:  # didn't reprice the surprise
                            key = f"D:{ev}|{u['x']}"
                            if key in seen:
                                continue
                            found += 1
                            notify("Kalshi SURPRISE-LAG (rain beat forecast, market flat)",
                                   f"{s}: {res['incr']:.2f}\" banked today vs {exp_today:.2f}\" "
                                   f"forecast (+{surprise:.2f}\" surprise); >{u['x']}\" still "
                                   f"asks {u['ask']}c (was {pa}c). Beat-forecast rain raised "
                                   f"its odds but the market hasn't repriced -> edge. "
                                   f"gap +{u['gap']:.2f}\", vol {u['vol']:.0f}")
                            seen.add(key)
                            save_seen(seen)
                            if a.journal:
                                journal_signal({"series": s, "event": ev,
                                    "threshold": u["x"], "side": "yes",
                                    "price_paid_c": u["ask"], "entry_ask": u["ask"],
                                    "entry_mtd": f"{res['live']:.2f}",
                                    "needs_in": f"{u['gap']:.2f}",
                                    "note": (f"AUTO Tier4 SURPRISE-LAG +{surprise:.2f}in "
                                             f"vs forecast, was {pa}c")})
            newsnap[ev] = {"mtd": res["live"],
                           "asks": {str(u["x"]): u["ask"] for u in res["unsettled"]}}

        prevsnap = newsnap
        save_prev(prevsnap)

        # per-cycle heartbeat: proof-of-life + closest lock + best model delta
        if gnearest:
            gs, gx, ggap, gask = gnearest
            close = f"closest: {gs} >{gx}\" needs +{ggap:.2f}\" (ask {gask}c)"
        else:
            close = "no unsettled rungs in range"
        if gbest:
            bs, bx, bask, bp, bd = gbest
            close += f"; next-level delta: {bs} >{bx}\" model {bp*100:.0f}% vs {bask}c ({bd:+.0f}c)"
        log(f"cycle: polled {len(watch)} cities, {found} new signal(s); {close}; "
            f"next in {a.fast}s")
        if a.once:
            break
        time.sleep(a.fast)


# ---------------------------------------------------------------------------
# PHASE 2 (NOT ENABLED): automatic execution.
# Wiring this means real money moves while you're not watching. Before it runs
# live it MUST have: dry-run default, per-trade cap, daily $ cap, min-liquidity
# and max-price checks, idempotency (never double-fill a lock), and a kill file.
# It uses Kalshi's AUTHENTICATED trading API (API key + RSA key), which is a
# different endpoint and subject to their rate limits and conduct terms.
# Left unimplemented on purpose -- build only after Phase 1 catches a real lock.
def place_bet(lock):  # noqa: D401
    raise NotImplementedError("Phase 2 not enabled -- notify-only watcher.")


if __name__ == "__main__":
    main()
