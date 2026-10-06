# Spike `bh-cvk70` — HQ placement CAS authority and clock-independent failover

**Bead:** `bh-cvk70` · **Seat:** `dev/placement`  
**Type:** research-only (no product code)  
**Parent:** `bh-qlgmm` (hive writer partitioning spike molecule)  
**Feeds decision on:** `bh-pr889` (the ADR), and the inputs of `bh-jbb6r` and `bh-32379`

## Question

Can hive placement `{hive, frame, epoch}` be held at one linearizable compare-and-swap point in
both HQ modes (`git` and `dolt-server`) without the trusted receiver? And does a failover
policy in which time only triggers reassignment, and never gates a write, behave correctly when
HQ is down, when frames are slow, and when adopters race?

This spike does not decide how the in-data fence behaves under bd's own pull, sync and
auto-push paths. That is `bh-vje85`, which covers scenarios 1–3, 5 and 6. It also does not
decide the local write guard (`bh-sieai`) or the receiver-free session and evidence rows
(`bh-wtsrc`). It uses a minimal model of the fence only where placement needs one: step 2 of
adopt and the clock-free write gate. It changes no product code.

## Method

Run on 2026-10-04 on `beadhive-factory` with git 2.55.0, Dolt CLI and sql-server 2.3.5 (the
`dolt` that the pinned-server tests use, reporting MySQL `8.0.31`), bd 1.3.0 (`f45b249ce`) and
PyMySQL from the locked environment.

1. **Read the code that exists today:** `gitref.py` (the CAS primitive), `host_lease.py` and
   `host_lease_contracts.py` (lease record, TTL and roles), `host_adopt.py` (two-phase adopt),
   `guard.py` `guard_primary`, `frame_eligibility.py` `evictable`, `hq_control_plane.py`
   `publish_hive_lease`, `hq_sql_receiver.py` (the receiver's hive-lease CAS) and
   `hq_sql_runtime_schema.py`. Also read the proposal, ADR Amendment 1 and the `bh-v0k3i`
   binding amendment.
2. **Read bd's lease and reclaim code** in a read-only clone of gastownhall/beads at
   `f45b249ce`: `internal/storage/issueops/lease.go`, `cmd/bd/reclaim.go` and
   `internal/storage/schema/schema.go`.
3. **Re-verified the pre-kickoff research leads by running them.** I did not take the
   analyst's notes on trust. I used throwaway scratch probes, then pinned each result in a
   test.
4. **Wrote a focused integration test,
   [`tests/test_placement_authority_int.py`](../../tests/test_placement_authority_int.py).** It
   is marked `integration` and `dolt_server`, and it does not depend on the `bh-eybn7` fixture.
   - HQ in git mode is a `file://` bare repo, as in `test_host_fence_int.py`.
   - Hive data is a Dolt database behind a `file://` remote that carries the proposal's
     `bh_writer`, `bh_epoch_live` and `bh_write_mark` tables.
   - HQ in dolt-server mode is a throwaway `dolt sql-server` on a free port. It has separate
     director, frame A and frame B principals and can be stopped and restarted on the same
     data.
   - Racing adopters are real processes from `harness.processes.process_context`.

   ```sh
   uv run pytest tests/test_placement_authority_int.py -n0 -q   # 17 passed in ~45 s
   ```

   All placement, adopt, failover and gate logic in the test is test-only model code.
5. **Observed the live frame without changing anything.** I ran `bh host eligible --hive bh`,
   `bh host list --lease-hive bh` and
   `journalctl -u beadhive-factory-heartbeat.service --since -48h`. I touched no live HQ data.

## Evidence

### Placement CAS in git mode

**E1.** **The existing primitive is already a true CAS and already holds placement.**
`gitref.cas` pushes `--force-with-lease=<ref>:<expected>` and returns `ok=False` with
git's own verdict. It never retries (`src/beadhive/gitref.py:160-170`). The host-lease
record at `refs/bh/lease/<prefix>` already carries `{host_id, epoch}`
(`host_lease_contracts.py:50-58`). Placement therefore needs no new record. `host_id` is the
frame, `epoch` is the epoch, and `expires_at` becomes a hint that no write gate reads.

**E2.** **Scenario 4 in git mode passes.** Six spawned processes read the same placement sha and
then CAS `{F_i, epoch 2}` at once, released together by a barrier. Exactly one CAS succeeds.
The remote record and sha are the winner's. All five losers get `ok=False` with git's
message (`test_s4_git_placement_cas_racing_adopters_exactly_one_wins`).

**E3.** **Trust boundary in git mode:**

- Any principal with push access to the HQ repo can CAS any hive's placement. There is
  no per-principal authority below "can push to HQ".
- The rule that every placement CAS raises the epoch is a client convention. Nothing on
  the server enforces it.
- Safety rests on the hosting service performing the ref update as an old-oid
  compare-and-swap under its ref lock. Local git does this (pinned by `tests/test_gitref.py`
  and by E2). GitHub was not exercised here. The live fleet already relies on it for
  `refs/bh/lease/*` (Amendment 1 §1).
- No receiver is involved in git mode, either today or in the proposal.

### Placement CAS in dolt-server mode

**E4.** **The receiver already uses the right CAS shape.**
`UPDATE hq_live_hive_leases SET revision=…, … WHERE prefix=%s AND revision=%s` treats
`rowcount != 1` as a CAS conflict (`src/beadhive/hq_sql_receiver.py:575-588`). The
question was never whether SQL can do this. It was which principal may issue the statement
and what that principal must be trusted for.

**E5.** **Scenario 4 in dolt-server mode passes.** Six spawned processes, each holding the
director credential, issue
`UPDATE bh_placement SET frame=?, epoch=epoch+1, revision=<fresh uuid> WHERE hive='bh' AND revision=<expected>`
at once, in three rounds. Every round has exactly one matched-and-committed winner. Each
loser either matched 0 rows, because it read the winner's commit, or failed at commit with
`1213 serialization failure: this transaction conflicts with a committed transaction from
another client`. Epoch advanced by exactly one per round
(`test_s4_sql_placement_cas_racing_adopters_exactly_one_wins`). The probe saw both kinds of
loser in one round (4 × `1213` and 3 × `rowcount 0`), so a client must treat `1213` as
"lost" and not as a retry.

**E6.** **Dolt merges concurrent transactions cell by cell, so every placement write must rewrite
the CAS token.** Take a "renew" that changes only a `note` cell, guarded by
`WHERE revision=<r0>` without rewriting `revision`. It commits silently after a competing
adopt has already replaced `r0`, and the row ends up as the adopter's frame with the
renewer's note. When the renew also rewrites `revision`, its commit fails with `1213`
(`test_s4_sql_cas_needs_a_fresh_revision_on_every_write`). bd documents the same property
for issues (`row_lock`, `beads/internal/storage/issueops/lease.go:47-60`).

**E7.** **Trust boundary on the pinned server** (`test_s4_sql_trust_boundary_grants_procedure_and_trigger`):

- (a) Table-level grants are enforced. A frame principal holding only `SELECT` on
  `bh_placement` gets `command denied` on `UPDATE`.
- (b) `SQL SECURITY DEFINER` is parsed and `EXECUTE` is honoured, but the body runs with
  the invoker's rights. A control procedure (`SELECT 1`) succeeds. A definer procedure that
  updates `bh_placement` gets `command denied` for the frame (dolthub/dolt#10190). **A
  stored procedure cannot delegate placement writes.**
- (c) A frame can `INSERT` into its own per-principal candidacy table. Dolt has no
  row-level and no enforced column-level grants, so the table itself is the identity
  boundary, as the `bh-v0k3i` inbox tables already are.
- (d) **Hazard:** if anyone creates an `AFTER INSERT` trigger on that candidacy table, the
  frame's plain `INSERT` changes `bh_placement` with no `UPDATE` grant. Trigger DML skips
  the invoker's write-grant check. A trigger on any frame-writable table is a privilege
  escalation path into placement.

**E8.** **The three dolt-server designs, against E5–E7:**

| Design | Who issues the CAS | What it trusts | Verdict |
|---|---|---|---|
| **A. Director-held credential, one protected row** | A director or operator principal with `UPDATE` on `bh_placement` only. Frames have `SELECT`. | One sql-server process serializing commits on one branch, which is not a replica that can be promoted with lost writes (ADR "Start with one authoritative SQL writer"). Custody of the director credential (fnox). Table grants. The operator not installing triggers that write placement. | **Works.** E5, E6, E7a. |
| **B. Stored procedure callable by frames** | Frames, through `CALL` | Would need `SQL SECURITY DEFINER`, which is not enforced. Frames would then need `UPDATE` on placement themselves, so every frame could CAS every hive. | **Rejected today.** E7b. Revisit only if #10190 lands. |
| **C. Frame-written candidacy row, decided by one director process** | Frames write their own candidacy table. The director decides and then writes using A. | Everything in A, plus the director process being up for handoffs (not for writes). | **A, plus an input channel.** Good for release, drain and "I want this hive" requests. The decision is still A. E7d forbids any trigger on candidacy tables. |

None of these needs the trusted receiver. The director writes placement directly under its
own grant. Frame liveness is a server-stamped row the frame writes under its own grant
(E15–E16). The receiver's job of copying, after validation, into protected tables does
not exist in A or C.

### Adopt ordering and the half-done state

**E9.** **Today the order is fence first, lease second, and it fails closed.** In
`host_adopt.py:86-113`, the fence on the hive remote is enforcement and the HQ lease is
bookkeeping. A crash between the two leaves nobody able to write. `_next_epoch` takes
`max(fence, lease) + 1` so that recovery never mints the same epoch twice (`:58-70`).

**E10.** **The proposal inverts the order. Placement must come first, because placement is now the
only thing that makes an epoch number unique to one frame.** The probe in E12 shows
that two adopts that both reach the data are reconciled only by a merge conflict, and that
the epoch table itself merges silently. Step 2 (`adopt_step2` in the test) is idempotent.
It always starts from the remote head. It checks the data first: if
`bh_writer.epoch ≥ mine`, it stops ("landed" if the row names me, otherwise "superseded").
It then checks that HQ placement still names `(me, epoch)`. Only then does it commit the
bump and push. A non-fast-forward rejection goes back to the data check.

**E11.** **The half-done state is recoverable and never yields two writers**
(`test_half_done_adopt_never_yields_two_writers`). After every step the test syncs all
three frames to the remote head and counts how many pass the clock-free gate (local
`bh_writer.frame == me`).

- **B wins placement at epoch 2 and crashes before step 2.** Writers: `[A]`. A's write
  lands. Placement-first leaves the old writer alone, so this "fails old" rather than
  "fails closed".
- **B restarts.** Step 2 completes. Re-running it reports "landed" and changes nothing.
  Writers: `[B]`.
- **C wins placement at epoch 3 and crashes.** Writers: `[B]`. The director re-places to
  A at epoch 4 and A completes step 2. Writers: `[A]`.
- **C finally runs step 2 at epoch 3.** Outcome: "abandoned: superseded in data". The
  remote ends at `A@4`, with live epochs `[4]`.

**E12.** **Racing step 2 in the data, scenario 4's second clause.**

- **Stale adopter overtaken** (`test_s4_loser_that_reached_step2_is_rejected_and_abandons`).
  B won epoch 2 and stalled. Its HQ check passed, and then C was re-placed at epoch 3 and
  landed before B pushed. B's push is non-fast-forward. Its retry sees `C@3 ≥ 2` and
  abandons. B's gate then refuses `main` writes, and C writes normally.
- **Stale bump lands first** (`test_s4_lower_epoch_landing_first_is_overtaken_and_retired`).
  B's bump at epoch 2 lands, and B writes once at epoch 2. C's push is rejected and C
  retries on top. The remote ends at `C@3`, with live epochs `[3]` and no marks below 3.
  B's next write is fenced: its push is non-fast-forward, and `dolt pull` stops with
  `CONSTRAINT VIOLATION … bh_write_mark`. The working set already names C, the push is
  still rejected, and `b-stale` never reaches remote `main`.
- **Merge evidence for `bh-vje85`**
  (`test_two_adopts_merge_epoch_live_into_a_union_unless_it_is_a_singleton`). With the
  proposal's `bh_epoch_live(epoch PRIMARY KEY)` shape, merging two adopt commits conflicts
  only on `bh_writer`. `bh_epoch_live` silently becomes `{2, 3}`, so any resolution of the
  `bh_writer` conflict would keep both epochs live. A singleton row
  (`id PRIMARY KEY, epoch UNIQUE`, with the foreign key targeting the unique column) makes
  `bh_epoch_live` a second both-modified conflict.

### Scenario 7 and the failover clock

**E13.** **Today, writes depend on clocks and on the receiver.**

- `guard_primary` refuses a write "when this host's own lease has lapsed" by local wall
  clock (`guard.py:380-383`, `host_lease_contracts.py:66-75`). The cached `expires_at`
  "decides when writes stop" while HQ is down (`host_lease.py` `renew_if_due`).
- In frame mode, every write verb first passes `frame_eligibility.require_intake`
  (`guard.py:388-392`), which requires an authenticated fresh heartbeat. Heartbeats are
  accepted only by the separately deployed receiver.
- The receiver judges eviction on its own clock: `now = self.clock()`, then
  `now - first_seen <= evict_after_s` (`hq_sql_receiver.py:347, 565`).

**E14.** **Scenario 7 in git mode passes**
(`test_s7_git_hq_down_past_ttl_primary_keeps_writing_and_handoff_is_refused`). The HQ
repo is renamed away and the clock is set 3 h past the lease's `expires_at`. Today's
predicate `HostLease.held_by(A, at)` is false, so `guard_primary` would make A read-only.

- **Writes continue.** Under the clock-free gate, A makes three guarded writes and all
  three land on the hive remote.
- **Handoff is refused.** B's placement read raises `RemoteUnreachable`, B's CAS returns
  `ok=False`, and step 2 aborts on the placement read before it commits anything.
- **Nothing moved.** When HQ returns, placement still has the same sha with `A@1`, and the
  data still names `A@1`.

**E15.** **Scenario 7 in dolt-server mode passes**
(`test_s7_sql_hq_down_past_ttl_primary_keeps_writing_and_failover_waits`). The test uses
`failover_after = 2 s` so that it runs quickly. The shape is under test, not the number.

- **During the outage.** The HQ server is stopped for longer than `failover_after`.
  Throughout, the director observes nothing and decides nothing. Every placement CAS
  fails to connect. A keeps writing hive data to its own remote.
- **On recovery.** When the server restarts, the server-clock staleness of A's session
  covers the whole outage. A naive `stale > failover_after` rule would evict a healthy
  primary at that moment. The observer rule counts only staleness it saw while HQ was
  reachable, measured on the director's monotonic clock as
  `min(server staleness, observed window)`, so it decides nothing. A renews and placement
  stays `A@1`.
- **When A really goes silent.** The rule fires only after at least `failover_after` of
  observed time. The director's CAS to `B@2` wins, and a second CAS with the same
  expectation loses. B's step 2 lands. A's next push is rejected and its gate refuses.

**E16.** **The session row needs no frame clock.** The frame runs
`UPDATE bh_session_frame_a SET renewed_at = UTC_TIMESTAMP(6)`. The director reads
`TIMESTAMPDIFF(MICROSECOND, renewed_at, UTC_TIMESTAMP(6))`. Both use the HQ server's clock.
The table is `dolt_ignore`d, and it survived an orderly server restart in E15.

**E17.** **Live evidence: the receiver turns a healthy primary into a non-writer.** Read-only
observations on `beadhive-factory`, 2026-10-04:

- **The frame is ineligible to write while it holds a live lease.**
  `bh host eligible --hive bh` at 09:27:52Z failed only `authenticated_fresh_heartbeat`
  and `current_hive_lease_holder`. Meanwhile `bh host list --lease-hive bh` reported
  `held … expires 2026-10-04T10:13:38Z`. This bead's own `bh work claim` was refused with
  `frame … ineligible: authenticated_fresh_heartbeat`.
- **Heartbeats were sent but not accepted for long stretches.** The heartbeat unit
  publishes every 211 s (median). In the 4.75 h journal window (04:45Z–09:32Z), the
  published `seq` stopped advancing for 35.4 min (`seq 60`, from 08:42:54Z), 25.3 min
  (`seq 59`, from 08:17:37Z) and 10.6 min (`seq 33`). Each stall is longer than the
  300 s heartbeat TTL.
- **The receiver's read path also fails.** One run died with
  `SqlRuntimeError: accepted observer receipt changed during read` (07:58:51Z).

In that one morning, a failover policy keyed on that liveness would have moved `bh` off a
healthy frame once at `failover_after` = 30 min, twice at 25 min and three times at 10 min.
At 60 min it would not have moved it.

### `failover_after` and bd's reclaim invariants

**E18.** **bd's own constants and rules:**

- `DefaultLeaseTTL = 5 min` (`beads/internal/storage/issueops/lease.go:26`).
- `bd reclaim --older-than` defaults to `2 × TTL = 10 min` (`cmd/bd/reclaim.go:227`).
- The two invariants bd states it cannot enforce: "grace window > sync interval, and lease
  TTL > sync interval" (`cmd/bd/reclaim.go:56-60`, `lease.go:656-663`).

**E19.** **bd's leases are node-local and do not move with placement.**

- The `leases` table is `dolt_ignore`d (`beads/internal/storage/schema/schema.go:340`).
- Reclaim selects `FROM leases l JOIN issues i` (`lease.go:688`). An `in_progress` issue
  with no lease row on this node is never reclaimed.
- After failover, the new primary's `bd reclaim` therefore has nothing to reclaim for the
  dead frame's claims. Those beads stay `in_progress` until something other than bd
  reverts them.

**E20.** **The proposed defaults pass the invariants**
(`test_proposed_failover_after_satisfies_bd_reclaim_invariants`, for every role and mode).

| role | `failover_after` | rationale |
|---|---|---|
| `executor` | **60 min** | Owns long sessions. Must ride through the 35.4 min false-stale stretch in E17. |
| `transient` | **30 min** | The current baseline lease TTL. A sleeping laptop gets the same bounded window it has today. |
| `viewer` | **n/a** | Never placed (`ttl_for_role` already refuses). Its staleness never triggers anything. |

The checked constraints use a session renewal of 60 s, a session TTL of 5 min
(dolt-server) or 10 min (git), and an HQ sync interval of 0 (dolt-server) or an assumed
5 min (git):

- session TTL > sync interval, and session TTL > renewal interval (bd: TTL > sync);
- `failover_after` > sync interval (bd: grace > sync);
- `failover_after` ≥ 2 × (session TTL + sync interval);
- `failover_after` > bd lease TTL + reclaim grace (15 min), so bd reclaims a dead *worker*
  on the live primary long before a dead *frame* could move the hive;
- for `executor`, `failover_after` > the largest observed false-stale stretch.

## Verdict — **GO**

**Placement can be held at one linearizable CAS point in both HQ modes, with no trusted
receiver.**

- **git mode** keeps the existing `refs/bh/lease/<prefix>` force-with-lease CAS and record
  shape (E1–E3).
- **dolt-server mode** uses a director-held credential to issue
  `UPDATE … SET …, revision=<fresh> WHERE hive=? AND revision=<expected>` on one protected
  row, with frames limited to `SELECT` (E4–E8).

Racing adopters produce exactly one winner in both modes, and a stale adopter that reaches
step 2 is overtaken in the data (E2, E5, E12). With placement first and the bump second,
there is never more than one writer, the half-done state recovers by rolling forward, and no
step depends on a clock (E10–E11).

When HQ is unreachable for longer than the TTL, the primary keeps writing and handoff is
refused in both modes (E14–E15). Time decides only when the director CASes placement. It
is measured on the HQ server's clock, and only over windows the director actually observed
(E15–E16).

The GO has three binding conditions:

1. every placement write rewrites the CAS token;
2. no trigger may sit on a frame-writable HQ table;
3. failover has to revert the dead frame's `in_progress` beads itself, because bd's leases do
   not travel (E19).

## Recommendation

For the ADR (`bh-pr889`) and the implementation molecule, if the decision is GO:

1. **Placement record and CAS.**
   - In git mode, keep the five-field host-lease blob at `refs/bh/lease/<prefix>`, with
     `host_id` read as the frame.
   - Remove `HostLease.expires_at` from every write gate: `guard_primary`, `held_by` and
     `renew_if_due`. It survives as a failover hint at most.
   - Every placement CAS raises the epoch. Release keeps the epoch, as the tombstone already
     does.
2. **dolt-server placement uses design A.**
   - One `bh_placement` row per hive, with a fresh `revision` UUID on every write. E6 rules
     out content-hash revisions that can repeat.
   - `rowcount == 1` with a successful commit means the CAS won. `1213` or `0` means it lost
     and is never retried with the same expectation.
   - The director credential holds `UPDATE` on that table only. Frames get `SELECT`.
   - Add design C's per-principal candidacy tables only as a request channel. Forbid triggers
     on any frame-writable table, and add a conformance check that lists triggers and fails
     on any that touch a protected table.
   - Do not build design B unless dolthub/dolt#10190 ships.
3. **Adopt is placement first, bump second, and idempotent.** Step 2 always starts from the
   remote head. It checks the data (`bh_writer.epoch ≥ mine` ends the attempt), then
   placement, then commits and pushes, and loops on a non-fast-forward rejection.
   - **Recovery** from the half-done state means re-running step 2 or re-placing at a higher
     epoch. Never roll placement back to an older epoch, and never resolve a `bh_writer`
     conflict.
   - `bh doctor` reports `placement.epoch > bh_writer.epoch` as "adopt incomplete".
   - While an adopt is incomplete, dispatch grants no new claims for that hive. The old writer
     may finish publishing in-flight work until the bump fences it.
4. **Failover policy.**
   - Defaults: `failover_after` of 60 min for executors and 30 min for transient hosts. Viewers
     are never placed.
   - Staleness comes from HQ-server-stamped session rows. The director counts only observed
     windows: `min(staleness, observed window) > failover_after`, measured on its monotonic
     clock. A window resets whenever HQ is unreachable.
   - Validate the configuration against the E20 invariants at load time, and refuse a
     `failover_after` that violates them.
5. **Reclaim after failover is bh's job.** The new primary's adopt reverts the dead frame's
   `in_progress` beads exactly once, tied to the epoch bump: in the bump commit or a fenced
   follow-up keyed by the new epoch. bd's `reclaim` cannot see them (E19). Hand this to
   `bh-jbb6r` scenario 9 as a pass condition.
6. **Hand-offs to sibling beads.**
   - `bh-vje85`: use a singleton `bh_epoch_live`, so that two adopts conflict on the epoch
     itself, or prove that no path resolves the `bh_writer` conflict (E12). The
     FK-violation pull refusal under the Dolt CLI is reproduced here; bd's linked Dolt still
     has to be checked.
   - `bh-wtsrc`: the session row and observer rule in E15–E16 are the shape to build.
   - `bh-32379`: the live frame's lease and epoch 2 map directly onto placement `{factory, 2}`,
     because the record is unchanged in git mode.
7. **Residual risks.**
   - Git mode cannot separate "frame" from "director" authority: anyone who can push to HQ can
     CAS. Epoch monotonicity there is a client convention, and GitHub's ref CAS is untested
     here.
   - Dolt-server mode is only as linearizable as one un-replicated server.
   - `bd dolt push --force` remains break-glass.

   None of these is new relative to today.
