"""Central config: read .env once, expose typed settings. No python-dotenv dep."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "catalog"
PRESETS_DIR = ROOT / "presets"        # sim-config mode templates (`make config`)


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip()
        os.environ.setdefault(k.strip(), v)


_load_env()


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except ValueError:
        return default


API_URL = os.environ.get("ZBX_API_URL", "http://127.0.0.1:8080")
API_USER = os.environ.get("ZBX_API_USER", "Admin")
API_PASSWORD = os.environ.get("ZBX_API_PASSWORD", "zabbix")

SENDER_HOST = os.environ.get("ZBX_SENDER_HOST", "127.0.0.1")
# Falls back like every other setting: `list`/`check`/`config` never dial this
# port, so a typo in it must not crash offline commands that don't need it.
SENDER_PORT = _i("ZBX_SENDER_PORT", 10051)

STICKINESS = _f("SIM_STICKINESS", 0.92)
TIME_SCALE = _f("SIM_TIME_SCALE", 10.0)

# Local timezone for the time_of_day sim feature (reuses the stack's tz var).
TIMEZONE = os.environ.get("ZBX_TIMEZONE", "UTC")
