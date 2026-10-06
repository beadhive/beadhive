# Spike `bh-55vvh` — pair bead state with work: where backups live, how they are ordered against bd's push, and how frame loss reclaims

**Bead:** `bh-55vvh` (M14) · **Seat:** `dev/pairing` · **Type:** decision spike (no product code)
**Parent:** `bh-16347` (hive writer partitioning B, 0.23.0)
**Feeds decision on:** binding condition 18 and deferred condition 17 of
[hive-writer-partitioning-adr.md](../design/hive-writer-partitioning-adr.md). It gates M14b
(`bh-cqvj6`), M3 (`bh-4z2rx`) and C/O8.
**Status:** proposed. **Operator acceptance: pending** (see [Operator acceptance](#operator-acceptance)).

## Question

The ADR's condition 18 is the operator's invariant. Bead lifecycle state is never published
unless the matching worktree commits are also pushed to a backup on `origin`. On frame loss, an
unbacked claim is rewound to its pre-claim state. A backed-up claim may be resumed or
reassigned with its work.

What concrete design delivers that invariant on the direct, forward and bd auto-push paths, and
lets failover scope its revert so that live agents on surviving frames keep their claims
(condition 17)? It must settle six things:

1. the backup ref scheme and its retention;
2. publish ordering, including partial failure in each direction;
3. what "recoverable" means operationally;
4. the reclaim decision table for primary and non-primary death;
5. how it uses a claiming-frame record;
6. the trust, storage and first-soak consequences.

It is **not** asking whether bd's leases can be made to travel (they cannot, `bh-cvk70` E19),
whether the fence or adopt change (they do not), or how forwarders re-lease on a new primary.
The last is measured by O9; see open question Q5.

## Method

1. Read the ADR (§3 forward path, §4 failover, conditions 7, 17 and 18, the Decisions and
   Follow-up decisions) and the spike evidence it cites: `bh-jbb6r` (scenario 9, E5, R2),
   `bh-cvk70` (E18–E20, R5), `bh-sieai` (T5 §8, recommendation 3) and `bh-32379` (S2, "data is
   the switch").
2. Read the lifecycle code that writes or publishes bead state today:
   - `packages/beadhive-core/src/beadhive_core/lifecycle.py`: `claim`, `resume`, `abandon`,
     `mark_submitted`;
   - `src/beadhive/work_lifecycle.py`: the `ShellWorkspace` capabilities `refresh`,
     `publish` and `record_claim`;
   - `src/beadhive/work_submission.py` (`impl_submit`, `_open_submit_gate`);
   - `src/beadhive/work.py` (`_push_state`);
   - `src/beadhive/engine.py` (`push_state`);
   - `src/beadhive/work_merge.py` and `src/beadhive/worktree_git.py` (`push_branch`, safety
     refs);
   - `src/beadhive/claim_authority.py` (`ClaimRecord`);
   - `src/beadhive/localloop.py` (the worker-lease heartbeat);
   - `src/beadhive/prepush.py` (hook reachability).
3. Ran git 2.55.0 against a local bare remote, from the session scratchpad, to pin the ref
   semantics the design relies on: custom-ref create, CAS and rewrite; directory/file
   conflicts; default clone and fetch visibility; `--atomic`; gc reachability; and the
   ancestor test used for "work present". **Nothing was pushed to any real remote.**

## Evidence

**E1. Lifecycle state is written in several places, and only some of them publish.**
- `Lifecycle.claim` refreshes, acquires the lease, re-reads, provisions the worktree, then
  writes a worktree-local claim record (`lifecycle.py:532-566`). It never calls
  `workspace.publish`.
- `assign` publishes (`lifecycle.py:522`).
- `submit` opens the review gate, sets `review=pending`, then calls `_push_state`
  best-effort (`work_submission.py:340-347`).
- Merge closes beads through `close_merged` (`work_logic.py:1271`).
- Publication to `origin` happens whenever any process on the writer runs a managed or raw
  `bd dolt push` (`engine.py:409`), or when bd auto-pushes.

So "state is published" is not an event the claimer controls.

**E2. On the forward path the publisher is a different frame.** A forwarder's write executes
on the primary's server under the primary's guard (`bh-sieai` §8: `created_by = f`, mark
stamped on `p`). Whatever the primary publishes next carries the forwarder's state, whenever
the primary next pushes. The forwarder cannot sequence anything before "the publish" of its
own write. It can only sequence things before the write itself.

**E3. No publish-time hook can enforce the pairing.** bd invokes git with
`core.hooksPath=/dev/null` in both storage modes (`prepush.py:1-30`, measured by `bh-ukit.2`).
The managed boundary around `Engine.push_state` is "sequenced rather than atomic" and raw bd
bypasses it (`engine.py:418-424`). On cut-over hives the ADR lifts `bh bd dolt push|sync` and bd
auto-push (§2). Any rule that must hold "on every publish" therefore has to hold at write time.

**E4. Today a bead can be closed or submitted while its work exists only on one disk.**
- With a local review gate, submit pushes nothing. `push_branch` runs only for `gh:*` gates
  (`work_submission.py:604-607`), yet submit publishes `review=pending` at a sha that lives in
  one worktree.
- A local merge lands onto the local integration branch and closes the bead. `main` reaches
  `origin` later, through the batched `main-gate` pre-push (`justfile:44-92`).
- A frame lost in either window leaves published state that points at work no other frame can
  reach. This is the orphaning condition 18 forbids.

**E5. The claim record that names the claiming frame is worktree-local.** `ClaimRecord` carries
`seat`, `host_id` and `epoch` (`claim_authority.py:81-114`). It is written into the worktree's
private git-dir, so it dies with the frame. Nothing in hive data names the frame that holds a
claim. `assignee` is a seat name, and seats are not bound to frames.

**E6. The failover revert tested so far is unscoped.** `bh-jbb6r`'s adopt runs
`UPDATE issues SET status='open', assignee='', started_at=NULL WHERE status='in_progress'` in
the bump commit. That is what `bd unclaim --force` leaves, and it runs exactly once (E5 there).
Scenario 9 had one frame doing all the work. R2 there notes that the revert "reverts *every*
`in_progress` bead".

**E7. bd's lease reclaim covers non-primary death and nothing else.**
- Leases live on the granting replica (the primary), and a dead forwarder's claims are reaped
  after the TTL plus grace, about 15 min (`bh-sieai` T5).
- After the *primary* dies, the new primary has 0 lease rows. `bd reclaim`, `--any-replica`
  and `--id` all reclaim `count: 0`, and 58 beads stranded in the T5 run (`bh-sieai` §8;
  `bh-cvk70` E19).
- bd's reclaim also does not consult any work backup: it rewinds a backed-up claim exactly
  like an unbacked one.

**E8. Surviving forwarders' claims were granted by the dead primary too.** They are
`in_progress` in data with no lease row on the new primary. `claim` on an already-held bead
skips `acquire` (`lifecycle.py:539-543`), so an idempotent re-claim does not re-create a lease.
Only `resume` re-acquires (`lifecycle.py:658`).

**E9. The worker-lease heartbeat already runs per in-flight bead.** `localloop` step 4 calls
`coordination.heartbeat` for each bead (`localloop.py:1754-1760`). That is the natural tick for
periodic work checkpoints. bd's lease table is `dolt_ignore`d, so the heartbeat itself publishes
nothing versioned.

**E10. Custom refs behave as needed** (git 2.55.0, local bare remote):

| # | Probe | Result |
|---|---|---|
| a | `push --force-with-lease=refs/bh/backup/bh-x/fA: <sha>:refs/bh/backup/bh-x/fA` (create-only) | `* [new reference]` |
| b | CAS update with the correct expected value | fast-forward accepted |
| c | CAS with a stale expected value | rejected, nothing changed |
| d | Rewrite after an amend (refine) with the correct lease | `+ … (forced update)` accepted |
| e | `refs/bh/backup/bh-x` next to an existing `refs/bh/backup/bh-x/fA` | `! [remote rejected] … (refname conflict)` |
| f | Fresh `git clone`, then a default `git fetch` | **0** `refs/bh/*` refs copied |
| g | `git ls-remote origin 'refs/bh/backup/*'` | lists exactly the backup refs |
| h | `--atomic` with a stale lease on one ref plus a branch | both rejected (`atomic push failed`) |
| i | `git gc --prune=now` on the remote | the tip of a backup ref survives; a superseded pre-rewrite commit is pruned |
| j | `merge-base --is-ancestor <tip> <base>` | false exactly when the tip carries work not in base |

A no-op push (remote already at the pushed sha) reports up to date without evaluating the
lease. That is harmless here.

**E11. The hive's bead data and its git work share one remote.** bd's git-backed Dolt remote is
`refs/dolt/data` on the hive's `origin`, and `hq.py:1520-1545` already copies refs on that
remote for backups. A single `git push --atomic` could in principle move `refs/dolt/data` and a
backup ref together (E10h). But bd builds and pushes `refs/dolt/data` from its hidden transport
repo, and on the forward path another frame does it (E2, E3). Atomicity across the two is not
reachable without a bd seam.

**E12. Pushes to custom refs on GitHub are unmeasured here.** The lease and epoch refs live in
the HQ repo, and the ADR records `git+ssh` CAS against GitHub as untested (`bh-vje85` E13,
condition 11). Whether a `refs/bh/*` push triggers Actions or shows in the UI was not probed: no
real remote was touched.

## Verdict — **GO**

Condition 18 is implementable without a bd seam. It needs:

- **per-(bead, frame) custom backup refs on `origin`**;
- **ordering at write time, not publish time.** A work-asserting state write happens only after
  its backup has landed. Every publisher (managed push, bd auto-push, the primary publishing a
  forwarder's write) then publishes only state whose work is already on the remote (E2, E3);
- **a claim-frame state dimension written in hive data**, which turns condition 17 into a
  data-scoped table instead of a blanket revert;
- **backup presence on `origin` as the single test of recoverability.**

The residual non-atomicity is one-sided by construction. Work can land without its state, but
never the reverse, and work without state is surfaced and resumable rather than lost.

## Recommendation — the decision

### D1. Backup location: `refs/bh/backup/<bead>/<frame>` on the hive's push remote

- **Name.** `refs/bh/backup/<bead-id>/<frame-id>`. `<frame-id>` is the placement frame id the
  principal registry names, with characters outside `[A-Za-z0-9._-]` mapped to `-`.
- **Remote.** The hive's `config.push_remote` (normally `origin`; the fork for external hives,
  never `upstream`; `worktree_git.py:296-306`).
- **Value.** The worktree branch tip (`wt/bead/<type>/<id>`, or `wt/batch/<group>` for a batch,
  `wt/bead/epic/<id>` for a container).
- **Single writer.** One ref per (bead, frame), and only the frame named in the ref writes it.
  Every write is a CAS (`gitref.cas`, `--force-with-lease=<ref>:<last pushed sha>`, empty for
  create; E10a–d), and the last pushed sha is kept in the `ClaimRecord` (new field
  `backup_sha`). A frame that loses the CAS stops and reports. It never retries blind.
- **Batches.** Every member gets its own `refs/bh/backup/<member>/<frame>` at the shared tip. A
  ref costs nothing, and reclaim stays a per-bead lookup.
- **Containers.** Epics are backed up like any bead (`refs/bh/backup/<epic>/<frame>`).

**Rejected alternatives:**

- **Pushing the bead branch itself (`refs/heads/wt/bead/...`).**
  - Branch pushes are what CI and the forge UI react to.
  - `refine` rewrites would force-push visible branches.
  - The `gh:pr` gate already writes that branch, so a resumed holder and a zombie would share a
    writer.
  - It is fetched by every default clone (E10f is the inverse).
- **`refs/bh/backup/<bead>` without the frame.** It makes a dead or partitioned frame and the
  new holder contend on one ref, so the zombie's last push can land after the new holder's.
  The per-frame segment means zero contention, a zombie can only write its own ref, and the
  segment is evidence for D5. It also cannot coexist with the nested form (E10e), so one shape
  has to be chosen forever.
- **An atomic push of `refs/dolt/data` plus the backup ref.** Not reachable (E11).

### D2. Ordering: backup before any work-asserting state write

**Rule P.** A `bh work` verb that writes lifecycle state asserting work first pushes the
worktree tip to its backup ref. It verifies the push from git's per-ref status line (or from
`ls-remote` when status is ambiguous) and only then writes state. If the push fails, the verb
fails closed and writes no state.

Because the order is at write time, it holds on every publish path without touching any of
them:

| Path | Who publishes | Why P covers it |
|---|---|---|
| **Direct** (primary's own agents, managed `push_state`) | the primary | the state row does not exist until the backup has landed |
| **bd auto-push / raw `bd dolt push` / sync** | bd, unhooked (E3) | same: whatever bd publishes was written after its backup |
| **Forward** (forwarder writes into the primary's server) | the primary, on its own schedule (E2) | the forwarder pushes its backup with its own git credential *before* the forwarded write; the primary's publish timing is irrelevant |

Which writes assert work, and which do not:

| Transition | Asserts work? | Under P |
|---|---|---|
| `claim` (status, assignee) | No. At claim time the branch is at base. | Claim writes state with no backup (offline-safe). It then writes the claim-frame dimension (D4). |
| Checkpoint while `in_progress` | No state write | Best-effort backup push on the heartbeat tick, on `check` and on `refine` (D2a). |
| `submit` (gate plus `review=pending` at sha S) | **Yes** | Push the backup at S, then `_open_submit_gate`, then `mark_submitted`. |
| `submit --group` | **Yes** | Push every member's backup at the shared S, then the gate. |
| `resume` (re-assert the claim) | No new work | Re-assert the claim-frame dimension. Provision from the backup when the local branch is absent (D6). |
| `merge` into a container, child closed | **Yes** (the child's work now lives in the container) | Push the container's backup at the post-merge tip, then close the child. |
| `merge` / `finish` onto `main`, bead closed | **Yes** | The bead's backup at the merged tip already exists from submit and is **retained until `origin/main` contains it** (D7). No extra push is needed at merge. |
| `abandon` (own claim) | No | No push. An existing backup is left in place and the bead is marked resumable (D5). |
| Comments, notes, labels | No | Unaffected. |

**D2a. Checkpoints.** The recoverable unit is whatever the backup holds, so checkpoints bound
lost work. Push the backup:

- from the worker-lease heartbeat tick when the worktree HEAD differs from `backup_sha`,
  rate-limited to once per 5 min per bead (E9);
- on `bh work check` and after `bh work refine`;
- from a new explicit `bh work backup <id>`.

A checkpoint failure only warns. It is not a state write, so P does not apply.

**Partial failure, each way:**

| Failure | Resulting state | Outcome |
|---|---|---|
| Backup push fails | No state written | The verb fails closed ("backup push failed — nothing submitted"). Nothing is orphaned. Retry when `origin` is reachable. |
| Backup lands, then the state write fails (or the frame dies in between) | Work on `origin` with no matching state | **Orphan work, never lost.** `bh doctor` `pairing_audit` lists it. Re-running the verb is idempotent: the CAS is a no-op at the same sha (E10), then the state is written. If the bead is later claimed, D6 offers the backup. |
| State is written on the primary's server, then the primary dies before publishing it (forward path) | `origin` has the backup but not the state | Same as the previous row. The new primary adopts from the remote head, where that state does not exist. A surviving agent's next verb refuses ("not currently claimed — re-claim"). Its re-claim reattaches its own worktree and backup. |
| A claim is published, then its holder's frame dies with no backup | State with no work anywhere | Not an orphan: this is exactly the "unbacked → rewind" row of D5. |
| State is published while the backup CAS loses to a foreign writer | Impossible under P | The state write never happens. |

The design deliberately does not make "state without work" impossible for the claim itself. A
claim has no work to pair with, so condition 18 is satisfied vacuously. The reclaim table makes
an unbacked claim safe to rewind.

### D3. "Recoverable" operationally

A claim on bead *b* held by frame *f* is **recoverable** when, in one fresh fetch of
`+refs/bh/backup/<b>/*:refs/bh/remote-backup/<b>/*` made by the reclaiming actor, all three of
these hold:

1. the ref `refs/bh/backup/<b>/<f>` exists on the push remote;
2. its tip object was fetched and is connected (the fetch succeeded);
3. its tip is **not** an ancestor of the bead's integration base as currently on the remote
   (`origin/main`, or the parent container's backup): it carries at least one commit of work
   not already landed (E10j).

The outcomes:

- If the ref is absent, or its tip is already in base, the claim is **unbacked**.
- If the fetch or `ls-remote` fails, the result is **unknown**. Unknown never rewinds: the
  reclaim step is skipped for that run and retried by the next adopt re-run, or by the sweep
  in D5b.
- In strict mode (open question Q3), a backup with commits not signed by an allowed fleet
  signer is treated as **suspect**: it is kept and marked for the operator, and is neither
  auto-resumed nor rewound.

For a submitted bead the test is stronger: the recorded submitted sha (`review` reason
`submitted <sha>`) must be reachable from that bead's backup ref. Anything else is a
`pairing_audit` violation.

### D4. The claiming-frame record lives in hive data

- **What is written.** A bd state dimension `claim-frame=<frame-id>` (`bd set-state`, which
  becomes label `claim-frame:<id>` plus an event). It is written by `claim`, immediately after
  the lease is won and re-read, and before provisioning. `resume` and the re-lease step (Q5)
  re-assert it. Rewind and release clear it, and close leaves it as history.
- **Why not the `ClaimRecord`.** The `ClaimRecord` keeps `host_id` as a local mirror, but it
  dies with the frame (E5). Only data travels to the new primary.
- **Torn write.** The lease acquire and the `set-state` are two writes. A claim that crashes
  between them is `in_progress` with no `claim-frame`, which is the **unattributed** case. It
  is never auto-rewound by a failover adopt (D5). A repeated `claim` by the same actor repairs
  the missing dimension. `pairing_audit` lists any that remain.
- **Trust.** The record is a scoping hint written by the claimer. Under option A a forwarder can
  already write any bead row (ADR §3), so the record adds no new trust. D9 covers its abuse.

### D5. Reclaim decision table (resolves condition 17)

**D5a. Failover adopt (primary death).** This runs in the bump commit, on failover adopts only
(condition 7), and exactly once (`bh-jbb6r` E5). It covers rows whose `claim-frame` equals the
dead primary's frame. The adopt computes the table against one fetch of
`refs/bh/backup/*` taken after placement, before building the bump commit. A non-fast-forward
retry recomputes from the new head and a new fetch, and a landed re-run stops at the data
check. That is what makes it idempotent.

| # | Bead state | `claim-frame` | Backup of (bead, dead frame) | Action in the bump commit |
|---|---|---|---|---|
| 1 | `in_progress`, review `pending` or `approved` (submitted) | dead frame | must exist at the submitted sha | **Untouched.** Submitted work is a handoff, not work in progress. The merger materializes the branch from the backup (D6). If the backup is missing, flag `pairing-violation` for the operator and do not touch it. |
| 2 | `in_progress`, not submitted | dead frame | **recoverable** (D3) | **Resumable.** Set `status=open`, clear `assignee` and `started_at`, clear `claim-frame`, set state `recovery=resumable` with reason `<ref>@<sha>`, and add an audit comment. Dispatch picks the bead up again, and the next claim provisions from the backup. |
| 3 | `in_progress`, not submitted | dead frame | absent, or tip already in base | **Rewind to pre-claim.** Same fields as `bd unclaim --force` (`status=open`, `assignee=''`, `started_at=NULL`), clear `claim-frame`, delete an empty backup ref, and add an audit comment. Lifecycle fields are identical to never-claimed. The audit lives only in the comment and the bump commit's history. |
| 4 | `in_progress` | dead frame | **unknown** (fetch failed) | **Untouched this run.** Reported as `reclaim-pending`. The adopt still lands the bump (writes are not blocked) and D5b retries the row. |
| 5 | `in_progress` | **another frame** | — | **Never touched by this adopt**, live or not. That frame's own loss is handled by D5b. |
| 6 | `in_progress` | **none** (unattributed, legacy or torn) | — | **Untouched.** Listed in the adopt report as `unattributed` for the operator. During the first soak this is the manual fallback (D10). |

The audit comment is authored as `ops/adopt@<new-frame>`. It records the epoch, the dead frame,
the outcome (`rewound` or `resumable`), the ref and sha if any, and the UTC time. This meets
M3's "who, when, why on each bead".

**D5b. Non-primary frame loss.** Two cases:

- **The primary is alive.** bd's lease reclaim on the primary rewinds a dead forwarder's claims
  after about 15 min (E7), whether or not they are backed up. That is compatible with
  condition 18 because recoverability is decided by the backup ref, not by bead state. A bead
  bd reclaims with a backup is "open with an unmarked backup". The sweep below marks it
  `recovery=resumable`, and D6 resumes it.
- **The forwarder's claims have no lease row** (it was granted by a primary that has since
  failed over, and it died before re-leasing, Q5). Neither bd nor D5a can see those claims.

The sweep is `bh fleet reclaim --frame <id>`. The director runs it automatically on
`dolt-server` HQ, and the operator runs it on `git` HQ. It runs on the current primary as an
ordinary guarded write, not in a bump, when the frame's session staleness exceeds
`failover_after` under the same observed-window rule (ADR §4). It applies rows 2–6 of D5a to
`in_progress` beads whose `claim-frame` is that frame and that hold no live lease row on the
primary. It also annotates bd-reclaimed open beads that have backups. It is idempotent: a row
already rewound or marked is skipped.

**D5c. Planned handoff.** Applies nothing, as today.

### D6. How work is resumed

**Claim and resume:**

- When `claim` or `resume` finds no local `wt/bead/...` branch but backup refs exist for the
  bead, it fetches them.
- If the bead is marked `recovery=resumable`, it provisions the worktree from the recorded
  `<ref>@<sha>`. A later backup from the same frame is never silently preferred.
- If there are unmarked backups (orphan work), `claim` lists them and provisions from base
  unless the caller passes `--from-backup <frame>`. Choosing between two frames' orphan work is
  a human decision.
- The new holder then backs up to its own `refs/bh/backup/<bead>/<new-frame>`.

**Review and merge.** `bh work review` and `merge` materialize a missing local branch from the
bead's backup at the submitted sha. This is also what lets a merger on one frame land work
done on another executor.

### D7. Security, storage and retention

**Retention.** A backup ref is reaped only when its content is covered or provably abandoned:

- **Covered.** Its tip is an ancestor of the integration ref on the remote (`origin/main`, or
  the parent container's backup for a child). The merger's existing safety-ref reaper
  (`work_submission.py:346`, `worktree_git.py` `delete_safety_refs`) runs it with a CAS delete
  after each land, and `bh doctor --fix` runs it as a sweep.
- **Rewound.** Row 3 deletes empty refs in the same adopt.
- **Superseded by a resumed holder.** Once the new holder's bead is closed as merged, every
  other frame's ref for that bead is deleted after a **14 d grace**. A refined history is not
  an ancestor, so ancestry alone cannot prove coverage.
- **Closed without landing** (rejected, wontfix): deleted after **30 d**.
- **Orphan work** (bead open, no marker): never auto-deleted. `pairing_audit` reports it, and
  a custodian removes it after operator confirmation.

**Storage.**
- The refs hold only bead-branch commits, which are small next to `refs/dolt/data`.
- Rewrites leave unreachable objects that the forge collects (E10i).
- The ref count is bounded by about (open beads × frames that touched them) plus unexpired
  graces, in the low hundreds for this fleet.
- Default clones and fetches never download them (E10f).

**Security.**
- **Exposure.** Backups make in-progress work readable to anyone who can read the remote; on a
  public repo, `ls-remote` lists them. That is a real delta for public hives when the review
  gate is local, because today nothing is pushed before merge. Checkpoints can also publish a
  secret committed by mistake before review. Mitigation: the backup push runs the same
  secret-scan the `main-gate` pre-push runs, on the commits new to the remote. This is a bh
  call, since hooks do not fire for bh's internal push of a sha to a custom ref unless it is
  invoked through lefthook. **Operator decision Q2.**
- **Integrity.** Executors need push access to `origin` (D9). GitHub rulesets and deploy keys
  cannot scope pushes to a ref prefix. A frame able to push backups can delete or overwrite
  another frame's ref. That turns a recoverable claim into an unbacked one (lost work, a
  denial of service), or plants commits that a resumer builds on (an integrity risk, still
  caught by review).
- **Mitigations:**
  - per-frame ref segment plus CAS;
  - `pairing_audit` flags refs whose frame segment never held `claim-frame` for that bead;
  - the resumer verifies commit signatures against the fleet's allowed signers before
    auto-resume (strict mode, Q3);
  - branch protection on `main` stays the integrity boundary for landed code.

### D8. Dormancy: data is the switch

Pairing (P, the checkpoints and `claim-frame`) is active on a hive whose data has `bh_writer`,
which means the hive is cut over. This is the same switch as the rest of 0.23.0
(`bh-32379` S2), so no config key becomes a phase switch (condition 12). A hive that is not cut
over behaves exactly as on 0.22.x. Whether to offer it earlier as an opt-in is Q1.

### D9. Trust deltas for the 4-executor fleet (O8)

| # | Delta | Before (one executor) | After M14b + M3 (four executors) | Mitigation / residual |
|---|---|---|---|---|
| T-a | Executor git credential on the hive remote | Only push-for-`gh:*` gates needed write | **Every executor needs push** to the hive's push remote (custom refs). On GitHub that is whole-repo write. | `main` branch protection; per-frame segment; `pairing_audit`; an operator-held credential per frame, revoked at retirement. Residual: a compromised executor can delete or overwrite other frames' backups (work DoS), which is no worse than its existing ability to write any bead row under option A. |
| T-b | Work visibility | Work stays on its frame until merge (local gate) | Checkpoints and submitted work are on the remote | Q2. Secret-scan before backup push. |
| T-c | Resume builds on another frame's commits | Not possible | A resumed bead continues a dead frame's commits | Signature check against allowed signers (Q3); review gate unchanged. |
| T-d | Who rewrites whose claims | The new primary reverts *all* `in_progress` (scenario 9) | It reverts only `claim-frame = dead primary` | The scope rests on data any forwarder can write (D4). A malicious forwarder could mislabel claims to steer a revert. That is within option A's existing trust, and `fence_audit` / `pairing_audit` detect it after the fact. |
| T-e | New dependency on the remote at submit | Local-gate submit worked offline | Submit on a cut-over hive needs the push remote reachable | Fails closed with nothing written. The forward path already needs the LAN. A transient laptop (`xeno-mac.lan`) cannot submit offline on a cut-over hive. |
| T-f | Reclaim of non-primary frames | bd only, backup-blind | bd plus `bh fleet reclaim --frame` (director/operator) | The director credential gains no new table grants: it is an ordinary guarded bead write on the primary, through the forward path. |

### D10. First-soak manual fallback (until M14b and M3 land, and through the Φ3 soak)

The ADR keeps manual reclaim after a primary's death as the fallback (§4). The procedure for
the runbook (M11), run on the new primary after the adopt lands:

1. **List candidates.** `bh work list --status in_progress --json`. Exclude beads held by seats
   on surviving frames: the operator's Mac and any live executor. During the first soak only
   the factory primary executes, so every remaining claim is the dead primary's.
2. **Find work on the remote.**
   `git ls-remote <push-remote> 'refs/heads/wt/bead/*' 'refs/bh/backup/*'`. Before M14b, only
   `gh:*`-gated submits pushed a branch.
3. **For each candidate**, decide which case applies:
   - **Submitted with its branch on the remote:** leave it.
   - **Branch on the remote with work beyond base:**
     `bh bd unclaim <id> --force`, then
     `bh bd comments add <id> "reclaimed after failover of <frame> at epoch <n>: resumable from <ref>@<sha> — ops/<name>"`.
   - **Otherwise:** `bh bd unclaim <id> --force` with a "rewound, unbacked" comment.

   `bh work abandon` as `ops/` refuses a missing lease, so it cannot be used here (E7).
4. **Re-lease surviving claims.** Have every surviving agent re-run `bh work resume`, which
   re-acquires its lease (E8). Until M3, a surviving agent whose heartbeat fails should
   `resume`, not `claim`.

### D11. Paths that change, and the M14b / M3 implementation outline

**M14b (`bh-cqvj6`, `feat(work)`): the state/work pairing.**

1. **A new `beadhive.work_backup` module** (pure policy plus a git adapter):
   - `backup_ref(bead, frame)`;
   - `push_backup(entry, bead, sha, *, expected)` → `Pushed | Refused | Unreachable`, using
     `gitref.cas` with the push remote, and refusing `upstream`;
   - `fetch_backups(entry, beads)`;
   - `recoverable(entry, bead, frame, base_ref)` → `recoverable | unbacked | unknown | suspect`
     (D3);
   - `reap_covered(entry, …)` (D7).
2. **`ClaimRecord`.** Add `backup_sha` (and `frame_id`, mirroring `host_id`), defaulting to
   empty so old records read cleanly.
3. **Claim** (`lifecycle.claim` and `ShellWorkspace`):
   - after `claim_won`, add `workspace.record_frame(bead, frame)`, which sets
     `claim-frame` through `set_state`;
   - an already-held claim repairs a missing dimension;
   - provisioning consults backups (D6, `--from-backup`).
4. **Resume.** Re-assert `claim-frame`, and provision from the backup when the branch is
   absent.
5. **Submit** (`impl_submit`):
   - insert `push_backup(S)` after `_validate_submit_checkout` and **before**
     `_open_submit_gate`;
   - on `Refused` or `Unreachable`, exit 1 with nothing written;
   - keep the `gh:*` branch push as it is: it is the review transport, not the backup;
   - `submit --group` (`work_group.py:513`) pushes every member's backup first.
6. **Merge.**
   - Materialize a missing local branch from the backup at the submitted sha.
   - Into a container: push the container backup, then close the child.
   - After a land: queue `reap_covered`. It only deletes refs once `origin/main` contains them,
     so it is safe before `main` is pushed.
7. **Checkpoints.** The `localloop` step-4 heartbeat pushes rate-limited backups (D2a), as do
   `bh work check`, `bh work refine` and the new `bh work backup <id>`.
8. **Abandon.** If a backup with work exists, set `recovery=resumable`. Never push.
9. **`bh doctor` `pairing_audit`.** Reports:
   - submitted beads without a backup at their sha (violation);
   - orphan work (info);
   - unattributed `in_progress` beads (warn);
   - backup refs whose frame never held `claim-frame` for that bead (warn).
10. **Dormancy.** Every step is gated on the hive's data having `bh_writer` (D8).
11. **Tests:**
    - unit tests of P's ordering with a remote that refuses or is unreachable (no state
      written);
    - the forward path with the publisher on another frame;
    - the partial-failure rows of D2;
    - the CAS rejection of a zombie push;
    - a real-git test of D3 on a bare remote (E10).

**M3 (`bh-4z2rx`): failover adopt applies D5a.**

1. The adopt's step 2 takes `reclaim_plan = plan_reclaim(dead_frame, fetched_backups, head)`
   before building the bump commit. It is pure and computed from the same head the bump starts
   from.
2. The bump commit applies rows 2 and 3 as guarded SQL in the same session, through the
   existing `UPDATE` shape restricted by `claim-frame`, plus the `recovery` state and the audit
   comment. Rows 1 and 4–6 are reported, never written.
3. Idempotent by construction: a landed re-run stops at `bh_writer.epoch ≥ mine`, and a
   non-fast-forward retry recomputes the plan.
4. `bh fleet reclaim --frame` (D5b) reuses `plan_reclaim` outside the bump.
5. Re-lease (Q5): on the first failed heartbeat against a new primary, a surviving holder runs
   `resume`'s re-acquire. M3 or M12 own it, and O9 verifies it.
6. **Tests** (M10 fixture):
   - three executors, primary killed: only the dead primary's claims change;
   - a surviving executor's live claim is untouched;
   - a backed-up claim ends `open` plus `recovery=resumable` with its ref intact;
   - an unbacked claim is field-identical to a never-claimed bead;
   - a re-run reclaims nothing;
   - a fetch failure leaves `reclaim-pending`.

## Operator acceptance

**Pending.** This doc proposes; it binds once the operator accepts it. The acceptance is then
recorded here and in the ADR's M14 addendum. The open questions below need answers or
confirmation:

- **Q1. Dormancy.** Pairing activates only on cut-over hives (D8). Should it also be an opt-in
  for non-cut-over hives, which is the single-primary factory today, so local-gate work stops
  being single-disk before Φ2?
- **Q2. Exposure on public hives.** Accept that checkpoints and submitted work become readable
  on the remote before review, with a secret scan on each backup push? Or should public hives
  back up to a separate private remote? That would amend D1 to "the hive's backup remote,
  defaulting to the push remote".
- **Q3. Strict resume.** Require commit signatures from allowed fleet signers before
  auto-resuming a backup (D3 "suspect")? This is recommended once the four executors are
  admitted.
- **Q4. Retention windows.** Confirm 14 d after a superseded holder lands and 30 d after a
  close without landing. Orphan work is never auto-deleted.
- **Q5. Re-lease on a new primary.** Surviving forwarders' claims have no lease row after a
  failover (E8). Confirm that M3 or M12 owns an automatic re-acquire on heartbeat failure.
  Until then, the runbook step D10.4 applies. O9 must measure bd's `heartbeat` behaviour for a
  lease-less held claim.
- **Q6. Checkpoint cadence.** 5 min per bead on the heartbeat tick, plus `check` and `refine`.
  Is that granularity of lost work acceptable, or should a git `post-commit` job push on every
  commit?
- **Q7. The GitHub custom-ref canary.** Extend condition 11's canary to push, CAS and delete a
  `refs/bh/backup/*` ref on the real remote, and confirm that it triggers no Actions workflow
  and no UI branch prompt (E12).
