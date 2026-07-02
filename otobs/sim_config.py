"""Load + validate catalog/sim_config.yml — the realism layer over the catalog.

Orthogonal to catalog.py (which owns bands/weights/triggers). Six independently
toggleable features: continuity, correlation, trend, time_of_day, dropout,
backfill. Every feature defaults OFF and is a strict no-op when disabled — so an
absent file, or one with everything `enabled: false`, is the plain state machine.

Ranges are validated here at load time (loud fail on a bad number); parameter/band
references are validated by validate() once the catalog is known.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import yaml

from .settings import CATALOG_DIR

SIM_CONFIG_FILE = "sim_config.yml"


def _num(d: dict, key: str, default, where: str,
         lo: float | None = None, hi: float | None = None) -> float:
    v = d.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise ValueError(f"{where}.{key}: expected a number, got {v!r}")
    v = float(v)
    if lo is not None and v < lo:
        raise ValueError(f"{where}.{key}={v} below minimum {lo}")
    if hi is not None and v > hi:
        raise ValueError(f"{where}.{key}={v} above maximum {hi}")
    return v


@dataclass
class Continuity:
    """Value continuity: step from the last reading instead of redrawing the whole
    band each tick. jitter is the step size; jitter=0 params (counters) hold still.

    reversion models closed-loop (PID) control: an analog signal is pulled back
    toward its setpoint (band centre, shifted by any time_of_day multiplier) each
    tick instead of drifting to a band edge. 0 = free random walk; ~0.1 = a real
    controlled process variable hovering at setpoint."""
    enabled: bool = False
    step_scale: float = 1.0
    reversion: float = 0.0

    def step(self, jitter: float) -> float:
        return jitter * self.step_scale


@dataclass
class Affect:
    param: str
    bias_band: str
    strength: float


@dataclass
class CorrGroup:
    name: str
    trigger_param: str
    trigger_band: str
    affects: list[Affect]


@dataclass
class Correlation:
    enabled: bool = False
    groups: list[CorrGroup] = field(default_factory=list)


@dataclass
class Trend:
    enabled: bool = False
    ramp_seconds: float = 1800.0
    overrides: dict[str, float] = field(default_factory=dict)

    def ramp_for(self, key: str) -> float:
        return self.overrides.get(key, self.ramp_seconds)


@dataclass
class TodProfile:
    peak_start: int
    peak_end: int
    peak_multiplier: float
    off_peak_multiplier: float

    def multiplier(self, hour: int) -> float:
        lo, hi = self.peak_start, self.peak_end
        peak = (lo <= hour < hi) if lo <= hi else (hour >= lo or hour < hi)
        return self.peak_multiplier if peak else self.off_peak_multiplier


@dataclass
class TimeOfDay:
    enabled: bool = False
    profiles: dict[str, TodProfile] = field(default_factory=dict)

    def multiplier(self, key: str, hour: int) -> float:
        if not self.enabled:
            return 1.0
        p = self.profiles.get(key)
        return p.multiplier(hour) if p else 1.0


@dataclass
class Dropout:
    enabled: bool = False
    probability: float = 0.0
    overrides: dict[str, float] = field(default_factory=dict)

    def prob_for(self, key: str) -> float:
        if not self.enabled:
            return 0.0
        return self.overrides.get(key, self.probability)


@dataclass
class Backfill:
    enabled: bool = False
    days: float = 14.0
    speed_multiplier: float = 500.0


@dataclass
class SimConfig:
    continuity: Continuity = field(default_factory=Continuity)
    correlation: Correlation = field(default_factory=Correlation)
    trend: Trend = field(default_factory=Trend)
    time_of_day: TimeOfDay = field(default_factory=TimeOfDay)
    dropout: Dropout = field(default_factory=Dropout)
    backfill: Backfill = field(default_factory=Backfill)

    def enabled_features(self) -> list[str]:
        pairs = (("continuity", self.continuity), ("correlation", self.correlation),
                 ("trend", self.trend), ("time_of_day", self.time_of_day),
                 ("dropout", self.dropout), ("backfill", self.backfill))
        return [name for name, f in pairs if f.enabled]


def _continuity(raw: dict) -> Continuity:
    return Continuity(bool(raw.get("enabled", False)),
                      _num(raw, "step_scale", 1.0, "continuity", 0.0),
                      _num(raw, "reversion", 0.0, "continuity", 0.0, 1.0))


def _corr(raw: dict) -> Correlation:
    groups = []
    for g in raw.get("groups", []) or []:
        name = g.get("name", "?")
        where = f"correlation.groups[{name}]"
        trig = g.get("trigger") or {}
        if "param" not in trig or "band" not in trig:
            raise ValueError(f"{where}.trigger needs 'param' and 'band'")
        affects = []
        for a in g.get("affects", []) or []:
            if "param" not in a or "bias_band" not in a:
                raise ValueError(f"{where}.affects entry needs 'param' and 'bias_band'")
            affects.append(Affect(a["param"], a["bias_band"],
                                  _num(a, "strength", 1.0, f"{where}.affects", 0.0, 1.0)))
        groups.append(CorrGroup(name, trig["param"], trig["band"], affects))
    return Correlation(bool(raw.get("enabled", False)), groups)


def _trend(raw: dict) -> Trend:
    overrides = {}
    for k, v in (raw.get("overrides") or {}).items():
        overrides[k] = _num(v or {}, "ramp_seconds", None, f"trend.overrides.{k}", 0.001)
    return Trend(bool(raw.get("enabled", False)),
                 _num(raw, "ramp_seconds", 1800.0, "trend", 0.001), overrides)


def _tod(raw: dict) -> TimeOfDay:
    profiles = {}
    for p in raw.get("profiles", []) or []:
        param = p.get("param")
        where = f"time_of_day.profiles[{param}]"
        if not param:
            raise ValueError(f"{where}: needs 'param'")
        ph = p.get("peak_hours", [0, 0])
        if not (isinstance(ph, list) and len(ph) == 2):
            raise ValueError(f"{where}.peak_hours must be [start, end]")
        start, end = int(ph[0]), int(ph[1])
        for h in (start, end):
            if not 0 <= h <= 24:
                raise ValueError(f"{where}.peak_hours hour out of range 0..24: {h}")
        profiles[param] = TodProfile(
            start, end,
            _num(p, "peak_multiplier", 1.0, where, 0.0),
            _num(p, "off_peak_multiplier", 1.0, where, 0.0))
    return TimeOfDay(bool(raw.get("enabled", False)), profiles)


def _dropout(raw: dict) -> Dropout:
    overrides = {}
    for k, v in (raw.get("overrides") or {}).items():
        overrides[k] = _num(v or {}, "probability", None, f"dropout.overrides.{k}", 0.0, 1.0)
    return Dropout(bool(raw.get("enabled", False)),
                   _num(raw, "probability", 0.0, "dropout", 0.0, 1.0), overrides)


def _backfill(raw: dict) -> Backfill:
    return Backfill(bool(raw.get("enabled", False)),
                    _num(raw, "days", 14.0, "backfill", 0.001),
                    _num(raw, "speed_multiplier", 500.0, "backfill", 0.001))


def _parse(raw: dict) -> SimConfig:
    return SimConfig(
        continuity=_continuity(raw.get("continuity") or {}),
        correlation=_corr(raw.get("correlation") or {}),
        trend=_trend(raw.get("trend") or {}),
        time_of_day=_tod(raw.get("time_of_day") or {}),
        dropout=_dropout(raw.get("dropout") or {}),
        backfill=_backfill(raw.get("backfill") or {}),
    )


def load_sim_config_file(path: Path) -> SimConfig:
    """Parse one sim-config YAML (any name). Absent file -> all features off."""
    if not path.exists():
        return SimConfig()
    return _parse(yaml.safe_load(path.read_text()) or {})


def load_sim_config(directory: Path | None = None) -> SimConfig:
    """The active config: catalog/sim_config.yml (whatever `make config` last put there)."""
    return load_sim_config_file((directory or CATALOG_DIR) / SIM_CONFIG_FILE)


def validate(cfg: SimConfig, param_bands: dict[str, set]) -> None:
    """Assert every referenced param key exists and every band is real for it.
    param_bands: {param_key: {band names}}. Raises ValueError on a typo."""
    def need_param(key: str, where: str) -> None:
        if key not in param_bands:
            raise ValueError(f"{where}: unknown parameter key {key!r}")

    def need_band(key: str, band: str, where: str) -> None:
        need_param(key, where)
        if band not in param_bands[key]:
            raise ValueError(
                f"{where}: parameter {key!r} has no band {band!r} "
                f"(available: {sorted(param_bands[key])})")

    for g in cfg.correlation.groups:
        need_band(g.trigger_param, g.trigger_band, f"correlation.{g.name}.trigger")
        for a in g.affects:
            need_band(a.param, a.bias_band, f"correlation.{g.name}.affects")
    for k in cfg.trend.overrides:
        need_param(k, "trend.overrides")
    for k in cfg.time_of_day.profiles:
        need_param(k, "time_of_day.profiles")
    for k in cfg.dropout.overrides:
        need_param(k, "dropout.overrides")
