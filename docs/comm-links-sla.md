# Communication-Link SLA System

The fifth system in the lab. It models the 24 inter-station links from the PGN
**"Media Link Komunikasi"** SLA report and simulates them with realistic physical
behaviour, so you can build per-link SLA % on top using **Zabbix's own SLA
module** — not a hand-rolled uptime calculation.

The lab provisions the **link data** (segment/circuit items + triggers) and
streams it; the Zabbix **SLA services, SLA object, and dashboard are set up by
hand** (see [Setting up the SLA yourself](#setting-up-the-sla-yourself-zabbix-ui)).

Catalog: [`catalog/comm_links.yml`](../catalog/comm_links.yml). Simulation
mechanic: [`otobs/simulate.py`](../otobs/simulate.py) (`segment_forces`).

---

## Why two layers (segments and circuits)

The report lists 24 named links, but they are **not** 24 independent things that
fail on their own. Several links physically share one fiber span. Row 5
(Grissik–PGD data trunk) and row 9 (Grissik–PGD PABX link) are two logical
circuits riding the **same** Grissik→Pagardewa fiber. If a backhoe cuts that
fiber, **both** drop at the same instant — and so does anything else on it.

Modelling each row as its own independent random failure would miss exactly the
behaviour a real SLA report captures. So the catalog has two layers:

| Layer | What it is | Count | Fails… |
|-------|-----------|------:|--------|
| **`segments:`** | Physical media that fails as a unit — a fiber span between two stations, or an MPLS backhaul | 13 | independently (a fiber cut) |
| **`circuits:`** | The 24 report rows | 24 | as the **worst** of the segment(s) it rides |

Four spans are **shared** by two circuits each, straight from the report's
`Lokasi` column:

| Physical segment | Circuits riding it (report rows) |
|------------------|----------------------------------|
| `seg.fiber_grissik_pgd` | Grissik–PGD data (5) + Grissik–PGD PABX (9) |
| `seg.fiber_tbb_pgd` | TBB–PGD data (6) + PGD–TBB FXO (12) |
| `seg.fiber_labuhanmaringgai_pgd` | LabuhanMaringgai–PGD data (7) + PGD–LabuhanMaringgai PABX (10) |
| `seg.fiber_labuhanmaringgai_mcs` | LabuhanMaringgai–MCS data (4) + LabuhanMaringgai–MCS PABX (11) |

The other spans carry one circuit each. When a shared span goes down in the
simulator, both its circuits go down **together** — the whole point of the
model. This is a genuine cross-host **dependency**, not the same-host
`correlation` feature (which only nudges a stream's next roll probabilistically
and, by its own docstring, only couples streams on the same host).

### How the dependency is simulated

Each tick, [`segment_forces`](../otobs/simulate.py) resolves the current state of
every segment, then **hard-forces** each circuit with a `depends_on` to the worst
of its segments (`down` beats `up`). A circuit does **not** roll its own state
machine — it's a pure function of its segments. It reads the segment's *current*
(pre-roll) state, exactly like `correlation_forces`, so it's causal and
order-independent, and it composes with the existing realism layer (the force is
merged on top of any correlation force and wins). Backfill uses the same path, so
historical SLA % is consistent with the live stream.

VSAT-IP circuits have **no** `depends_on`: each satellite link is an independent
RF path, so it rolls its own machine (see below).

---

## Collection method per media type

This is deliberately **not** uniform, because the real access constraints aren't:

| Media | `collection:` in the catalog | Why |
|-------|------------------------------|-----|
| **Metro-E / fiber** | *derived from the terminating Metro-E port* — `net.if.oper_status` (IF-MIB `ifOperStatus`), the same collection story as the Switch/Router asset class | We terminate both ends on switches we own; the segment **is** a switch port's up/down |
| **VSAT-IP** | *Simple check (`icmppingloss`)* — a genuinely different collection string | We do **not** own or have MIB access to the leased satellite modem, so the only honest signal is an ICMP reachability probe. Simulated as a packet-loss % (0 → rain-fade → 100 %) |
| **MPLS** | modelled like Metro-E (oper-status-derived) — **assumption, flagged below** | |

VSAT down = ping loss ≥ 60 % (`high`); 20–60 % is a `warning` (rain fade,
degraded but up). Only the **down** condition counts against the SLA (see tags).

---

## Setting up the SLA yourself (Zabbix UI)

The tooling deliberately stops at the **link data** — the segment/circuit items,
their triggers, and the live simulation. Building the SLA services, the SLA
object, and the dashboard is done by hand in Zabbix (that's the intended job).
Everything below is the plan the data was shaped to support.

**The one thing the loader does for you:** each circuit's **high/disaster**
("down") trigger is pre-tagged `link : <circuit_key>` (e.g.
`link : circ.grissik_pgd`) — deliberately *not* the VSAT `warning` trigger, so a
degraded-but-up VSAT link won't burn SLA. That tag is the hook everything else
hangs off. Confirm it under *Data collection → Hosts → COMM-MCS-NOC01 → Triggers*.

The target shape (Zabbix 7.0 **Services + SLA**, under *Data collection*):

```
circuit 'down' trigger  --(event tag: link=<circuit_key>)-->  Service (one per circuit)
                                                              tag: sla_group=comm_link
                                                                     |
                             SLA object (SLO 98%, monthly, 24x7) ----+  selects all 24
                             via service_tag sla_group=comm_link         -> 24-row report
```

1. **One Service per circuit** (24). Give each a **problem tag** `link` = the
   circuit key it should track (`circ.grissik_pgd`, `circ.vsat_pgd_mcs`, …), and a
   plain **tag** `sla_group` = `comm_link` so a single SLA can select them all.
2. **One SLA object** — *Data collection → SLA → Create SLA*. SLO **98 %** (the
   report's target), **monthly** period (its *Monthly Time* column), 24×7
   schedule, and a **service tag** `sla_group` = `comm_link`. Zabbix computes the
   SLA report **per service**, so this single SLA already renders one row per
   circuit — you only need 24 separate SLA objects if a link ever needs a
   *different* target.
3. **Dashboard** — a new dashboard with a native **SLA report** widget bound to
   that SLA object shows all 24 circuits' SLA % with Zabbix's own colouring. Map
   its bands to the project's Good / Underperform / Failed scheme.

Because a shared fiber cut drops every circuit on that span together (the
simulated dependency above), those circuits' Services will show correlated
downtime — the behaviour the two-layer model exists to produce.

---

## Assumptions (flagged, per the brief — not presented as fact)

1. **MPLS backhaul (report row 23) is modelled like Metro-E** (oper-status-derived
   from a CE port), on the assumption that it is CPE we terminate at both ends. If
   in reality it's a black-box provider cloud we can only reach by ping, its
   segment should switch to the VSAT-style `icmppingloss` collection instead.
2. **Segment topology is inferred only from the report's `Lokasi` column.** The
   four shared spans above are the ones two rows explicitly name the same
   endpoints for. No segment beyond what the report implies has been invented.
3. **All 24 links are hosted on one NOC host** (`COMM-MCS-NOC01`, MCS Cilegon),
   because these are point-to-point links watched centrally, not per-station
   assets — so the comm-link catalog uses a hand-listed `hosts:` entry, not the
   per-site `host_template` the other four asset classes use.
4. **Failure rates are a simulation knob, not a claim about the real network.**
   Segments use the default `good`/`failed` band weights so the SLA visibly
   hovers near the 98 % target for a useful demo; the real report shows ~100 %.
   Tune via segment band weights and `SIM_STICKINESS`.
