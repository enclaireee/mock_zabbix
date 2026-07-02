# Environment loading

[`otobs/settings.py`](../otobs/settings.py) reads `.env` once at import and
exposes typed settings. There is no `python-dotenv` dependency.

## .env parser

`_load_env` reads the repo-root `.env` if present and, for each line, skips
blanks and full-line `#` comments, splits on the first `=`, and **strips a
trailing ` #` inline comment** from the value before storing it. Values are
written with `os.environ.setdefault`, so a variable already set in the real
environment wins over the file.

## Typed getters

`_f(key, default)` reads a float setting and falls back to the default if the
variable is missing or unparseable.

## Settings groups

- **Zabbix API** (`ZBX_API_*`) — used by provisioning.
- **Trapper sender** (`ZBX_SENDER_*`) — used by simulation.
- **Simulation tuning** (`SIM_STICKINESS`, `SIM_TIME_SCALE`) — the global
  calibration knobs for the mock plant; see [the architecture doc](architecture.md)
  for what they control.
- **Timezone** (`TIMEZONE`, from `ZBX_TIMEZONE`) — local hour for the
  `time_of_day` realism feature.

Structured, per-parameter realism knobs deliberately live **outside** `.env`, in
[`catalog/sim_config.yml`](../catalog/sim_config.yml) — see
[sim-states.md](sim-states.md).
