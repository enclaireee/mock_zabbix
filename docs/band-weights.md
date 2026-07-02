# Band weights

Each parameter's simulator picks a state by weight. A weight is either a number,
used as-is, or one of the band tokens below, which maps to a default steady-state
probability. Weights are normalized per parameter, so they need not sum to 1.

| Token          | Default probability |
|----------------|---------------------|
| `good`         | 0.90                |
| `underperform` | 0.08                |
| `failed`       | 0.02                |

These are the steady-state odds for the Good / Underperform / Failed bands when a
catalog parameter doesn't override them. See `DEFAULT_WEIGHTS` in
[`otobs/catalog.py`](../otobs/catalog.py).

A weight that is neither a number nor a known token is rejected at load time.

> These are the *independent* odds. When `correlation` is enabled in
> [`catalog/sim_config.yml`](../catalog/sim_config.yml), an affected parameter's
> weight-based roll can be overridden on a given tick and forced toward a
> `bias_band` instead — see [sim-states.md](sim-states.md).
