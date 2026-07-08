# Communication-Link SLA System

The fifth system in the lab. It models a **fully fictional** WAN — 8 stations,
11 physical spans, 20 logical circuits, across 8 real-world transport
technologies — and simulates it with realistic physical behaviour, so you can
build per-link SLA % on top using **Zabbix's own SLA module** — not a
hand-rolled uptime calculation.

> The original design mirrored a real PGN "Media Link Komunikasi" SLA report
> one-for-one (real station names, real link routing, real capacities). That
> report is NDA-protected, so the catalog was rebuilt from scratch as an
> invented topology (`PGN_Station1..8`, `PGN_SegmentN`, `PGN_CircuitN`) —
> independently designed from public WAN-technology references, not derived
> from or structurally mirroring the real report. The mechanic, math, and
> Zabbix setup below are unchanged; only the topology and the technology mix
> are new (see [Technology mix](#technology-mix) — it now covers 8 transports
> instead of the original's 3).

The lab provisions the **link data** (segment/circuit items + triggers) and
streams it; the Zabbix **SLA services, SLA object, and dashboard are set up by
hand** (see [Setting up the SLA yourself](#setting-up-the-sla-yourself-zabbix-ui)).

Catalog: [`catalog/comm_links.yml`](../catalog/comm_links.yml). Simulation
mechanic: [`otobs/simulate.py`](../otobs/simulate.py) (`segment_forces`).

---

## Technology mix

| # | Technology | Segments | Circuits | Signal metric |
|---|---|---:|---:|---|
| 1 | Metro-E fiber | 3 | 3 | Optical Rx power (dBm), SFF-8472 DDM |
| 2 | MPLS / DWDM backbone | 3 | 3 | Optical Rx power (dBm), self-managed CE port SFP |
| 3 | SDH/SONET self-healing ring | 3 | 3 | Optical Rx power (dBm), weighted for APS resilience |
| 4 | Microwave PTP | 2 | 3 | RSL / RSSI (dBm) off the ODU |
| 5 | SCPC VSAT (dedicated) | — | 3 | ICMP ping-loss % |
| 6 | IP VSAT (shared TDMA) | — | 2 | ICMP ping-loss % |
| 7 | Private APN (4G/LTE) | — | 2 | Modem RSRP (dBm) |
| 8 | LEO satellite (Starlink-class), emergency backup | — | 1 | ICMP ping-loss % |

11 segments / 20 circuits. Only the first four (wired/carrier-terminated media)
have physical segments; the last four are independent RF paths with nothing to
share, exactly like the original VSAT-IP design.

---

## Why two layers (segments and circuits)

Logical links are **not** independent things that fail on their own — several
share one physical span. `PGN_Circuit1` (Metro-E data trunk, Station1–Station2)
and `PGN_Circuit2` (the channelized voice circuit riding the same fiber) both
ride `seg.pgn_metroe_segment1`. If that fiber gets cut, **both** drop at the
same instant — and so does anything else on it.

Modelling each circuit as its own independent random failure would miss exactly
the behaviour a real SLA report captures. So the catalog has two layers:

| Layer | What it is | Count | Fails… |
|-------|-----------|------:|--------|
| **`segments:`** | Physical/carrier media that fails as a unit — a fiber span, an MPLS CE port, an SDH ring path, or a microwave hop | 11 | independently (a fiber cut, a fade, a correlated ring fault) |
| **`circuits:`** | The 20 logical links | 20 | as the **worst** of the segment(s) it rides |

One span is **shared** by two circuits (channelized voice riding the same fiber
as its data trunk):

| Physical segment | Circuits riding it |
|------------------|---------------------|
| `seg.pgn_metroe_segment1` | `circ.pgn_metroe_circuit1` (data trunk) + `circ.pgn_metroe_circuit2` (channelized voice) |

Two circuits are **multi-hop** — they ride 2-3 segments in series:
`circ.pgn_metroe_circuit3` rides `segment1` + `segment2` + `segment3`
(a long-haul Station1→Station4 trunk), and `circ.pgn_microwave_circuit12` rides
`segment10` + `segment11` (a two-hop relay). A fault on *any* segment in the
chain drops the circuit. The rest carry one dedicated segment each. When a
shared or chained span goes down in the simulator, every circuit riding it
goes down **together** — the whole point of the model. This is a genuine
cross-host **dependency**, not the same-host `correlation` feature (which only
nudges a stream's next roll probabilistically and, by its own docstring, only
couples streams on the same host).

### How the dependency is simulated

Each tick, [`segment_forces`](../otobs/simulate.py) resolves the current band of
every segment, then **hard-forces** each circuit with a `depends_on` to the
**worst** of its segments, ranked `good < underperform < failed`. A circuit does
**not** roll its own state machine — it's a pure function of its segments. It
reads the segment's *current* (pre-roll) band, exactly like `correlation_forces`,
so it's causal and order-independent, and it composes with the existing realism
layer (the force is merged on top of any correlation force and wins). Backfill
uses the same path, so historical SLA % is consistent with the live stream.

SCPC VSAT, IP VSAT, Private-APN, and LEO circuits have **no** `depends_on`: each
is an independent carrier-managed RF path, so it rolls its own machine (see below).

### Gradual degradation: a span warns before it cuts

A real physical/carrier span rarely flips clean from perfect to dead — its signal
metric drifts toward a failure floor (aging laser, macro-bend, dirty connector,
rain fade, RF contention) so there's usually a *warning window* of low margin /
rising errors before the outage. Every segment type is a **numeric** metric with
three bands, using the signal appropriate to its medium:

| Segment type | Metric | `good` | `underperform` | `failed` |
|---|---|---|---|---|
| Metro-E / MPLS / SDH | Optical Rx power (dBm) | −14 … −4 | −20 … −16 | −30 … −22 (below ~−21 dBm sensitivity floor) |
| Microwave PTP | RSL (dBm) | −35 … −25 | −55 … −45 | −80 … −65 (below typical Rx threshold) |

Because each segment is numeric, the existing **`trend`** realism feature ramps
the value across a band transition (default ~30 min) instead of stepping it — so
under any sim mode with `trend` on (e.g. `realistic`), the metric visibly *slides*
good → underperform → failed, exactly like a real span degrading. No new
mechanic: numeric segment + the trend layer that already ramps every analog metric.

**Decision — how a segment's `underperform` maps to its circuits:** a
warning-level span forces its circuits to **`underperform` (degraded), not
`good`**. Reasoning: low margin means rising errors/retransmits — throughput
drops — but the link is still *up*. That's precisely "impaired but carrying
traffic," so the circuit should show `degraded`, not pretend nothing is wrong
(`good`) and not falsely claim an outage (`failed`). Only a fully `failed`
segment forces its circuits `down`. A circuit's `degraded` state fires a
**warning** trigger that is deliberately **not** SLA-tagged, so — consistent with
the satellite rain-fade rule below — a degraded-but-up circuit does **not** burn
SLA; only a real `down` does.

### The SDH/SONET ring: resilience without a new mechanic

Real SDH/SONET rings run **Automatic Protection Switching** — ITU-T G.841
requires a protection switchover to complete within 50 ms (detection adds up to
~10 ms more), so a single-direction fiber cut on a protected ring is masked
before it ever shows up as a sustained outage. The catalog models this
resilience **without** touching `segment_forces()`'s worst-of-dependents logic:
the three `seg.pgn_sdh_*` segments use the *same* optical-dBm bands as plain
fiber, but their `weights` are skewed hard toward `good`
(`[0.97, 0.025, 0.005]` vs. the default `[0.90, 0.08, 0.02]`). A sustained
`failed` reading only shows up statistically as often as a genuine **correlated**
fault on both ring paths would in reality — rare, but real, and it does count
against SLA when it happens. This was a deliberate choice over adding a
"best-of-two-paths" dependency type: that would require a second mechanic in
`segment_forces()` (worst-of vs. best-of per segment type) for a resilience
story that tuned sim weights already capture just as honestly.

---

## Collection method per media type

This is deliberately **not** uniform, because the real access constraints aren't:

| Media | `collection:` in the catalog | Why |
|-------|------------------------------|-----|
| **Metro-E / fiber, MPLS/DWDM, SDH/SONET** | *optical DDM/DOM* — Rx power (dBm) off the terminating SFP (SFF-8472) | We terminate both ends on equipment we own (Metro-E switch, MPLS CE router, SDH ADM), so we can read the optical margin — the signal that degrades *before* a cut |
| **Microwave PTP** | *RSL* polled via NMS/craft SNMP on the ODU | We own or manage the radio at both ends, same "own both ends" story as fiber, just a different link-budget signal |
| **SCPC VSAT (dedicated)** | *Simple check (`icmppingloss`)* | We do **not** own or have MIB access to the leased satellite modem, so the only honest signal is an ICMP reachability probe. Dedicated carrier = near-zero baseline loss (0-0.5% `good`) |
| **IP VSAT (shared TDMA)** | *Simple check (`icmppingloss`)* | Same access constraint as SCPC, but the remote shares its inroute timeslot pool with other sites on the same beam/hub, so baseline loss is non-zero even in `good` (0-3%) and the weighting skews worse |
| **Private APN** | Modem RSRP via AT command / SNMP MIB on the cellular router | Carrier-managed radio path, no owned segment — but cellular routers expose their own RSRP so we don't need to fall back to a ping check |
| **LEO (Starlink-class)** | *Simple check (`icmppingloss`)* | Portable emergency terminal, no MIB access. Public LEO performance data reports >99.9% uptime and <1% loss in clear conditions, so its `good` band is the tightest of any satellite tier — it's a backup by *role*, not because the link itself is unreliable |

Only the **down** condition counts against the SLA (see tags). Per-medium
thresholds are documented as `sim`/`triggers` in the catalog itself — see the
`_anchors` block in [`catalog/comm_links.yml`](../catalog/comm_links.yml) for
the exact bands and the real-world spec each is grounded in.

---

## Repair time (MTTR): outages last realistically long

A fiber cut, a satellite rain-fade, and a cellular congestion blip do not
recover on the same timescale, and none recover as fast as the raw state
machine (left alone) would flip back. The global `SIM_STICKINESS` scalar can't
express this — it's one symmetric number for every parameter and both up and
down states. So the simulator has a small opt-in realism feature, **`hold`**
(see [sim-config.md](sim-config.md#7-hold--minimum-state-dwell-mttr)): once a
stream enters a band with a configured window, it must stay there for a
randomized `uniform(min, max)` before it can recover.

The `realistic` mode sets these per media type — see the `hold.overrides` block
in [`catalog/sim_config.yml`](../catalog/sim_config.yml) for the exact windows
and the reasoning comment above it (buried-fiber splice hours vs. tower
truck-roll hours vs. satellite rain-fade minutes vs. cellular congestion
minutes vs. LEO's near-instant handover recovery).

Only self-rolling streams carry a dwell: **segments** and the **independent
SCPC/IP-VSAT/APN/LEO circuits**. Wired circuits (Metro-E/MPLS/SDH/microwave) are
slaved to their segment (they mirror it), so their outage length is whatever
their segment's dwell dictates — a shared span's cut keeps *all* its circuits
down for the same window, which is exactly the correlated-outage behaviour the
SLA report is meant to show.

---

## Setting up the SLA yourself (Zabbix UI)

The tooling deliberately stops at the **link data** — the segment/circuit items,
their triggers, and the live simulation. Building the SLA services, the SLA
object, and the dashboard is done by hand in Zabbix (that's the intended job).
Everything below is the plan the data was shaped to support.

**The one thing the loader does for you:** each circuit's **high/disaster**
("down") trigger is pre-tagged `link : <circuit_key>` (e.g.
`link : circ.pgn_metroe_circuit1`) — deliberately *not* the `warning`-level
degraded trigger, so a degraded-but-up circuit won't burn SLA. That tag is the
hook everything else hangs off. Confirm it under *Data collection → Hosts →
COMM-PGN-NOC01 → Triggers*.

> ⚠ **If you already built Services/SLA in Zabbix against the previous catalog
> version, see [Migration note](#migration-note-if-youve-already-provisioned-this-in-zabbix)
> below before re-running `make provision`.**

The target shape (Zabbix 7.0 **Services + SLA**, under *Data collection*):

```
circuit 'down' trigger  --(event tag: link=<circuit_key>)-->  Service (one per circuit)
                                                              tag: sla_group=comm_link
                                                                     |
                             SLA object (SLO 98%, monthly, 24x7) ----+  selects all 20
                             via service_tag sla_group=comm_link         -> 20-row report
```

1. **One Service per circuit** (20). Give each a **problem tag** `link` = the
   circuit key it should track (`circ.pgn_metroe_circuit1`,
   `circ.pgn_scpc_circuit13`, …), and a plain **tag** `sla_group` = `comm_link`
   so a single SLA can select them all.
2. **One SLA object** — *Data collection → SLA → Create SLA*. SLO **98 %**
   (matches the catalog's `{$SLA_TARGET}` macro), **monthly** period, 24×7
   schedule, and a **service tag** `sla_group` = `comm_link`. Zabbix computes the
   SLA report **per service**, so this single SLA already renders one row per
   circuit — you only need separate SLA objects if a link ever needs a
   *different* target.
3. **Dashboard** — a new dashboard with a native **SLA report** widget bound to
   that SLA object shows all 20 circuits' SLA % with Zabbix's own colouring. Map
   its bands to the project's Good / Underperform / Failed scheme.

Because a shared or chained span drops every circuit riding it together (the
simulated dependency above), those circuits' Services will show correlated
downtime — the behaviour the two-layer model exists to produce.

### Migration note (if you've already provisioned this in Zabbix)

This redesign **renames every segment/circuit key** (old real-report-derived
keys → new `seg.pgn_*_segmentN` / `circ.pgn_*_circuitN`) and changes the
topology itself (11 segments / 20 circuits across 8 technologies, replacing the
old 12-13 segments / 24 circuits across 3). If you already ran `make provision`
against an earlier version and built Services/an SLA object by hand:

1. **The 20-24 existing Services will orphan.** Their problem-tag `link` values
   point at circuit keys that no longer exist in the catalog, so they'll stop
   receiving events. Either delete and recreate them against the new
   `circ.pgn_*_circuitN` keys, or bulk-edit each Service's problem tag value.
2. **The SLA object itself does not need to change.** It selects services by
   the `sla_group=comm_link` tag, which is unaffected by circuit-key renames —
   as long as you re-tag (or recreate) the Services with `sla_group=comm_link`,
   the same SLA object picks them back up.
3. **Historical SLA data for the old keys is not migrated.** Zabbix computes
   SLA off event history tied to the old trigger/tag; a brand-new set of
   Services starts a fresh SLA history. This is expected — the underlying
   topology is entirely different data now, not a renamed continuation of it.
4. Run `make check` (catalog validation) and `make provision` (pushes the new
   items/triggers/tags) before touching Services — steps 1-2 above assume the
   new triggers already exist in Zabbix.

---

## Decisions & assumptions

1. **Topology is entirely fictional, not real PGN data.** The real "Media Link
   Komunikasi" SLA report (station names, exact routing, capacities, provider
   contracts) is NDA-protected. `PGN_Station1..8`, `PGN_SegmentN` /
   `PGN_CircuitN` numbering, the shared/multi-hop pattern, and the carrier
   names are invented but structurally realistic — designed independently from
   general WAN-design and public vendor/standards references (SFF-8472 DDM,
   ITU-T G.841, Aviat Networks RSL guidance, SCPC-vs-TDMA contention
   comparisons, 3GPP RSRP benchmarks, public LEO performance data), not from or
   mirroring the real report in any way.
2. **All 20 links are hosted on one NOC host** (`COMM-PGN-NOC01`), because
   these are point-to-point links watched centrally, not per-station assets —
   so the comm-link catalog uses a hand-listed `hosts:` entry, not the
   per-site `host_template` the other four asset classes use.
3. **Failure rates and repair times are simulation knobs, not claims about a
   real network.** The sim deliberately produces occasional, realistically-shaped
   outages so the SLA layer is demonstrable. Frequency = segment band weights +
   `SIM_STICKINESS`; duration = the `hold` MTTR windows above.
4. **The SDH ring's resilience is modelled via weight-skew, not a new
   dependency type** — see [above](#the-sdhsonet-ring-resilience-without-a-new-mechanic).
   This keeps `segment_forces()` untouched; only catalog data changed.

### Deliberately *not* modelled: shared root cause across segments

Two nominally-independent segments can share a physical vulnerability (same duct
bank, same river crossing, same tower, same right-of-way) and fail together for a
cause the model can't see. We **chose not to** simulate this. Rationale: the
deliverable is the per-circuit SLA report, which reads each circuit's uptime
independently — correlated *multi-segment* outages would not change any number or
row it shows, so modelling them would be realism for its own sake. If it's ever
wanted (e.g. for a topology-risk view rather than an SLA view), the minimal form
is a same-host-style `correlation` group between two named segments with a
`strength`, reusing the existing `correlation_forces` path — **not** a new
`segment_forces` extension.
