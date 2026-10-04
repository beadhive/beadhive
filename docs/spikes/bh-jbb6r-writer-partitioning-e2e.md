# Spike `bh-jbb6r` — does the composed writer-partitioning prototype hold end to end?

**Bead:** `bh-jbb6r` · **Seat:** `dev/e2e-run` · **Type:** spike (test-only code, no product code)
**Parent:** `bh-qlgmm` (hive writer partitioning spike molecule)
**Feeds decision on:** `bh-pr889` (the ADR), with `bh-32379` (migration map)
**Proposal:** [hive-writer-partitioning-proposal.md](../design/hive-writer-partitioning-proposal.md)
("Validation approach")
**Executable evidence:**
[`tests/spikes/test_bh_jbb6r_writer_partitioning_e2e_int.py`](../../tests/spikes/test_bh_jbb6r_writer_partitioning_e2e_int.py)
(`integration` + `dolt_server`, 13 tests), the composed prototype
[`tests/harness/composed_fence.py`](../../tests/harness/composed_fence.py), and the soak driver
[`tests/spikes/bh_jbb6r_soak.py`](../../tests/spikes/bh_jbb6r_soak.py) (outside the land gate).

## Question

Composed together, do the five spikes' recommendations pass the proposal's full ten-scenario
validation table under fault injection, with acceptable latency, and with no stale write ever
reaching `main`? The composed pieces are:

- placement CAS (`bh-cvk70`): design A on a `dolt-server` HQ, and `refs/bh/lease/<prefix>` in git
  mode;
- the in-data epoch fence (`bh-vje85` binding conditions 1–4);
- the write guard as built (`bh-sieai`: 14 tables, 42 triggers);
- the forward path for non-primary writes (`bh-sieai`);
- failover that reverts the dead frame's `in_progress` beads (`bh-cvk70` R5, `bh-sieai` T5),
  triggered by server-timed session rows (`bh-wtsrc`).

It also asks whether a seeded randomized interleaving run over adopt, push, partition, kill and
the stale pull-then-push path through bd holds four invariants:

1. at most one frame's commits extend `main` per epoch;
2. `main`'s `bh_writer` epoch is monotonic;
3. no commit authored under epoch `e` lands after the bump to `e+1`, except through a deliberate
   orphan merge;
4. no merge commit on `main` carries a `bh_write_mark` row whose epoch is retired.

The bead's acceptance asked for at least 500 randomized schedules. **By operator decision on
2026-10-04 that item was relaxed to an optional benchmark** (Evidence 13): the 500-schedule
sharded run pushed the shared host's load average to about 27, and the factory frame's measured
heartbeat then timed out. The gate evidence is the fixed-seed integration test; the randomized
driver ships as an opt-in script with a small default.

It does not re-decide any piece on its own (each sibling spike did that), does not test GitHub-
hosted `git+ssh` refs, and does not cut over the live frame (`bh-32379`). It changes no product
code.

## Method

Run on 2026-10-04 on `beadhive-factory` (32 cores, shared with other agents; load average 2–30
during the runs) with bd `1.3.0 (f45b249ce)`, Dolt CLI and sql-server `2.3.5`, git `2.55.0`. Every
Dolt process ran with a private `HOME` and `DOLT_ROOT_PATH`; no `SET GLOBAL` or `SET PERSIST` was
issued; `~/.dolt/config_global.json` was not touched. Targeted pytest stayed at `-n 2` (all
tests are `dolt_server`). The interrupted 500-seed run used four shard processes, two of them
with bd-server frames; the default benchmark runs one world at a time and pauses above load 16.

1. **The composed prototype** is [`tests/harness/composed_fence.py`](../../tests/harness/composed_fence.py),
   built on the `bh-eybn7` fixture (`Cluster`, `Frame`, `Recorder`, barriers, partition, kill):
   - **Placement.** `SqlHQ` is an owned `dolt sql-server`: one `bh_placement` row with a fresh
     revision UUID on every write, `UPDATE … WHERE revision = <expected>` under a director
     principal (frames hold `SELECT` only), `1213` counted as a lost race. `GitHQ` is a bare repo
     whose `refs/bh/lease/fx` holds the existing five-field `HostLease` record, CAS'd by the
     product's own `beadhive.gitref.cas` (`--force-with-lease`). Both replace the fixture's
     in-memory HQ, so the `Recorder` and `rewind` see the real authority.
   - **Fence + guard, one script.** `bh-vje85`'s tables are created first — `bh_writer {frame,
     epoch, revision}`, singleton `bh_epoch_live (id PK, epoch UNIQUE)`, `bh_write_mark (id,
     epoch FK, tbl)` plus the `adopt-<epoch>` sentinel — and then `bh-sieai`'s
     `write_guard.guard_ddl()` runs unchanged on top: its `create table if not exists` no-ops,
     and its procedure and 42 `BEFORE` triggers stamp `(uuid(), e, '<table>')` into the FK'd
     mark table. `bh-vje85`'s two monotonic triggers follow. Each frame provisions its
     `dolt_ignore`d `bh_local_ident` right after joining.
   - **Adopt** is `bh-cvk70`'s placement-first, idempotent step 2 with `bh-vje85`'s sentinel
     bump. It refuses to act as writer when the node has fewer than 44 `bh_*` triggers
     (`bh-sieai` R1). With `reclaim=True` (failover), the same commit also runs
     `UPDATE issues SET status='open', assignee='', started_at=NULL WHERE status='in_progress'`,
     which is exactly what `bd unclaim --force` leaves (measured).
   - **Managed path** (`bh-vje85` R3): a server frame commits its working set before each push;
     a rejected push fetches without merging and, if `origin/main` names a newer epoch, pushes
     the unpublished commits to `frame/<id>/orphan-<epoch>-<n>` and resets. The new writer's
     orphan merge is `bh-vje85`'s one-session forced merge with mark re-stamping.
   - **Forward path**: a bd workspace on another frame whose metadata points at the primary's
     `dolt sql-server` (`bh-sieai` T5). **Branch path**: a `role='branch'` frame pushes to a
     frame-private data ref `refs/dolt/frame/<id>`; the primary fetches it and merges with a
     `bh: merge frame/<id>` commit.
   - **Liveness**: per-frame `frame_<id>_session` / `_evidence` rows in `SqlHQ`, stamped with
     server UTC by operator triggers, a one-statement eligibility read (`bh-wtsrc`), renewal
     loops in spawned processes, and a `Director` that counts only staleness it observed
     (`bh-cvk70` E15).
2. **The invariant checker** (`check_invariants`) reads only remote `main` (through the observer
   clone, never `refs/dolt/data`: `bh-eybn7` E6) and HQ:
   - **I1** outside-in: every observed move of remote `main` was made by the frame that the moved-to
     `main` names in `bh_writer`. Moves are attributed by the driver: after a single-frame op the
     mover is that frame; in a concurrent op each push that returned ok is attributed to the
     commit it published. Also: no epoch is ever named with two frames anywhere in history.
   - **I2**: along every parent→child edge of `main`'s commit DAG, and across the time series of
     observed remote `main`, `bh_writer.epoch` never decreases; the head holds the highest epoch
     in its history.
   - **I3**: every non-adopt commit stamped with epoch `e` is an ancestor of every adopt commit
     with a higher epoch, unless it entered `main` only through a `bh: merge frame/…` merge.
   - **I4**: no commit (reported separately for merge commits) carries a mark whose epoch is not
     that commit's live epoch.
   - plus `bh-vje85`'s `fence_audit` (`stale_marks`, `epoch_regressed`, `placement_ahead`), and
     **I5**: every write whose push was acknowledged is still on `main`.

   Per-commit epochs, live epochs and marks come from `dolt_history_bh_writer`,
   `dolt_history_bh_epoch_live` and `dolt_history_bh_write_mark`; the DAG from
   `dolt_commit_ancestors` filtered to `dolt_log`.
3. **Scenarios.** One test per proposal scenario, each against a fresh composed world (three
   frames: bd embedded, bd server, Dolt CLI, unless noted), each ending with the full checker.
   `Recorder.step` wraps the decisive steps.
4. **A negative control** proves the checker is not vacuous: a stale CLI writer forces a refused
   pull through with raw `dolt commit --force` and pushes.
5. **Randomized interleavings.** `run_schedule(world, seed)` draws six events from: write, push,
   stale pull-then-push (through `bd dolt pull` / `bd dolt push` on bd frames), `bd sync`, bd
   auto-push, adopt (30 % of them failover-style with reclaim), adopt while the incumbent is parked
   mid-push (both release orders), a barrier-parked push race, partition, heal, kill (half of them
   mid-push, at the CAS), managed divert, orphan merge, and a whole-HQ outage. Push-like events
   write first 70 % of the time, so stale paths carry stale data. Every frame failure is an
   outcome, not an exception. After the events: heal, restart, divert anything unpublished, roll
   any incomplete adopt forward, then the checker. A `checkpoint`/`rewind` (remote ref, every
   frame, HQ placement, and every frame's remote-tracking branches) restores the start state.
   - The integration test runs fixed seeds 0–3 (gate evidence).
   - The opt-in benchmark is `uv run python tests/spikes/bh_jbb6r_soak.py --out <dir>`: 25 seeds
     by default, one world at a time, paused while the load average is above 16, resumable.
     Seeds with `seed % 4 < 2` run on a bd-embedded + bd-server + CLI world, the rest on two
     bd-embedded frames + CLI. Each schedule is one JSONL line with its seed, events, outcomes and
     invariant result, so a seed replays. It is not a `test_*.py` module, so nothing collects it.

## Evidence

### All ten scenarios pass, composed

**E1. The scenario table.** One run of the whole file on the refreshed container base
(`f52e619e`): `uv run pytest tests/spikes/test_bh_jbb6r_writer_partitioning_e2e_int.py -n 2`
→ **13 passed in 505 s**. Every row ended with `check_invariants` clean (`ok: true`, no
`fence_audit` signal).

| # | Scenario | Result | What the composed run showed |
|---|---|---|---|
| 1 | Planned handoff A→B, A idle | **pass** | Handoffs bd-embedded→bd-server and bd-server→CLI. The bump lands (`writer = new`, `live = [e]`, marks only at `e`). A's next raw push: `! [rejected] … (non-fast-forward)`, remote `main` unchanged. After the managed divert, A's `bd create` fails in SQL: `bh-guard: this replica is not the bh_writer for main`. |
| 2 | Handoff while A is mid-write | **pass** | A parked at its push CAS, both release orders, A embedded and server. Bump first: A's push non-ff, title absent. Write first: A's push lands, the adopter's push is rejected, adopt retries from the new head (`attempts = 2`), so the write sits below the bump. |
| 3 | A partitioned, failover to B, A rejoins | **pass** | Partitioned A writes twice locally; the managed push reports `unreachable`; A's HQ read raises `HQUnreachable`. After failover and heal, raw `bd sync` on A is refused on the epoch FK; remote `main` unchanged. The managed divert puts both titles on `frame/<id>/orphan-<e>-<n>`; A's next write is refused by the guard. The new writer (CLI) merges the orphan deliberately; the checker counts 7 commits admitted only through `bh: merge frame/…` merges. |
| 4 | Two adopters race for one epoch | **pass** | Six spawned adopters per round, three rounds per HQ mode: exactly one winner every round. dolt-server losers fail with `1213` serialization errors; git-mode losers get git's `! [rejected]`. A loser that reached step 2 returns `abandoned: superseded in data` and its next write is refused; in the reverse order the stale bump lands first, the later adopter retires it, and the stale frame's pull hits the FK and its push is non-ff. |
| 5 | Stale A: raw push, pull+push, `bd sync`, auto-push | **pass** | All four paths, A bd-embedded and bd-server; remote `main` byte-identical before and after each, stale title never on `main` (table below). |
| 6 | `hive_sync --strategy ours` on a stale replica | **pass** | Stale writer and stale adopter, both bd modes: `hive_sync ours`, `bd sync` and `bd dolt pull` never moved remote `main` and `bh_writer` stayed at the current writer. Re-running the stale bump on top of the new head is refused locally: `bh: fence epoch must increase`. |
| 7 | HQ unreachable longer than the TTL | **pass** | Both HQ modes, ~9.7 s outage against a 2 s session TTL: 3 writes landed through the managed path, 4 handoff attempts refused (`place` fails; adopt returns `abandoned: hq unreachable`), placement unchanged afterwards. On recovery the HQ server reported 22 s of staleness, but the director decided nothing (E9). |
| 8 | Forwarded claim; branch push | **pass** | 5 rounds × 4 concurrent `bd update --claim` through the primary's server: exactly one exit-0 winner per round, matching `claim_won`. A `role='branch'` frame's label and comment edits, pushed to `refs/dolt/frame/q`, merged into `main` through the primary (E8). |
| 9 | Frame killed, session stale, reclaim | **pass** | Primary (bd-server) killed with 3 forwarded claims; its renewer dies with it. Failover became due 3.15 s after the kill (`failover_after = 3 s`); the director's CAS moved placement; the new primary's adopt reverted exactly the 3 claims in the bump commit. A new claim then survived a re-run of the same adopt (`landed`, nothing reverted). `dolt_diff_issues` on `main` shows each bead going `in_progress → open` exactly once. No second failover. The restarted old primary, after pulling, refuses forwarded writes. |
| 10 | Probe slower than the session TTL | **pass** | Session TTL 3 s, evidence TTL 1.5 s, a 6 s probe: the session stayed fresh in every sample, eligibility was blocked by `evidence_unexpired` alone (6 of 7 samples), the director never fired, and the writer landed 2 writes during the probe. Republished evidence restored eligibility. |

No scenario failed, so no step admitted a stale write.

**E2. Scenario 5, path by path** (the line bd printed last):

| Path | bd embedded (bd's linked Dolt) | bd server (Dolt 2.3.5 sql-server) |
|---|---|---|
| `bd dolt push` | non-fast-forward (`hint: 'dolt pull ...' before pushing again`) | same, `exit status 1` |
| `bd dolt pull` then `bd dolt push` | pull: `pull merge left constraint violations bd cannot auto-repair`; push: non-ff | pull: Dolt's constraint-violation report; push: non-ff |
| `bd sync` | `sync failed: pull: … constraint violations bd cannot auto-repair` | Dolt's constraint-violation report |
| auto-push (`BD_DOLT_AUTO_PUSH=true bd create`) | create succeeds; auto-push non-ff | same |

These match `bh-vje85` E5 exactly: the guard's extra 40 triggers and the `tbl` column change
nothing on any default path.

### What composition changed or added

**E3. The fence and the guard compose with no change to either.** Creating `bh-vje85`'s tables
first and then running `bh-sieai`'s guard script verbatim yields 44 `bh_*` triggers on every
frame (42 guard + 2 monotonic), which arrive on joiners by pull. The guard's inline mark insert
fills the FK'd `bh_write_mark (id, epoch, tbl)`. bd-embedded, bd-server and CLI writes all pass
on the writer and are refused elsewhere; every scenario above ran on this one schema.

**E4. bd's commit identity is not the frame.** In `dolt_log`, every bd write commit is
authored `beads <beads@local>` (both modes) and bd's schema commits `root <root@%>`; only Dolt CLI
frames commit under their own `user.name`. So "which frame extended `main`" cannot be read back
from history. The checker therefore attributes moves outside-in (I1), and the after-the-fact
audit has to keep relying on data signals (`stale_marks`, `epoch_regressed`, `placement_ahead`),
never on authorship.

**E5. Failover reclaim in the bump commit is exactly-once.** The bump's
`UPDATE issues … WHERE status = 'in_progress'` is a guarded write on the new writer, so it
stamps a mark at the new epoch, and it is inside the idempotent adopt: a retry after a
non-fast-forward recomputes from the new head, and a re-run after landing stops at the data check
(`landed`) before touching anything. `bd unclaim --force` leaves the same row
(`status = open`, `assignee = ''`), so bd reads reclaimed beads normally.

**E6. The adopt-time trigger check costs one query.** `information_schema.triggers` on the
adopter, compared with 44; a short guard makes adopt return `refused: guard incomplete` before
it commits anything (`bh-sieai` R1).

**E7. Placement CAS cost.** Design A's `UPDATE … WHERE revision` round trip takes **21–22 ms**
on loopback; a six-way race resolves in 0.21–0.23 s. Git mode's `gitref.cas` race resolves in
1.28–1.29 s (spawn, blob write, `push --force-with-lease`).

### Two defects found only by composing

**E8. A Dolt 2.3.5 sql-server ignores `--ref` on a second remote with the same URL.** On the
bd-server primary, `CALL DOLT_REMOTE('add', '--ref', 'refs/dolt/frame/q', 'frame-q', <hive url>)`
succeeds and `dolt_remotes` shows `{"git_ref": "refs/dolt/frame/q"}`, but `DOLT_FETCH('frame-q')`
sets `remotes/frame-q/main` to **the same hash as `origin/main`**: it re-reads
`refs/dolt/data`, so the merge is a silent no-op. The branch frame's push did land on
`refs/dolt/frame/q` (`git for-each-ref` on the remote). Adding the same remote as
`<hive url>/` (a trailing slash) makes the fetch read the private ref, and the merge brings the
label in. `bh-sieai` measured the private ref with an embedded primary, where the CLI does not
share an open remote, so it did not see this. Any product use of frame-private refs on a
server-mode primary must give each such remote a distinct URL, and should verify after fetch
that the private branch's hash differs from `origin/main` when it should.

**E9. The director's observed-window rule needs a gap reset.** `bh-cvk70`'s rule restarts the
window only when the director *sees* HQ unreachable. In S7 the director looked once before the
outage and once after it; the HQ server then reported 22 s of staleness, the window spanned the
whole outage, and the rule fired at once on a healthy primary (first run of
`test_s7_…`: `assert director.due() is False` failed). The fix in the composed `Director`: a
gap between two successful observations longer than `failover_after / 2` starts a new window.
With it, S7 and S9 both pass: no failover after an outage, failover 3.15 s after a real kill.

### Latency

**E10. Adopt, handoff and write latency** (loopback `git+file://` remote, loaded shared host;
seconds):

| Measure | bd embedded → bd server | bd server → Dolt CLI |
|---|---|---|
| Placement CAS (design A) | 0.021 | 0.022 |
| Adopt step 2 (sync, data and HQ checks, bump, commit, push) | 4.67 | 2.37 |
| **Handoff downtime**: CAS to the new writer's first write on `main` | **8.60** | **5.33** |

During a planned handoff the old writer may keep publishing until the bump lands (placement
first, `bh-cvk70` R3), so the window in which *no* frame can land a write is the adopt (2.4–4.7 s
here); the downtime row adds the new writer's first write and push. Mid-write handoffs (S2)
took 2.4 s when the bump won and 4.8–6.9 s when the incumbent's write won and adopt retried.
Failover-with-reclaim adopt (S9): 2.97 s, after detection at `failover_after` + 0.15 s.

| Write path (S8) | Ack | On remote `main` |
|---|---|---|
| Primary writes `main` directly (bd-server `bd create` + managed push) | — | 3.04–3.32 |
| **Forward** (`bd create` on another frame, executed on the primary's server) | **0.50–0.72** (committed on the primary) | **3.31–3.56** (after the primary's next push) |
| **Branch** (label + comment on a branch frame, push to its private ref, primary fetches, merges, pushes) | local | **5.71–6.04** |

The forward path costs one LAN round trip over a local write; on `main` it equals a primary
write because the primary's push dominates. The branch path adds a second push and a merge.

### The invariant checker and the randomized run

**E11. The checker is not vacuous** (`test_invariant_checker_flags_a_stale_write_forced_past_the_fence`).
A stale CLI writer's pull is refused on the FK; it commits the merge with raw
`dolt commit --force` (break-glass, `bh-vje85` E11) and pushes. The checker reports:

- I1: `{"mover": "c", "main_names": ["a", 3]}`;
- I3: the commit `cli: neg-stale` at epoch 2 lies outside the epoch-3 bump's ancestry;
- I4: the merge commit carries a mark at retired epoch 2 while live is `[3]`;
- `fence_audit`: `stale_marks = 1`.

**E12. Fixed seeds in the gate.** Seeds 0–3 (six events each, plus recovery and the check) all
held every invariant, 19–29 s per schedule on one world; the test took 144 s.

**E13. Randomized runs outside the gate.**

- **Interrupted 500-seed run (stopped by the operator).** Four shards (two bd-server worlds,
  two all-embedded worlds) completed **195 schedules** from seeds 0–203 before the operator
  stopped it at 22:08Z for host load (load average ~27, factory heartbeat measurement timing
  out). All 195 held every invariant: **0 violating seeds, 0 harness errors**. They drew 1,361
  events (counting the writes that precede push-like events), including 139 pull-then-push
  attempts through bd (43 pulls refused on the FK, 43 pushes non-ff), 77 `bd sync` (32 FK
  refusals), 127 raw pushes (76 non-ff), 84 mid-push handoffs, 48 barrier push races, 39 kills,
  55 whole-HQ outages (the writer's managed push landed in all 55), 97 diverts and 128 adopts.
  281 writes were made by frames HQ no longer placed: 137 refused by the guard, 144 accepted
  locally and then fenced on push or diverted. Median schedule 25.7 s, p95 34.3 s.
  Orphan merges appear in the run but merged nothing (orphans were usually created during
  recovery, after the events); the orphan path is covered by S3.
- **Default benchmark (operator's relaxed item).** `uv run python tests/spikes/bh_jbb6r_soak.py
  --out <dir>` at its defaults (seeds 0–24, one world at a time, started at load 7.9): **25/25
  schedules held every invariant, 0 violating seeds, 0 harness errors**, 12.2 min wall (it
  paused twice for load above 16). 169 events: 15 pull-then-push through bd, 24 raw pushes,
  12 mid-push handoffs, 5 push races, 5 kills, 6 HQ outages, 11 adopts, 11 diverts; 38 writes by
  frames HQ no longer placed (18 refused by the guard, 20 accepted locally and fenced
  afterwards). Median schedule 21.0 s, p95 27.5 s.

### Cost

**E14.** The file runs in **505 s at `-n 2`** (13 tests; the slowest are S5 at 159 s and the
fixed seeds at 144 s); each composed world costs ~24 s to build (founder `bd init` ~7 s, a
server join ~10 s, the HQ server < 1 s). All tests are `integration` + `dolt_server`.

## Verdict — **GO**

**The composed prototype passes all ten proposal scenarios under fault injection, and no
stale write reached `main` on any default path, in any scenario, in the four gate seeds, or in
the 195 randomized schedules that ran.** The concrete enablers compose without modification:
the HQ placement CAS decides who (one winner per race in both HQ modes, E1 row 4); the in-data
fence enforces it on every bd path, because a stale write is either non-fast-forward or trips
the retired epoch's foreign key at merge (E2); the guard refuses a stale node's next write once
it has the bump (E1 rows 1, 3); failover reclaim is exactly-once because it rides in the
idempotent bump commit (E5); and no write path consults a clock or HQ (E1 rows 7, 10).

Latency is acceptable for the stated workload: a planned handoff costs 5–9 s from CAS to the new
writer's first write on `main`, an adopt 2.4–4.7 s, and a forwarded write is acknowledged in
0.5–0.7 s and reaches `main` with the primary's next push (E10).

The GO carries the four sibling spikes' binding conditions unchanged, plus two found here:

1. **The failover observer resets its window on any observation gap** longer than
   `failover_after / 2`, not only when it sees HQ down (E9). Without it a director that missed
   the outage fails a healthy primary over the moment HQ returns.
2. **Frame-private data refs on a server-mode primary use a distinct remote URL** per ref (E8).
   Without it the primary's fetch silently re-reads `refs/dolt/data` and branch-path edits never
   merge. This loses no data and admits no stale write, but it silently drops the branch path.

The randomized acceptance item was relaxed by operator decision (Question, E13); the gate holds
the fixed seeds and the driver remains available as an opt-in benchmark.

## Recommendation

For the ADR (`bh-pr889`) and the implementation molecule:

1. **Adopt the composition as built in [`tests/harness/composed_fence.py`](../../tests/harness/composed_fence.py).**
   One install script: `bh-vje85`'s fence tables first, `bh-sieai`'s guard unchanged on top,
   the two monotonic triggers last (E3). Adopt is `bh-cvk70`'s placement-first idempotent step 2
   with the sentinel bump, a 44-trigger check before committing (E6), and — on failover only —
   the in-progress revert inside the bump commit (E5).
2. **Failover policy:** keep `bh-cvk70` R4's observed-window rule with E9's gap reset, and keep the
   reclaim in the bump. Note for the policy (not a defect): reclaim reverts *every*
   `in_progress` bead, which is right under the forward path (the dead primary granted all live
   claims) but would also revert claims granted while a planned handoff was in flight; run it
   only on failover adopts, as here.
3. **Branch path:** if it ships, register each frame-private remote with a distinct URL on
   server-mode primaries, and have the merge step assert that `remotes/<frame>/main` differs
   from `origin/main` after fetch (E8). File the Dolt issue: a sql-server reuses an open remote
   for an identical URL and ignores the new remote's `--ref` (2.3.5).
4. **Audit attribution:** `bh doctor`'s `fence_audit` must not rely on commit authorship — bd
   commits as `beads`/`root` (E4). If per-frame attribution is wanted after the fact, the mark
   could also carry `bh_writer.frame` as the writing node saw it (the trigger may read
   `bh_writer`, not `bh_local_ident`); for a stale write that is the stale frame itself.
5. **Keep the invariant checker as a reusable test asset.** The implementation molecule's
   integration tests should end every fencing test with `check_invariants`, and run the fixed
   seeds. The randomized driver stays an **optional benchmark**:
   `uv run python tests/spikes/bh_jbb6r_soak.py --out <dir>` runs 25 seeds, one world at a time,
   pausing above load 16; scale with `--seeds 0:500` and, on an idle host only, `--parallel N`
   (≤ 4 shards, ≤ 2 with bd-server frames). Never put it in the land gate: at ~25 s per schedule,
   500 schedules are ~3.5 h of single-world time and loaded this shared host past what the
   factory heartbeat tolerates.
6. **Residual risks**, unchanged from the siblings: GitHub-hosted `git+ssh` refs are untested;
   `bd dolt push --force`, `bd dolt remote reset-data` and raw `dolt commit --force` remain
   break-glass and are detectable afterwards (E11 shows the checker and `fence_audit` catching
   the last one); dolt-server HQ is only as linearizable as one un-replicated server.
