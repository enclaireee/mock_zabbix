# Simulator states

A `Sim` has a `kind` and a list of `State` objects
([`otobs/catalog.py`](../otobs/catalog.py)).

## Sim kind

- `numeric` — readings are drawn from a numeric band (`lo`/`hi` with `jitter`).
- `enum` — readings are a fixed discrete value per state.

## State fields

| Field    | Meaning                                                            |
|----------|-------------------------------------------------------------------|
| `weight` | selection weight (see [band weights](band-weights.md))            |
| `band`   | `good` / `underperform` / `failed`, or `custom` for display-only  |
| `value`  | enum states only: the fixed value (int / float / str)            |
| `lo`,`hi`| numeric states only: inclusive band bounds                        |
| `jitter` | numeric states only: Gaussian noise added on top of the band     |

The `band` drives display and which triggers are expected to fire; `custom` is
used for enum states whose weight was given as a number rather than a band token.
