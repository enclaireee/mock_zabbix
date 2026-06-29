"""Central config: read .env once, expose typed settings. No python-dotenv dep."""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "catalog"


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #", 1)[0].strip()   # drop inline comments
        os.environ.setdefault(k.strip(), v)


_load_env()


def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        return default


# Zabbix API (provisioning)
API_URL = os.environ.get("ZBX_API_URL", "http://127.0.0.1:8080")
API_USER = os.environ.get("ZBX_API_USER", "Admin")
API_PASSWORD = os.environ.get("ZBX_API_PASSWORD", "zabbix")

# Trapper sender (simulation)
SENDER_HOST = os.environ.get("ZBX_SENDER_HOST", "127.0.0.1")
SENDER_PORT = int(os.environ.get("ZBX_SENDER_PORT", "10051"))

# Simulation tuning — the calibration knobs for the mock plant
STICKINESS = _f("SIM_STICKINESS", 0.92)
TIME_SCALE = _f("SIM_TIME_SCALE", 10.0)
