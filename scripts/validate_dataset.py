#!/usr/bin/env python
"""Validate an extracted PdM dataset CSV against the simulator's physics invariants.

Usage: python scripts/validate_dataset.py <csv> [--preset NAME] [--out PATH]

Read-only. Band thresholds come from the catalog (otobs.catalog.load_all), never
hardcoded here — YAML stays the single source of truth.

Resolution caveat (stated in the report too): extracts made via trend.get carry
HOURLY AVERAGES. For any parameter whose native interval is shorter than the row
spacing, "single-step" transition checks are advisory, not exact — a legal
good->underperform->failed ladder inside one hour looks like good->failed here.
Checks are exact only for keys whose native interval equals the row spacing.
"""
from __future__ import annotations
import csv
import statistics
import sys
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from otobs.catalog import load_all  # noqa: E402

MAINT_KEY = "hmi.maintenance"
HORIZONS_H = [2, 12, 24, 72, 168]
BAND_RANK = {"good": 0, "underperform": 1, "failed": 2}


def load_band_maps():
    """{key: (classify_fn, interval_s, monotonic)} from the catalog."""
    out = {}
    for a in load_all():
        for p in a.parameters:
            sim = p.sim
            if sim.kind == "enum":
                try:
                    pairs = [(float(st.value), st.band) for st in sim.states]
                except (TypeError, ValueError):
                    continue  # string-valued enum (e.g. text status) — not classifiable

                def classify(v, pairs=pairs):
                    return min(pairs, key=lambda pv: abs(pv[0] - v))[1]
            else:
                bands = [(st.lo, st.hi, st.band) for st in sim.states]

                def classify(v, bands=bands):
                    best, best_d = None, None
                    for lo, hi, band in bands:
                        d = 0.0 if lo <= v <= hi else min(abs(v - lo), abs(v - hi))
                        if best_d is None or d < best_d:  # tie -> first (least severe)
                            best, best_d = band, d
                    return best
            out[p.key] = (classify, p.interval_s, sim.monotonic)
    return out


def pctl(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    i = (len(sorted_vals) - 1) * q
    lo, hi = int(i), min(int(i) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (i - lo)


def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    csv_path = Path(argv[0])
    preset = argv[argv.index("--preset") + 1] if "--preset" in argv else "unknown"
    out_arg = argv[argv.index("--out") + 1] if "--out" in argv else None

    band_maps = load_band_maps()
    L = []  # report lines
    say = L.append

    # ---- load ----------------------------------------------------------------
    series = defaultdict(list)  # (host, key) -> [(ts, value)]
    nulls = defaultdict(int)
    n_rows = 0
    unknown_keys = set()
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            n_rows += 1
            for col, v in row.items():
                if v is None or v == "":
                    nulls[col] += 1
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M")
                val = float(row["value"])
            except (ValueError, KeyError):
                nulls["<unparseable>"] += 1
                continue
            key = row["key"]
            if key not in band_maps:
                unknown_keys.add(key)
                continue
            series[(row["host"], key)].append((ts, val))
    for s in series.values():
        s.sort()

    hosts = sorted({h for h, _ in series})
    keys = sorted({k for _, k in series})
    say(f"Dataset validation report — preset={preset}")
    say(f"File: {csv_path}  ({n_rows} data rows)")
    say(f"Generated: {datetime.now():%Y-%m-%d %H:%M}")
    say(f"Hosts ({len(hosts)}): {', '.join(hosts)}")
    say(f"Keys  ({len(keys)}): {', '.join(keys)}")
    if unknown_keys:
        say(f"Keys in CSV but not in catalog (skipped): {sorted(unknown_keys)}")

    # Row spacing per (host,key): median delta = the extract's actual resolution.
    spacing = {}
    for hk, pts in series.items():
        deltas = [(b[0] - a[0]).total_seconds() for a, b in zip(pts, pts[1:])]
        spacing[hk] = statistics.median(deltas) if deltas else float("nan")
    row_spacing_s = statistics.median(v for v in spacing.values() if v == v)
    say(f"\nObserved row spacing (median): {row_spacing_s / 3600:.2f} h per (host,key) series")

    catalog_keys_missing = sorted(set(k for k in band_maps if k.startswith("hmi."))
                                  - set(keys))
    if catalog_keys_missing:
        say(f"!! HMI catalog keys ABSENT from the extract: {catalog_keys_missing}")
    have_maint = any(k == MAINT_KEY for k in keys)
    if not have_maint:
        say(f"!! {MAINT_KEY} is missing — every invariant conditioned on the repair "
            f"marker is UNCHECKABLE as specified; raw counts are reported instead.")

    # Band-classify every series once.
    bands = {}  # (host,key) -> [band per row]
    for (h, k), pts in series.items():
        clf = band_maps[k][0]
        bands[(h, k)] = [clf(v) for _, v in pts]

    # Maintenance marker per host: set of timestamps where marker >= 0.5.
    maint_ticks = {h: {ts for ts, v in series.get((h, MAINT_KEY), []) if v >= 0.5}
                   for h in hosts}

    def near_repair(h, ts):
        """True if a repair marker fired at this hour on this host (marker present)."""
        return ts in maint_ticks[h]

    # ---- physics invariants --------------------------------------------------
    say("\n" + "=" * 72)
    say("PHYSICS INVARIANTS (must be zero at native resolution — see caveat)")
    say("=" * 72)
    say("Caveat: rows are hourly aggregates. Checks are EXACT only for keys whose "
        "native interval >= row spacing; for faster keys they are advisory.")

    # 1. monotonic decreases (realloc)
    for (h, k), pts in sorted(series.items()):
        if not band_maps[k][2]:
            continue
        dec_repair, dec_bare, examples = 0, 0, []
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            if v1 < v0:
                if have_maint and near_repair(h, t1):
                    dec_repair += 1
                else:
                    dec_bare += 1
                    if len(examples) < 3:
                        examples.append(f"    {t0:%Y-%m-%d %H:%M} {v0:g} -> {t1:%Y-%m-%d %H:%M} {v1:g}")
        tag = "outside repair" if have_maint else "marker missing — all decreases"
        say(f"{k} @ {h}: decreases {tag}: {dec_bare}"
            + (f", at repair tick: {dec_repair}" if have_maint else ""))
        for e in examples:
            say(e)

    # 2. single-step good->failed; 3. failed->good outside repair
    say("")
    for k in keys:
        if k == MAINT_KEY:
            continue
        native = band_maps[k][1]
        exact = native >= row_spacing_s
        gf = fg_bare = fg_repair = 0
        gf_ex = []
        for h in hosts:
            b = bands.get((h, k))
            pts = series.get((h, k))
            if not b:
                continue
            for i in range(1, len(b)):
                if b[i - 1] == "good" and b[i] == "failed":
                    gf += 1
                    if len(gf_ex) < 3:
                        t0, v0 = pts[i - 1]
                        t1, v1 = pts[i]
                        gf_ex.append(f"    {h} {t0:%m-%d %H:%M} {v0:g}(good) -> {t1:%m-%d %H:%M} {v1:g}(failed)")
                if b[i - 1] == "failed" and b[i] == "good":
                    if have_maint and near_repair(h, pts[i][0]):
                        fg_repair += 1
                    else:
                        fg_bare += 1
        flag = "EXACT" if exact else "advisory (native interval < row spacing)"
        say(f"{k} [{flag}]: good->failed single-step: {gf}; "
            f"failed->good {'outside repair' if have_maint else '(marker missing)'}: {fg_bare}"
            + (f", at repair: {fg_repair}" if have_maint else ""))
        for e in gf_ex:
            say(e)

    # ---- event structure -----------------------------------------------------
    say("\n" + "=" * 72)
    say("EVENT STRUCTURE (reported, not judged)")
    say("=" * 72)
    step_h = row_spacing_s / 3600
    onsets = defaultdict(int)   # (host,key) -> n transitions into failed
    episodes = defaultdict(list)  # key -> [duration_h]; right-censored counted separately
    censored = defaultdict(int)
    failed_hours = defaultdict(float)  # key -> host-hours failed
    total_hours = defaultdict(float)
    for (h, k), b in bands.items():
        if k == MAINT_KEY:
            continue
        total_hours[k] += len(b) * step_h
        run = 0
        for i, band in enumerate(b):
            if band == "failed":
                failed_hours[k] += step_h
                if i == 0 or b[i - 1] != "failed":
                    onsets[(h, k)] += 1
                    run = 1
                else:
                    run += 1
            elif run:
                episodes[k].append(run * step_h)
                run = 0
        if run:
            censored[k] += 1
            episodes[k].append(run * step_h)

    say("Onset count (transitions into failed) per host/parameter:")
    for h in hosts:
        parts = [f"{k.split('.', 1)[1]}={onsets[(h, k)]}" for k in keys
                 if k != MAINT_KEY and onsets[(h, k)]]
        say(f"  {h}: " + (", ".join(parts) or "none"))
        say(f"    total={sum(onsets[(h, k)] for k in keys)}")

    say("\nEpisode duration (hours) per parameter [min/p25/med/p75/max] (n, censored):")
    all_ep = []
    for k in keys:
        ep = sorted(episodes.get(k, []))
        all_ep += ep
        if not ep:
            continue
        say(f"  {k}: {ep[0]:.0f}/{pctl(ep, .25):.0f}/{pctl(ep, .5):.0f}/"
            f"{pctl(ep, .75):.0f}/{ep[-1]:.0f}  (n={len(ep)}, censored={censored[k]})")
    if all_ep:
        all_ep.sort()
        say(f"  ALL: {all_ep[0]:.0f}/{pctl(all_ep, .25):.0f}/{pctl(all_ep, .5):.0f}/"
            f"{pctl(all_ep, .75):.0f}/{all_ep[-1]:.0f}  (n={len(all_ep)})")

    say("\nFraction of host-hours failed per parameter (and share of all failed hours):")
    tot_failed = sum(failed_hours.values())
    for k in keys:
        if k == MAINT_KEY or not total_hours.get(k):
            continue
        frac = failed_hours[k] / total_hours[k]
        share = failed_hours[k] / tot_failed if tot_failed else 0.0
        flag = "  << >40% OF ALL FAILED HOURS" if share > 0.40 else ""
        say(f"  {k}: {frac * 100:5.2f}% failed-time, share {share * 100:5.1f}%{flag}")
    grand = sum(total_hours.values())
    say(f"  OVERALL: {tot_failed / grand * 100:.2f}% of stream host-hours failed"
        if grand else "  OVERALL: n/a")

    # host-level any-failed
    host_any = {}
    for h in hosts:
        grid = sorted({ts for (hh, k) in series for ts, _ in series[(hh, k)]
                       if hh == h and k != MAINT_KEY})
        state = {}
        anyf = {}
        per_key_state = {k: None for k in keys}
        # walk each key series with pointers
        ptr = {k: 0 for k in keys}
        for ts in grid:
            for k in keys:
                if k == MAINT_KEY:
                    continue
                pts = series.get((h, k))
                if not pts:
                    continue
                b = bands[(h, k)]
                while ptr[k] < len(pts) and pts[ptr[k]][0] <= ts:
                    per_key_state[k] = b[ptr[k]]
                    ptr[k] += 1
            anyf[ts] = any(per_key_state[k] == "failed" for k in keys if k != MAINT_KEY)
        host_any[h] = anyf
    n_any = sum(sum(1 for v in a.values() if v) for a in host_any.values())
    n_all = sum(len(a) for a in host_any.values())
    say(f"  Host-hours with ANY failed stream: {n_any}/{n_all} "
        f"({n_any / n_all * 100:.1f}%)" if n_all else "  no host-hours")

    say("\nMaintenance visits:")
    if have_maint:
        for h in hosts:
            visits = sorted(maint_ticks[h])
            found_failed = sum(1 for t in visits if host_any[h].get(t))
            say(f"  {h}: {len(visits)} marker ticks, {found_failed} coincided with a failed stream")
        say("  (routine vs reactive dispatch cannot be distinguished from the marker alone)")
    else:
        # Inferred repair events: any failed->good heal on the host at a tick.
        for h in hosts:
            heals = set()
            for k in keys:
                b = bands.get((h, k))
                pts = series.get((h, k))
                if not b:
                    continue
                for i in range(1, len(b)):
                    if b[i - 1] == "failed" and b[i] != "failed":
                        heals.add(pts[i][0])
            say(f"  {h}: marker missing — {len(heals)} inferred repair tick(s) "
                f"(any failed->good heal)")
        say("  Routine-visit and reactive-dispatch counts: UNCHECKABLE without the marker.")

    # ---- data integrity ------------------------------------------------------
    say("\n" + "=" * 72)
    say("DATA INTEGRITY")
    say("=" * 72)
    host_bounds = {}
    for h in hosts:
        ts_all = [ts for (hh, k), pts in series.items() if hh == h for ts, _ in pts]
        host_bounds[h] = (min(ts_all), max(ts_all))
    earliest = min(b[0] for b in host_bounds.values())
    for h in hosts:
        b0, b1 = host_bounds[h]
        late = (b0 - earliest).total_seconds() / 86400
        flag = f"  << starts {late:.1f}d after the earliest host" if late > 1 else ""
        say(f"  {h}: {b0:%Y-%m-%d %H:%M} .. {b1:%Y-%m-%d %H:%M}{flag}")
    span_d = (max(b[1] for b in host_bounds.values()) - earliest).total_seconds() / 86400
    say(f"  Total span: {span_d:.1f} days")

    say("\nGap analysis (interval > 2x the series' median spacing):")
    any_gap = False
    for (h, k), pts in sorted(series.items()):
        nom = spacing[(h, k)]
        gaps = [(a[0], b[0]) for a, b in zip(pts, pts[1:])
                if (b[0] - a[0]).total_seconds() > 2 * nom]
        if gaps:
            any_gap = True
            worst = max(gaps, key=lambda g: g[1] - g[0])
            say(f"  {h}/{k}: {len(gaps)} gap(s), worst "
                f"{(worst[1] - worst[0]).total_seconds() / 3600:.1f}h at {worst[0]:%m-%d %H:%M}")
    if not any_gap:
        say("  none")

    say("\nNull/missing counts per column:")
    if nulls:
        for col, n in sorted(nulls.items()):
            say(f"  {col}: {n}")
    else:
        say("  none")

    # ---- contamination -------------------------------------------------------
    say("\n" + "=" * 72)
    say("ALREADY-FAILED CONTAMINATION (host already failed at t AND at t+H)")
    say("=" * 72)
    for H in HORIZONS_H:
        num = den = 0
        for h in hosts:
            anyf = host_any[h]
            grid = sorted(anyf)
            idx = {t: i for i, t in enumerate(grid)}
            for t in grid:
                # nearest grid point at t+H (exact hour grid expected)
                target = None
                from datetime import timedelta
                tH = t + timedelta(hours=H)
                if tH in idx:
                    target = tH
                if target is None:
                    continue
                den += 1
                if anyf[t] and anyf[target]:
                    num += 1
        rate = num / den * 100 if den else float("nan")
        say(f"  H={H:>3}h: already-failed contamination = {num}/{den} rows ({rate:.1f}%)")
    say("  (label: rows failed at t that are STILL failed at t+H — these rows make a")
    say("   'state at t+H' target trivially predictable from current state)")

    report = "\n".join(L) + "\n"
    out = Path(out_arg) if out_arg else \
        Path(f"reports/dataset_validation_{preset}_{date.today()}.txt")
    out.parent.mkdir(exist_ok=True)
    out.write_text(report)
    print(report)
    print(f"[saved -> {out}]", file=sys.stderr)


if __name__ == "__main__":
    main()
