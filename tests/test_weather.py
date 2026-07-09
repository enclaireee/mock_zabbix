"""Pytest suite for the Omega-Realistic weather engine and its integration:
determinism (backfill correctness depends on it), process_stream's bmkg.*
early-return, and correlation_forces' cross-host resolution for weather
triggers (the one exception to "trigger and affects share a host")."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest

from otobs.catalog import AssetClass, Host, Parameter, Sim, State
from otobs.simulate import (Stream, process_stream, correlation_forces,
                            _by_host, _band_idx_for_value)
from otobs.sim_config import SimConfig, Correlation, CorrGroup, Affect
from otobs.weather_engine import WeatherNode


def _ts(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def test_weather_is_deterministic_across_instances():
    t = _ts(2026, 8, 15, 14, 0)
    assert WeatherNode().get_weather(t) == WeatherNode().get_weather(t)


def test_weather_fields_stay_in_documented_ranges():
    w = WeatherNode()
    for day in range(1, 29, 4):
        for hour in range(24):
            weather = w.get_weather(_ts(2026, 2 if day % 2 else 8, day, hour))
            assert 25.0 <= weather["temp"] <= 35.0
            assert 50.0 <= weather["humidity"] <= 95.0
            assert 0.0 <= weather["rain_intensity"] <= 45.0
            assert weather["lightning_event"] in (0, 1)
            assert 0.0 <= weather["dust_index"] <= 100.0


def test_dry_season_reaches_the_failed_dust_band_within_a_week():
    """The lookback window must be long enough for 'failed' to be reachable
    under realistic conditions, not just in a contrived unit test — a prior
    48h lookback capped dust at ~42 (band ceiling 70), making the
    dust_thermal_load correlation in presets/omega.yml permanently dead."""
    w = WeatherNode()
    weather = w.get_weather(_ts(2026, 8, 27, 18, 0))  # a week into dry season
    assert weather["dust_index"] >= 70.0


def _weather_param(key: str) -> Parameter:
    sim = Sim("numeric", [State(0.9, "good", None, 0, 30, 0),
                          State(0.08, "underperform", None, 30, 70, 0),
                          State(0.02, "failed", None, 70, 100, 0)])
    return Parameter(key, key, "float", "", "30s", "c", "col", "fm", "src", sim, [])


def test_process_stream_bypasses_the_state_machine_for_weather_keys():
    """bmkg.* streams get their value/band from the physics engine directly —
    next_state()/hold/trend/sample_stream must never touch them."""
    s = Stream("BMKG-STATION", _weather_param("bmkg.dust_index"))
    ts = _ts(2026, 8, 27, 18, 0)
    value = process_stream(s, now=0.0, scale=1.0, cfg=SimConfig(), forced={}, hour=0.0, clock=ts)
    assert value == WeatherNode().get_weather(ts)["dust_index"]
    assert s.param.sim.states[s.state_idx].band == "failed"
    # a wildly different `now` (a monotonic scheduling clock) must not affect
    # the result -- only `clock` (the real timestamp) may.
    same = process_stream(s, now=999999.0, scale=1.0, cfg=SimConfig(), forced={}, hour=0.0, clock=ts)
    assert same == value


def test_process_stream_falls_back_to_now_without_clock():
    """Callers that never pass clock (e.g. older/test call sites) still work —
    they just can't get real calendar-correct weather, which is fine for
    anything that isn't a bmkg.* stream."""
    s = Stream("BMKG-STATION", _weather_param("bmkg.temp"))
    value = process_stream(s, now=_ts(2026, 8, 15, 14, 0), scale=1.0, cfg=SimConfig(), forced={}, hour=0.0)
    assert value is not None


def test_band_idx_for_value_numeric_and_enum():
    numeric = _weather_param("x").sim
    assert numeric.states[_band_idx_for_value(numeric, 10)].band == "good"
    assert numeric.states[_band_idx_for_value(numeric, 50)].band == "underperform"
    assert numeric.states[_band_idx_for_value(numeric, 95)].band == "failed"
    assert numeric.states[_band_idx_for_value(numeric, -5)].band == "good"    # below range
    assert numeric.states[_band_idx_for_value(numeric, 500)].band == "failed"  # above range

    enum_sim = Sim("enum", [State(1, "good", 0), State(1, "failed", 1)])
    assert enum_sim.states[_band_idx_for_value(enum_sim, 0)].band == "good"
    assert enum_sim.states[_band_idx_for_value(enum_sim, 1)].band == "failed"


def test_correlation_forces_resolves_weather_trigger_across_hosts():
    """The one exception to 'trigger and affects share a host': a bmkg.*
    trigger must fire for every host that has a matching affects param, since
    weather lives on one regional station, not per-site like everything else."""
    weather_host = Stream("BMKG-STATION", _weather_param("bmkg.temp"))
    weather_host.state_idx = 2  # 'failed' (hot)
    target_a = Stream("SITE-A", _weather_param("proc.comp.bearing_temp"))
    target_b = Stream("SITE-B", _weather_param("proc.comp.bearing_temp"))
    by_host = _by_host([weather_host, target_a, target_b])

    cfg = SimConfig(correlation=Correlation(True, [
        CorrGroup("heat", "bmkg.temp", "failed",
                 [Affect("proc.comp.bearing_temp", "underperform", 1.0)])
    ]))
    forced = correlation_forces(cfg, by_host)
    assert forced.get(("SITE-A", "proc.comp.bearing_temp")) == "underperform"
    assert forced.get(("SITE-B", "proc.comp.bearing_temp")) == "underperform"
    # the weather host itself is never a target of its own trigger
    assert ("BMKG-STATION", "proc.comp.bearing_temp") not in forced


def test_correlation_forces_same_host_semantics_unchanged():
    """Non-weather triggers must still resolve strictly per-host — the
    pre-existing invariant this change must not weaken."""
    fan = Stream("H1", _weather_param("fan"))
    fan.state_idx = 2
    temp_h1 = Stream("H1", _weather_param("temp"))
    temp_h2 = Stream("H2", _weather_param("temp"))  # same key, different host
    by_host = _by_host([fan, temp_h1, temp_h2])

    cfg = SimConfig(correlation=Correlation(True, [
        CorrGroup("thermal", "fan", "failed", [Affect("temp", "underperform", 1.0)])
    ]))
    forced = correlation_forces(cfg, by_host)
    assert forced.get(("H1", "temp")) == "underperform"
    assert ("H2", "temp") not in forced, "a same-host trigger leaked across hosts"


def test_omega_preset_validates_against_the_real_catalog():
    from otobs.catalog import load_all
    from otobs.sim_config import load_sim_config_file, validate
    from otobs.settings import ROOT

    assets = load_all()
    bands = {p.key: {st.band for st in p.sim.states} for a in assets for p in a.parameters}
    numeric = {p.key for a in assets for p in a.parameters if p.sim.kind == "numeric"}
    cfg = load_sim_config_file(ROOT / "presets" / "omega.yml")
    validate(cfg, bands, numeric)  # raises on any bad param/band reference
    assert cfg.enabled_features() == ["correlation", "trend", "dropout"]


def test_bmkg_catalog_present_and_shaped():
    from otobs.catalog import load_all
    assets = load_all()
    env = next(a for a in assets if a.asset_class == "External Environment")
    assert [h.host for h in env.hosts] == ["BMKG-STATION"]
    keys = {p.key for p in env.parameters}
    assert keys == {"bmkg.temp", "bmkg.humidity", "bmkg.rain_intensity",
                    "bmkg.lightning_event", "bmkg.dust_index"}
