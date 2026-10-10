# HQ trust profiles, composable host capabilities, elected tenure and batch bead sync

**Status:** **proposed** (operator design session, 2026-10-09) · **Date:** 2026-10-09
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
  the hives placed on it (`bh-ld32r`). The profile ceiling (section 2) can remove `place` or
  `admin` from every host regardless of roles.
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

A profile sets five knobs at once. Profiles are named by the trust they require of the
environment, least to most, not by who uses them.

| Profile | Authority | Admission | Placement | Writes off-primary | Tenure cache | Revocation latency |
|---|---|---|---|---|---|---|
| `open` | unsigned; HQ write access is admin | open: registration suffices | self-place on any hive with no live writer session | `open`: replicas author directly, merged on sync | unbounded while HQ is unreachable | next successful refresh |
| `trusted` | unsigned, content honoured (today's `trusted`) | open via `bh hq authority join` | self-place when the hive has no live writer session; otherwise director | `forward`: `author` works everywhere through the primary | = the hive's `failover_after` | one refresh interval |
| `verified` | signed, non-expiring (today's `signed`) | operator-granted | director CAS only | `forward` with per-frame accounts (option A) | bounded by `hq.tenure` | one refresh interval |
| `attested` | signed with finite `--duration` | granted plus three fresh heartbeats | director CAS, failover loop on | `forward` only; `author` needs a fresh heartbeat | TTL only, no offline grace | immediate on the next command |

Rules:

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
  defaults to the hive's `failover_after`; `attested` sets it to the heartbeat TTL.
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
| `forward` | any host with `author` or `claim` | create, update, claim, comment, dep, label, close | bh's bd points at the primary's hive `dolt sql-server` with a per-host account ([FORWARD-WRITE-PATH](../FORWARD-WRITE-PATH.md), option A); claims are granted in one place |
| `open` | any host with `author` | non-allocating edits only: comments, labels, notes, status of beads already held; create with a reserved id range | the replica writes its own Dolt data and `hive.sync.auto_push` merges it; the partitioning ADR's restricted "branch" path, made first-class |

`forward` is the default for `trusted` and `verified`; it needs `bh-hy2hp` and `bh-0tjiq`
(forward marker on passthrough create, forwarded claims not gated by the local writer, replica
`bh_local_ident` provisioning) before it is usable from an operator host. `open` is the only
policy that lets a replica write with the primary unreachable, and it is limited to edits that
cannot allocate a conflicting id.

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
  ref, never `main`.
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
| `hq.tenure`, `hq.tenure_refresh` | host override | durations |
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
| `bh-hy2hp`, `bh-0tjiq` (forwarding bugs) | prerequisites of the `forward` policy; fixed first |
| `bh-qrsec`, `bh-kjxe9` (per-process HQ reload, fail-closed reader blip) | absorbed by the tenure cache |
| `bh-ajgot`, `bh-6csoa` (onboarding invents identity) | `hive.adopt_existing` |
| `bh-3muxc`, `bh-dcg0o` (one `bh hq sync`, HQ as a federation participant) | the HQ half of the sync loop; filed beside, not inside |
| `bh-nc052.2` (typed sync outcomes) | the outcome contract the sync loop records |
| `bh-neyy4` (partitioning C rollout), `bh-ujp49` (four-executor proof) | unchanged; the profiles ship with `verified` as the default so the rollout is not affected |
| `bh-vfrem` (range grants) | unchanged; it applies in `verified` and `attested` |
| `bh-infra-b7at` (push cadence spike) | sets the auto-push default interval |

## 9. Open questions

1. Reserved id ranges for `open` writes on replicas: a prefix-scoped allocation band, or refuse
   `create` under `open` until Beads supports per-replica allocation.
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
5. `open` writes with reserved ids, last, behind the profile.
