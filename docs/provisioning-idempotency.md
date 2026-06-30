# Provisioning idempotency

`make provision` is safe to re-run. Nothing is duplicated and stale objects are
cleaned up. The mechanics live in `Provisioner`
([`otobs/provision.py`](../otobs/provision.py)).

## Get-or-create helpers

`_templategroup`, `_hostgroup`, and `_template` each look the object up by name
and create it only if absent, returning its id either way. These run once per
asset class.

## Create-if-absent against a pre-fetched set

Before creating items, triggers, and hosts for an asset, `apply` fetches the
existing keys / descriptions / host names **once** (scoped to the template) and
passes those sets down. `_item`, `_triggers`, and `_host` skip anything already
present. Fetching once avoids an N+1 storm of existence-check API calls.

## Re-sync on re-run

For a host that already exists, `_host` issues a `host.update` so mutable data —
the visible name and inventory/location — is re-synced from the catalog on every
run, even though the host itself isn't recreated.

## Prune

`prune` deletes catalog-managed hosts that are no longer in the catalog. It is
scoped strictly to the catalog's own host groups, so it never touches hosts
created outside this tool.
