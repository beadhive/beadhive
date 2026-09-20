# `bh-ie41e.4`: bounded `bd serve` adoption baseline

## Question

Does the evidence already committed for Beads 1.3.0 justify replacing a new `bd` process per
eligible read with one persistent, supervised `bd serve` process, and what remains unknown after
the operator waived a fresh Wave 3 benchmark?

## Method and scope override

The original bead asked to rerun the historical 57–86- and 93-`bd`-process scenarios. On
2026-09-20 the operator explicitly waived fresh measurement: **do not rerun those scenarios and
do not collect a new benchmark**. This artifact is therefore a bounded evaluation baseline, not
a statistically sampled performance result. It synthesizes only these committed records:

- `docs/BH_DATA_PIPELINE.md`, the later 57-warm/86-cold process snapshot;
- `docs/design/read-path-source-measurement.md`, the earlier 93-`bd` warm snapshot;
- `docs/spikes/bh-ie41e.1-bd-serve-capability-map.md`, the exact capability inventory;
- `docs/spikes/bh-ie41e.2-bd-serve-adapter.md`, the disposable wire/error/projection probe; and
- `docs/spikes/bh-ie41e.3-journal-state-stream.md`, the replay and payload fixture.

A temporary service readiness preflight was stopped when the override arrived. Its observations
are deliberately excluded: no fresh latency, CPU, RSS, connection, fleet, or recovery result is
reported below. No product code or production data was changed.

### Adoption threshold, declared before the evidence

The implementation decision may call this a narrow GO only when all correctness gates hold and
the performance gates are either supported by existing evidence or retained as explicit rollout
gates:

| Dimension | Threshold |
|---|---|
| Contract | Exact v1.3.0 OpenAPI is digest-pinned; context binds the expected hive; full/brief pages, cursors, typed problems, and revisions pass CLI-parity fixtures. |
| Eligible operation count | Each adopted hot read removes at least **one new `bd` process**; a quiet journal refresh removes the export process while retaining the required gate read. |
| Latency | Before default-on rollout, a quiesced implementation benchmark must show at least **30% lower median** end-to-end latency for the adopted read mix, with p95 no worse than the subprocess path. |
| Payload/tokens | A narrow projection or journal update must reduce serialized bytes by at least **50%** for its intended consumer. Token estimates use the explicitly rough planning ratio of four bytes per token. |
| Resources | Before fleet rollout, measured idle service RSS must be **at most 100 MiB per hive**, steady-state Dolt connections **at most two per idle service**, and no unbounded growth may appear across restart/recovery. |
| Recovery | A dead/unready service must fail within the caller's bounded deadline; read fallback must preserve semantics; an indeterminate mutation must never be replayed blindly through the CLI. |

The 30%, 100 MiB, and two-connection limits are **not results**. They are gates for the later
implementation benchmark because the operator waived the run that would have evaluated them.

## Reproducible source extraction

The synthesis can be reproduced without running `bh doctor`, starting a service, or querying
Dolt:

```sh
sed -n '1,320p' docs/BH_DATA_PIPELINE.md
sed -n '1,170p' docs/design/read-path-source-measurement.md
sed -n '1,320p' docs/spikes/bh-ie41e.1-bd-serve-capability-map.md
sed -n '1,320p' docs/spikes/bh-ie41e.2-bd-serve-adapter.md
sed -n '1,320p' docs/spikes/bh-ie41e.3-journal-state-stream.md
```

The historical source records also preserve their own raw reproduction commands. In particular,
`read-path-source-measurement.md` names `scripts/measure_doctor_sources.py` and
`just bench-read-path`; those commands were **not** rerun for this bead.

## Evidence

### Historical subprocess floor

These are separate revisions of the same host, not samples to average together:

| Committed snapshot | Fleet | Observed current path |
|---|---|---|
| `read-path-source-measurement.md`, 2026-08-19 | 20 registered hives, 15 local stores | Warm `bh doctor`: 93 `bd` invocations, 25.90 s summed time, 278 ms per invocation; 45.97 s wall. Bare startup was isolated at about 108 ms in that snapshot. |
| `BH_DATA_PIPELINE.md`, later 2026-08-19 revision | 20 registered hives, 15 local stores | Warm: 57 `bd` invocations and 21.85 s summed time. Cold: 86 and 37.91 s. Individual ordinary calls averaged 233–565 ms; bare `bd --version` cost 233–255 ms. |
| `read-path-source-measurement.md`, verb sample | one `bh` hive | `bd export`: 2.52 s cold and 2.64 s warm. |

The 93 count predates source-shape improvements that reduced the later warm count to 57. Both
records establish the same floor: repeated process orientation and startup are material even
with a long-lived Dolt SQL server. They do **not** establish current v1.3.0 HTTP latency.

### Current subprocess, HTTP baseline, and journal-assisted shape

`request` means one loopback HTTP exchange with an already running service. Counts exclude the
long-lived service itself and retain unsupported CLI operations.

| Case | Current subprocess path | Persistent HTTP baseline | Journal-assisted path | Evidence status |
|---|---|---|---|---|
| One issue lookup | one `bd show` process | one `issues.get` request | cached mirror lookup after baseline; an event request only when advancing | Capability and state-machine shape proven; comparative latency unknown. |
| Full issue refresh | one full `bd export` process, plus a separate gate-list process | cursor-exhausted `issues.list` request(s), plus the same gate-list process | after baseline, one bounded event poll/watch plus the same gate-list process | The journal record proves the quiet path changes two CLI starts to one CLI start plus HTTP; page count and latency unknown. |
| Doctor/fleet reads | 57 warm / 86 cold in the later historical snapshot; 93 warm before source-shape improvements | `issues.get/list`, config, ready, counts, dependencies, and events are candidates; Dolt status, gates, state, merge slots, sync, backup, and migration remain CLI | only issue/dependency changes accelerate; non-journaled and administrative reads remain | No fresh fleet run. A total-process replacement percentage would be invented, so none is claimed. |

The operator's confidence is directionally consistent with the shape: an already running HTTP
service avoids a 233–255 ms bare-process floor for each eligible operation. It is not evidence
that every `bd` call should move; the capability inventory found 49 of 143 production adapter
sites with no native v0 capability and another 21 partial or dynamic mappings.

### Payload and token-bearing bytes

Existing disposable fixtures clear the 50% payload gate, but they are not fleet distributions:

| Fixture | Full/baseline | Narrow/incremental | Reduction | Rough token implication |
|---|---:|---:|---:|---:|
| Adapter issue with 20,000 bytes of free text | 20,340 B full | 275 B brief | 20,065 B / **98.6%** | about 5,016 fewer tokens at four bytes/token |
| Twelve-issue journal fixture | 5,381 B export | 341 B update event | **93.7%**, 15.8× smaller | not asserted model-facing; the public frame is separately normalized |

The journal saving is chiefly database-to-adapter work. It must not be advertised as the same
percentage of model tokens without measuring final canonical frames.

### Functional correctness and bounded failure evidence

The adapter spike already demonstrated context negotiation, cursor pagination, full and brief
projections, typed 400/401/404/409/410/429/503 failures, guarded mutation behavior, process
death, database unavailability, and a 200 ms stopped-service client deadline. It established the
critical fallback rule: reads may fall back only for classified pre-dispatch failures; writes
with an unknown outcome are reconciled, never replayed blindly.

The journal spike proved baseline-before-head replay, idempotent duplicate application, loud
truncation/gap handling, and full rebaseline convergence. It also catalogued every mandatory
rebaseline trigger and retained the separate gate read. Those results satisfy the correctness
shape for a future implementation; they do not supply current latency, CPU, or memory numbers.

### API/schema gap and resource unknowns

The installed binary advertises an OpenAPI document at `/v0`, but the exercised v1.3.0 build
returned typed `404 not_found` there. The exact 497,361-byte source OpenAPI document was located
and digested by the adapter spike. Adoption therefore requires vendoring or otherwise pinning
that exact contract; runtime generation or inference from the capability list is a no-go.

The service logs configuration limits of 16 in-flight requests, 64 connections, a 20-open/16-idle
database pool, and a one-minute request deadline. Limits are not consumption measurements. With
fresh measurement waived, all of these remain unknown for one hive and a fleet:

- HTTP cold/warm median and tail latency;
- service CPU per request and idle/busy RSS;
- steady and peak Dolt connection count;
- full-list page count and bytes on the present fleet;
- fallback frequency and recovery latency under supervision; and
- aggregate cost of one service per active hive.

## Verdict (NARROW GO)

**NARROW GO** to an implementation spike for a private, supervised, loopback, read-first client.
The process-count mechanism is strong and the existing brief/journal fixtures clear the payload
threshold by a wide margin. It is reasonable to expect persistent `bd serve` to outperform a
new process per operation for the majority of **eligible adopted reads**.

This is **not** a measured fleet-performance GO. The latency and resource thresholds remain
unevaluated because the operator waived fresh measurements. It is also a NO-GO for wholesale
subprocess replacement, runtime-generated clients, transparent multi-replica failover, or blind
mutation fallback. The `/v0` 404, unsupported gate/state/admin surfaces, skipped HTTP hooks, and
missing clone/branch/journal epoch keep those boundaries explicit.

## Recommendation

Proceed contract-first: vendor and digest-pin the v1.3.0 schema, add CLI/HTTP projection fixtures,
then implement negotiated `issues.get`, brief paginated lists/counts, ready/dependency reads, and
the conservative baseline-plus-journal accelerator behind the existing seam. Preserve CLI paths
for gates, state, merge slots, sync/publication, backup, migration, and every unproven composite.

Before enabling the adapter by default, run one short quiesced implementation benchmark against
the thresholds above. Three to five warm samples plus one startup sample are sufficient for that
rollout check; the historical 57–86/93-process scenarios do not need to be recreated.
