# IoT fleet walkthrough

Run a deterministic EWMA telemetry Line over an edge camera fleet (one central
hub and three cameras) so routine batches and transient spikes settle at zero
LLM tokens, while a sustained thermal fault or flatlined sensor wakes
`cruxible next` and invalidates prior `healthy` sign-offs automatically.

### What you'll have by the end

- A governed fleet instance running `iot-fleet:2.1.0` and its bundled
  pure-Python `iot-telemetry-provider` (`telemetry.analyze`).
- A commissioned site (`yard-north`) with one hub (`hub-north-01`), three
  cameras (`cam-gate-01`, `cam-dock-02`, `cam-lot-03`), four temperature
  sensors, and four approved operating baselines.
- Four live Lines under a strict `scope: observations` Mandate boundary:
  `fleet.telemetry-ingest.hub`, `fleet.sensor-health.hub`,
  `fleet.anomaly-ingest.hub`, and `fleet.attention`.
- Live proof of spike absorption on Batch 1 (`cruxible next` stays quiet) and
  automatic fault escalation on Batch 2 (`claim_dependency_stale` on the prior
  `condition=healthy` sign-off plus `line_inbox_unread` for both anomalies).

### What you'll do

1. Initialize Cruxible, install `iot-fleet:2.1.0` from GHCR, and pull the
   verified example fixtures with `cruxible kit pull`.
2. Seed the hub, three cameras, sensors, and approved thermal baselines via the
   typed Python SDK (`seed_fleet.py`).
3. Adopt the kit's automation recipes with `cruxible kit adopt` (zero JSON
   hand-authoring) and enable the landing-triggered Lines.
4. Run Batch 1 (single-sample `74.0 C` spike absorbed by the EWMA filter) and
   pin a reviewed `condition=healthy` sign-off to `cam-dock-02`'s live state
   and baseline.
5. Drop Batch 2 (sustained thermal climb + flatlined sensor) and watch
   `cruxible next`, `fleet.assets_needing_attention`, and `fleet.open_anomalies`
   surface only the real faults.

### Prerequisites

Python 3.11+, Git, Linux (x86-64) or macOS (Apple silicon; Windows via WSL2),
and network access to `ghcr.io` (for `iot-fleet:2.1.0`). The bundled telemetry
provider is pure Python stdlib and needs no native wheels or GPU.

---

## 1. Initialize, install `iot-fleet:2.1.0`, and pull examples

Create a fresh scratch repository and install `iot-fleet` from
`ghcr.io/cruxible-ai/kits`:

~~~bash
uv tool install cruxible        # or: pip install cruxible
mkdir camera-hub-demo && cd camera-hub-demo
git init -q -b main
cruxible init --principal-id owner
cruxible kit add iot-fleet --commit
cruxible kit pull iot-fleet --out .cruxible/kits/iot-fleet
~~~

`cruxible kit add iot-fleet --commit` installs the bundled
`iot-telemetry-provider 0.1.0` wheel along with 7 SubjectKinds (`fleet.site`,
`fleet.asset`, `fleet.sensor`, `fleet.firmware`, `fleet.baseline`,
`fleet.anomaly`, `fleet.maintenance`), 39 ClaimTypes, 4 Procedures, 6 saved
queries, and one governed review guidance Document
(`document:fleet.guidance.review-guidance`).

`cruxible kit pull iot-fleet --out .cruxible/kits/iot-fleet` copies the
verified kit bundle, including `examples/camera-hub/` and
`examples/camera-opencv/`, into `.cruxible/kits/iot-fleet`.

## 2. Seed the fleet and approved baselines

Run `seed_fleet.py` through `cruxible python`. It uses the typed `0.6` World
SDK (`w.fleet.site.define(...)`, `w.fleet.asset.define(...)`,
`w.fleet.baseline.define(...)`) to commission `yard-north` in one atomic
generation:

~~~text
$ cruxible python .cruxible/kits/iot-fleet/examples/camera-hub/seed_fleet.py
Seeded yard-north (1 hub, 3 cameras, 4 sensors, 4 approved baselines).
~~~

Notice the approved thermal ceilings (stored in centi-degC so every comparison
stays exact integer math):

- `hub-north-01`: `500` to `6500` (`5.0 C` to `65.0 C`)
- `cam-gate-01` (direct sun): `500` to `6000` (`5.0 C` to `60.0 C`)
- `cam-dock-02` and `cam-lot-03`: `500` to `5500` (`5.0 C` to `55.0 C`)

## 3. Adopt the telemetry automation recipes

Stage the first telemetry batch at `telemetry/hub.json`, adopt both kit recipes
(`iot-fleet` for the attention backbone and `iot-fleet:telemetry` for the hub's
ingest, sensor-health, and anomaly Lines), and enable them:

~~~bash
mkdir -p telemetry
cp .cruxible/kits/iot-fleet/examples/camera-hub/hub-batch-01.json telemetry/hub.json
cruxible kit adopt iot-fleet --yes
cruxible kit adopt iot-fleet:telemetry --param name=hub --param relative_path=telemetry/hub.json --yes
cruxible line enable fleet.sensor-health.hub fleet.anomaly-ingest.hub --runbook fleet.check
~~~

Look at the Mandate boundary these recipes enforce:

| Component | Authority | What it can write |
|---|---|---|
| `fleet.telemetry-ingest.hub` | `settle` (`scope: observations`) | `fleet.asset.operating_state` and the `fleet.telemetry-analysis` Capture |
| `fleet.sensor-health.hub` | `settle` (`scope: observations`) | `fleet.sensor.status` on `capture_landing` (zero recompute) |
| `fleet.anomaly-ingest.hub` | `settle` (`scope: observations`) | `fleet.anomaly` observations (`asset`, `metric`, `severity`) on `capture_landing` |
| `fleet.attention` | `observe` | Posts unjudged anomalies to the Line inbox |

No automated Line can touch normative judgments (`fleet.baseline.*`,
`fleet.asset.condition`, `fleet.anomaly.disposition`, or
`fleet.firmware.status`).

## 4. Batch 1: absorb a transient spike and sign off

In `hub-batch-01.json`, `cam-gate-01-temp` reports a single-sample `74.0 C`
(`7400`) spike inside an otherwise normal `42 C` window. Run `fleet.check`,
then pin a reviewed `condition=healthy` sign-off on `cam-dock-02` against its
live `operating_state` and approved baseline (`--sign-off`):

~~~text
$ cruxible runbook run fleet.check
Runbook fleet.check: completed  [RBR-4fe576e287f94b7e8cea1a2c9e221ac1]
   1  succeeded   Line:fleet.telemetry-ingest.hub  (via Procedure:fleet.telemetry-ingest, RUN-c2115416…)
      written 4 (settled 4, proposed 0); unchanged 0; refused 0; inbox 0
   2  succeeded   Line:fleet.attention  (RUN-50ddc766…)
      written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 0

$ cruxible python .cruxible/kits/iot-fleet/examples/camera-hub/seed_fleet.py --sign-off
Signed off cam-dock-02 condition=healthy pinned to operating_state and baseline.

$ cruxible next
No repair work in the observed domains.
~~~

The EWMA gain filter (`alpha=0.3`) and sustained-breach window
(`min_breach_samples=3`) absorbed the single-sample spike deterministically:
all four assets settled as `operating_state=normal`, zero anomalies opened, and
zero LLM tokens were spent.

## 5. Batch 2: sustained thermal fault and flatlined sensor

Now drop `hub-batch-02.json` in place. In this batch:

- `cam-dock-02-temp` climbs past its approved `55.0 C` (`5500`) ceiling for
  three consecutive filtered samples.
- `cam-lot-03-temp` flatlines at `4100` for six consecutive samples.

Run `fleet.check`, wait a moment for the background `capture_landing` Triggers
(`fleet.sensor-health.hub` and `fleet.anomaly-ingest.hub`) to settle sensor
status and anomaly records, then run `fleet.attention` and check `cruxible next`:

~~~text
$ cp .cruxible/kits/iot-fleet/examples/camera-hub/hub-batch-02.json telemetry/hub.json
$ cruxible runbook run fleet.check
Runbook fleet.check: completed  [RBR-a8caaa5513bb4ad687010cca42fced8f]
   1  succeeded   Line:fleet.telemetry-ingest.hub  (via Procedure:fleet.telemetry-ingest, RUN-894cb22f…)
      written 2 (settled 2, proposed 0); unchanged 2; refused 0; inbox 0
   2  succeeded   Line:fleet.attention  (RUN-7ef46359…)
      written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 0

$ sleep 3 && cruxible line run fleet.attention
RUN-0bbd1c8c6af18770…: succeeded  (needs attention)
Counts: written 0 (settled 0, proposed 0); unchanged 0; refused 0; inbox 2 new
~~~

Now ask `cruxible next` and the kit's saved queries what needs human or agent
judgment:

~~~text
$ cruxible next
repair  claim_dependency_stale  Claim:CLM-d679f1aebdecd2a849fc97442a54f5fa  next=cruxible.claim.retire
  input changed: CLM-f0928eefd261c019af1a9cee57a3e11a  current=5c39f0aee63a  used=ff9bc92ee3c0  reasons=source_superseded
  repair: cruxible retire CLM-d679f1aebdecd2a849fc97442a54f5fa --because '<TEXT>'
  or: cruxible get CLM-f0928eefd261c019af1a9cee57a3e11a
  fill in: <TEXT> (why, in your words)
repair  line_inbox_unread  Line:fleet.attention  next=cruxible.inbox.list
  repair: cruxible inbox list --line fleet.attention --status unread

$ cruxible query --name fleet.assets_needing_attention
subject                  asset_type  condition  criticality  operating_state  site                   flags
fleet.asset/cam-dock-02  camera      healthy    high         fault            fleet.site/yard-north  -
fleet.asset/cam-lot-03   camera      -          medium       warning          fleet.site/yard-north  -

$ cruxible query --name fleet.open_anomalies
subject                                                    asset                    metric       severity  flags
fleet.anomaly/cam-dock-02-temperature-bound-breach         fleet.asset/cam-dock-02  temperature  high      -
fleet.anomaly/cam-lot-03-temp-temperature-sensor-flatline  fleet.asset/cam-lot-03   temperature  medium    -
~~~

Look at what the loop delivered:

- `cam-dock-02`'s automated `operating_state` moved from `normal` to `fault`,
  and because the reviewed `condition=healthy` Claim was pinned `based_on`
  `operating_state`, `cruxible next` immediately surfaced
  `claim_dependency_stale` so nobody trusts an outdated sign-off.
- `cam-gate-01` and `hub-north-01` stayed `normal` and untouched.
- Both new anomalies (`bound-breach` and `sensor-flatline`) landed in
  `fleet.open_anomalies` and the `fleet.attention` inbox, ready for an engineer
  or coding agent to inspect, record a reviewed `disposition` (`confirmed`,
  `false_positive`, or `investigating`), and link a `fleet.maintenance` record.

Inspect the full fleet graph and lineage in the read-only Console:

~~~bash
cruxible console
~~~

## Extending the telemetry provider

The bundled `iot-telemetry-provider` is pure Python stdlib so every fleet
installs fast with zero native dependencies. When your fleet needs custom
signal processing or vision checks, adapt and publish your own provider:
`.cruxible/kits/iot-fleet/examples/camera-opencv/frame_health_provider.py`
provides an optional OpenCV starter (blur, lens occlusion, and frozen-frame
detection) ready for your agent to customize and pin with
`cruxible provider publish`.

## Where to go next

- [Lines and automation](lines-and-automation.md): how Procedures, Triggers,
  Mandates, and `@derive` providers work under the hood.
- [Review and governance](review-and-governance.md): require independent cosign
  before changes to approved baselines, asset conditions, or firmware trust
  status land.
- [Project state walkthrough](project-state-walkthrough.md): use `based_on`
  pins and cited Markdown docs to coordinate coding agents across sessions.
- [Security walkthrough](security-walkthrough.md): automate SBOM + OSV/KEV/EPSS
  intelligence and agent-led code audits under two-principal cosign.
