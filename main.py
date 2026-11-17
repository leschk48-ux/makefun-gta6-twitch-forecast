#!/usr/bin/env python3
"""Forecast a game launch day's Twitch category viewers, hour by hour.

Reads every case file (*.in) in the input directory. Each case gives the
launch day's first-hour viewer count plus a library of comparable past
launches with hourly viewers from 14 days before to 3 days after their
release day. For each of hours 2..24 the program writes five quantiles
(5%, 25%, 50%, 75%, 95%) of that hour's viewer count.

Method
------
Past launches fall into two regimes. In a midnight-rolling console launch
(New Zealand first, then Asia, Europe, the Americas) the category is
already busy in hour 1 and the day mostly decays from there, with bumps as
big regions unlock and reach prime time. In a single simultaneous global
unlock the category is near zero in hour 1 and explodes later; nothing in
the first hour predicts that shape, and the target here (a rolling
midnight console launch, like every practice case) is of the first kind.

So the program:

1. keeps the comparables whose hour-1 viewers already exceed a quarter of
   their own release-day peak (the rolling-launch regime), falling back to
   wider sets if too few qualify;
2. turns each kept comparable into a log-ratio curve, ln(viewers_h /
   viewers_1) for h = 2..24;
3. weights each curve by how close its hour-1 size is to the target's
   (a Gaussian kernel on log10 viewers), because big launches move like
   big launches;
4. takes the weighted median curve as the central forecast, and a
   lognormal spread around it whose sigma per hour comes from the
   weighted interquartile range of the curves, smoothed over neighbouring
   hours;
5. scales the result by the target's hour-1 print.

Standard library only, deterministic, no network, no model calls.
"""

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

LEVELS = (0.05, 0.25, 0.5, 0.75, 0.95)
Z = {0.05: -1.6448536269514722, 0.25: -0.6744897501960817, 0.5: 0.0,
     0.75: 0.6744897501960817, 0.95: 1.6448536269514722}
REGIME_MIN_SHARE = 0.25   # hour-1 viewers / release-day peak for regime 1
KERNEL_BW = 0.6           # kernel bandwidth in log10 viewers
MIN_FIT = 8               # smallest fitting set before relaxing the regime
SIGMA_FLOOR = 0.12
SIGMA_CAP = 1.0


def parse_ts(text):
    """Parse an ISO-8601 UTC timestamp into an aware datetime."""
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def release_day_hours(comp):
    """Map hour-of-release-day (1..24) -> viewers for one comparable."""
    start = parse_ts(comp["day_start"])
    out = {}
    for pt in comp.get("hourly") or []:
        viewers = pt.get("viewers")
        if viewers is None or viewers <= 0:
            continue
        rel = int((parse_ts(pt["from"]) - start).total_seconds() // 3600)
        if 0 <= rel < 24:
            out[rel + 1] = float(viewers)
    return out


def weighted_quantile(pairs, tau):
    """pairs: list of (value, weight). Weighted quantile, lower convention."""
    ordered = sorted(pairs)
    total = sum(w for _, w in ordered)
    if total <= 0:
        return ordered[len(ordered) // 2][0]
    acc = 0.0
    for value, w in ordered:
        acc += w
        if acc >= tau * total:
            return value
    return ordered[-1][0]


def fit_curves(comps, target_h1):
    """Weighted (median, sigma) of the log-ratio curves, per hour 2..24."""
    days = []
    for comp in comps:
        d = release_day_hours(comp)
        if 1 in d and all(h in d for h in range(2, 25)):
            days.append(d)
    target_log = math.log10(max(target_h1, 1.0))

    def weight(d):
        dist = (math.log10(d[1]) - target_log) / KERNEL_BW
        return math.exp(-0.5 * dist * dist)

    chosen = None
    for threshold in (REGIME_MIN_SHARE, 0.15, 0.0):
        pool = [d for d in days
                if d[1] / max(d.values()) > threshold] if threshold else days
        if len(pool) >= MIN_FIT:
            chosen = pool
            break
    if chosen is None:
        chosen = days

    med, sig = {}, {}
    for h in range(2, 25):
        pairs = [(math.log(d[h] / d[1]), weight(d)) for d in chosen]
        med[h] = weighted_quantile(pairs, 0.5)
        q25 = weighted_quantile(pairs, 0.25)
        q75 = weighted_quantile(pairs, 0.75)
        sig[h] = (q75 - q25) / 1.349
    hours = list(range(2, 25))
    smoothed = {}
    for i, h in enumerate(hours):
        lo = sig[hours[max(0, i - 1)]]
        hi = sig[hours[min(len(hours) - 1, i + 1)]]
        s = (lo + sig[h] + hi) / 3.0
        smoothed[h] = min(max(s, SIGMA_FLOOR), SIGMA_CAP)
    return med, smoothed


def persistence_forecast(p):
    """Fallback when a case carries no usable comparables: every hour stays
    at the first hour's level, with a spread that widens over the day."""
    rows = []
    for h in range(2, 25):
        spread = 0.15 * math.sqrt(h - 1)
        qs = [min(max(p * math.exp(Z[t] * spread), 0.01), 1e9) for t in LEVELS]
        rows.append({"hour": h, "q": qs})
    return {"hours": rows}


def forecast_case(case):
    known = case.get("known") or []
    p = float(known[0]["viewers"]) if known and known[0].get("viewers") else 0.0
    if p <= 0:
        # Fall back to the last hourly history point before the checkpoint.
        hist = case.get("history_hourly") or []
        for pt in reversed(hist):
            if pt.get("viewers"):
                p = float(pt["viewers"])
                break
    if p <= 0:
        p = 1.0
    comps = case.get("comparables") or []
    usable = [c for c in comps
              if 1 in release_day_hours(c)]
    if len(usable) < 3:
        return persistence_forecast(p)
    med, sig = fit_curves(comps, p)
    rows = []
    for h in range(2, 25):
        qs = []
        for tau in LEVELS:
            q = p * math.exp(med[h] + Z[tau] * sig[h])
            qs.append(min(max(q, 0.01), 1e9))
        qs.sort()
        rows.append({"hour": h, "q": qs})
    return {"hours": rows}


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR", "/input")
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR", "/output")
    os.makedirs(out_dir, exist_ok=True)
    names = sorted(n for n in os.listdir(in_dir) if n.endswith(".in"))
    for name in names:
        with open(os.path.join(in_dir, name), encoding="utf-8") as fh:
            case = json.load(fh)
        result = forecast_case(case)
        out_name = name[:-3] + ".out"
        with open(os.path.join(out_dir, out_name), "w", encoding="utf-8") as fh:
            json.dump(result, fh, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
