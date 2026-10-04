# Spike `bh-32379` — migration map and cutover plan to the writer-partitioning model

**Bead:** `bh-32379` · **Seat:** `dev/migration` · **Type:** research-only (no product code)
**Parent:** `bh-qlgmm` (hive writer partitioning spike molecule)
**Feeds decision on:** `bh-pr889` (the ADR, then `/bh:replan` from the outline in
[Recommendation §6](#6-sequenced-implementation-outline-for-the-bh-pr889-replan))
**Executable evidence:**
[`tests/spikes/test_bh_32379_cutover_int.py`](../../tests/spikes/test_bh_32379_cutover_int.py)
(`integration` + `dolt_server`, 1 test, about 60 s at `-n0`)

## Question

Can every hive, and the live factory frame, move from today's model to the model the four
sibling spikes proved, along a path that is safe, reversible, and correct while old and new bh
run side by side? Today's model is the host lease, the `refs/bh/epoch` fence and the SQL
trusted receiver. The new model is HQ placement, the in-data epoch fence, the write guard, and
server-timed session and evidence rows. "Safe" means epochs never go backwards and a stale
writer never lands on `main`. The spike also asks exactly what happens to each existing module:
keep, narrow, replace or delete.

The bar, from the bead:

- every module named in the design gets an outcome and a reason;
- the cutover keeps epochs monotonic from `refs/bh/epoch` to `bh_writer`, with a rollback that
  is tested or written out step by step;
- mixed-version behaviour is stated for each coexistence phase;
- a sequenced implementation outline (not filed beads) is ready for `bh-pr889`'s replan.

The operator added one constraint on 2026-10-04: follow-up releases should be **patch**
versions wherever possible. The outline therefore tags each item **patch-safe** (`fix`,
`refactor`, `test`, `docs`, `chore`) or **requires-minor** (`feat`, or a breaking removal,
which `major_version_zero = true` turns into a minor bump).

This spike does **not** re-prove fencing, the guard, placement or liveness. `bh-vje85`,
`bh-sieai`, `bh-cvk70` and `bh-wtsrc` did that, and `bh-jbb6r` composes them. It changes no
product code, and it observed live HQ and services read-only.

## Method

1. **Read the evidence.** I read the proposal
   ([hive-writer-partitioning-proposal.md](../design/hive-writer-partitioning-proposal.md)) and
   all five merged spike docs: [`bh-eybn7`](bh-eybn7-writer-fencing-fixture.md),
   [`bh-cvk70`](bh-cvk70-placement-authority-and-failover.md),
   [`bh-wtsrc`](bh-wtsrc-receiver-removal-liveness.md),
   [`bh-vje85`](bh-vje85-in-data-epoch-fencing.md) and
   [`bh-sieai`](bh-sieai-write-guard-and-nonprimary-writes.md). I took each one's binding
   conditions as inputs to the migration.
2. **Read the code as it stands on `main` at 0.22.0** (`36f93bc8`), not on this spike's
   container base. These are the modules the design names, plus the places that call them.
   - **Lease:** `host_lease.py`, `host_lease_contracts.py`.
   - **Fence and adopt:** `host_fence.py`, `host_adopt.py`.
   - **Guards:** `guard.py` (`guard_primary`, `live_epoch`, `guard_claim_epoch`,
     `bd_write_refusal`, `is_store_publish`), `claim_authority.py`, `frame_eligibility.py`.
   - **Frame lease and heartbeat:** `hq_frame_lease.py`, `hq_framelease_contracts.py`,
     `heartbeat_report.py`.
   - **SQL HQ:** `hq_sql_receiver.py`, `hq_sql_runtime.py`, `hq_sql_runtime_schema.py`,
     `hq_control_plane.py`.
   - **Sync:** `engine.py` (`push_state`, `sync_state`), `hive_sync.py`, `sync_remote.py`.
   - **Other callers:** `prepush.py`, `doctor.py`, `localloop.py`, `host_cli.py` (`eligible`).
3. **Read what 0.22.0 already changed.** That is
   `docs/releases/receiver-free-liveness-0.22.0.md` (on `main`; not yet on this container's
   base) and commits `e61b66c5` (bh-jto52), `9e250892` / `37f70b75` (bh-0acs8), and `9abd412f`
   (bh-7y6b2).
4. **Read the follow-up beads with `bh work issue`:** `bh-cszmo`, `bh-vfrem`, `bh-rjjjo` and
   `bh-np024`, plus `bh-pr889` and `bh-jbb6r`.
5. **Observed the live factory frame, read-only, on 2026-10-04 between 20:49Z and 20:53Z.**
   Nothing was signalled or written.
   - `git ls-remote origin 'refs/bh/*' 'refs/dolt/*'`, then `git cat-file -p` on the fence blob.
   - `bh host list --lease-hive bh [--json]` and `bh host eligible --hive bh --json`.
   - The bh-owned claim records under `<git-common-dir>/bh/worktrees/*`.
   - The `bh` hive's `.beads/metadata.json`, `~/.local/bin/bh` and `systemctl list-units`.
6. **Tested the cutover and its rollback.** The test uses the `bh-eybn7` fixture and the
   `bh-vje85` fence model (`harness.epoch_fence`). It does not change either harness. The
   legacy fence is written into the fixture's bare remote exactly as `host_fence` lays it out:
   a `{"epoch", "host_id", "seq"}` JSON blob at `refs/bh/epoch`, moved only by an old-oid CAS.
   The writer is a bd shared-server frame, as the live `bh` hive is. The second replica is a
   Dolt CLI frame.

   ```sh
   uv run pytest tests/spikes/test_bh_32379_cutover_int.py -n0 -q -p no:cacheprovider
   # 1 passed in 59.5 s (load 3-7 on a 32-core host); a 46.7 s run of an earlier cut also passed
   ```

## Evidence

### Live facts the cutover has to start from

**L1. The writer epoch is 221, not 2.** The bead's text says "epoch 2". That number dates from
when the proposal was written.

- `refs/bh/epoch` on the `bh` hive remote is blob `2a8dd075` with content
  `{"epoch":221,"host_id":"6ae345b9-81a8-4c9b-8661-c5a4420fc12d","seq":0}`. `seq 0` means no
  managed push has reserved the fence since the last adopt.
- `refs/dolt/data` is at `02945ec3`.
- Every claim record minted that day carries `epoch 221` and the same `host_id`. That includes
  this bead's own claim at 20:49:22Z, and bh-jubfc's at 19:52:01Z. Claims take their epoch from
  the authoritative hive lease (`guard.live_epoch` → `frame_eligibility.authoritative_primary`).
  So the fence ref, the HQ lease and every in-flight token agree on **(beadhive-factory, 221)**.
- The cutover must read the epoch when it runs, never hard-code it. Re-adopting before then (see
  L6) moves it.

**L2. The hive lease is held past its own expiry.** At 20:50:42Z,
`bh host list --lease-hive bh` reported `held … expires 2026-10-04T20:49:05Z`. 0.22.0's advisory
expiry (bh-7y6b2) is working as designed: `HostLease.advisory_expiry` makes `is_expired` false
for a live holder in signed mode.

**L3. The frame is eligible on signed heartbeats.** `bh host eligible --hive bh --json` reported
`"eligible": true`, with all 15 predicates true, including `authenticated_fresh_heartbeat` and
`current_hive_lease_holder`. The beat age was 113–151 s, with
`heartbeat_age_basis: signed-envelope-reader-clock` and `liveness_source: signed-heartbeat`. No
receiver round trip is on the dev path any more.

**L4. The `bh` hive store is a bd shared-server store.** `.beads/metadata.json` has
`"dolt_mode": "server"` and `"dolt_database": "bh"`, and the local server is on port 3308. Two
consequences:

- Server-mode bd leaves the guard's `bh_write_mark` row unstaged in its own write commit
  (`bh-vje85` E7), so the adopt sentinel and a commit-before-push are mandatory.
- `BdEngine.push_state` already runs `bd dolt commit -m` before `bd dolt push`
  (`engine.py:409-413`), which is that commit.

`bh-sieai` E10 is also relevant: an embedded joiner could not open a store a server frame had
founded. Every replica of `bh` should therefore stay in server mode.

**L5. The frame is mixed-version today.**

- `bh` on `PATH` is `~/.local/bin/bh`, a shim for the parallel 0.22.0 `uv` env with
  `BH_HQ_SQL_LIVENESS=signed`. Its rollback line is in the shim.
- These units run on the attested 0.21.3 env, so that the measured release digest still matches
  the signed `desired.release`:
  - `beadhive-factory-heartbeat.service` and its timer;
  - `beadhive-host.service` (`bh-host-daemon`);
  - `beadhive-frame-bridge@factory`.

  `bh-cszmo` tracks the cleanup.
- The receiver runs on the operator's laptop. In 0.22.0 it is still required for adopt,
  release, failover and registration, even in signed mode.

**L6. Every fleet-config publication fences every frame until the operator renews authority.**
The trigger was `work.validation_bypass`, which was switched on for `bh` and has since been
switched off by the operator (both are fleet-config publications). `bh-rjjjo` observed the
effect at 19:20Z, and `bh-87l3y` tracks the fix: authority is cross-bound to the exact config
head (`hq_sql_runtime.load_config_at`). Any migration step that edits fleet config therefore
costs an operator authority renewal from the laptop, until `bh-87l3y` lands. `bh-cszmo`'s
active-frame rotation advances the **frame incarnation epoch**
(`hq_principal_registry.epoch`). That number is separate from the hive **writer epoch** (221).
If rotation re-binds the hive lease to the new incarnation, the lease and fence will have moved
to ≥ 222 before cutover.

**L7. A second, non-frame host is still on the legacy lease policy.** `xeno-mac.lan` is
`transient`, has no `frame_id`, and shows `liveness legacy-mtime`, `stale`, last seen
`unknown`. That is the `legacy_lease_policy` path (`frame_eligibility.decision_for` → `None`).
It must be enrolled or removed before that path is deleted. Before cutover, confirm it holds
no unpublished `bh` commits (see C1).

### What 0.22.0 already moved

**S1.** Three changes shipped in 0.22.0. All three are default-off or log-only, and all three
are on the migration path:

| Change | Code | Effect on the migration |
|---|---|---|
| `renew_if_due` swallows SQL control-plane errors (bh-jto52) | `host_lease.py:586-603` | The write verb no longer dies on a receiver timeout. In the new model `renew_if_due` stops being called at all. |
| `hq.sql.liveness: receiver\|signed` and `BH_HQ_SQL_LIVENESS` (bh-0acs8) | `hq_sql_runtime.liveness_mode` / `newest_signed_heartbeat`, `SqlControlPlane._public_observation(signed=)` | First step of receiver removal: reads are read-time verified. It is a **transitional** mode. It keeps frame-clock freshness (`renewTime`, 30 s skew clamp) and an unpruned inbox, and it is replaced by server-stamped session rows (`bh-wtsrc`). |
| Advisory hive-lease expiry, no receiver renewals (bh-7y6b2) | `HostLease.advisory_expiry` (non-record field), `read_hive_lease_record(advisory_expiry=signed)`, `lease_state` | This is `bh-cvk70` Recommendation 1 ("remove `expires_at` from every write gate"), done for SQL signed mode only. Until the in-data fence lands, split-brain exposure equals today's raw-push exposure, as the release notes state. |

**S2. A config key is a version-skew hazard.** `HqSqlConfig` forbids unknown keys, so a 0.21.3
reader fails on any `host.yaml` that carries `hq.sql.liveness` (release notes, "Rollout order").
The same strictness applies to any key a later step adds. L6 adds a second cost: a fleet-config
edit forces an authority renewal. **Design rule for the migration: no new `host.yaml` or
fleet-config key is the switch for any phase.** A hive is cut over when its own data carries
`bh_writer`. A frame is on session liveness when its incarnation's session table exists. Both
switches are data, written by the operator or the adopt, and every bh version can read them or
ignore them safely.

### Module map

Outcomes: **keep** (unchanged role), **narrow** (smaller role, same module), **replace** (role
moves to a new mechanism, then the old code goes), **delete**. "Phase" refers to
[Recommendation §2](#2-phases-coexistence-and-mixed-version-behaviour).

| Module / surface | Outcome | Reason (evidence) | Already changed in 0.22.0 | Phase |
|---|---|---|---|---|
| `host_lease.py` (`adopt`, `renew`, `release`, `takeover`, `_cas_or_reject`, `read_cached`, `cache`, `lease_state`, `renew_if_due`, `ttl_for_role`) | **narrow** → the **placement** CAS | `bh-cvk70` E1: the record at `refs/bh/lease/<prefix>` is already `{host_id, epoch}` with a true force-with-lease CAS, so placement needs no new record in git mode. `expires_at` leaves every write gate (`bh-cvk70` R1, E13–E15). `renew_if_due` becomes a no-op in every mode, and so does the `localloop.HostLeaseKeeper` renewal. `ttl_for_role` becomes `failover_after` per role: 60 min executor, 30 min transient, viewer never placed (`bh-cvk70` E20). In SQL mode, `_cas_or_reject` stops going through `plane.publish_hive_lease` (inbox → receiver) and goes through the director CAS. `frame_emergency.cap_lease` keeps capping the hint. | `renew_if_due` is a no-op in SQL signed mode and swallows SQL errors | P-M2, P-M3, P-M8 |
| `host_lease_contracts.py` (`HostLease`, `lease_ref`, stamps) | **keep** (semantics narrowed) | Wire format and ref are unchanged, so the live `(factory, 221)` lease *is* the placement record (`bh-cvk70` R6). `held_by` becomes "names this host". Expiry is a hint everywhere, so the 0.22.0 `advisory_expiry` flag becomes the default and is then removed. A tombstone is still released. | `advisory_expiry` field (signed SQL only) | P-M3, P-D3 |
| `host_fence.py` `reserve_managed_push` / `verify_managed_push` | **replace**, then **delete** | The in-data fence closes the reserve-to-push window and the raw-push bypass that this pair cannot (`bh-vje85` E17). In coexistence they **keep running** on cut-over hives, so older bh hosts are still fenced by the ref (test, step "legacy-adopt"). After that the ref is frozen as a read-only epoch floor (proposal §6). | — | P-M2 (dual), P-D2 |
| `host_fence.py` `fenced_push`, `_atomic_push`, `_fallback_push`, `probe_atomic`, `atomic_default`, `FORGE_ATOMIC_SUPPORT` | **delete** | No production caller: `BdEngine` cannot own a stable local data ref (module docstring; `git grep` finds no caller outside the module). | — | P-R1 (patch-safe refactor) |
| `host_fence.py` `install_fence`, `read_fence`, `EpochFence` | **narrow**, then **delete** | `read_fence` seeds the cutover (C1) and feeds the floor check in rollback (R2). `install_fence` is the coexistence adopt's legacy-ref step. Both go when the ref is frozen. | — | P-M2, P-D2 |
| `host_fence.py` `transport_lookup`, `transport_repos` | **keep** (move) | These are diagnostics used by `doctor`, `prepush` and `hub`. They belong next to `store_locator`. | — | P-R1 |
| `host_adopt.py` (two-phase, fence first, lease second; `_next_epoch`; `AdoptHalfDone`) | **replace** | Order inverts to placement first, then an idempotent step 2 in data with the `adopt-<e>` sentinel (`bh-cvk70` E10–E11, R3; `bh-vje85` condition 1). Step 2 also: (a) reverts the dead frame's `in_progress` beads (`bh-cvk70` R5, `bh-sieai` R3); (b) re-runs the idempotent guard install and checks for 42 triggers (`bh-sieai` R1). `_next_epoch` survives as `max(refs/bh/epoch, placement, bh_writer, max(dolt_history_bh_writer)) + 1`. The half-done state becomes "adopt incomplete" (`placement_ahead`), recovered by re-running step 2 (test, "adopt-step2-recovery"). | — | P-M2 |
| `guard.py` `guard_primary`, `primary_state`, `_refresh_expired`, `_primary_refusal` | **narrow** | On a cut-over hive the write decision is local: `bh_writer.frame == bh_local_ident.frame`, with no clock (`bh-cvk70` E14). `require_intake` stays for new intake. The `renew_if_due` call goes. The lease path stays only for hives not yet cut over. | Renewal swallow and no-op | P-M3 |
| `guard.py` `live_epoch`, `guard_claim_epoch`, `_stale_claim_refusal` | **keep** (source changes) | The `ClaimRecord.epoch` token stays the fencing token. On a cut-over hive the live epoch is the local `bh_writer.epoch`, and it is seeded *equal* to the lease epoch, so in-flight claims survive cutover (test: `is_stale` is false at 221 and true after the real handoff to 222). | — | P-M3 |
| `guard.py` `bd_write_refusal` / `is_store_publish` (the `bh bd dolt push\|sync` refusal) | **replace** | Lifted per cut-over hive, because raw push, auto-push, `bd sync` and pull-then-push are fenced in data (`bh-vje85` E5). The same change **adds** refusals on fenced hives (`bh-vje85` R4): (a) `dolt push --force`, `dolt remote reset-data`, `backup restore --force`; (b) any `--strategy`; (c) `conflicts resolve` on `bh_*`; (d) plain `vc merge`. It stays as it is for hives that are not cut over. | — | P-M5 |
| `claim_authority.py` | **keep** | `ClaimRecord{host_id, epoch}` is already the right token. The optional audit addition is to record the admitted session and evidence stamps in the claim (`bh-wtsrc` R4, threat T15). | — | P-M9 (optional) |
| `frame_eligibility.py` (`eligible`, `decision_for`, `require_intake`, `GuardedClaimSession`, `authoritative_primary`, `incumbent_primary`) | **narrow** (inputs only) | Predicate names, the decision shape and the claim-time reread all stay (`bh-wtsrc` R1). Inputs change: (a) `authenticated_fresh_heartbeat` ← session fresh by server `UTC_TIMESTAMP`; (b) `conformance_pass` / `release_matches` ← evidence row vs grant (`bh-vfrem` ranges); (c) `current_hive_lease_holder` ← placement names the frame, **and** for writes `bh_writer` names it. | Signed read path | P-M9 |
| `frame_eligibility.evictable` | **replace** | Today it is a receiver-receipt, first-seen staleness rule. It becomes the director's observed-window rule, `min(server staleness, observed window) > failover_after` (`bh-cvk70` E15), so an HQ outage never evicts a healthy primary. | — | P-M8 |
| `hq_frame_lease.py` (signed v1/v2 frame hive-lease carrier at `refs/bh/lease`) | **keep** (git HQ) / **narrow** | This is git mode's placement carrier and CAS (`bh-cvk70` E1–E3). The unsigned legacy-blob branch of `read` goes with `legacy_lease_policy`. | — | P-D3 |
| `hq_framelease_contracts.HeartbeatLease` (embedded conformance) | **narrow** | SQL HQ splits it into a session row and an evidence row, because coupling expires healthy frames (`bh-wtsrc` E7: 40/58 samples blocked by `session_fresh` alone). Git HQ keeps the signed `HeartbeatLease` and `observe` (`bh-wtsrc` E9, R5). `ConformanceCheck` / `HeartbeatConformance` become the evidence payload. `ObservationAuthority` (moved here in 0.22.0) stays. | `ObservationAuthority` moved in | P-M9, P-D4 |
| `heartbeat_report.py` (`generate`, `send`) | **narrow** (split) | It splits into a session renewal loop (one `UPDATE`, p50 3 ms, `bh-wtsrc` E6) and a conformance job on its own timer that writes evidence. `installed_release` already lives in `release_measurement.py`. `bh-vfrem` decides how the measured digest is compared. | `installed_release` moved; `hive_ready` via `bd` boundary; `seq` from newest verified inbox beat | P-M9 |
| `hq_sql_receiver.py` (`SqlTrustedReceiver`: `accept_hive_lease`, `accept_registration`, `accept_heartbeat`, `recover_heartbeat`) | **delete** (SQL HQ) | Each job is re-homed first: (a) hive-lease CAS → director credential, design A (`bh-cvk70` E8); (b) registration binding → operator-side check at enrollment (`bh-wtsrc` T19); (c) heartbeats → session rows (`bh-wtsrc` verdict). Git HQ never had a receiver (`bh-wtsrc` E9). The test harness `sql_receiver_worker.py` and `test_emergency_sql_receiver.py` go with it. | Bypassed on heartbeat reads and renewals in signed mode | P-D1 |
| `hq_sql_runtime.py` | **narrow** | Kept: `verified_state_at`, `load_config_at`, the config and runtime-authority floors (`_check_floor`, HOST floor files), `fresh_hive_lease_fence` (the placement reread), and `load_frame_binding`. Deleted: `publish_inbox`, `read_public_result`, `fresh_public_observation_fence`, `newest_signed_heartbeat` and `liveness_mode` / `signed_liveness`. `read_frame_composite` reads session, evidence and grant. | `liveness_mode`, `newest_signed_heartbeat` added | P-M9, P-D1 |
| `hq_sql_runtime_schema.py` | **narrow** | `COMMITTED_SCHEMA` stays; the registry's `inbox_table` column now names the incarnation's session and evidence tables. `LIVE_SCHEMA` (inbox) is replaced by per-incarnation `_session` / `_evidence` DDL (`bh-wtsrc` R1). In `PROTECTED_LIVE_SCHEMA`: (a) `hq_live_receipts`, `hq_live_floors`, `hq_live_public_observations` and `hq_live_results` are **deleted**; (b) `hq_live_registrations` becomes an operator enrollment record or is deleted; (c) `hq_live_hive_leases` is **kept** as the placement row. Frames get `SELECT`, the director gets `UPDATE`, and every write rewrites `revision` (`bh-cvk70` E6). | — | P-M8, P-M9, P-D1 |
| `engine.BdEngine.push_state` | **narrow** | Kept: `bd dolt commit` before push. That is exactly `bh-vje85` R3's mark-staging step. Kept during coexistence, then removed: reserve and verify around `bd dolt push`. Added: fetch and compare `bh_writer.epoch` before pushing, and divert to `frame/<id>/orphan` when superseded (`bh-vje85` E3). `force=True` is refused on fenced hives. `sync_remote.py` and `report.py` inherit all of this. | — | P-M2, P-M6, P-D2 |
| `engine.BdEngine.sync_state` + `hive_sync` strategies | **narrow** | `--strategy ours\|theirs` is refused on fenced hives (`bh-vje85` R4). Strategy merges skip triggers, and only the sentinel saves `bh_writer` (E11). The reporting gap is fixed independently: `Merged: false`, a non-null `Error`, or a `✗` line currently returns `ok=True` (`bh-vje85` E9). | — | P-F1 (patch-safe), P-M5 |
| `prepush.py` (transport pre-push hook) | **delete** | bd forces `core.hooksPath=/dev/null`, so the hook never enforces (`BEADS-SYNC.md`). | — | P-D2 |
| `doctor.py` fence checks (`:1795-1972`) | **replace** | These become `fence_audit` (`stale_marks`, `epoch_regressed`, `placement_ahead`; `bh-vje85` E12), the guard trigger count (42; `bh-sieai` R1) and "adopt incomplete". | — | P-M4 |
| `legacy_lease_policy` (`frame_eligibility.decision_for → None`, `local_intake_decision(legacy_primary=)`, `host_cli eligible` "legacy lease policy", `host_lease._frame_plane → None`, `hq_frame_lease` unsigned blobs) | **delete** | Retired once every hive has a `bh_writer` row and every host that executes work is an enrolled frame (proposal §6). `xeno-mac.lan` (L7) is the only legacy host observed. | — | P-D3 |

### What the cutover test showed

All of the following come from
`test_cutover_seeds_from_the_epoch_ref_fails_closed_across_versions_and_rolls_back`, a single
cluster run end to end. Frame `a` is bd-server, the incumbent. Frame `c` is the Dolt CLI, first
a stale replica, then the next writer.

**T1. Seeding is monotonic and keeps in-flight tokens.**

- **Starting state.** `refs/bh/epoch = {221, a, seq 0}` and placement `(a, 221)`, as on the live
  frame. `a` had published a `pre-cutover` issue.
- **The cutover commit.** In one commit, the cutover created `bh_writer`, `bh_epoch_live`,
  `bh_write_mark`, the monotonic triggers and the guard, and a `cutover-221` sentinel. It seeded
  `bh_writer = (a, 221)`. It then reserved the legacy ref (`seq 0 → 1`, CAS on its sha), pushed
  and verified.
- **Remote state afterwards.** `bh_writer = (a, 221)`, live `[221]`, marks `[221]`, the ref at
  `{221, seq 1}`, and `pre-cutover` still on `main`.
- **Tokens and identity.** A `ClaimRecord(epoch=221)` is not stale against the seeded epoch.
  `bh_local_ident` never reached the remote.

**T2. The writer keeps writing on the managed path.** `bd create`, then `bd dolt commit` (which
sweeps in the unstaged server-mode mark), then `bd dolt push` landed `post-cutover-a`.

**T3. An older bh replica fails closed.** After pulling the cutover, `c` had no `bh_local_ident`
because its bh never provisioned one. Its `main` insert failed with
`table not found: bh_local_ident`. Once provisioned, as new bh does, the same insert failed
with `bh: not the writer for main`.

**T4. A coexistence adopt keeps three carriers in lockstep.**

- **The adopt.** It ran in order: placement CAS `(a, 221) → (c, 222)`, then the legacy-ref CAS
  `→ {222, c, seq 0}`, then step 2 in data (`landed`). Afterwards, placement, ref and
  `bh_writer` all read 222, with live `[222]` and marks `[222]`.
- **The old token.** `ClaimRecord(epoch=221)` is stale now, as it should be after a real
  handoff.
- **Old bh on the incumbent.** Its legacy reservation from the sha it last saw was refused by
  the ref CAS.
- **New bh on the incumbent.** After pulling, its `bd create` was refused by the guard.
- **The new writer.** `post-adopt-c` landed.

**T5. An older bh adopting a cut-over hive fails closed and recovers by rolling forward.**

- **The legacy adopt.** A legacy adopt by `a` moved placement and the ref to 223 but not
  `bh_writer`, which stayed `(c, 222)`.
- **Nobody could write.**
  - The new holder `a` was refused by the guard.
  - The data writer `c` could not reserve the ref, so its managed push was refused.
- **Recovery.** After `a` re-ran adopt step 2 at 223 (`landed`), `bh_writer = (a, 223)` and
  `recovered-a` landed.

**T6. Rollback is one forward commit and moves no epoch back.**

- **Before dropping anything.** On writer `a`, `max(dolt_history_bh_writer.epoch) = 223` was
  read and checked against the ref (223) and placement (223). The ref already carries the floor.
- **The drop.** Dropping the four triggers, then `bh_write_mark`, `bh_epoch_live` and
  `bh_writer`, was committed and pushed as a fast-forward.
- **Remote state afterwards.** No `bh_*` tables on remote `main`. Every published title was
  kept, and none of the refused titles had landed. The ref was byte-identical (`223`).
- **Back on the legacy model.** `c` pulled and wrote again, and the next legacy adopt epoch
  (`max(ref, placement) + 1 = 224`) is above the history floor.
- **One detail matters.** The `dolt_ignore` row for `bh_local_ident` is kept. Dropping it would
  let a later `bd dolt commit` on a replica stage that replica's leftover identity table.

## Verdict — **GO**

A safe, reversible migration exists for every hive and for the live factory frame.

**How it stays safe:**

- **Monotonic seed.** Each hive is cut over in place by its current holder. `bh_writer` is
  seeded at `max(refs/bh/epoch, placement)`, which is 221 today, so every in-flight claim token
  stays valid and no epoch goes backwards (T1).
- **Coexistence.** The coexistence release honours both fences. Adopt moves placement,
  `refs/bh/epoch` and `bh_writer` together (T4).
- **Mixed versions.** An older bh fails closed on a cut-over hive in both directions, as a
  stale replica (T3) or as an adopter (T5). It never becomes a second writer.
- **Rollback.** Rollback is one forward commit that the legacy carriers already floor (T6).

The fence lives in the replica's own data, so once it is installed it holds whatever bh version
runs on a host.

**Binding conditions:**

1. **The legacy carriers must agree before cutover.** The fence ref and placement must name the
   same holder and epoch; if they do not, converge with a normal adopt first. Only the current
   holder cuts over. The holder runs bh at or above the coexistence release, and the guard is
   installed with the cutover commit, never ahead of it.
2. **No fleet-config or `host.yaml` key is the switch for any phase (S2, L6).**
3. **Older bh must not adopt a cut-over hive.** It is fail-closed, not unsafe (T5), but it stops
   all writes until the adopter is upgraded and re-runs step 2, or the hive is rolled back.
   `doctor` must flag it (`placement_ahead`).
4. **The receiver goes last, in SQL HQ only.** It goes after:
   - `bh-cszmo` has put every process on the frame on one release (L5);
   - placement writes have moved to the director credential;
   - every reader is on session rows.
5. **Every sibling spike's binding conditions still apply:**
   - `bh-cvk70`: a fresh revision on every placement write; no trigger on a frame-writable HQ
     table; failover reverts the dead frame's `in_progress` beads;
   - `bh-vje85`: the sentinel; the singleton live epoch; mark pruning only by adopt or for
     marks already on `origin/main`; force paths are break-glass;
   - `bh-sieai`: the guard shape and its 14 tables.

**On patch releases.** The new model cannot ship entirely as patches. The in-data fence, the
new adopt, the lifted refusal and the session rows are features. Breaking removals bump the
minor version too, because `major_version_zero = true`. The outline below confines `feat` to
**one coexistence minor (0.23.0)** and the deletions to **one later minor (0.24.0)**, which can
be deferred indefinitely. Everything else, and every per-hive cutover, ships as patches or as
operator procedure with no release.

## Recommendation

### 1. Per-hive cutover (C1–C6)

Run this on the current holder, one hive at a time. Use a low-traffic hive as the canary, and
cut `bh` over last. The test runs this procedure as `_cutover`.

- **C1. Read and agree.**
  - Read `refs/bh/epoch` from the hive remote (`host_fence.read_fence`) and the placement record
    (git: `refs/bh/lease/<prefix>`; SQL: the `hq_live_hive_leases` row).
  - Require the same holder and the same epoch, with no `placement_ahead`.
  - Require that no other host holds unpublished commits for the hive. This is the one case the
    in-data fence cannot see: commits made before cutover carry no marks. Its exposure equals
    today's raw-push exposure. For the live frame, ask about `xeno-mac.lan`'s clone (L7).
  - If any check fails, run a normal (legacy) adopt to converge first, then restart C1.
- **C2. Choose the seed.** `E = max(fence.epoch, placement.epoch)`. Same holder, no bump: the
  holder's tenure does not change, so tokens minted under `E` stay valid (T1). Bump to `E+1`
  only if the cutover is also a handoff, and then do it as a coexistence adopt (T4), not a
  seed.
- **C3. One commit on the holder, starting from the remote head.** Create:
  - the fence tables (`bh-vje85` shape, with the `bh_epoch_live` singleton);
  - the monotonic triggers;
  - the `dolt_ignore` row `bh_local_%`;
  - `bh_writer(1, holder, E, <fresh revision>)` and `bh_epoch_live(1, E)`;
  - a `cutover-E` sentinel mark;
  - the `bh-sieai` guard: the procedure plus 42 triggers on 14 tables.

  How to run it depends on the engine:
  - **Server mode** (`bh`): send the statements through the server, then `bd dolt commit`.
  - **Embedded mode:** use the Dolt CLI on `.beads/embeddeddolt/<db>` (`bh-eybn7` E7).
  - In both modes, write any statement that reads the fence with variables, not subqueries
    (`bh-vje85` E3).
- **C4. Provision the holder's identity.** After the commit and before the push, create the
  holder's `bh_local_ident` row. The ignore row is committed by then, so the identity is never
  staged.
- **C5. Publish on the coexistence managed path.**
  - Reserve `refs/bh/epoch` with `seq+1` as a CAS on its sha, push, then verify the reservation.
  - A non-fast-forward means the holder wrote meanwhile: reset to the remote and redo C3. The
    script is drop-then-create and idempotent.
- **C6. Verify.**
  - `fence_audit` is clean.
  - The trigger count is 42.
  - The holder can write.
  - Each other replica provisions its identity on its next pull. Unprovisioned replicas are
    read-only on `main` (T3).
  - Record `{hive, E, cutover commit, ref sha}` in the hive's doctor output.

### 2. Phases, coexistence and mixed-version behaviour

| Phase | State | Old bh (0.21.3/0.22.x) on another frame | Old bh process on the writer frame (e.g. 0.21.3 heartbeat/daemon) | New bh |
|---|---|---|---|---|
| **Φ0** today | Lease + `refs/bh/epoch` + receiver; the 0.22.0 signed interim | Unchanged. | Supported as the 0.22.0 interim (env shim). Mixed *reader* liveness modes against one frame are unsupported (release notes). | — |
| **Φ1** 0.23.0 installed, no hive cut over | No `bh_writer` anywhere | Fully compatible: new bh behaves as 0.22 when there is no `bh_writer` (lease gate, reserve/verify). | Compatible. | Downgrade to 0.22.x is free. |
| **Φ2** hive H cut over; ref maintained (dual fence) | `bh_writer` on H's `main`; ref and lease kept in lockstep by every new adopt | As a non-writer: every `main` write fails closed (`table not found: bh_local_ident`, T3); reads work; its managed push loses the ref CAS after any adopt (T4). As an adopter: fail-closed stall until roll-forward (T5, condition 3). | Works. The guard identity is per *replica*, not per process, so its bd writes pass and stamp marks. Its managed push still reserves the maintained ref. Its receiver renewals work while the receiver runs. | Writer: data fence plus ref reservation. Non-writers: provisioned and refused, or forwarding to the primary's server (`bh-sieai` forward GO). `bh bd dolt push\|sync` is lifted on H; force and strategy paths are refused. |
| **Φ3** SQL HQ: director placement + session rows, receiver still running (soak) | `hq_live_hive_leases` written by the director with a fresh 64-hex revision. That is `sha256(record ‖ uuid)`, unique like a UUID but in the receiver's format, so the receiver can take over again. Per-incarnation session and evidence tables exist. Frames dual-write session rows and signed inbox beats. | 0.22 signed readers keep working from the inbox. 0.21.3 receiver-mode readers work while the receiver runs. Old adopt and release still go through the receiver. | Same. | Reads session and evidence when the incarnation's tables exist (the data switch), else signed inbox. |
| **Φ3b** receiver stopped | No receiver | Old adopt and release fail (`HqLeaseUnknown`), which is fail-closed and also blocks condition 3. 0.21.3 receiver-mode eligibility goes stale, so none may remain (condition 4). | None may remain on 0.21.3. | Unchanged. |
| **Φ4** 0.24.0: ref frozen, removals | No reserve/verify. `refs/bh/epoch` frozen at its last epoch as a floor record (do not delete it). | Must not exist. If one appears, the data fence still stops it, because it is version-independent (T3). | Must not exist. | Delete list in §6 (D-items). |

Git HQ follows the same Φ0–Φ2 and Φ4. It has no receiver and keeps the signed `HeartbeatLease`
(`bh-wtsrc` R5). The ADR should say plainly that SQL and Git HQ apply identical predicates over
different evidence carriers.

### 3. Rollback

| From | Steps | Restores |
|---|---|---|
| Φ1 | Reinstall 0.22.x (for the live frame, the shim's own `ln -sfn` line). | Everything. Nothing was written. |
| Φ2, before C5's push | `DOLT_RESET('--hard', 'origin/main')` on the holder and drop its local `bh_local_ident`. | Everything. Nothing was published. |
| Φ2, after cutover (**R1–R5, tested as T6**) | **R1.** Stop new adopts for H; take the rollback on the current `bh_writer` holder. **R2.** Read `floor = max(dolt_history_bh_writer.epoch, refs/bh/epoch, placement)` *before* dropping anything, because the history system table goes with the table. If the ref is below the floor, which coexistence should never allow, first legacy-adopt so the ref and lease are at `floor+1`. **R3.** In one commit: drop the four fence triggers and the 42 guard triggers and procedure, then `bh_write_mark`, `bh_epoch_live`, `bh_writer`. **Keep** the `dolt_ignore` row. **R4.** Push on the managed path (reserve, push, verify). Never `--force`, never `reset-data`, never move the ref back. **R5.** Each replica pulls (a plain fast-forward) and drops its local identity table. The lease gate resumes, and the next adopt is `> floor`. | The legacy model, with epochs still monotonic. Downgrade below 0.23.0 is allowed only after R1–R5. |
| Φ3 | Keep the receiver deployed and the inbox tables intact for the whole soak. To roll back, restart the receiver and put readers back on `signed` or `receiver` through the 0.22.0 env override. The director's placement revisions are receiver-format (Φ3 row). | Receiver liveness and receiver-written placement. |
| Φ4 / P-D1 | No in-place rollback once tables and code are deleted. Delete only after a Φ3b soak, and export `hq_live_*` first (`bh-wtsrc` T15). | — |

### 4. The live factory frame, in order

1. **Now (Φ0).** Keep the 0.22.0 signed interim. Do not set `hq.sql.liveness` in `host.yaml`
   while any 0.21.3 process reads it (S2).
2. **Ship the patch-safe items (P-F1…P-F5)** as 0.22.x. P-F3 (`bh-87l3y`) lands before any step
   that needs a fleet-config edit.
3. **`bh-cszmo` / `bh-mucmd`:** rotate the active frame to one release, restart
   `beadhive-host`, `frame-bridge@factory` and `factory-heartbeat` on one env, and remove the
   shim (`bh-3q5m9`). This still needs the laptop receiver. Expect the hive lease to be
   re-bound, and possibly the writer epoch to move past 221 (L6). The cutover reads it then.
4. **Install 0.23.0 by the same rotation.** `bh-vfrem` ranges remove the per-patch re-grant
   after that. That is Φ1.
5. **Cut over a canary hive, then `bh`** (C1–C6). The `bh` store is in server mode (L4), so
   `push_state`'s commit-before-push stays mandatory. That is Φ2.
6. **SQL Φ3.**
   - Provision session and evidence tables for the current incarnation: host-pinned account,
     TLS (`bh-wtsrc` R2), operator triggers.
   - Move placement writes to a director credential held off-frame. For a laptop-free factory it
     is held on the HQ host under `bh-rjjjo`'s scoped delegate.
   - Soak with the receiver running, then stop the laptop LaunchAgent (Φ3b). Use a laptop-off
     soak, with `bh-wj8hu` delegate renewal, as the acceptance test.
7. **Φ4: 0.24.0 removals,** after every hive is cut over and `xeno-mac.lan` is enrolled or
   removed.

### 5. Docs and ADRs to amend (with the ADR in `bh-pr889`)

- **[multi-host-model-adr.md](../design/multi-host-model-adr.md): add Amendment 2.** It
  supersedes these parts of Amendment 1:
  - §1: the lease record becomes placement, and `expires_at` becomes a failover hint;
  - §2: the fence split becomes the in-data fence, with `refs/bh/epoch` frozen as a floor;
  - §3: role TTL scaling becomes `failover_after` per role;
  - §4: an HQ outage blocks handoff, never writes;
  - §5: vocabulary — placement, host lease, bd lease, writer epoch and incarnation epoch.
- **[frame-dolt-server-hq-mode-adr.md](../design/frame-dolt-server-hq-mode-adr.md):** retire the
  receiver half of binding amendment `bh-v0k3i`. Record:
  - session and evidence rows, and director placement (design A);
  - "identical predicates, different evidence carriers";
  - threats T11 and T16 and their mitigations;
  - never co-host hive databases on the HQ server.
- **[BEADS-SYNC.md](../BEADS-SYNC.md):** rewrite "The epoch fence beside the data". Cover:
  - the in-data fence, under which raw `bd dolt push` is safe on cut-over hives;
  - break-glass force paths and their audit;
  - the orphan branch;
  - `--strategy` refused on fenced hives.
- **[FRAME-FLEET-MEMBERSHIP.md](../FRAME-FLEET-MEMBERSHIP.md):** in the lifecycle and
  eligibility section, `AUTHORITY_READY` "trusted observer receipts" becomes session and
  evidence. Update the heartbeat section, and add release ranges (`bh-vfrem`).
- **[HQ.md](../HQ.md):** the bound hive-lease paragraphs (around l.179–196) become placement,
  written by the director in SQL mode, and the receiver references go.
- **Also update:**
  - [CONFIGURATION.md](../CONFIGURATION.md): deprecate `hq.sql.liveness`;
  - [frame-release-upgrade-runbook.md](../design/frame-release-upgrade-runbook.md): active
    rotation (`bh-cszmo`);
  - the proposal's status line;
  - a release note per release, with the trust delta and rollout order.

### 6. Sequenced implementation outline (for the `bh-pr889` replan)

These are not filed beads. Order is top to bottom, and `→` marks a hard dependency.

**Patch-safe (0.22.x; independent of the ADR's details):**

| # | Item | Tag | Why it is patch-safe |
|---|---|---|---|
| P-F1 | `fix(sync)`: `sync_state` treats `Merged: false`, a non-null `Error` or a `✗` line as failure (`bh-vje85` E9) | patch-safe | Fixes a false `ok=True`. No new surface. |
| P-F2 | `fix(work)`: resolve the parent epic by link so container refresh works for hash-id children (`bh-np024` / `bh-bd8hq`) | patch-safe | Already filed as a bug. The implementation molecule's containers need it. |
| P-F3 | `fix(fleet)`: fleet-config edits must not fence frames (`bh-87l3y`) | patch-safe if done as the bug fix (re-bind in the same publication). A new binding model is `feat`. | Removes the cost of every config edit in the migration (L6). |
| P-F4 | `test(fence)`: promote the `bh-sieai` and `bh-vje85` Dolt/bd trigger-semantics probes to a canary that re-runs on every Dolt or bd pin bump (`bh-sieai` R5) | patch-safe | Test only. |
| P-F5 | `chore(ops)` / `docs`: T16 globals watchdog and read-only server config for the HQ server (`bh-wtsrc` R3), and the upstream issue drafts (Dolt globals, bd E7/E8/E9 bugs) | patch-safe | Deployment and docs. |
| P-R1 | `refactor(fence)`: delete the unused `fenced_push` family, and move `transport_lookup` / `transport_repos` beside `store_locator` | patch-safe | No caller and no behaviour change. |
| P-F6 | `fix(fleet)`: bound the signed-mode inbox (0.22.0 trust delta "no pruning yet"), only if Φ3 is more than a release away | patch-safe | Fixes unbounded growth. |

**Requires-minor: the 0.23.0 coexistence release** (one minor; P-M1 → P-M2 → P-M3 → P-M4 →
P-M5/P-M6; P-M7 with P-M2; P-M8 → P-M9):

| # | Item | Tag | Notes |
|---|---|---|---|
| P-M1 | `feat(fence)`: product fence and guard module. Covers the schema, the idempotent install, the 42-trigger check, `bh_local_ident` provisioning (only once the ignore row is present, i.e. after pull on a cut-over hive) and `fence_audit` | requires-minor | Product version of `harness.epoch_fence` plus `harness.write_guard`. |
| P-M2 | `feat(fleet)`: placement-first adopt with an idempotent step 2 and the sentinel. While the ref exists it also CASes `refs/bh/epoch` (dual write). Epoch is `max(ref, placement, bh_writer, history) + 1`. It reports "adopt incomplete" | requires-minor | Replaces `host_adopt`. `push_state` keeps reserve/verify on cut-over hives. |
| P-M7 | `feat(fleet)`: step 2 reverts the dead frame's `in_progress` beads exactly once (`bd unclaim --force` on the new writer) | requires-minor | `bh-cvk70` R5, `bh-sieai` R3. `bh-jbb6r` scenario 9. |
| P-M3 | `feat(guard)`: on cut-over hives, `guard_primary` and `live_epoch` read the local `bh_writer` (clock-free); expiry is advisory in every HQ mode; `renew_if_due` is retired | requires-minor | Dormant until a hive is cut over. |
| P-M4 | `feat(hive)`: an operator verb `bh hive fence cutover\|status\|rollback <hive>` (C1–C6, R1–R5), and `doctor` uses `fence_audit` | requires-minor | Patch-safe alternative: ship C1–C6 and R1–R5 as a documented runbook plus a `scripts/` helper, and add the verb later. |
| P-M5 | `feat(guard)`: lift `bh bd dolt push\|sync` on cut-over hives; refuse force, `reset-data`, `restore --force`, `--strategy`, `conflicts resolve` on `bh_*` and plain `vc merge` | requires-minor | `bh-vje85` R4. Lifting and refusing ship together. |
| P-M6 | `feat(work)`: managed push diverts to `frame/<id>/orphan` when superseded; add an orphan-merge verb for the writer | requires-minor | `bh-vje85` E3, R3. |
| P-M8 | `feat(fleet)`: SQL placement by director credential on `hq_live_hive_leases` (receiver-format fresh revisions, frames `SELECT`), plus the observed-window failover observer with code-default `failover_after` 60/30 min | requires-minor | Replaces `evictable` and `accept_hive_lease`. No config key (S2). Any override waits for P-F3. |
| P-M9 | `feat(fleet)`: per-incarnation session and evidence DDL; a renewal loop separate from the conformance job; one-statement `read_eligibility`; data-switched reader; claim-time audit stamps | requires-minor | `bh-wtsrc` R1, R2, R4. Dual-writes inbox beats during Φ3. |
| P-M10 | `feat(fleet)`: forward write path for non-primary frames (bd pointed at the primary's server) | requires-minor, optional | `bh-sieai` forward GO. Not needed while the factory has one frame. |
| — | Filed separately, all `feat`: `bh-mucmd` (active rotation, `bh-cszmo`), `bh-kmxyp` (release ranges, `bh-vfrem`), `bh-wj8hu` (laptop-free renewal, `bh-rjjjo`) | requires-minor | Land in the same 0.23.0 if possible, so the frame takes one minor. |

**Requires-minor: the 0.24.0 removals** (breaking; each requires all hives cut over and Φ3b soaked):

| # | Item | Tag | Note |
|---|---|---|---|
| P-D1 | Remove `SqlTrustedReceiver`, the inbox, receipt, floor, public-observation and result tables (operator DDL drop after an export), `publish_inbox` / `read_public_result`, `sql_receiver_worker`, and the `receiver`/`signed` values of `hq.sql.liveness`. Keep the key accepted and ignored with a warning for one release | requires-minor (breaking) | The receiver is deployed separately, so removing it breaks that deployment. |
| P-D2 | Stop reserving `refs/bh/epoch`; remove `install_fence`, `read_fence`, `EpochFence` and `prepush`; freeze the ref as a floor | requires-minor (breaking) | Older bh would lose its ref fence, so ship it only after Φ4's precondition. |
| P-D3 | Remove `legacy_lease_policy` paths and `HostLease.advisory_expiry` | requires-minor (breaking) | Needs `xeno-mac.lan` enrolled or removed. |
| P-D4 | SQL HQ: remove `HeartbeatLease` embedded conformance from the SQL path; Git HQ keeps it | requires-minor (breaking) | — |

**If the operator wants to avoid 0.24.0:** code that no supported configuration reaches can
stay dormant indefinitely, which needs no release. P-D3's dead branches and P-D2's internal
functions can then go as `refactor` patches once nothing calls them. Only the receiver
deployment and the config-value removals strictly need a minor.

**Hand-offs.**

- `bh-jbb6r`: add the Φ2 mixed-version events (an unprovisioned replica, a legacy adopt) to the
  randomized event set.
- `bh-pr889`: the ADR should cite T1–T6 for the cutover and its rollback, and adopt S2's "data
  is the switch" rule.
