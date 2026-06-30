# Zabbix API codes

The Zabbix JSON-RPC API takes integers, not names, for item value types and
trigger severities. The catalog uses readable names; these maps translate them at
provision time (`VALUE_TYPE_CODE` and `SEVERITY_CODE` in
[`otobs/catalog.py`](../otobs/catalog.py)).

## Item value types

| Name       | Code |
|------------|------|
| `float`    | 0    |
| `char`     | 1    |
| `log`      | 2    |
| `unsigned` | 3    |
| `text`     | 4    |

## Trigger priorities (severity)

| Name              | Code |
|-------------------|------|
| `not_classified`  | 0    |
| `info`            | 1    |
| `warning`         | 2    |
| `average`         | 3    |
| `high`            | 4    |
| `disaster`        | 5    |

## Item type

Every provisioned item is created with `type=2` — the Zabbix **Trapper** type.
Trapper items accept pushed values from `zabbix_sender` rather than being polled,
which is the path the simulator (and a real Node-RED bridge) uses. See
`Provisioner._item` in [`otobs/provision.py`](../otobs/provision.py).
