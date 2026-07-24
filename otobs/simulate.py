"""Stream catalog-driven mock OT data into Zabbix via Trapper.

Each (host, parameter) is a Stream with a sticky state machine: with probability
STICKINESS it stays in its current Good/Underperform/Failed state, otherwise it
re-rolls by the catalog weights. Sticky states produce long, smooth degradation
stretches — the kind of Underperform curve Tahap 2/3 ML training needs, not flicker.

catalog/sim_config.yml layers optional realism on top (correlation, trend,
time-of-day, dropout, backfill, ladder progression, maintenance visits). Every
feature defaults OFF and is a strict no-op when disabled: with all of them off,
output is identical to the plain state machine.
"""
from __future__ import annotations
import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from . import settings
from .catalog import AssetClass, Parameter, Sim, State, load_all
from .sim_config import SimConfig, load_sim_config
from .weather_engine import WeatherNode

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    _TZ = ZoneInfo(settings.TIMEZONE)
except (ZoneInfoNotFoundError, ValueError):
    _TZ = None

log = logging.getLogger(__name__)

WEATHER = WeatherNode()
_WEATHER_FIELDS = {
    "bmkg.temp": "temp",
    "bmkg.humidity": "humidity",
    "bmkg.rain_intensity": "rain_intensity",
    "bmkg.lightning_event": "lightning_event",
    "bmkg.dust_index": "dust_index",
}


def _hour(unix_ts: float) -> float:
    """Local hour as a fraction (13.5 = 13:30) — the time-of-day shoulder blend
    needs sub-hour resolution or the 'smooth' ramp would still step hourly."""
    t = datetime.fromtimestamp(unix_ts, _TZ)
    return t.hour + t.minute / 60.0


def _clamp_units(v: float, units: str) -> float:
    return max(0.0, min(100.0, v)) if units == "%" else v


def sample(sim: Sim, state: State, value_type: str = "float", units: str = ""):
    """Produce one reading for the given state, typed for the Zabbix item."""
    if sim.kind == "enum":
        return state.value
    v = random.uniform(state.lo, state.hi) + random.gauss(0, state.jitter)
    v = max(state.lo - state.jitter, min(state.hi + state.jitter, v))
    v = _clamp_units(v, units)
    return int(round(v)) if value_type == "unsigned" else round(v, 3)


def _typed(v: float, value_type: str):
    return int(round(v)) if value_type == "unsigned" else round(v, 3)


def sample_stream(s: "Stream", st: State, now: float, scale: float,
                  cfg: SimConfig, hour: float):
    """Value for this tick honoring continuity walk + trend ramp + time-of-day.
    Falls back to the exact original sample() when none is engaged — the no-op path."""
    sim = s.param.sim
    if sim.kind == "enum":
        return st.value
    ramp_dur = cfg.trend.ramp_for(s.param.key) / scale if cfg.trend.enabled else 0.0
    ramping = (cfg.trend.enabled and s.ramp_to is not None
               and ramp_dur > 0 and (now - s.ramp_start) < ramp_dur)
    walk = (cfg.continuity.enabled and not ramping
            and s.last_value is not None and st.lo <= s.last_value <= st.hi)
    mult = cfg.time_of_day.multiplier(s.param.key, hour)
    if not ramping and not walk and mult == 1.0:
        return sample(sim, st, s.param.value_type, s.param.units)
    if walk:
        if st.jitter > 0:
            target = ((st.lo + st.hi) / 2.0) * mult
            base = (s.last_value + cfg.continuity.reversion * (target - s.last_value)
                    + random.gauss(0, cfg.continuity.step(st.jitter)))
        else:
            base = s.last_value
        return _typed(min(st.hi, max(st.lo, base)), s.param.value_type)
    if ramping:
        frac = (now - s.ramp_start) / ramp_dur
        base = s.ramp_from + (s.ramp_to - s.ramp_from) * frac
    else:
        base = random.uniform(st.lo, st.hi)
    v = _clamp_units(base * mult + random.gauss(0, st.jitter), s.param.units)
    return _typed(v, s.param.value_type)


def next_state(sim: Sim, cur: int | None, stickiness: float,
               forced_idx: int | None = None, ladder: bool = False) -> int:
    if forced_idx is not None:
        return forced_idx
    if cur is not None and random.random() < stickiness:
        return cur
    r, acc = random.random(), 0.0
    weights = sim.normalized_weights()
    target = len(weights) - 1
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            target = i
            break
    # Ladder: move one band toward the rolled target instead of teleporting.
    # Catalog states are ordered good -> underperform -> failed, so degradation
    # must pass through underperform and recovery can't skip it either.
    if ladder and cur is not None and target != cur:
        return cur + (1 if target > cur else -1)
    return target


def _idx_of_band(sim: Sim, band: str) -> int | None:
    for i, st in enumerate(sim.states):
        if st.band == band:
            return i
    return None


def _band_idx_for_value(sim: Sim, value) -> int:
    """Which State's range contains value — weather streams are driven by
    physics, not the probabilistic state machine, but still need a band index
    so correlation rules (and Zabbix triggers) can read good/underperform/failed."""
    if sim.kind == "enum":
        for i, st in enumerate(sim.states):
            if st.value == value:
                return i
        return len(sim.states) - 1
    for i, st in enumerate(sim.states):
        if st.lo <= value <= st.hi:
            return i
    return 0 if value < sim.states[0].lo else len(sim.states) - 1


@dataclass
class Stream:
    host: str
    param: Parameter
    state_idx: int | None = None
    next_due: float = 0.0
    last_value: float | None = None
    ramp_from: float = 0.0
    ramp_to: float | None = None
    ramp_start: float = 0.0
    hold_until: float = 0.0  # MTTR dwell: can't leave the current band until this time
    send_key: str = ""  # trapper key actually sent; per-port streams append [<port>]

    def __post_init__(self):
        if not self.send_key:
            self.send_key = self.param.key


def build_streams(assets: list[AssetClass]) -> list[Stream]:
    """One stream per (host, param). Discovery-prototype params fan out to one
    stream per port, each reusing the *base* Parameter — so a port's Good/
    Underperform/Failed machine and every sim_config feature key off the base key,
    while only the trapper `send_key` carries the [<port>] suffix."""
    streams = []
    for a in assets:
        disc = a.discovery
        protos = set(disc.prototypes) if disc else set()
        for h in a.hosts:
            for p in a.parameters:
                if disc and p.key in protos:
                    for port in disc.ports:
                        streams.append(Stream(host=h.host, param=p,
                                              send_key=f"{p.key}[{port}]"))
                else:
                    streams.append(Stream(host=h.host, param=p))
    return streams


def discovery_payloads(assets: list[AssetClass]) -> list[tuple[str, str, str]]:
    """(host, lld_key, json) trapper LLD traps seeding the server's per-port items
    before any port value arrives. Lab stand-in for a real SNMP interface walk."""
    out = []
    for a in assets:
        d = a.discovery
        if not d:
            continue
        payload = json.dumps({"data": [{d.macro: port} for port in d.ports]})
        for h in a.hosts:
            out.append((h.host, d.key, payload))
    return out


def _by_host(streams: list[Stream]) -> dict[str, dict[str, Stream]]:
    idx: dict[str, dict[str, Stream]] = {}
    for s in streams:
        idx.setdefault(s.host, {})[s.param.key] = s
    return idx


def correlation_forces(cfg: SimConfig, by_host: dict) -> dict:
    """{(host, param_key): bias_band} for streams to force this tick, based on
    each trigger param's CURRENT (pre-roll) band. Per-host; composable — the
    first firing group wins for a given target.

    One exception: `bmkg.*` weather triggers resolve against the single shared
    regional weather host regardless of which host is being iterated (weather
    isn't scoped to one station like everything else), so omega.yml's groups
    can target `hvac`/`proc`/`net` params on other hosts without duplicating
    the weather stream onto every one of them."""
    forced: dict = {}
    if not cfg.correlation.enabled:
        return forced
    weather = {key: s for sd in by_host.values() for key, s in sd.items()
              if key.startswith("bmkg.")}
    for host, sd in by_host.items():
        for g in cfg.correlation.groups:
            trig = weather.get(g.trigger_param) if g.trigger_param.startswith("bmkg.") \
                else sd.get(g.trigger_param)
            if trig is None or trig.state_idx is None:
                continue
            if trig.param.sim.states[trig.state_idx].band != g.trigger_band:
                continue
            for a in g.affects:
                key = (host, a.param)
                if a.param not in sd or key in forced:
                    continue
                if random.random() < a.strength:
                    forced[key] = a.bias_band
    return forced


_BAND_RANK = {"good": 0, "underperform": 1, "failed": 2}


class MaintenanceCrew:
    """Routine PM visits (cfg.maintenance). Tracks per-host visit schedules on the
    caller's clock (`now`): monotonic in the live loop, virtual epoch in backfill —
    durations are divided by `scale` the same way stream intervals are.

    forces() returns {(host, key): 'good'} for every failed stream on hosts whose
    visit is due this tick, and re-arms the schedule. The absorbing-failed rule
    lives in process_stream; together: nothing heals until the crew shows up,
    then everything on the host heals at once.

    ponytail: visits repair ALL hosts in the fleet (PLC/switch too, not just HMI);
    scope per-asset-class if that ever matters."""

    def __init__(self, cfg: SimConfig, hosts, start: float, scale: float = 1.0):
        m = cfg.maintenance
        self.cfg = m
        self.scale = scale
        self.last_visit: dict[str, float] = {}
        self.dispatch_due: dict[str, float] = {}  # armed reactive visits per host
        self.on_site: set[str] = set()  # hosts mid-repair: force until every stream consumes it
        # stagger first visits across one interval so the fleet doesn't sync up
        self.next_visit = {h: start + random.uniform(0, m.interval_s) / scale
                           for h in hosts} if m.enabled else {}

    def forces(self, by_host: dict, now: float) -> dict:
        forced: dict = {}
        if not self.cfg.enabled:
            return forced
        for host, sd in by_host.items():
            failed = [key for key, s in sd.items() if s.state_idx is not None
                      and s.param.sim.states[s.state_idx].band == "failed"]
            # A visit only repairs streams processed on its tick, but slow
            # (5m/1h) streams are usually not due then — so the tech stays
            # on site, re-forcing 'good' until every failed stream has
            # consumed the repair. Without this, slow streams miss the visit,
            # stay failed, and re-trigger dispatch forever.
            if host in self.on_site:
                if not failed:
                    self.on_site.discard(host)  # all fixed — leave site
                else:
                    for key in failed:
                        forced[(host, key)] = "good"
                continue
            # reactive dispatch: first failure on a host arms an unscheduled visit
            if self.cfg.dispatch and failed and host not in self.dispatch_due:
                lo, hi = self.cfg.dispatch
                self.dispatch_due[host] = now + random.uniform(lo, hi) / self.scale
            routine = now >= self.next_visit.get(host, float("inf"))
            if not (routine or now >= self.dispatch_due.get(host, float("inf"))):
                continue
            if routine:  # re-arm the schedule
                j = self.cfg.jitter_s
                self.next_visit[host] = now + (self.cfg.interval_s + random.uniform(-j, j)) / self.scale
            self.dispatch_due.pop(host, None)  # any visit clears a pending dispatch
            self.last_visit[host] = now
            if failed:
                self.on_site.add(host)
                for key in failed:
                    forced[(host, key)] = "good"
        return forced

    def visited_within(self, host: str, now: float, seconds: float) -> bool:
        lv = self.last_visit.get(host)
        return lv is not None and (now - lv) <= seconds / self.scale


def segment_forces(assets: list[AssetClass], by_host: dict) -> dict:
    """{(host, circuit_key): band} forcing each comm-link circuit to the WORST
    current band of the physical segment(s) it rides — a hard, deterministic
    force (unlike the probabilistic same-host `correlation` web). Worst by
    severity rank good < underperform < failed, so a degrading (warning) span
    shows its circuits as 'degraded' (impaired-but-up) and a cut shows them
    'down' — and every circuit on a shared span moves together. VSAT circuits
    (no depends_on) are skipped and roll their own independent ping-loss machine.

    Reads segments' CURRENT (pre-roll) state, exactly like correlation_forces, so
    it's causal and order-independent. Segments and circuits share the one NOC
    host, so the same per-host stream index resolves the dependency."""
    forced: dict = {}
    for a in assets:
        for c in a.circuits:
            if not c.param.depends_on:
                continue
            for host, sd in by_host.items():
                if c.param.key not in sd:
                    continue
                worst = "good"
                for seg_key in c.param.depends_on:
                    seg = sd.get(seg_key)
                    if seg and seg.state_idx is not None:
                        band = seg.param.sim.states[seg.state_idx].band
                        if _BAND_RANK.get(band, 0) > _BAND_RANK[worst]:
                            worst = band
                forced[(host, c.param.key)] = worst
    return forced


def process_stream(s: Stream, now: float, scale: float, cfg: SimConfig,
                   forced: dict, hour: float, clock: float | None = None,
                   maint: MaintenanceCrew | None = None):
    """Advance one due stream one tick. Returns the emitted value, or None if the
    reading was dropped. Shared by live run() and backfill(). Caller owns next_due.

    `clock` is the real Unix instant this reading represents, for weather
    streams only — NOT the same thing as `now`, which is a scheduling clock
    (time.monotonic() in the live loop, an arbitrary epoch unrelated to
    calendar time) and would make WeatherNode's seasonal/diurnal math
    nonsense. Callers that don't touch bmkg.* streams can omit it."""
    if cfg.dropout.enabled and random.random() < cfg.dropout.prob_for(s.param.key):
        return None
    field = _WEATHER_FIELDS.get(s.param.key)
    if field is not None:
        raw = WEATHER.get_weather(clock if clock is not None else now)[field]
        s.state_idx = _band_idx_for_value(s.param.sim, raw)
        value = (s.param.sim.states[s.state_idx].value if s.param.sim.kind == "enum"
                else _typed(raw, s.param.value_type))
        s.last_value = value
        return value
    if (maint is not None and cfg.maintenance.event_key == s.param.key):
        # CMMS work-order marker: 1 on the first reading after a visit, else 0.
        # Driven by the crew's schedule, never the state machine.
        visited = maint.visited_within(s.host, now, s.param.interval_s)
        s.state_idx = _band_idx_for_value(s.param.sim, 1 if visited else 0)
        s.last_value = 1 if visited else 0
        return s.last_value
    fb = forced.get((s.host, s.param.key))
    forced_idx = _idx_of_band(s.param.sim, fb) if fb is not None else None
    # Absorbing failed: with maintenance on, a failed stream stays failed until
    # the crew forces it to 'good' — no self-heal, no correlation-bias escape.
    if (cfg.maintenance.enabled and s.state_idx is not None and fb != "good"
            and s.param.sim.states[s.state_idx].band == "failed"):
        forced_idx = s.state_idx
    # MTTR dwell: a self-rolling stream can't leave its band until the window
    # expires — real repair time. Forced streams (segment-derived circuits) are
    # unaffected; they mirror their segment.
    if (forced_idx is None and cfg.hold.enabled and s.state_idx is not None
            and now < s.hold_until):
        forced_idx = s.state_idx
    new_idx = next_state(s.param.sim, s.state_idx, settings.STICKINESS, forced_idx,
                         cfg.progression.ladder)
    transitioned = new_idx != s.state_idx
    s.state_idx = new_idx
    if cfg.hold.enabled and transitioned:  # arm the dwell on entering a new band
        win = cfg.hold.window_for(s.param.key, s.param.sim.states[new_idx].band)
        if win:
            s.hold_until = now + random.uniform(*win) / scale
    st = s.param.sim.states[new_idx]
    # A monotonic counter repaired to 'good' is a part swap: it resets instantly
    # (no ramp down, no clamp this tick). Otherwise it can only stay or rise.
    reset = s.param.sim.monotonic and fb == "good"
    if (cfg.trend.enabled and s.param.sim.kind == "numeric"
            and transitioned and s.last_value is not None and not reset):
        s.ramp_from = s.last_value
        s.ramp_to = random.uniform(st.lo, st.hi)
        s.ramp_start = now
    value = sample_stream(s, st, now, scale, cfg, hour)
    if (s.param.sim.monotonic and not reset and s.last_value is not None
            and isinstance(value, (int, float))):
        value = max(value, s.last_value)
    s.last_value = value
    return value


def _note(s: Stream, value) -> str | None:
    st = s.param.sim.states[s.state_idx]
    return f"{s.host}/{s.send_key}={value}({st.band})" if st.band != "good" else None


def run(assets: list[AssetClass], cfg: SimConfig | None = None) -> None:
    from zabbix_utils import ItemValue, ProcessingError, Sender

    cfg = cfg or load_sim_config()
    streams = build_streams(assets)
    by_host = _by_host(streams)
    sender = Sender(server=settings.SENDER_HOST, port=settings.SENDER_PORT)
    scale = max(settings.TIME_SCALE, 0.001)
    maint = MaintenanceCrew(cfg, by_host, time.monotonic(), scale)
    feats = ", ".join(cfg.enabled_features()) or "none"
    log.info("Streaming %d items -> %s:%d (stickiness=%s, time_scale=%sx, sim-config: %s). "
             "Ctrl+C to stop.", len(streams), settings.SENDER_HOST, settings.SENDER_PORT,
             settings.STICKINESS, scale, feats)

    # AttributeError here is zabbix_utils itself: TrapperResponse.parse() (sender.py)
    # regex-matches the server's "processed: N; failed: N; ..." info string with no
    # None-check, so a reply it doesn't recognize (e.g. values rejected as older than
    # an item's history retention) raises AttributeError instead of ProcessingError —
    # would otherwise kill a live/backfill run outright instead of logging and moving on.
    lld = discovery_payloads(assets)
    if lld:
        try:
            sender.send([ItemValue(h, k, v) for h, k, v in lld])
            log.info("Sent %d LLD discovery payload(s) — per-port items appear once the "
                     "server processes the trap (a few seconds).", len(lld))
        except (ProcessingError, OSError, json.JSONDecodeError, AttributeError) as e:
            log.error("discovery send error: %s", e)

    # Bounds in-flight sends to SIM_SENDER_WORKERS: a saturated pool drops this
    # tick's batch (logged) instead of queuing unboundedly when Zabbix is slow,
    # which also keeps Ctrl+C's executor shutdown from waiting on a backlog.
    send_slots = threading.Semaphore(settings.SIM_SENDER_WORKERS)

    def send_batch(batch: list, notes: list[str]) -> None:
        """Runs on a worker thread: the trapper round-trip is the slow part of a
        tick, so it happens off the scheduling loop and next_due stays on-time."""
        try:
            resp = sender.send(batch)
            ok = getattr(resp, "processed", "?")
            fail = getattr(resp, "failed", "?")
            tail = ("  | " + ", ".join(notes[:4]) + ("…" if len(notes) > 4 else "")) if notes else ""
            log.info("sent=%d processed=%s failed=%s%s", len(batch), ok, fail, tail)
        except (ProcessingError, OSError, json.JSONDecodeError, AttributeError) as e:
            log.error("send error: %s", e)
        finally:
            send_slots.release()

    with ThreadPoolExecutor(max_workers=settings.SIM_SENDER_WORKERS) as executor:
        while True:
            now = time.monotonic()
            due = [s for s in streams if s.next_due <= now]
            if not due:
                time.sleep(settings.SIM_POLL_INTERVAL)
                continue
            clock = time.time()  # real wall-clock instant, for both hour and weather
            hour = _hour(clock) if cfg.time_of_day.enabled else 0.0
            forced = correlation_forces(cfg, by_host)
            forced.update(segment_forces(assets, by_host))  # hard segment->circuit force wins
            forced.update(maint.forces(by_host, now))  # repair visit beats everything
            batch, notes = [], []
            for s in due:
                s.next_due = now + s.param.interval_s / scale
                value = process_stream(s, now, scale, cfg, forced, hour, clock, maint)
                if value is None:
                    continue
                batch.append(ItemValue(s.host, s.send_key, str(value)))
                n = _note(s, value)
                if n:
                    notes.append(n)

            if batch:
                if send_slots.acquire(blocking=False):
                    executor.submit(send_batch, batch, notes)
                else:
                    log.warning("sender queue full (%d in flight) — dropping this tick's "
                               "%d readings", settings.SIM_SENDER_WORKERS, len(batch))
            time.sleep(settings.SIM_POLL_INTERVAL)


def _fmt_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _print_progress(frac: float, eta_s: float, sent: int) -> None:
    """Single self-overwriting line, like a download bar -- never scrolls."""
    bar_w = 24
    filled = int(bar_w * frac)
    bar = "#" * filled + "-" * (bar_w - filled)
    eta = _fmt_eta(eta_s) if frac > 0 else "--:--"
    print(f"\r[{bar}] {frac * 100:5.1f}%  eta {eta}  sent={sent:,}\x1b[K", end="", flush=True)


def run_backfill(assets: list[AssetClass], cfg: SimConfig | None = None,
                 days: float | None = None, speed: float | None = None) -> None:
    """Discrete-event sweep of the same state machine from now-`days` to now,
    sending each value with its historical `clock`. Intervals are real (no
    TIME_SCALE) so timestamps are physically correct; `speed` compresses wall time.

    Streams are scheduled by interval bucket, not one-by-one: every stream on
    the same interval_s was armed at the same `start` and so is due at the
    same tick for the entire run -- catalogs are typically a handful of
    distinct intervals (5s/30s/1m/...) shared by many streams. Checking one
    due-time per bucket instead of per stream turns an O(n_streams) scan into
    O(n_intervals); degrades to a per-stream scan only if every stream has a
    unique interval, never worse.
    """
    from zabbix_utils import ItemValue, ProcessingError, Sender

    cfg = cfg or load_sim_config()
    days = float(days if days is not None else cfg.backfill.days)
    speed = max(float(speed if speed is not None else cfg.backfill.speed_multiplier), 0.001)
    streams = build_streams(assets)
    by_host = _by_host(streams)
    sender = Sender(server=settings.SENDER_HOST, port=settings.SENDER_PORT)

    end = time.time()
    start = end - days * 86400.0
    span = max(end - start, 1e-9)

    groups: dict[int, list[Stream]] = {}
    for s in streams:
        groups.setdefault(s.param.interval_s, []).append(s)
    group_due = {iv: start for iv in groups}
    maint = MaintenanceCrew(cfg, by_host, start)  # backfill runs on real seconds (scale=1)

    log.info("Backfilling %gd for %d items at %gx -> %s:%d ...",
             days, len(streams), speed, settings.SENDER_HOST, settings.SENDER_PORT)

    lld = discovery_payloads(assets)
    if lld:
        try:  # seed discovery at the window start so per-port items exist first
            sender.send([ItemValue(h, k, v, clock=int(start)) for h, k, v in lld])
        except (ProcessingError, OSError, json.JSONDecodeError, AttributeError) as e:
            log.error("discovery send error: %s", e)

    FLUSH = settings.ZBX_SENDER_BATCH_SIZE
    batch, sent = [], 0
    wall_start = last_print = time.time()

    def flush():
        nonlocal sent
        if not batch:
            return
        try:
            sender.send(batch)
            sent += len(batch)
        except (ProcessingError, OSError, json.JSONDecodeError, AttributeError) as e:
            log.error("send error: %s", e)
        batch.clear()

    vt = start
    sleep_debt = 0.0
    while group_due and vt < end:
        hour = _hour(vt) if cfg.time_of_day.enabled else 0.0
        forced = correlation_forces(cfg, by_host)
        forced.update(segment_forces(assets, by_host))
        forced.update(maint.forces(by_host, vt))  # repair visit beats everything
        for iv, due_t in group_due.items():
            if due_t > vt:
                continue
            group_due[iv] = due_t + iv
            for s in groups[iv]:
                value = process_stream(s, vt, 1.0, cfg, forced, hour, vt, maint)
                if value is not None:
                    batch.append(ItemValue(s.host, s.send_key, str(value), clock=int(vt)))
                    if len(batch) >= FLUSH:
                        flush()

        prev = vt
        vt = min(group_due.values())
        now = time.time()
        if now - last_print >= 0.2 or vt >= end:
            frac = min((prev - start) / span, 1.0)
            eta = (now - wall_start) * (1 - frac) / frac if frac > 0 else 0.0
            _print_progress(frac, eta, sent)
            last_print = now
        sleep_debt += (vt - prev) / speed
        if sleep_debt >= 0.005:
            time.sleep(min(sleep_debt, 5.0))
            sleep_debt = 0.0

    flush()
    _print_progress(1.0, 0.0, sent)
    print()
    day = datetime.fromtimestamp(start, _TZ).date()
    log.info("Backfill done: %d points from %s to now.", sent, day)


def main() -> None:
    try:
        run(load_all())
    except KeyboardInterrupt:
        log.info("Keyboard Interrupted, Thanks For Simulating.")


if __name__ == "__main__":
    main()
