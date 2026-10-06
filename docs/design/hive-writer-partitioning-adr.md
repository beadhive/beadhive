# Hive writer partitioning ADR — in-data epoch fence, director placement, no trusted receiver

**Status:** **accepted** (operator, 2026-10-05) — verdict **GO (staged)**; the operator's
answers are recorded under [Decisions](#decisions-operator-2026-10-05) · **Date:** 2026-10-05
**Decision bead:** `bh-pr889` · **Spike epic:** `bh-qlgmm`
**Proposal:** [hive-writer-partitioning-proposal.md](hive-writer-partitioning-proposal.md)
**Amends:** [multi-host-model-adr.md](multi-host-model-adr.md) Amendment 1 §§1–5 (this is its
Amendment 2); [frame-dolt-server-hq-mode-adr.md](frame-dolt-server-hq-mode-adr.md) binding
amendment `bh-v0k3i` (the trusted-receiver half) and its "Grants, signatures and the direct-SQL
limitation" section.
**Supersedes:** nothing outright. Both amended ADRs stand except where this one says otherwise.

## Verdict

**GO, staged.** Both halves of the question proceed:

- **In-data epoch fencing: GO.** Per-hive partitioning stays. The writer token moves from the
  sibling ref `refs/bh/epoch` into each hive's own Dolt `main`. The remote's non-fast-forward
  CAS, epoch retirement by foreign key, and a local guard trigger enforce it.
- **Trusted-receiver removal: GO.** Each of the receiver's three jobs is re-homed first:
  placement goes to a director credential, registration to an operator enrollment check, and
  liveness to server-stamped session and evidence rows. The receiver is then soaked, stopped
  and deleted, in `dolt-server` HQ only. Git HQ never had one.

"Staged" means the GO is not one release. It is ordered:

1. patch-safe prerequisites in 0.22.x, available now;
2. one coexistence minor, 0.23.0, that is dormant until a hive's own data switches it on;
3. operator cutovers, hive by hive, with a tested rollback;
4. removals of legacy code, left dormant and unscheduled (Decision 5).

Each stage has entry gates (see [Binding conditions](#binding-conditions)). The current
deployment holds at 0.22.3 until the stage-1 items and the rotation prerequisites land.

Parts of the proposal that are **not** adopted, or are adopted only in a restricted form:

| Proposal element | Disposition | Evidence |
|---|---|---|
| "The writer may prune marks at any time" (§2) | **Rejected.** Marks are deleted only by adopt, or by the writer for marks already on `origin/main`. | `bh-vje85` E14 |
| `bh_epoch_live(epoch PRIMARY KEY)` | **Replaced** by a singleton `(id PK, epoch UNIQUE)`. | `bh-cvk70` E12 |
| Adopt bump without a sentinel | **Rejected.** Every bump inserts an `adopt-<epoch>` mark. | `bh-vje85` E8, E11 |
| Proposal's guard sketch (`@`-variables, a mark insert in a procedure) | **Rejected.** It fails open or loses marks on Dolt 2.3.5. The `bh-sieai` shape is adopted. | `bh-sieai` E1 |
| Stored procedure for placement writes (design B) | **Rejected** until dolthub/dolt#10190 ships. | `bh-cvk70` E7b |
| Branch write path for claims and ID-allocating writes | **NO-GO.** Child IDs collide and claims double-win. Branch is restricted to non-allocating edits on a frame-private data ref. | `bh-sieai` E9 |
| Signed session rows in `git` HQ mode | **Not built.** Git HQ keeps the signed `HeartbeatLease`. Execution frames are scoped to `dolt-server` HQ. | `bh-wtsrc` E9, R5 |

## Evidence base

Every spike in the molecule reported GO. Each is cited below by its own evidence numbers.

| Spike | Question | Verdict | Load-bearing evidence |
|---|---|---|---|
| [`bh-eybn7`](../spikes/bh-eybn7-writer-fencing-fixture.md) | Can a hermetic multi-frame fault-injection fixture drive real bd? | GO | E2 (one `git` choke point), E5 (barriers make races deterministic), E6 (a refused push still moves `refs/dolt/data`, so judge on `main`) |
| [`bh-vje85`](../spikes/bh-vje85-in-data-epoch-fencing.md) | Does the in-data fence stop a stale writer on every bd path? | GO, 4 conditions | E2 (no reserve-to-push window), E5 (every default bd path refused, both modes), E8 (sentinel), E11 (force-path map), E12 (`fence_audit`), E17 (vs `refs/bh/epoch`) |
| [`bh-sieai`](../spikes/bh-sieai-write-guard-and-nonprimary-writes.md) | Does a Dolt trigger refuse raw bd on non-writers without wedging the bump? Forward or branch? | GO guard, GO forward, branch restricted | E1 (trigger semantics), E2 (no firing on merge), E3 (every raw bd verb refused), E6 (14 tables), E8 (0 double wins in 60 × 8 races), E9 (#4796 by construction) |
| [`bh-cvk70`](../spikes/bh-cvk70-placement-authority-and-failover.md) | One linearizable placement CAS per HQ mode, without the receiver? Clock-free failover? | GO, 3 conditions | E2/E5 (one winner in both modes), E6 (rewrite the token), E7 (grant semantics), E10–E11 (placement first), E14–E15 (HQ down blocks handoff only), E17 (live false-stale), E19–E20 (bd leases do not travel; defaults) |
| [`bh-wtsrc`](../spikes/bh-wtsrc-receiver-removal-liveness.md) | Can session rows, evidence rows and read-time eligibility replace the receiver? | GO | E1 (per-frame grants), E2 (server UTC stamp), E3 (trigger DML is invoker-checked), E4 (globals, unchanged exposure), E7 (scenario 10), threat table T1–T19 |
| [`bh-jbb6r`](../spikes/bh-jbb6r-writer-partitioning-e2e.md) | Does the composition hold end to end? | GO, 2 more conditions | E1 (all ten scenarios), E3 (fence and guard compose unchanged), E5 (exactly-once reclaim), E8 (`--ref` URL defect), E9 (observer gap reset), E10 (latency), E13 (195 + 25 randomized schedules, 0 violations) |
| [`bh-32379`](../spikes/bh-32379-writer-partitioning-migration.md) | Is there a safe, reversible, mixed-version migration? | GO, re-verified at 0.22.3; 6 conditions | T1–T6 (cutover, coexistence, fail-closed old bh, rollback), L1–L10 (live facts), S1–S3 (0.22.x deltas, "data is the switch"), module map, §6 outline |

Facts re-checked against the tree at `58ac2f5f` (0.22.3 plus this container) while writing
this ADR:

- `HISTORY_LIMIT = 16` (`frame_release_upgrade.py:28`);
- `leaseDurationSeconds` is `le=900` (`hq_framelease_contracts.py:93`), while
  `heartbeat_report.generate` hard-codes 300 (`heartbeat_report.py:134`);
- `BH_FRAME_HEARTBEAT` and the `waived` term (`frame_eligibility.py:23`, `:135`);
- `gitref.cas` is `--force-with-lease=<ref>:<expected>` (`gitref.py:169`);
- `HostLease.advisory_expiry` (`host_lease_contracts.py:63`);
- `reserve_managed_push` / `verify_managed_push` (`host_fence.py:307`, `:365`);
- `bd_write_refusal` / `is_store_publish` (`guard.py:888`, `:919`);
- `fenced_push` has no caller outside `host_fence.py`.

Bead states: `bh-rjjjo`, `bh-87l3y`, `bh-3q5m9`, `bh-wj8hu`, `bh-kmxyp` and `bh-vfrem` are all
open.

### Why GO — the five load-bearing reasons

1. **The fence closes the two holes Amendment 1 recorded as unfixable.** A takeover can no longer
   land stale data in a reserve-to-push window, because there is no separate token to race: the
   push's own branch CAS is the check (`bh-vje85` E2, E17). Raw `bd dolt push`, auto-push,
   `bd sync` and pull-then-push are fenced on every default path, in both bd storage modes
   (`bh-vje85` E5, E6; `bh-jbb6r` E2). The bypasses left are explicit operator force flags.
   Each one is mapped and detectable after the fact (`bh-vje85` E11, E12; `bh-jbb6r` E11).
2. **Composition holds under fault injection.** All ten proposal scenarios pass on one schema
   with fence and guard unchanged (`bh-jbb6r` E1, E3). 220 randomized schedules (195 from the
   interrupted 500-seed run, 25 from the default benchmark) and four gate seeds held every
   invariant: 0 violating seeds, 0 harness errors (`bh-jbb6r` E12, E13). The invariant checker
   is shown not to be vacuous (`bh-jbb6r` E11).
3. **Write safety stops depending on clocks and on HQ.** With HQ down past the TTL in both modes,
   the primary keeps writing and handoff is refused (`bh-cvk70` E14, E15; `bh-jbb6r` scenario 7).
   Today the same outage degrades a primary to read-only, and receiver lag has made a healthy
   frame ineligible in production (`bh-cvk70` E13, E17).
4. **The coupled heartbeat is already a production outage, with no headroom left.** One process
   both renews liveness and measures conformance, about 3.5 min per loaded beat. It fenced
   every lifecycle write on the factory (`bh-32379` L8). The beat TTL is now at its 900 s
   contract ceiling, and the only escape, `BH_FRAME_HEARTBEAT=advisory`, is safe only with one
   executor frame (`bh-32379` S3). Decoupling fixes this by construction: 0 of 58 samples
   expired versus 40 of 58 coupled (`bh-wtsrc` E7; `bh-jbb6r` scenario 10).
5. **The receiver adds no protection under the co-residence premise, and the migration is
   reversible.** A compromised frame signs whatever it would otherwise write. Grants plus
   read-time checks cover T1–T8, T10 and T12–T14, and are stronger on skew and flooding
   (`bh-wtsrc` threat table). The cutover seeds `bh_writer` at the live epoch, so in-flight
   claim tokens stay valid. Old bh fails closed in both directions, and rollback is one forward
   commit that moves no epoch back (`bh-32379` T1–T6).

### What argues for holding, and why it does not change the verdict

- **GitHub-hosted `git+ssh` ref CAS is untested** (`bh-vje85` E13, `bh-cvk70` E3). The live fleet
  already relies on the same `--force-with-lease` semantics for `refs/bh/lease/*` and for bd's
  own `refs/dolt/data` pushes. This becomes a canary gate before the first GitHub-hosted cutover
  (condition 11), not a reason to stop.
- **The factory cannot deploy without the operator's laptop** (`bh-32379` L6, L9). `bh-87l3y`
  (config edits fence every frame) and `bh-rjjjo` (laptop-free authority) are open. They gate
  *deployment* stages (Φ1 install, Φ3), not the design. The outline makes them explicit
  dependencies. The operator accepted one laptop session for the Φ1 install (Decision 4).
- **Signed-mode lease continuity has a 16-rotation horizon** (`bh-32379` L9, derived from code
  and not tested). The new model removes it, because placement names the frame, not the grant
  epoch. Until then it is a counted risk (condition 13).
- **Dolt 2.3.5 trigger semantics are fragile** (`bh-sieai` E1: seven engine bugs that shape the
  guard). The guard is pinned to those tests and re-run on every Dolt or bd bump (condition 9).

## Decision

### 1. Placement: one CAS authority per HQ mode

Placement is the record `{hive, frame, epoch}`. It answers *who should write*. It is changed only
by compare-and-swap at exactly one linearizable point per HQ mode. Every placement CAS raises the
epoch; release keeps it, as the tombstone already does.

| HQ mode | The single CAS authority | Who may issue it | Loser semantics |
|---|---|---|---|
| **`git`** | `refs/bh/lease/<prefix>` in the HQ repo, via `gitref.cas` (`--force-with-lease=<ref>:<expected>`). The five-field `HostLease` record is unchanged; `host_id` reads as the frame. | Any principal with push access to HQ, as today. Epoch monotonicity is a client convention. | git's `! [rejected]`; never retried with the same expectation. |
| **`dolt-server`** | One `hq_live_hive_leases` row per hive (design A): `UPDATE … SET frame=?, epoch=epoch+1, revision=<fresh> WHERE prefix=? AND revision=<expected>`. | A director/operator credential holding `UPDATE` on that table only. Frames hold `SELECT`. | `rowcount 0` or `1213` serialization failure means lost; never retried with the same expectation. |

Rules:

- **Every placement write rewrites the CAS token.** Dolt merges concurrent transactions cell by
  cell, so an update that keeps `revision` can silently commit over a competing adopt
  (`bh-cvk70` E6). Revisions are fresh and never content hashes that can repeat. During Φ3 the
  revision is `sha256(record ‖ uuid)`, in the receiver's 64-hex format, so the receiver can take
  over again on rollback (`bh-32379` Φ3).
- **No trigger may sit on a frame-writable HQ table.** A conformance check lists triggers and
  fails on any that touch a protected table (`bh-cvk70` E7d). `bh-wtsrc` E3 later showed that
  trigger DML is invoker-checked on 2.3.5, which weakens the escalation path. The rule stays as
  defence in depth, because the two spikes measured different trigger shapes.
- **Per-principal candidacy tables (design C)** are allowed only as a request channel into the
  director. The decision is still design A.
- **Placement is not the write gate.** It decides who adopts. The data decides who writes (§2).

### 2. Fencing mechanism: the epoch lives in the hive's data

Adopted as composed in `bh-jbb6r` ([`tests/harness/composed_fence.py`](../../tests/harness/composed_fence.py)):

- **Fence tables**, versioned on `main`:
  - `bh_writer (id=1, frame, epoch, revision)`, rewritten only by adopt;
  - `bh_epoch_live (id PK, epoch UNIQUE)`, a singleton;
  - `bh_write_mark (id uuid, epoch FK → bh_epoch_live.epoch, tbl)`.
- **Monotonic `BEFORE UPDATE` triggers** on `bh_writer` and `bh_epoch_live` refuse any epoch
  that does not increase (`bh-vje85` E10).
- **The write guard** (`bh-sieai` shape): a `bh_guard_check()` procedure plus 42 `BEFORE`
  triggers on 14 versioned bead tables. Each trigger inlines the mark insert, and the `CALL` is
  its last statement. The node identity lives in `bh_local_ident`, which is `dolt_ignore`d
  (`bh_local_%`). `child_counters`, `metadata` and every ignored table are left unguarded.
  With the two monotonic triggers that makes **44 `bh_*` triggers**. A node short of 44 refuses
  to act as writer (`bh-jbb6r` E6).
- **Adopt** is placement first, then an idempotent step 2 in data. Step 2:
  1. always starts from the remote head;
  2. stops if `bh_writer.epoch ≥ mine`;
  3. re-checks that placement still names `(me, epoch)`;
  4. in one commit, names the writer, rewrites `revision`, deletes marks below the new epoch,
     moves the live epoch and inserts the `adopt-<epoch>` sentinel;
  5. pushes, and on non-fast-forward loops back to the data check.

  The epoch is `max(refs/bh/epoch, placement, bh_writer, max(dolt_history_bh_writer)) + 1`.
  The half-done state is "adopt incomplete" (`placement_ahead`). It is recovered by re-running
  step 2 or by re-placing at a higher epoch, never by rolling placement back and never by
  resolving a `bh_writer` conflict.
- **Managed write path:**
  - in server mode, `bd dolt commit` runs before every managed push so the mark travels with
    the write (`bh-vje85` E7; `push_state` already does this, `bh-32379` L4);
  - on any fetch that shows a higher `bh_writer.epoch`, the frame stops writing `main`, pushes
    its unpublished commits to `frame/<id>/orphan-<epoch>-<n>`, and resets;
  - the new writer merges an orphan deliberately, in one SQL session that re-stamps its marks
    (`bh-vje85` E3).
- **bd verb policy on cut-over hives:** `bh bd dolt push|sync` and bd auto-push are **lifted**.
  In the same change, these are **refused**: `dolt push --force`, `dolt remote reset-data`,
  `backup restore --force`, any `--strategy`, `conflicts resolve` on `bh_*`, and plain
  `vc merge`.
- **Detection:** `bh doctor` runs `fence_audit` against remote `main` and HQ (`stale_marks`,
  `epoch_regressed`, `placement_ahead`) plus the 44-trigger count. It never relies on commit
  authorship, because bd commits as `beads`/`root` (`bh-jbb6r` E4).
- **`refs/bh/epoch`** is maintained in lockstep by every coexistence adopt (dual fence, Φ2),
  then frozen as a read-only epoch floor in Φ4. It is never deleted.

### 3. Non-primary write path

| Frame role | Write path | Status |
|---|---|---|
| The primary | Writes `main` directly. | — |
| Execution frames and agents that claim, create or close | **Forward:** bd on the frame points at the primary's `dolt sql-server`. Claims are granted in one place, so bd's claim, heartbeat and reclaim work as designed (0 double wins in 60 eight-way races, `bh-sieai` E8). A forwarder to a demoted primary fails closed. | **GO, in 0.23.0** (M12). Required before additional executor frames join (Decision 6). |
| Frames that cannot reach the primary | **Branch**, only for non-allocating edits (comments, labels, notes, status of beads already held), published to a frame-private data ref `refs/dolt/frame/<id>` with a distinct remote URL per ref on server-mode primaries. The merge asserts that the private branch differs from `origin/main` after fetch. | **Restricted.** Not built until a need exists. |
| Branch writes that allocate IDs or claim | — | **NO-GO** until upstream #4796 (frame-scoped child IDs) and a bd branch-write option. |

**Forwarding makes executors logins on the primary's hive server, and that exposure cannot
be closed on Dolt today.** Stated plainly:

- On Dolt 2.3.5 any login can `SET GLOBAL` any variable, including
  `dolt_force_transaction_commit`, and `SET PERSIST` survives a restart (`bh-wtsrc` E4). The
  Dolt 2.4.0 and 2.4.1 release notes show no change. Grants and `system_variables` cannot
  prevent it, and a session-level `SET` is always possible whatever the server does.
- A forwarder that forces transaction commits on the primary *may* defeat the FK epoch
  retirement for every writer of that hive. Whether a forced global actually lets a stale write
  land end to end is **unmeasured**. No spike drove that path.
- A primary outage stops hive writes for every forwarder until failover or recovery, so
  placement spreads hives across executors (M8b): one frame's death stalls only the hives it
  holds.

**Option A, adopted for 0.23.0 (condition 16).** Narrow and detect, do not claim to prevent:

- the primary's hive `dolt sql-server` listens on the LAN only with TLS, and each forwarding
  frame gets its own host-pinned account (`'<principal>'@'<frame address>'`), never a shared
  root login;
- a globals watchdog on every primary's hive server asserts `dolt_force_transaction_commit`,
  `dolt_allow_commit_conflicts`, `dolt_transaction_commit` and `read_only`;
- the server runs with a read-only `DOLT_ROOT_PATH` config, so `SET PERSIST` cannot stick;
- `fence_audit` remains the after-the-fact detector.

None of these stops a session-level `SET`. A forwarder is therefore trusted not to issue one.

**Option B, under spike (M13).** Forward through a bh RPC service on the primary, so executors
hold no Dolt login at all. The spike also measures whether a forced global on the primary's
hive server lets a stale write land, which decides how urgent B is. M12 ships option A
regardless; the spike's verdict only decides whether B replaces it.

B is in tension with "no trusted receiver", and the difference is deliberate. The receiver was
one fleet-wide, separately deployed component that every frame's liveness and every hive-lease
CAS waited on. B is a per-hive service run by that hive's primary, inside the bh process that
already holds the write. It validates nothing on behalf of the fleet and sits on no other
hive's path. If the primary is down, its hive is down with or without B. It adds no new
single point of failure, but it is a new trusted network surface, and the spike must show
that it is smaller than a Dolt login.

**M13 result (`bh-uhx2r`, proposed, awaiting operator decision): NO-GO for B replacing A in
0.23.0. B is feasible and is deferred.** Full record:
[bh-uhx2r-forward-rpc-option-b.md](../spikes/bh-uhx2r-forward-rpc-option-b.md).

- **A forced global lets no stale write land.** The spike ran 15 honest publish paths under
  `dolt_force_transaction_commit=1`, alone and with `dolt_transaction_commit=1`, and 0 landed.
  `DOLT_COMMIT` refuses a constraint violation whatever the global says.
  `dolt_allow_commit_conflicts` is session-only and cannot be set globally.
- **The exposure is the grant shape.** A forwarder login granted `ALL` on the hive database
  lands a stale write with no global at all, and once it re-stamps the marks `fence_audit`
  cannot see it. Database-wide DML lets a forwarder rewrite `bh_local_ident` and become a
  second writer that nothing detects.
- **Table-scoped grants close both, and bd's verbs still work.** The shape is:
  - DML on bd's tables and `dolt_ignore`;
  - `SELECT` on the fence tables;
  - `SELECT, INSERT` on `bh_write_mark`;
  - no `DOLT_COMMIT` right.
- **Proposed amendments to M12, M1, M10 and condition 16** (operator decision):
  - those table-scoped grants, with a conformance check for them;
  - the watchdog list corrected;
  - forwarder sessions killed before a divert reset, because an in-flight forwarded write is
    otherwise acknowledged and then dropped;
  - an I3-style history check added to `fence_audit`.
- **Option B is feasible.** A prototype RPC service carried create, claim and close with one
  winner per claim race and no added latency. It refused SQL, and it failed closed on demotion.
  B stays on file for when executors are not operator-controlled, or when per-claim
  authorization or globals become a need.

### 4. Failover policy

- **Time triggers reassignment; it never gates a write.** `HostLease.expires_at` leaves every
  write gate (`guard_primary`, `held_by`, `renew_if_due`) and survives only as a failover hint.
  An HQ outage blocks handoff, never writes.
- **`failover_after` per role, configurable from the start, in data (M8c).** Defaults:
  - `executor`: 60 min. It must ride through the 35.4 min false-stale stretch observed live.
  - `transient`: 30 min.
  - `viewer`: never placed.

  Overrides are a per-role, per-hive field on the HQ placement row. They are **never** a
  `host.yaml` or fleet-config key. That keeps `bh-32379` S2's "data is the switch" rule, avoids
  the 0.21.3 unknown-key skew, and costs no authority renewal (`bh-87l3y`). A value is
  validated on load against the `bh-cvk70` E20 invariants and **refused, never clamped**, below
  its floor:
  - executors at least about 36 min, above the 35.4 min false-stale stretch (**proposed,
    operator to confirm**);
  - at least bd lease TTL plus reclaim grace (15 min);
  - at least 2 × (session TTL + sync interval).
- **Staleness source:**
  - In `dolt-server` HQ, the age of the frame's session row by the HQ server's
    `UTC_TIMESTAMP(6)`.
  - In `git` HQ, the observer's first-seen staleness of verified signed beats
    (`host_heartbeat_core.observe`). This is unchanged, and still frame-clock-assisted.
- **Observed-window rule.** The director fails over only when
  `min(server staleness, observed window) > failover_after`, measured on its own monotonic clock.
  The window resets whenever HQ is unreachable, **and on any gap between two successful
  observations longer than `failover_after / 2`** (`bh-cvk70` E15; `bh-jbb6r` E9). Without the
  gap reset, a director that missed an outage fails a healthy primary over the moment HQ
  returns.
- **Who acts.** The director or operator CASes placement (§1), and the new frame runs adopt.
  Amendment 1 §3's lazy "next host sees an expired lease and CASes" is retired: expiry no longer
  grants anything. Unattended failover is a `dolt-server` HQ capability. In `git` HQ, failover is
  an operator or director placement CAS after `failover_after`.
- **Reclaim is bh's job.** bd's leases are node-local and do not travel (`bh-cvk70` E19,
  `bh-sieai` T5). A **failover** adopt reverts claims inside the bump commit: exactly once,
  idempotent, and leaving what `bd unclaim --force` leaves (`bh-jbb6r` E5). A **planned**
  handoff does not run it.
- **What the evidence covers.** Scenario 9 tested only the unscoped revert, with one frame
  doing all the work. bd's lease reclaim already handles the death of a *non-primary*
  executor: the primary granted its claims and holds their leases, so they are reclaimed after
  about 15 min (5 min TTL + 10 min grace; `bh-sieai` T5). It does not handle the death of the
  *primary*: the new primary has 0 lease rows, so those claims strand (`bh-cvk70` E19).
- **Scoping the failover revert is deferred (condition 17, open design point).** With several
  executors, a blanket revert would pull claims from live agents on surviving frames. How to
  scope it is decided by the state/work pairing design (M14), under the invariant below.
- **State and work travel together (condition 18, binding).** Bead lifecycle state is never
  pushed or published unless the corresponding worktree commits are also pushed to a backup
  location on the remote (`origin`): for example `refs/bh/backup/<bead>` or the bead branch.
  Losing one side must never leave the other orphaned. On frame loss:
  - a dead frame's claim whose work is **not** recoverable from the remote backup is rewound
    to its pre-claim state, as if never done, so it can be claimed again;
  - a claim whose work **is** backed up may be resumed or reassigned with that work.

  M14 designs where backups live, how they are ordered against bd's push, and the reclaim
  policy driven by backup presence. It gates M3. Until it lands, bd's lease reclaim covers
  non-primary death, and manual reclaim is the fallback after a primary's death through the
  first soak.
- **`failover_after` is revisited after the Φ3 soak.** The 60 min executor default was sized
  to ride the coupled heartbeat's false-stale stretches. Session rows remove that cause, and
  with four executors forwarding to one primary a shorter window may be wanted. Any change
  needs soak data and goes through the E20 invariant check.
- **During "adopt incomplete"** dispatch grants no new claims for that hive. The old writer may
  finish publishing until the bump fences it.

### 5. Liveness, conformance and the receiver

- **Session row (`dolt-server` HQ).** One `frame_<principal>_<epoch>_session` table per frame
  incarnation: single row, `CHECK (id = 1)`, UPDATE-only for that frame's principal,
  `dolt_ignore`d before creation. An operator `BEFORE` trigger sets
  `NEW.renewed_at = UTC_TIMESTAMP(6)`, and trigger bodies never write another table
  (`bh-wtsrc` E1–E3). A renewal is one `UPDATE` (p50 3 ms, `bh-wtsrc` E6). Its loop runs
  independently of measurement.
- **Evidence row.** A matching `_evidence` table is written by a conformance job on its own
  timer. `measured_at` is server-stamped. Expiry is `measured_at` plus an **operator**
  `evidence_ttl`, computed by the reader. Signing it is optional, for audit.
- **Eligibility at read time.** One statement joins grant, session and evidence. The predicate
  names stay:
  - `authenticated_fresh_heartbeat` ← session fresh;
  - `conformance_pass` / `release_matches` ← evidence against the grant (range-aware once
    `bh-vfrem` lands);
  - `current_hive_lease_holder` ← placement names the frame, and for writes, `bh_writer` names
    it.

  The claim-time reread stays. The admitted session and evidence stamps are recorded in the
  claim record, which replaces the receiver's per-beat audit (T15).
- **The receiver is deleted in `dolt-server` HQ, last.** Its jobs are re-homed first:
  - hive-lease CAS → director design A (§1);
  - registration binding → operator verification at enrollment, before the grant is written;
  - heartbeats → session and evidence rows.

  It keeps running through the Φ3 soak, is stopped at Φ3b, and is deleted in 0.24.0 (P-D1)
  after an export of `hq_live_*`.
- **Account hardening is mandatory, not optional.** It replaces the one guarantee the receiver
  had that grants do not (T11, a password leaked apart from its host):
  - frame accounts are `'<principal>'@'<frame address>'`;
  - `require_secure_transport` is on;
  - TLS stays as specified in the amended ADR.
- **Server globals (T16)** are an exposure shared with today's design. Mitigate it whether or
  not the receiver goes:
  - run with a read-only `DOLT_ROOT_PATH` config;
  - an operator watchdog asserts critical globals;
  - **never co-host hive databases on the HQ server.** A global
    `dolt_force_transaction_commit=1` would defeat FK epoch retirement.

### 6. Git HQ mode position

- **Placement:** `refs/bh/lease/<prefix>` CAS, unchanged in format (§1).
- **Fencing:** identical, because the fence is in the hive's data and does not depend on HQ mode.
- **Receiver:** none existed and none is added (`bh-wtsrc` E9).
- **Liveness:** keeps the signed `HeartbeatLease`, verified at read time by `observe`. It gets
  the sender-side decoupling (P-F7), so a beat signs cached conformance and no longer waits on
  `hive_ready`. It gets no server-stamped session row.
- **Execution frames are scoped to `dolt-server` HQ.** Git HQ supports single-primary fleets and
  operator-driven handoff. Multi-frame unattended execution on git HQ needs a separate signed
  evidence carrier and is out of scope until required.
- **The amended ADR's "identical eligibility semantics across adapters" becomes "identical
  predicates, different evidence carriers".**

## Binding conditions

The GO holds only with all of these. Conditions 1–12 come from the spikes; 13–18 are added here.
Condition 17 is recorded as a deferred open design point and does not bind.

| # | Condition | Source |
|---|---|---|
| 1 | Every bump inserts an `adopt-<epoch>` sentinel mark. | `bh-vje85` cond. 1 |
| 2 | The live epoch is a singleton row; every adopt rewrites `bh_writer.revision`; both fence tables have monotonic triggers. | `bh-vje85` cond. 2; `bh-cvk70` E12 |
| 3 | Marks are deleted only by adopt, or by the writer for marks already on `origin/main`. | `bh-vje85` cond. 3 |
| 4 | `--force`, `reset-data`, `restore --force` and raw `dolt commit --force` are break-glass: refused through `bh bd`, detected by `fence_audit`. | `bh-vje85` cond. 4 |
| 5 | Every placement write rewrites the CAS token; `1213` and `rowcount 0` mean lost. | `bh-cvk70` cond. 1 |
| 6 | No trigger on a frame-writable HQ table. | `bh-cvk70` cond. 2 |
| 7 | Failover reverts the dead frame's `in_progress` beads in the bump commit, on failover adopts only. | `bh-cvk70` cond. 3; `bh-jbb6r` R2 |
| 8 | The failover observer resets its window on any observation gap longer than `failover_after / 2`. | `bh-jbb6r` cond. 1 |
| 9 | The guard uses the `bh-sieai` shape and table set. The adopt checks for 44 triggers. The Dolt/bd trigger-semantics tests re-run on every Dolt or bd pin bump. | `bh-sieai` verdict, R5; `bh-jbb6r` E6 |
| 10 | Frame-private data refs on a server-mode primary use a distinct remote URL per ref. | `bh-jbb6r` cond. 2 |
| 11 | Before the first cutover of a GitHub-hosted hive, the `bh-vje85` E13 two-writer push race runs against that real remote and shows one winner per round. | Residual risk, all spikes |
| 12 | Migration: the legacy carriers agree before cutover; only the holder cuts over; no fleet-config or `host.yaml` key is a phase switch ("data is the switch"); older bh must not adopt a cut-over hive; the receiver goes last, SQL only. | `bh-32379` cond. 1–4 |
| 13 | `BH_FRAME_HEARTBEAT=advisory` is a single-executor-frame escape. **Clearing it is a hard prerequisite for admitting any additional executor frame** (Decision 6): it is unset on every frame, by the gated A1–A4 procedure (O5), before O8 admits a new executor, whether or not the hive is cut over. Never retire it by deleting the code first. | `bh-32379` cond. 6, S3, §3; operator |
| 14 | Until the cutover, count active-frame rotations per lease. Before a 16th rotation without a renewal or re-adopt, re-adopt deliberately rather than let the lease's grant leave the `HISTORY_LIMIT = 16` archive. | `bh-32379` L9 (code-derived) |
| 15 | Account hardening (host-pinned principals, required TLS) ships with session rows (P-M9), not after them. No hive database is co-hosted on the HQ server. | `bh-wtsrc` T11, T16, R2–R3 |
| 16 | The forward path ships with option A: per-frame host-pinned TLS accounts on the primary's hive server; a globals watchdog there for `dolt_force_transaction_commit`, `dolt_allow_commit_conflicts`, `dolt_transaction_commit` and `read_only`; a read-only `DOLT_ROOT_PATH` config; `fence_audit`. This narrows and detects a forwarder forcing a global; it does not prevent a session-level `SET`. | `bh-wtsrc` E4; this ADR §3; operator |
| 17 | **Deferred — open design point, not binding.** How failover scopes its revert with several executors. Decided by M14 under condition 18. | `bh-jbb6r` R2; operator |
| 18 | **State and work travel together.** Bead lifecycle state is never published unless the matching worktree commits are pushed to a backup on the remote. On frame loss, an unbacked claim is rewound to its pre-claim state; a backed-up claim may be resumed or reassigned with its work. | operator invariant; this ADR §4 |

## Amendment to multi-host-model-adr.md Amendment 1 (its Amendment 2)

| Amendment 1 | Status after this ADR |
|---|---|
| Admission core: "a managed publisher must win a CAS on a git ref before it invokes bd" | **Replaced on cut-over hives.** The admission CAS is the hive data push itself, plus epoch retirement and the guard. The pre-bd ref reservation remains only during coexistence. |
| §1 Leases centralize in HQ | **Stands, renamed.** The record at `refs/bh/lease/<prefix>` is now **placement**. `expires_at` is a failover hint only. `adopt` no longer CASes "from expired-or-absent": a placement CAS is a deliberate director, operator or release action. `takeover --force` becomes an ordinary placement CAS by an authorized principal. |
| §2 The fence splits from the lease | **Superseded.** The fence moves into the data (§2 above). `refs/bh/epoch` is dual-maintained during coexistence, then frozen as a floor. Adopt order inverts from "fence first, lease second" to "placement first, data second", because placement is now what makes an epoch unique to one frame (`bh-cvk70` E10). The half-done state "fails old", not "fails closed", and recovers by rolling forward. The `bh-tfapu` atomicity gap is closed without an upstream bd seam. |
| §3 Asymmetric roles, TTL 30 min / renew 5 min | **Roles stand. TTL scaling becomes `failover_after` per role** (60/30 min defaults, viewer never placed, per-hive overrides in the placement row). Lease renewal from the dispatcher loop is retired; `renew_if_due` is a no-op. |
| §4 HQ as a coordination dependency; "a current primary keeps writing only until its cached lease expires" | **The dependency stands for handoff only.** The quoted failure mode is reversed: an established primary keeps writing indefinitely while HQ is unreachable, because the data, not a cached clock, decides who writes. |
| §5 Vocabulary | **Extended.** **Placement** is the HQ record (git HQ still spells it `refs/bh/lease/<prefix>` and `bh host list` still calls it the host lease). The **writer epoch** is the hive's `bh_writer.epoch` (221 on `bh` today). The **incarnation epoch** is the frame grant's `hq_principal_registry.epoch`, which release rotation advances and which never moves the writer epoch. The **worker lease** is still bd's. |
| Consequence: "`ClaimRecord` carries the `epoch`" | **Stands.** On a cut-over hive the live epoch is read from local `bh_writer`, seeded equal to the lease epoch so in-flight tokens survive (`bh-32379` T1). |
| Consequence: direct `bh bd dolt push\|sync` refused even on the primary | **Lifted per cut-over hive** (§2 above). |

## Amendment to frame-dolt-server-hq-mode-adr.md (`bh-v0k3i`)

| `bh-v0k3i` / original section | Status after this ADR |
|---|---|
| Config service: separate database, trusted publisher, exact-HEAD reads, restore floors | **Stands unchanged.** |
| Runtime authority in a protected database; operator-signed authority | **Stands.** The operator key stays off-frame. |
| "Each frame incarnation gets a distinct authenticated SQL principal and inbox table" | **Amended.** The principal stays. The inbox is replaced by per-incarnation `_session` and `_evidence` tables (§5). The registry's `inbox_table` column names them. |
| "The separately deployed trusted receiver validates … envelopes … before writing … receipts, monotonic floors, public signed observations and global hive lease CAS/results" | **Retired.** Placement CAS → director design A. Liveness → server-stamped session rows. Registration → operator enrollment check. `hq_live_receipts`, `_floors`, `_public_observations` and `_results` are deleted in 0.24.0. `hq_live_hive_leases` is kept as the placement row. |
| Original "Grants…" section: "grants do not restrict a frame to its own rows" | **Superseded for liveness.** With one table per frame, the table grant is the row isolation (`bh-wtsrc` E1). |
| Original: "SQL credentials never replace frame signing keys" | **Reversed for liveness in SQL HQ**, under the co-residence premise. The SQL principal is the liveness credential, narrowed by host pinning and required TLS (condition 15). Operator authority stays signed. Evidence signing is optional, for audit. |
| Original: "Identical eligibility semantics; different availability" | **Becomes "identical predicates, different evidence carriers"** (§6). |
| Residual availability risk: cross-frame overwrite and flooding | **Closed** for liveness (single-row, UPDATE-only tables, T5). Server-global DoS (T16) is recorded as open in both designs, with the §5 mitigations. |
| "Start with one authoritative SQL writer" | **Stands and becomes load-bearing.** Placement is only as linearizable as one un-replicated server. Hive databases are never co-hosted on it. |

## Consequences

- **Lifted:** `bh bd dolt push|sync` and bd auto-push on cut-over hives; the receiver as an
  availability anchor; the laptop LaunchAgent once `bh-rjjjo` lands.
- **New operator duties:** per-hive cutover (C1–C6) and rollback (R1–R5), through a hidden,
  temporary verb that is removed once every hive is cut over (Decision 3); the GitHub canary
  (condition 11); orphan merges after a partitioned writer rejoins; reading `fence_audit` in
  `bh doctor`.
- **New refusals:** force, reset-data, restore-force and strategy verbs through `bh bd` on
  cut-over hives. An old bh replica on a cut-over hive is read-only on `main` (fail-closed).
- **Cost:** about 77 ms per guarded bd write (`bh-sieai` E3). A planned handoff costs 5–9 s
  from CAS to the new writer's first write; an adopt 2.4–4.7 s (`bh-jbb6r` E10).
- **Still break-glass and still undetectable before the fact:** `bd dolt push --force`. That is
  no worse than today, where every raw push bypasses the fence.
- **Upstream issues: tracked internally only, not filed upstream** (Decision 7). The drafts
  live in-tree under F4. Fixes would make conditions belts rather than the only brace; none
  is required:
  - bd: the server-mode write commit omits trigger side-effect tables; `vc merge` skips the
    pre-merge commit; `MergeWithStrategy` skips the FK settle gate; federation sync exits 0 on
    a failed merge;
  - Dolt: `SET GLOBAL`/`PERSIST` are not privilege-checked; the seven trigger bugs in
    `bh-sieai` R5; a sql-server ignoring `--ref` on a same-URL remote (`bh-jbb6r` E8).

## Implementation molecule — replan outline

**Not filed yet.** `/bh:replan bh-qlgmm` files this now that the operator has accepted the
verdict, linked back to `bh-qlgmm`. The source is `bh-32379` §6, amended by the
[Decisions](#decisions-operator-2026-10-05) below. The shape follows the house rule that a split
becomes sub-epics, each landing on `main`. `→` is a hard dependency. Existing beads are linked,
not re-filed. Item ids (F, M, O, E, D) are local to this outline; `P-F1`-style references are
`bh-32379` §6 ids.

**Parent epic:** `feat(fleet): hive writer partitioning — in-data fence, director placement,
receiver removal` (implements this ADR).

### Sub-epic A — 0.22.x patch-safe prerequisites (start immediately)

| # | Bead title | Depends on |
|---|---|---|
| F1 | `fix(sync): sync_state treats Merged:false, a non-null Error or a ✗ line as failure` (P-F1) | — |
| F2 | *link* `bh-87l3y` `fix(fleet): fleet-config edits must not fence every frame` (P-F3) | — |
| F3 | `test(fence): Dolt/bd trigger-semantics canary re-run on every Dolt or bd pin bump` (P-F4) | — |
| F4 | `chore(ops): globals watchdog and read-only server config for the HQ server and every primary's hive server; keep the bd/Dolt issue drafts in-tree, not filed upstream` (P-F5) | — |
| F5 | `refactor(fence): delete the unused fenced_push family; move transport helpers beside store_locator` (P-R1) | — |
| F6 | `fix(fleet): bound the signed-mode inbox` (P-F6; file only if Φ3 is more than a release away) | — |
| F7 | `fix(fleet): decouple the heartbeat from conformance in the sender; one in-tree TTL source; bh host heartbeat units` (P-F7) | — |
| F8 | `docs(fleet): rotation runbook/ADR corrections (signed mode never renews; 16-entry horizon; check needs off-host binding); document BH_FRAME_HEARTBEAT` (P-F8) | — |
| F9 | *link* `bh-3q5m9` + ops task `chore(ops): rotate the factory frame in one operator laptop session, deploy the in-tree sender, remove the shim and factory-local-heartbeat.py` | F7 |

### Sub-epic B — 0.23.0 coexistence minor (dormant until a hive's data switches it on)

| # | Bead title | Depends on |
|---|---|---|
| M1 | `feat(fence): product fence + guard module — schema, idempotent install in composed order, 44-trigger check, bh_local_ident provisioning, fence_audit` (P-M1) | F3 |
| M2 | `feat(fleet): placement-first adopt with idempotent step 2, sentinel bump and dual refs/bh/epoch CAS; adopt-incomplete reporting` (P-M2) | M1 |
| M3 | `feat(fleet): failover adopt reclaims the dead frame's beads in the bump commit, per the M14 pairing policy` (P-M7; conditions 17–18) | M2, M14 |
| M4 | `feat(guard): clock-free write gate and live_epoch from local bh_writer on cut-over hives; expiry advisory everywhere; retire renew_if_due` (P-M3) | M2 |
| M5 | `feat(hive): hidden, temporary bh hive fence cutover\|status\|rollback verb (C1–C6, R1–R5), documented only in the runbook; doctor uses fence_audit` (P-M4) | M1, M4 |
| M6 | `feat(guard): lift bh bd dolt push\|sync on cut-over hives; refuse force, reset-data, restore --force, --strategy, conflicts resolve on bh_*, plain vc merge` (P-M5) | M4, F1 |
| M7 | `feat(work): managed push diverts to frame/<id>/orphan when superseded; writer orphan-merge verb` (P-M6) | M4 |
| M8 | `feat(fleet): SQL placement by director credential with fresh receiver-format revisions; observed-window failover observer with gap reset` (P-M8) | M2, F2 |
| M8b | `feat(fleet): spread hive primaries across executors in placement; bh doctor warns when placement is lopsided` | M8 |
| M8c | `feat(fleet): per-role, per-hive failover_after field on HQ placement rows (defaults 60/30), validated on load against the E20 invariants and refused, never clamped, below the floor` | M8 |
| M9 | `feat(fleet): per-incarnation session/evidence rows, separate renewal loop and conformance job, one-statement read_eligibility, data-switched reader, claim-time audit stamps, host-pinned TLS accounts` (P-M9) | M8, F7 |
| M10 | `test(fence): composed integration suite ending in check_invariants; fixed seeds plus Φ2 mixed-version events, multi-executor forwarding and backup-driven reclaim; sender-stall event for the Φ3 soak` | M2–M7, M9, M12 |
| M11 | `docs(fleet): BEADS-SYNC, HQ.md, FRAME-FLEET-MEMBERSHIP, CONFIGURATION (deprecate hq.sql.liveness; failover_after lives on placement rows), cutover runbook, proposal status, 0.23.0 release note with trust delta` | M5–M9, M8b, M8c, M12 |
| M12 | `feat(fleet): forward write path for non-primary executors, option A — bd pointed at the primary's hive server over TLS with per-frame host-pinned accounts, globals watchdog, read-only DOLT_ROOT_PATH config` (P-M10; condition 16) | M4; M13 only for choosing option B |
| M13 | `spike(fleet): forward through a bh RPC service on the primary so executors hold no Dolt login (option B); measure whether a forced global on the primary's hive server lets a stale write land` (early; may land in 0.23.0) | — |
| M14 | `spike(fleet): DECISION — pair bead state with work: remote backup location for worktree commits, ordering against bd push, backup-driven reclaim policy on frame loss` (condition 18) | — |
| — | *link* `bh-kmxyp` / `bh-vfrem` (release ranges) and `bh-wj8hu` / `bh-rjjjo` (laptop-free authority); land in 0.23.0 if possible | — |

### Sub-epic C — operator rollout (procedures; no release)

| # | Bead title | Depends on |
|---|---|---|
| O1 | `chore(ops): install 0.23.0 on the factory by active-frame rotation (Φ1), one operator laptop session` | M1–M12, M8b, M8c, F9 |
| O2 | `chore(ops): GitHub-hosted ref CAS canary, then canary hive cutover (Φ2)` | O1 |
| O3 | `chore(ops): cut the bh hive over (Φ2), seed from the live writer epoch` | O2, O10 |
| O4 | `chore(ops): SQL Φ3 — provision session/evidence tables, director credential on the HQ host, soak with the receiver running, then stop it (Φ3b, laptop-off acceptance)` | O3, M8, M9, `bh-rjjjo` |
| O5 | `chore(ops): retire BH_FRAME_HEARTBEAT=advisory by bh-32379 §3 steps A1–A3 (condition 13; hard prerequisite for O8)` | O4, or F9 with a clean full-gate soak |
| O6 | `chore(ops): bake the executor frame image on the latest working 0.23.x` | O1 |
| O7 | `chore(ops): deploy three executor frames to PVE from the baked image` | O6 |
| O8 | `chore(ops): enroll and admit the three new executor frames (grants, host-pinned HQ and hive-server accounts, session/evidence tables)` | O7, O5, O4, M13 verdict, M14 decision |
| O9 | `test(ops): verify placement spreading, forwarding, reclaim and failover across four executors, including forwarders' bd heartbeats against a new primary` | O8, O3 |
| O10 | `chore(ops): upgrade xeno-mac.lan (operator Mac) to the latest release and verify bead management (forwarded writes on a cut-over hive) and HQ administration; confirm it holds no unpublished bh commits before O3` | O1, O2 |

### Sub-epic E — post-cutover cleanup (scheduled; patch-safe)

| # | Bead title | Depends on |
|---|---|---|
| E1 | `refactor(hive): remove the temporary bh hive fence verb once every hive is cut over; rollback after that follows the runbook by hand` | every hive cut over |

### Sub-epic D — legacy removals (dormant; not scheduled)

Per Decision 5 these are not scheduled. Code that no supported configuration reaches stays
dormant. When they ship, they may ship as a patch that only removes legacy functionality that
was never officially supported, rather than as a 0.24.0 minor.

| # | Bead title | Depends on |
|---|---|---|
| D1 | `refactor(fleet): remove SqlTrustedReceiver, inbox/receipt/floor/observation/result tables and sql_receiver_worker after an hq_live_* export` (P-D1) | O4 |
| D2 | `refactor(fence): stop reserving refs/bh/epoch, freeze it as a floor; remove install_fence/read_fence/EpochFence/prepush` (P-D2) | every hive cut over |
| D3 | `refactor(fleet): remove legacy_lease_policy and HostLease.advisory_expiry` (P-D3) | O10 (`xeno-mac.lan` off the legacy policy) |
| D4 | `refactor(fleet): SQL HQ drops HeartbeatLease embedded conformance; git HQ keeps it` (P-D4) | O4 |
| D5 | `refactor(fleet): remove BH_FRAME_HEARTBEAT` (P-D5) | O5 |

## Decisions (operator, 2026-10-05)

1. **Verdict accepted.** GO, staged, for both halves, with conditions 13–15. Conditions 16–17
   were added afterwards, when Decision 6 made multiple executors a plan.
2. **Git HQ accepted as proposed.** Execution frames are scoped to `dolt-server` HQ. Failover on
   git HQ is operator-driven.
3. **The cutover verb is hidden and temporary.** It ships in 0.23.0 outside the public help and
   docs surface, documented only in the runbook (M5). A scheduled removal (E1) follows once every
   hive is cut over, so no vestigial verb remains.
4. **One operator laptop session is accepted** for F9 and O1. In addition, the operator Mac
   `xeno-mac.lan`, today a legacy transient host, moves to the latest release and is verified
   for bead management and HQ administration (O10). That unblocks D3.
5. **Removals stay dormant and unscheduled.** They may later ship as a patch that only removes
   legacy functionality that was never officially supported.
6. **The forward path is not deferred.** The operator plans three new executor frames: bake an
   image, deploy to PVE on the latest working 0.23.x, then enroll them (O6–O9). M12 stays in
   0.23.0. Condition 13 becomes a hard prerequisite before the new frames join (O5 → O8). The
   single-executor assumptions in this ADR were revisited:
   - the forward path's status and hardening (§3, condition 16);
   - failover reclaim with several executors (§4; now governed by the follow-up decisions);
   - the `failover_after` executor default, revisited after the Φ3 soak (§4);
   - test coverage for forwarding and scoped reclaim (M10) and a four-executor verification (O9).
7. **Upstream bd and Dolt issues are tracked internally only.** The drafts stay in-tree under
   F4. Nothing is filed upstream at this time.

### Follow-up decisions (operator, 2026-10-05)

1. **Condition 16 is restated honestly.** On Dolt 2.3.5 any login can set any global, and
   2.4.0/2.4.1 show no change. Option A ships in 0.23.0 to narrow and detect. A spike (M13)
   explores option B, a per-hive bh RPC service with no Dolt login for executors, and measures
   whether a forced global actually lets a stale write land. M12 does not wait on it.
2. **Condition 17 is deferred** as an open design point. The operator's governing principle is
   binding instead, as condition 18: state and work travel together, and on frame loss unbacked
   claims are rewound while backed-up claims may be resumed. M14 designs the pairing and gates
   M3. bd lease reclaim (non-primary death) and manual reclaim (first soak) are the fallbacks.
3. **Hive primaries spread across executors** (M8b), with a `bh doctor` lopsidedness warning.
4. **`failover_after` is configurable from the start** (M8c), in data on the placement row,
   validated and refused below the floor. The about-36-minute executor floor is proposed and
   awaits operator confirmation. Tune after the Φ3 soak.
5. **Admitting the three new executors (O8)** also waits on the M13 verdict and the M14
   decision.
