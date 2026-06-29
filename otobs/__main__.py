"""CLI: python -m otobs {provision|simulate|list|check}."""
from __future__ import annotations
import sys

from .catalog import load_all
from .simulate import sample, next_state


def cmd_list() -> None:
    total = 0
    for a in load_all():
        print(f"\n{a.asset_class}  (group {a.host_group}, template '{a.template_name}')")
        print(f"  hosts: {', '.join(h.host for h in a.hosts)}")
        for p in a.parameters:
            total += 1
            bands = "/".join(s.band for s in p.sim.states)
            print(f"  - {p.key:28s} {p.value_type:8s} {p.interval:>4s}  "
                  f"[{p.sim.kind}:{bands}]  triggers={len(p.triggers)}")
    print(f"\nTotal parameters: {total}")


def cmd_check() -> None:
    """Self-check: catalog parses and the generator produces sane values."""
    assets = load_all()
    n = 0
    for a in assets:
        for p in a.parameters:
            idx = None
            seen_bands = set()
            for _ in range(500):
                idx = next_state(p.sim, idx, stickiness=0.5)
                st = p.sim.states[idx]
                seen_bands.add(st.band)
                v = sample(p.sim, st, p.value_type)
                if p.sim.kind == "numeric":
                    assert isinstance(v, (int, float)), f"{p.key}: non-numeric {v!r}"
                    if p.value_type == "unsigned":
                        assert isinstance(v, int), f"{p.key}: unsigned got float {v!r}"
                    assert st.lo - 5 * st.jitter - 0.501 <= v <= st.hi + 5 * st.jitter + 0.501, \
                        f"{p.key}: {v} out of band [{st.lo},{st.hi}]"
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
    print(f"OK — {sum(len(a.parameters) for a in assets)} parameters across "
          f"{len(assets)} asset classes, {n} samples generated, all in-band.")


def main() -> None:
    cmds = {"provision": None, "simulate": None, "list": cmd_list, "check": cmd_check}
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "provision":
        from .provision import main as m; m()
    elif arg == "simulate":
        from .simulate import main as m; m()
    elif arg in ("list", "check"):
        cmds[arg]()
    else:
        print(f"usage: python -m otobs {{{'|'.join(cmds)}}}")
        sys.exit(2)


if __name__ == "__main__":
    main()
