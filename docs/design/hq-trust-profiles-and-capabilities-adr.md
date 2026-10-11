# HQ trust profiles, composable host capabilities, elected tenure and batch bead sync

**Status:** **proposed** (operator design session, 2026-10-09) · **Date:** 2026-10-09
**Amended:** 2026-10-10 by the operator replan of `bh-sfp9o` (`bh-sfp9o.3`): the six dimensions
and the capability ceiling per profile, `open` never forwards and warns, frame-ref merges without
`--strategy`, and open question 1 narrowed. The record stays proposed until `bh-qj2x0` accepts it.
**Amended:** 2026-10-11 by `bh-xwy5x`: open question 1 answered (GO with conditions; section 9).
**Design bead:** `bh-7jq08` (this record) · **Implementation molecule:** filed from this record by
`bh plan file`; its epic links back here.
**Amends:** [hive-writer-partitioning-adr.md](hive-writer-partitioning-adr.md) §3 (asymmetric
roles) and §4 (`failover_after` per role); [HQ — Authority modes](../HQ.md#authority-modes)
(`bh-taa04`, 0.24.0) becomes one column of the trust-profile table.
**Relates:** [frame-eligibility.md](frame-eligibility.md), [FORWARD-WRITE-PATH.md](../FORWARD-WRITE-PATH.md),
[frame-dolt-server-hq-mode-adr.md](frame-dolt-server-hq-mode-adr.md),
[dolt-first-hq-config-cutover-adr.md](dolt-first-hq-config-cutover-adr.md).
**Supersedes:** nothing. Every invariant of the amended ADRs stands except where this record says
otherwise; the writer fence, placement CAS and conditions 13–18 are unchanged.

## Context

By 0.24.0 HQ in Dolt has four layers that every bead write passes, in order: the fleet
configuration snapshot (`hq.mode: dolt-server`, verified per process), frame authority and
admission (fourteen eligibility predicates, `signed` or `trusted` mode), placement (one
`hq_live_hive_leases` row per hive, CAS on the director credential) and the in-data writer fence
(`bh_writer.epoch` plus triggers, enforced by `guard_primary`). Each layer is correct on its own.
Administering a fleet through them is not, for five reasons the operator hit in October 2026:

1. **Authoring is gated like execution.** A planner or operator host with no bound runtime frame
   is refused with `AUTHORITY_NOT_READY` or `frame ineligible` even for `bh bd create`
   (`bh-bd09o`, `bh-ld32r`, `bh-lzxo8`). The eligibility set has no author-only profile.
2. **Forwarding is half wired.** `host.forward` exists, but passthrough create ignores the
   forward marker, forwarded claims are gated by the local writer, and replicas lack the
   node-local `bh_local_ident` table (`bh-hy2hp`, `bh-0tjiq`).
3. **Adopt needs the director.** A never-placed hive is seeded by hand, CAS-placed with an
   expected revision, then adopted; `bh hq placement` and `bh hive fence` are hidden verbs bound
   through an operator settings file.
4. **Every command talks to HQ.** The configuration snapshot is loaded 11 times and YAML-parsed
   423 times per `bh` process, about 8 s of a 10 s `bh bd show` (`bh-qrsec`), and a transient
   reader failure fails an unrelated local write closed (`bh-kjxe9`).
5. **`trusted` relaxes one layer.** The 0.24.0 authority mode removes signatures and expiry from
   layer 2 only; layers 3 and 4 still require the director credential and writer placement for
   every bead operation, so a single-operator factory still pays the full ceremony.

## Decision (proposed)

Five changes, designed as one model so that one setting can describe a fleet's trust posture
end to end:

1. **Capabilities, and roles as composable bundles of them.** Eligibility predicates attach to
   capabilities; a host declares a *set* of roles; verbs ask for one capability.
2. **Trust profiles on a spectrum.** `open`, `trusted`, `verified`, `attested`: each sets
   authority, admission, placement, off-primary write policy and tenure cache together, with
   per-knob overrides.
3. **An elected-tenure cache.** A placed frame keeps working against its local Dolt while HQ is
   unreachable, for the length of its tenure, refreshed by the host daemon on a timer rather than
   by every command.
4. **Write policies for non-primary hosts.** `fenced`, `forward`, `open`, chosen by profile and
   overridable per hive.
5. **Batch bead sync.** `hive.sync` settings for batch auto-commit, periodic auto-push and
   auto-pull, run by the host daemon, with the lifecycle verbs that need durability kept
   synchronous.

The operator decisions of 2026-10-10 add six decisions to the list:

1. **A capability ceiling per profile.** `open` and `trusted` remove nothing from a host's role
   set; `verified` and `attested` remove `admin` from any host without the operator key and remove
   self-placement, so `place` is reached only through the director CAS (section 2).
2. **`open` never forwards.** A non-primary write under `open` lands on the replica's own Dolt
   data, is published to `refs/dolt/frame/<id>` and is merged into `main` by the writer.
   `trusted` forwards through per-frame pinned TLS accounts exactly as `verified` does (section 4).
3. **Child create is refused under `open`** until upstream
   [gastownhall/beads#4796](https://github.com/gastownhall/beads/issues/4796) gives frame-scoped
   child ids; top-level create waits on the `bh-xwy5x` decision. This is a correctness limit, not
   a trust knob (section 4).
4. **Frame refs merge without `--strategy`.** A conflict stops that ref's merge, is recorded as a
   typed outcome and is resolved by the operator with a verb (section 4).
5. **A readiness warning under `open`.** Every non-primary write prints one warning per command
   and is counted, so a fleet can see how much it relies on behaviour the next profile refuses.
   The warning is configurable, on by default, and never refuses (section 4).
6. **`open` tenure is bounded.** A configurable ceiling, default 24 h, replaces "unbounded"
   (section 3).

Everything below is a proposal: no key, verb or predicate named here exists until the
implementation molecule lands it, and the shipped behaviour documented in [HQ](../HQ.md),
[FRAME-FLEET-MEMBERSHIP](../FRAME-FLEET-MEMBERSHIP.md), [FORWARD-WRITE-PATH](../FORWARD-WRITE-PATH.md)
and [CONFIGURATION](../CONFIGURATION.md) stays authoritative until then.

## 1. Capabilities and composable roles

A **capability** is the unit a verb asks for. Each capability names the eligibility predicates
it consults; a verb is allowed when the host's effective capability set contains the capability
and those predicates hold. Predicates not listed for a capability are not evaluated for it.

| Capability | Verbs it gates | Predicates consulted |
|---|---|---|
| `author` | `bh bd create`, `update`, `comment`, `dep`, `label`, `close` of planning items; `bh plan file`, `approve` | identity (`current_frame_incarnation`, `beadyard_binding`), `admitted_active`, `not_cordoned` |
| `claim` | `bh work claim`, `resume`, `abandon`, `assign` | `author` + the hive's writer is this host or reachable through the write policy |
| `execute` | `bh work check`, dispatch, validation runs, `bh work next` | `claim` + `authenticated_fresh_heartbeat`, `release_matches`, `conformance_pass`, `capabilities_match_admission`, `hive_requirements`, `available_capacity`, `dispatch_enabled` |
| `publish` | `bh work submit`, `merge`, `finish`, `push_state` | `execute` + `bh_writer` names this frame (the in-data fence, unchanged) |
| `place` | `bh host lease adopt`, `release`, failover | admitted frame with placement rights for the profile (section 2) |
| `admin` | `bh hq authority *`, fleet-config publish, admission of other frames | HQ write access as the profile defines it |

A **role** is a named bundle of capabilities. Roles compose: a host declares one or more in
`host.roles`, and its effective set is the union of the bundles, capped by the fleet trust
profile's ceiling. A single `host.role` string (today's `executor` / `transient` / `viewer`)
reads as a one-element set, so existing manifests need no migration.

| Role | Capabilities | Placement tenure (`failover_after`) |
|---|---|---|
| `viewer` | read only | never placed |
| `planner` | `author` | never placed |
| `operator` | `author`, `admin` | never placed |
| `transient` | `author`, `claim`, `execute`, `publish` | 30 min (unchanged) |
| `executor` | `author`, `claim`, `execute`, `publish`, `place` | 60 min, floor 45 min (unchanged) |
| `director` | `place`, `admin` (placement half) | never placed |

Rules:

- **Union, then ceiling.** `[planner, executor]` is `executor` plus nothing new;
  `[operator, executor]` is a workstation that authors everywhere, administers HQ and executes
  the hives placed on it (`bh-ld32r`). The profile's capability ceiling (section 2) can remove
  `admin` and self-placement from every host regardless of roles.
- **Tenure follows the highest-tenure role in the set.** The `hq_live_failover_policy` table
  keeps its `executor` / `transient` rows; a set containing `executor` uses the executor row.
- **Admission approves the set.** The grant's capability list already feeds the
  `capabilities_match_admission` predicate; the declared role set is projected into that list,
  so an operator admits exactly the capabilities a host may exercise and nothing in the
  predicate code changes shape.
- **Verbs name one capability.** `guard_primary` becomes the check for `publish`; a new
  `guard_capability(name)` sits in front of every write verb, and `bh host eligible` reports the
  decision per capability, not as one eligible/ineligible bit.

## 2. Trust profiles

A profile sets five knobs at once (authority, admission, placement, off-primary write policy and
tenure) and, through them, a sixth dimension, revocation latency; it also sets a capability
ceiling. Profiles are named by the trust they require of the environment, least to most, not by
who uses them.

| Profile | Authority | Admission | Placement | Writes off-primary | Tenure cache | Revocation latency | Capability ceiling removes |
|---|---|---|---|---|---|---|---|
| `open` | unsigned; any host with HQ write access holds `admin` (single-operator lab posture, printed by `bh doctor`) | on registration (`hq.admission_policy: open`) | self-place on a hive with no live writer session; no director fallback (a live writer is waited out to tenure or failover) | `open`, forwarding disabled: replicas write their own data, published to `refs/dolt/frame/<id>` and merged by the writer; child create refused | bounded by `hq.tenure`, a configurable ceiling, default 24 h | next successful refresh | nothing |
| `trusted` | unsigned, content honoured (today's `trusted`) | open via `bh hq authority join` | self-place when the hive has no live writer session; otherwise director | `forward` through per-frame pinned TLS accounts, as `verified` | = the hive's `failover_after` | one refresh interval | nothing |
| `verified` | signed, non-expiring (today's `signed`) | operator-granted | director CAS only | `forward` through per-frame pinned TLS accounts (option A) | bounded by `hq.tenure` | one refresh interval | `admin` from hosts without the operator key; self-placement |
| `attested` | signed with finite `--duration` | granted plus three fresh heartbeats | director CAS, failover loop on | `forward` only; `author` needs a fresh heartbeat | TTL only, no offline grace | immediate on the next command | `admin` from hosts without the operator key; self-placement |

### The six dimensions

Read the table one column at a time when choosing a profile. Each column is one dimension; the
first five are knobs a profile sets and an override can tighten, the sixth follows from them.

**Authority** controls whether HQ's authority rows (frame bindings and fleet-config publications)
carry signatures and expiry, and therefore who can administer the fleet. Under `open` nothing is
signed and any host with HQ write access holds `admin`: the single-operator lab posture, which
`bh doctor` prints so it is never adopted by accident. Under `trusted` rows are unsigned but their
content is honoured, as in 0.24.0's `trusted` mode, and `admin` follows the host's roles. Under
`verified` rows are signed and do not expire (today's `signed`). Under `attested` they are signed
with a finite `--duration`, so a binding lapses unless it is renewed.

**Admission** controls how a registered host becomes `admitted_active`, the predicate every
capability consults. Under `open` registration alone admits (`hq.admission_policy: open`). Under
`trusted` a host is admitted through `bh hq authority join`, as 0.24.0 auto-admits a trusted
fleet. Under `verified` an operator grants admission (`hq.admission_policy: manual`). Under
`attested` the grant takes effect only after three fresh authenticated heartbeats, and a stale
heartbeat restarts the count.

**Placement** controls who may make a frame a hive's writer: the `hq_live_hive_leases` CAS and
the in-data fence bump, which are unchanged in every profile. Under `open` a frame self-places
with `bh host lease adopt` on a hive with no live writer session, and there is no director
fallback: a hive with a live writer is waited out until that writer's tenure ends or failover
fires, and the refusal says which and when. Under `trusted` a frame self-places on a hive with no
live writer session and otherwise needs the director, whose command the refusal prints. Under
`verified` placement is the director CAS only. Under `attested` it is the director CAS with the
failover loop on.

**Off-primary write policy** controls what a host that is not a hive's writer may do with bead
writes (section 4). Under `open` the `open` policy applies with forwarding disabled: the write
lands on the replica's own Dolt data, is published to `refs/dolt/frame/<id>` and is merged by the
writer, child create is refused, and every such write prints the readiness warning. Under
`trusted` and `verified` the `forward` policy sends the write to the primary through a per-frame,
address-pinned, TLS-required account; the two profiles use the same forward mechanism and differ
in authority, admission, placement and tenure instead. Under `attested` the policy is `forward`
only, and `author` also needs a fresh heartbeat.

**Tenure** controls how long a placed frame keeps working from its elected-tenure cache without a
successful HQ refresh (section 3). Under `open` tenure is bounded by `hq.tenure`, a configurable
ceiling that defaults to 24 h; it is never unbounded. Under `trusted` it equals the hive's
`failover_after`. Under `verified` it is bounded by `hq.tenure` and `failover_after`, whichever is
shorter. Under `attested` it is the heartbeat TTL, with no offline grace. When tenure lapses,
`publish` and `place` refuse in every profile; `author` and `claim` continue only under `open` and
`trusted`.

**Revocation latency** is how long a cordon, retirement or quarantine published at HQ takes to
reach a frame. It is not a separate knob; it follows from how the tenure cache refreshes. Under
`open` it is the next successful refresh, which can be as late as the tenure ceiling while HQ is
unreachable. Under `trusted` and `verified` it is one refresh interval (`hq.tenure_refresh`).
Under `attested` it is immediate: the eligibility boundary keeps today's fresh authoritative
reread, so the next command sees it.

### Capability ceiling

After a host's roles are unioned (section 1), the profile's ceiling removes capabilities from
every host regardless of the roles it declares. Under `open` and `trusted` the ceiling removes
nothing, so the roles alone decide (and under `open` any host with HQ write access also holds
`admin`, as above). Under `verified` and `attested` the ceiling removes `admin` from any host that
does not hold the operator key, and removes self-placement: `place` is reached only through the
director CAS, and `bh host lease adopt` without it refuses and prints the director command.

### Profile rules

- `hq.trust_profile` is a **fleet** key published like `hq.default_authority_mode` today: raising
  trust (towards `open`) needs the operator key once, lowering it always takes effect. The
  existing `hq.default_authority_mode` becomes the first column; `bh hq authority mode-trusted`
  stays as the alias for selecting the `trusted` profile's authority column.
- **Per-knob overrides** are host-local (`hq.placement_mode`, `hive.write_policy`,
  `hq.tenure`) or per hive (`hive.write_policy`). An override can only move one knob
  *towards more trust required* than the profile unless the operator key signs it, so a key-less
  HQ writer cannot loosen a `verified` fleet.
- `bh doctor` prints the effective profile and every override on one line, for example
  `profile trusted; placement overridden to director (host.yaml)`.
- `bh hq why <hive>` prints which layer refused the last write and the one command that fixes it.

## 3. Elected-tenure cache

Today the writer decision is already local (the in-data `bh_writer` epoch) but every `bh`
process still re-reads and re-validates the HQ configuration snapshot and re-runs the eligibility
read against HQ. The cache moves both to the host daemon:

- **What is cached.** The verified configuration snapshot (keyed by its revision and the reader's
  floor) and the per-capability eligibility decision for each hive this host is placed on, with
  the authority revision they were derived from.
- **Tenure.** The cache expires at `min(hq.tenure, failover_after of the hive)`. `hq.tenure`
  defaults to the hive's `failover_after`; `attested` sets it to the heartbeat TTL. Under `open`
  the cache expires at `hq.tenure` alone, a configurable ceiling that defaults to 24 h: an `open`
  frame outlives a short `failover_after` while HQ is unreachable, but never works unbounded.
- **Refresh.** The host daemon refreshes on `hq.tenure_refresh` (default `failover_after / 4`)
  and on every successful heartbeat; a CLI command never refreshes synchronously. A command that
  finds no cache (fresh host, daemon down) falls back to today's per-process read once and seeds
  the cache.
- **HQ unreachable.** Commands keep working from the cache until tenure lapses, printing one
  warning per hour (`HQ unreachable since …; tenure ends at …`). When tenure lapses without a
  refresh, `publish` and `place` are refused; `author` and `claim` continue in `open` and
  `trusted` profiles, and are refused in `verified` and `attested`.
- **Revocation.** Cordon, retire and quarantine are learned at the next refresh, so revocation
  latency equals the refresh interval; the profile table makes that delta explicit. `attested`
  keeps today's fresh authoritative reread at the eligibility boundary.
- **Not cached.** The placement CAS, `bh work submit` and `finish` state pushes, authority
  writes. Condition 18 of the partitioning ADR (state and work travel together) is unchanged.

This replaces the per-process snapshot reload (`bh-qrsec`) and the fail-closed transient reader
error (`bh-kjxe9`) with one timer in the daemon.

## 4. Write policies for non-primary hosts

| Policy | Who may write | Allowed operations off the primary | Mechanism |
|---|---|---|---|
| `fenced` | the writer frame only | none | today's behaviour: `guard_primary` refuses every write verb on a non-writer |
| `forward` | any host with `author` or `claim` | create, update, claim, comment, dep, label, close | bh's bd points at the primary's hive `dolt sql-server` with a per-frame account pinned to the frame's address and requiring TLS ([FORWARD-WRITE-PATH](../FORWARD-WRITE-PATH.md), option A), under `trusted` and `verified` alike; provisioning already refuses a shared login; claims are granted in one place |
| `open` | any host with `author` | comments, labels, notes, status, dependencies, claims, close, and top-level create subject to `bh-xwy5x`; child create refused | never forwards: the replica writes its own Dolt data, publishes it to `refs/dolt/frame/<id>` and the writer merges it into `main` without `--strategy`; the partitioning ADR's restricted "branch" path, made first-class |

The profile supplies the default: `open` for `open`, `forward` for `trusted` and `verified`,
`forward` only for `attested`, and `fenced` for a hive whose forwarder is not configured.
`forward` needs `bh-hy2hp` and `bh-0tjiq` (the remaining forwarding fixes after `bh-7yuny`)
before it is usable from an operator host. `open` is the only policy that lets a replica write
with the primary unreachable.

### `open` never forwards

Under `open` a hive's forward marker and the host's `host.forward` endpoint are ignored, and
`bh hive forward point` refuses, naming the profile. A non-primary write lands on the replica's
own Dolt data and is published only to the frame-private ref `refs/dolt/frame/<id>`, never to
`main`; the hive's writer merges it.

Allowed under `open`: comments, labels, notes, status, dependencies, claims, close, and top-level
create once the `bh-xwy5x` decision confirms that bd's hash ids merge cleanly across replicas
(until then top-level create is refused with a pointer to it).

Refused under `open`: child create (`<epic>.N`), until upstream
[gastownhall/beads#4796](https://github.com/gastownhall/beads/issues/4796) gives frame-scoped child
ids. Two replicas allocate the same child id, and a merge would silently drop one of the two
beads ([`bh-sieai` section 9](../spikes/bh-sieai-write-guard-and-nonprimary-writes.md#9-branch-path-merges-collisions-and-contention)).
This is a correctness limit, not a trust knob: no profile or override lifts it.

### Merging frame refs

The writer merges each `refs/dolt/frame/<id>` into `main` and never passes `--strategy`, because
`bd vc merge --strategy` resolves a conflict by silently dropping the losing side (`bh-sieai`
section 9). A clean merge lands and records a typed outcome. A conflict, including two frames
claiming one bead, stops that ref's merge and is recorded as a typed outcome with the conflicting
rows (table, bead id, both values; a double claim names both holders). `bh doctor` and
`bh hq why` report it per hive and frame, and the operator resolves it with a verb that takes
`main`'s side or the frame's side for each bead. A conflicting ref never blocks other frames'
refs or any verb.

### Readiness warning

Every non-primary write under `open` prints exactly one console warning per command. The warning
names the hive and its writer frame and says that under `trusted` and above the write would be
forwarded to that writer. Each warning is also counted as an event, and `bh doctor` reports the
count per hive over a window (`hive.readiness_window`, default 7 days). The purpose is to measure
how much a fleet relies on behaviour the next profile refuses, so the operator can see when it is
ready to harden. The warning is configurable (`hive.readiness_warning`, default on) and is never
a refusal.

## 5. Batch bead sync

Today bead state moves only inside verbs: `submit` and `assign` push synchronously, `claim`
and dispatch pull. The host daemon gains a sync loop with these settings:

| Key | Default | Meaning |
|---|---|---|
| `hive.sync.auto_commit` | `on` | bd's `dolt.auto-commit`: `on` commits every write, `batch` coalesces local writes until the next flush or verb that needs durability |
| `hive.sync.auto_push.enabled` | `false` | the daemon pushes this host's hive data (`bd sync`, or `refs/dolt/data` on Git-backed hives) on a timer and after N commits |
| `hive.sync.auto_push.interval` | `5m` | timer between pushes while there is anything pending |
| `hive.sync.auto_push.max_pending` | `50` | push early once this many local commits are pending |
| `hive.sync.auto_push.retry_backoff` | `30s..10m` | exponential backoff while the remote is unavailable; a verb is never blocked by a failed background push |
| `hive.sync.auto_pull.enabled` | `false` | replicas and forwarders pull on a timer |
| `hive.sync.auto_pull.interval` | `15m` | timer between pulls |
| `hive.sync.durable_verbs` | `[submit, finish]` | verbs that still push synchronously; others may pass `--await-sync` to wait for the next flush |

Rules:

- **Condition 18 stands.** Lifecycle state is never published ahead of the worktree commits it
  describes, so `submit` and `finish` keep their synchronous push (or wait for the flush that
  includes their commits) regardless of these settings.
- **Only the writer pushes `main`.** On a cut-over hive the auto-push runs under the same
  `bh_writer` check as a manual push; a replica under the `open` policy pushes its frame-private
  ref `refs/dolt/frame/<id>`, never `main`, and the writer merges it as section 4 describes.
- **Outcomes are typed.** Every background push or pull records a typed outcome (the
  `bh-nc052.2` contract) and `bh doctor` shows pending commits, last push, last error and the
  next retry per hive.
- **Cadence evidence.** The push-cadence spike `bh-infra-b7at` sets the default interval for the
  central remote; the defaults above are placeholders until it reports.

## 6. Configuration surface (proposed keys)

| Key | Scope | Values |
|---|---|---|
| `hq.trust_profile` | fleet (operator-published) | `open`, `trusted`, `verified`, `attested` (default `verified`, which equals today's defaults) |
| `host.roles` | host manifest | list of `viewer`, `planner`, `operator`, `transient`, `executor`, `director`; `host.role` remains as a one-element alias |
| `hq.placement_mode` | host override | `director`, `self` |
| `hive.write_policy` | fleet default, per-hive override | `fenced`, `forward`, `open` |
| `hive.readiness_warning` | fleet default, per-hive override | `on` (default), `off`: the readiness warning under `open` (section 4) |
| `hive.readiness_window` | fleet default | duration over which `bh doctor` counts readiness warnings (default 7 days) |
| `hq.tenure`, `hq.tenure_refresh` | host override | durations; under `open`, `hq.tenure` is the tenure ceiling (default 24 h) |
| `hq.admission_policy` | fleet | `manual` (today) or `open`; the profile sets it |
| `hive.adopt_existing` | host, onboarding | `true`: `bh hive add` and `onboard` read the store's own prefix and project id instead of deriving them (`bh-ajgot`, `bh-6csoa`) |
| `hive.sync.*` | fleet default, per-hive override | section 5 |

No key here is settable at fleet scope when the shipped doc marks it HOST-only, and no
fleet key changes shape: `hq.default_authority_mode` stays valid as the profile's first column.

## 7. Invariants kept

- The data decides who writes: `bh_writer.epoch`, the triggers and the placement CAS are
  untouched. No profile or cache can make two frames write one hive.
- Conditions 13–18 of the partitioning ADR stand. The advisory-heartbeat escape is retired by
  `bh-bnpgq` before any additional executor is admitted, in every profile.
- No merge of a frame ref passes `--strategy`; every conflict is surfaced and resolved by the
  operator, never silently.
- The operator key never moves onto a frame; `open` and `trusted` make it unnecessary for
  routine work, as 0.24.0 already does.
- Host-only keys stay host-only; a profile can only be raised towards `open` by an
  operator-signed publication.

## 8. Reconciliation with existing beads

| Bead | Relationship |
|---|---|
| `bh-bd09o` (operator and planner hosts author through fenced writers) | delivers `author` and `host.roles`; the implementation molecule absorbs its remaining scope |
| `bh-ld32r` (attended workstation topology) | the `[operator, executor]` role set |
| `bh-lzxo8` (SQL HQ enrollment handoff for xeno-mac and the factory) | `author` without a bound runtime frame, under `trusted` |
| `bh-hy2hp`, `bh-0tjiq` (forwarding bugs) | prerequisites of the `forward` policy; fixed first (`bh-7yuny` landed the forward marker on passthrough create, forwarded claims and replica `bh_local_ident`) |
| `bh-sieai` (write guard and non-primary write paths spike) | section 9 is the evidence for refusing child create under `open` and for merging frame refs without `--strategy` |
| `bh-xwy5x` (top-level create under `open`) | the decision that answers open question 1 |
| gastownhall/beads#4796 (frame-scoped child ids) | upstream prerequisite for child create under `open` |
| `bh-qrsec`, `bh-kjxe9` (per-process HQ reload, fail-closed reader blip) | absorbed by the tenure cache |
| `bh-ajgot`, `bh-6csoa` (onboarding invents identity) | `hive.adopt_existing` |
| `bh-3muxc`, `bh-dcg0o` (one `bh hq sync`, HQ as a federation participant) | the HQ half of the sync loop; filed beside, not inside |
| `bh-nc052.2` (typed sync outcomes) | the outcome contract the sync loop records |
| `bh-neyy4` (partitioning C rollout), `bh-ujp49` (four-executor proof) | unchanged; the profiles ship with `verified` as the default so the rollout is not affected |
| `bh-vfrem` (range grants) | unchanged; it applies in `verified` and `attested` |
| `bh-infra-b7at` (push cadence spike) | sets the auto-push default interval |

## 9. Open questions

1. ~~Top-level create under `open`~~ — **answered by `bh-xwy5x`: GO with three conditions**
   ([decision record](bh-xwy5x-open-top-level-create-decision.md)). Two replicas creating
   top-level beads under `open` and merged by the writer without `--strategy` showed no id or
   `issue_counter` collision with bd's hash ids; a forced collision stops the merge on the
   `issues` row and drops nothing. Counter ids (`issue_id_mode=counter`) always collide. The
   conditions: top-level create stays refused under `open` for a counter-mode hive; the frame-ref
   merge treats bd's per-clone `metadata.clone_id` row as frame-local, since every second frame
   ref conflicts on it whatever was written; and an id collision is never resolved by taking a
   side. The refusal is lifted through `/bh:replan` on `bh-sfp9o`, not by the decision. Child
   create stays refused under `open` until gastownhall/beads#4796.
2. Whether `director` should be a role on an executor (a frame that places itself and others) or
   stay a credential bound only through the operator settings file; the profile table assumes
   the latter for `verified` and `attested`.
3. The `attested` profile's heartbeat requirement for `author`: strict as written, or the
   advisory-with-warning form the eligibility code already supports.

## 10. Rollout

1. Forwarding fixes (`bh-hy2hp`, `bh-0tjiq`) and `author` behind `guard_capability`: an operator
   host can file and edit beads on every hive with no new key.
2. Role sets and the profile table with `verified` as default: no behaviour change for existing
   fleets; `trusted` fleets gain self-placement and open `author`.
3. The tenure cache in the host daemon, with `bh hq why`.
4. `hive.sync` auto-push and auto-pull, after the cadence spike reports.
5. `open` writes, last, behind the profile: local writes published to `refs/dolt/frame/<id>`,
   merged without `--strategy`, with the readiness warning; child create refused, top-level create
   as `bh-xwy5x` decides.
