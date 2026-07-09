"""CLI: python -m otobs {provision|simulate|backfill|list|check|config}."""
from __future__ import annotations
import sys
from pathlib import Path

from . import settings
from .catalog import load_all
from .simulate import sample, next_state
from .sim_config import SIM_CONFIG_FILE, load_sim_config, load_sim_config_file, validate


def _param_bands(assets) -> dict[str, set]:
    return {p.key: {st.band for st in p.sim.states}
            for a in assets for p in a.parameters}


def _numeric_keys(assets) -> set[str]:
    return {p.key for a in assets for p in a.parameters if p.sim.kind == "numeric"}


def _sim_config_summary(cfg) -> None:
    feats = cfg.enabled_features()
    print(f"\nsim-config: {', '.join(feats) if feats else 'all features off (baseline behavior)'}")


def cmd_list() -> None:
    total = 0
    assets = load_all()
    for a in assets:
        print(f"\n{a.asset_class}  (group {a.host_group}, template '{a.template_name}')")
        print(f"  hosts: {', '.join(h.host for h in a.hosts)}")
        for p in a.parameters:
            total += 1
            bands = "/".join(s.band for s in p.sim.states)
            print(f"  - {p.key:28s} {p.value_type:8s} {p.interval:>4s}  "
                  f"[{p.sim.kind}:{bands}]  triggers={len(p.triggers)}")
    print(f"\nTotal parameters: {total}")
    _sim_config_summary(load_sim_config())


def cmd_check() -> None:
    """Self-check: catalog parses, sim_config is valid, and the generator is sane."""
    assets = load_all()
    n = 0
    for a in assets:
        for p in a.parameters:
            idx = None
            seen_bands = set()
            for _ in range(500):
                idx = next_state(p.sim, idx, stickiness=settings.STICKINESS)
                st = p.sim.states[idx]
                seen_bands.add(st.band)
                v = sample(p.sim, st, p.value_type, p.units)
                if p.sim.kind == "numeric":
                    assert isinstance(v, (int, float)), f"{p.key}: non-numeric {v!r}"
                    if p.value_type == "unsigned":
                        assert isinstance(v, int), f"{p.key}: unsigned got float {v!r}"
                    assert st.lo - 5 * st.jitter - 0.501 <= v <= st.hi + 5 * st.jitter + 0.501, \
                        f"{p.key}: {v} out of band [{st.lo},{st.hi}]"
                    if p.units == "%":
                        assert 0 <= v <= 100, f"{p.key}: {v}% outside physical 0-100% range"
                else:
                    assert v == st.value, f"{p.key}: enum value mismatch"
                n += 1
            assert len(seen_bands) >= 1
    for a in assets:
        keys = {p.key for p in a.parameters}
        for p in a.parameters:
            for t in p.triggers:
                assert p.key in keys
                assert t.severity and t.op

    cfg = load_sim_config()
    validate(cfg, _param_bands(assets), _numeric_keys(assets))
    print(f"OK — {sum(len(a.parameters) for a in assets)} parameters across "
          f"{len(assets)} asset classes, {n} samples generated, all in-band.")
    feats = cfg.enabled_features()
    print(f"sim-config valid — features enabled: {', '.join(feats) if feats else 'none'}.")


def _presets() -> list[Path]:
    return sorted(settings.PRESETS_DIR.glob("*.yml"))


def _active_mode(active: Path, presets: list[Path]) -> str:
    """Name of the preset matching the live sim_config.yml, else 'custom'/'none'."""
    if not active.exists():
        return "none (baseline)"
    txt = active.read_text()
    for p in presets:
        if p.read_text() == txt:
            return p.stem
    return "custom"


def cmd_config(rest: list[str]) -> None:
    """Select the simulation mode: copy a preset (or a custom file) to the active
    catalog/sim_config.yml, after validating it against the catalog. No arg = status."""
    presets = _presets()
    active = settings.CATALOG_DIR / SIM_CONFIG_FILE
    names = [p.stem for p in presets]

    if not rest:
        print(f"Active mode: {_active_mode(active, presets)}")
        feats = load_sim_config().enabled_features()
        print(f"  enabled features: {', '.join(feats) if feats else 'none (baseline)'}")
        print(f"Available modes: {', '.join(names)}")
        print("Usage: make config MODE=<name>   |   make config FILE=<path.yml>")
        return

    if rest[0] == "--file":
        if len(rest) < 2:
            print("config --file needs a path"); sys.exit(2)
        src = Path(rest[1])
        if not src.exists():
            print(f"no such file: {src}"); sys.exit(2)
    else:
        src = settings.PRESETS_DIR / f"{rest[0]}.yml"
        if not src.exists():
            print(f"unknown mode {rest[0]!r}. Available: {', '.join(names)}"); sys.exit(2)

    assets = load_all()
    validate(load_sim_config_file(src), _param_bands(assets), _numeric_keys(assets))
    active.write_text(src.read_text())
    feats = load_sim_config().enabled_features()
    print(f"Activated '{src.stem}' -> catalog/sim_config.yml")
    print(f"  enabled features: {', '.join(feats) if feats else 'none (baseline)'}")
    print("Run `make check` then `make simulate` (or `make backfill`).")


def _flag(name: str, cast):
    """Pull `--name VALUE` out of argv, or None. Tiny; argparse is overkill here."""
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return cast(sys.argv[i + 1])
    return None


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "provision":
        from .provision import main as m; m()
    elif arg == "simulate":
        from .simulate import main as m; m()
    elif arg == "backfill":
        from .simulate import run_backfill
        try:
            run_backfill(load_all(), days=_flag("--days", float), speed=_flag("--speed", float))
        except KeyboardInterrupt:
            print("\nstopped.")
    elif arg == "config":
        cmd_config(sys.argv[2:])
    elif arg == "list":
        cmd_list()
    elif arg == "check":
        cmd_check()
    elif arg == "export-dashboards":
        from .dashboard import export_main as m; m()
    elif arg == "import-dashboards":
        from .dashboard import import_main as m; m()
    elif arg == "extract":
        from .extract import main as m; m(sys.argv[2:])
    else:
        print("usage: python -m otobs {provision|simulate|backfill|config|list|check|"
              "export-dashboards|import-dashboards|extract}")
        sys.exit(2)


if __name__ == "__main__":
    main()
