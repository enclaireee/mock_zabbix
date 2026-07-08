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

Each tick, [`segment_forces`](../otobs/simulate.py) resolves the current band of
every segment, then **hard-forces** each circuit with a `depends_on` to the
**worst** of its segments, ranked `good < underperform < failed`. A circuit does
**not** roll its own state machine — it's a pure function of its segments. It
reads the segment's *current* (pre-roll) band, exactly like `correlation_forces`,
so it's causal and order-independent, and it composes with the existing realism
layer (the force is merged on top of any correlation force and wins). Backfill
uses the same path, so historical SLA % is consistent with the live stream.

VSAT-IP circuits have **no** `depends_on`: each satellite link is an independent
RF path, so it rolls its own machine (see below).

### Gradual degradation: a span warns before it cuts

A real fiber span rarely flips clean from perfect to dead — the terminating SFP's
**optical Rx power** drifts down (aging laser, macro-bend, dirty connector, splice
loss) toward the receiver's sensitivity floor, so there's usually a *warning
window* of low margin / rising errors before loss-of-signal. The model captures
this: a segment is a **numeric** metric — optical Rx power in dBm — with three
bands:

| Segment band | Optical Rx power | Meaning |
|--------------|------------------|---------|
| `good` | −14 … −4 dBm | healthy margin above sensitivity |
| `underperform` | −20 … −16 dBm | low margin — degrading, **warning** |
| `failed` | −30 … −22 dBm | below the ~−21 dBm sensitivity floor → **LOS / down** |

Because the segment is numeric, the existing **`trend`** realism feature ramps the
dBm value across a band transition (default ~30 min) instead of stepping it — so
under any sim mode with `trend` on (e.g. `realistic`), the optical power visibly
*slides* good → underperform → failed, exactly like a real span degrading. No new
mechanic: numeric segment + the trend layer that already ramps every analog metric.

**Decision — how a segment's `underperform` maps to its circuits:** a
warning-level span forces its circuits to **`underperform` (degraded), not
`good`**. Reasoning: low optical margin means rising BER — frames are errored and
retransmitted, throughput drops — but the link is still *up*. That's precisely
"impaired but carrying traffic," so the circuit should show `degraded`, not
pretend nothing is wrong (`good`) and not falsely claim an outage (`failed`). Only
a fully `failed` (LOS) segment forces its circuits `down`. A circuit's `degraded`
state fires a **warning** trigger that is deliberately **not** SLA-tagged, so —
consistent with the VSAT rain-fade rule below — a degraded-but-up circuit does
**not** burn SLA; only a real `down` does.

---

## Collection method per media type

This is deliberately **not** uniform, because the real access constraints aren't:

| Media | `collection:` in the catalog | Why |
|-------|------------------------------|-----|
| **Metro-E / fiber** | *optical DDM/DOM* — Rx power (dBm) off the terminating Metro-E SFP (SFF-8472), same "we own both ends" collection story as the Switch/Router asset class | We terminate both ends on switches we own, so we can read the SFP's optical margin — the signal that degrades *before* a cut |
| **VSAT-IP** | *Simple check (`icmppingloss`)* — a genuinely different collection string | We do **not** own or have MIB access to the leased satellite modem, so the only honest signal is an ICMP reachability probe. Simulated as a packet-loss % (0 → rain-fade → 100 %) |
| **MPLS** | optical DDM/DOM on the **self-managed CE-router SFP** — same as Metro-E | The MPLS backhaul is CPE we terminate at both ends (confirmed with the operator), so we read the CE router's own optical margin, not a carrier black box |

VSAT down = ping loss ≥ 60 % (`high`); 20–60 % is a `warning` (rain fade,
degraded but up). Fiber warning = optical Rx ≤ −16 dBm; down = ≤ −22 dBm (LOS).
Only the **down** condition counts against the SLA (see tags).

---

## Repair time (MTTR): outages last realistically long

A fiber cut and a satellite rain-fade do not recover on the same timescale, and
neither recovers as fast as the raw state machine (left alone) would flip back.
The global `SIM_STICKINESS` scalar can't express this — it's one symmetric number
for every parameter and both up and down states. So the simulator has a small
opt-in realism feature, **`hold`** (see [sim-config.md](sim-config.md#7-hold--minimum-state-dwell-mttr)):
once a stream enters a band with a configured window, it must stay there for a
randomized `uniform(min, max)` before it can recover.

The `realistic` mode sets these per media type, with the reasoning:

| Media (band) | Dwell window | Why |
|--------------|--------------|-----|
| fiber segment `failed` (LOS) | **2–8 h** | a buried OSP fiber cut needs fault-locate + truck-roll + splicing — hours, not a reboot |
| fiber segment `underperform` | 20–90 min | a marginal span (dirty connector, aging SFP, borderline splice) lingers in low-margin before it clears or fully cuts |
| VSAT circuit `failed` | **15–45 min** | a satellite outage is usually rain-fade; it clears as the weather cell passes |
| VSAT circuit `underperform` | 10–40 min | partial fade builds and clears over minutes |

Only self-rolling streams carry a dwell: **segments** and **independent VSAT
circuits**. Fiber/MPLS circuits are slaved to their segment (they mirror it), so
their outage length is whatever their segment's dwell dictates — a shared span's
2–8 h cut keeps *all* its circuits down for the same window, which is exactly the
correlated-outage behaviour the SLA report is meant to show.

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

## Decisions & assumptions

1. **MPLS backhaul (report row 23) — resolved.** Confirmed with the operator that
   it's self-managed CPE terminated at both ends, so it's modelled exactly like a
   Metro-E span (optical DDM on the CE-router SFP). This is *not* a black-box
   carrier cloud, so the VSAT-style ICMP-only model does not apply here.
2. **Segment topology is inferred only from the report's `Lokasi` column.** The
   four shared spans above are the ones two rows explicitly name the same
   endpoints for. No segment beyond what the report implies has been invented.
3. **All 24 links are hosted on one NOC host** (`COMM-MCS-NOC01`, MCS Cilegon),
   because these are point-to-point links watched centrally, not per-station
   assets — so the comm-link catalog uses a hand-listed `hosts:` entry, not the
   per-site `host_template` the other four asset classes use.
4. **Failure rates and repair times are simulation knobs, not claims about the
   real network.** The real billing period showed ~100 % / zero downtime; the sim
   deliberately produces occasional, realistically-shaped outages so the SLA layer
   is demonstrable. Frequency = segment band weights + `SIM_STICKINESS`; duration =
   the `hold` MTTR windows above.

### Deliberately *not* modelled: shared root cause across segments

Two nominally-independent segments can share a physical vulnerability (same duct
bank, same river crossing, same right-of-way) and fail together for a cause the
model can't see. We **chose not to** simulate this. Rationale: the deliverable is
the per-circuit SLA report, which reads each circuit's uptime independently —
correlated *multi-segment* outages would not change any number or row it shows, so
modelling them would be realism for its own sake. If it's ever wanted (e.g. for a
topology-risk view rather than an SLA view), the minimal form is a same-host-style
`correlation` group between two named segments with a `strength`, reusing the
existing `correlation_forces` path — **not** a new `segment_forces` extension.
