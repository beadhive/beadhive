# Spike `bh-wl4jh.1` — native leases, CAS, and claim pools versus seat invariants

**Bead:** `bh-wl4jh.1` · **Seat:** `dev/codex-wl4jh-1` · **Type:** research-only
(no product code)  
**Feeds decision on:** `bh-wl4jh.4`, and through it `bh-6p2j`

## Question

Which Beads v1.3 coordination primitives can Beadhive delegate to or adopt without weakening
its seat model?

The comparison covers claim, heartbeat, reclaim, reassignment, review, interruption, and crash
recovery. The invariants to preserve are: kickoff authorization; seat-typed actors; review of an
exact branch identity; atomic batch ownership; molecule/container topology; a live holder's
authority; and the separate host epoch fence.

## Method

Read the v1.3.0 (`f45b249ce`) help contracts for `update --claim`, `heartbeat`, `reclaim`,
`unclaim`, `config claim.pools`, gates, and `serve`; inspected Beadhive's claim, submit, gate,
batch, merge-slot, molecule, and epoch guards; then exercised an isolated embedded Dolt database.
The probe assigned one issue to pool `crew`, raced `dev/a` and `dev/b`, exercised guarded
unclaim and guarded update mismatches, and attempted a heartbeat after release. It did not touch
the hive database.

## Native contracts, exactly

| primitive | success | contention / stale precondition | durability and scope |
|---|---|---|---|
| `bd update ID --claim` | exit 0; atomically sets caller as assignee, status `in_progress`, and grants a lease | exit 1; e.g. `issue already claimed by dev/a` | status and assignee commit; lease row is ephemeral and local to the granting store/replica |
| pool claim | same claim contract when current assignee is listed by `claim.pools` | an alias not in `claim.pools` remains protected like a real actor | pool membership is config; after reclaim the issue becomes **unassigned**, not reassigned to its former pool |
| `bd heartbeat ID` | exit 0 only for the current owner of an `in_progress` issue; advances expiry and stamps heartbeat time | exit 1 after release/reclaim/close or for the wrong owner; measured released case: `issue not claimable: ... status open` | no Dolt commit or history; enforceable only at the node/store that granted it |
| `bd reclaim` | exit 0 and returns sufficiently stale, locally granted claims to `open`, clears assignee, records recovery | skips foreign-replica leases by default; `--any-replica` is an explicit unsafe override | granting replica is recorded; TTL and grace must each exceed the sync interval |
| `bd unclaim ID --if-assignee A` | exit 0 only while A is still holder; measured output `Unclaimed ...` | exit 1, no write, and names actual/expected holders | atomic inverse-of-claim CAS, but only on assignee |
| `bd update ID --if-assignee A` / `--if-status S` | exit 0 if all supplied guards match | exit **13** when every failure is a stale guard; no write; mixed/general failures exit 1 | field-scoped compare-and-set; cannot combine either guard with `--claim` |

The race had exactly one winner: `dev/a` exited 0 and `dev/b` exited 1 naming `dev/a`; the final
row was `in_progress`, assignee `dev/a`. A stale `--if-assignee dev/a` and stale
`--if-status in_progress` each exited 13 without modifying the released row.

There is **no general revision CAS** in these mutation commands despite issue JSON exposing a
`revision` value. The guards compare only assignee and/or status. There is also no claim epoch
token returned to the worker: after expiry, reclaim, and a same-name re-claim, an old process and
the new process present the same assignee string. A heartbeat proves current name ownership, not
continuity of incarnation.

## Race table

| race | native result | Beadhive consequence |
|---|---|---|
| two workers claim an unassigned or pooled issue | one transaction wins; loser exits 1 naming holder | delegate the final arbitration to `--claim`; retain seat checks and worktree provisioning around it |
| worker heartbeats while local reaper runs | transaction ordering chooses heartbeat or stale reclaim; subsequent loser observes no longer-held state | acceptable only if every privileged boundary re-reads holder; heartbeat failure must stop the worker |
| reclaim then old worker submits | native lease does not guard `bh work submit` automatically | retain `_guard_holds_claim`; add a heartbeat/ownership check before expensive validation and again before opening review |
| reclaim, then same actor name reclaims | old and new processes are indistinguishable by assignee | native lease is insufficient as a fencing token; retain a Beadhive run/holder incarnation wherever concurrent old work can survive |
| dispatcher assigns while worker claims | assignment and claim are separate transactions; guarded update cannot combine with `--claim` | assignment may pre-stage an actor/pool, but claim remains the authority transition; do not synthesize reassignment from two unguarded writes |
| supervisor interrupts while claim changes hands | `unclaim --if-assignee` cannot clobber a different named holder | adopt this CAS for interruption/abandon recovery; same-name reincarnation still needs a holder token |
| pool member is reclaimed | issue returns unassigned, losing pool routing | Beadhive must restore the pool deliberately with a guarded update, or accept global readiness; silent native reclaim changes dispatch scope |
| review approval races resubmit | claim CAS says nothing about reviewed commit | retain SHA-marked review gates and stale-gate supersession |
| two batch members are claimed separately | each per-issue claim is atomic, the set is not | retain Beadhive's all-member preflight/rollback and shared batch branch/gate; no native multi-issue claim transaction is exposed |
| replica B reaps replica A | skipped by default; `--any-replica` can force it | never use broad `--any-replica`; recovery of a dead replica is an operator decision, narrowly by id |

## Replica behavior

A "node" here means a store replica, not necessarily a physical host. Clients of one Dolt SQL
server are one replica and must share one `node_id` (or leave it unset). Committing a `node_id` in
`.beads/config.yaml` arms the guard incorrectly because every clone then claims the same identity;
the supported placement is per-machine config or `BEADS_NODE_ID`.

Cross-replica synchronization carries status and assignee, but not live lease state. Therefore a
remote observer is stale by at least a sync interval. Native reclaim deliberately refuses a lease
granted elsewhere. `--any-replica --id ID` is suitable only after the granting replica is known
permanently dead; bare `--any-replica` can steal every foreign stale-looking claim, including live
peers. This is host-local crash recovery, not fleet coordination and not an epoch fence.

## Invariant mapping

| Beadhive invariant | native feature fit | verdict |
|---|---|---|
| exactly one claimant | transactional `--claim`, including configured pool aliases | **delegate arbitration** |
| seat type (`dev/`, `disp/`, reviewer, merger) | actor is caller-asserted text; pools are untyped strings | **retain Beadhive guard** |
| kickoff before epic execution | leases do not inspect `kickoff:approved` or kickoff gates | **retain** |
| current holder at submit/merge boundary | assignee/status CAS helps, but has no incarnation token and is not implicit in later verbs | **adopt checks, retain boundary guards** |
| exact reviewed branch/tree | leases and CAS do not bind a Git SHA; native generic gates do not classify Beadhive review authority | **retain SHA review gate contract** |
| batch is one ownership/review/merge unit | claims are per issue; pool config does not make an atomic claim set | **retain batch machinery** |
| molecule branch and parent topology | no native lease/CAS representation | **retain** |
| merge-slot holder | Beads merge-slot has a holder string but Beadhive adds process/host/time staleness and signal cleanup | **retain until separately proven** |
| host epoch / primary writer fence | claim leases are store-local and ephemeral | **strictly orthogonal; retain** |

## Lifecycle model

1. **Assign:** dispatcher may assign a concrete seat or an approved pool. Pool aliases are a
   scheduling convenience, not authorization.
2. **Claim:** Beadhive validates seat, kickoff, batch/molecule shape, and provisioning intent;
   native `--claim` performs the atomic state transition. Provisioning failure must guarded-unclaim
   only the actor it just claimed for.
3. **Work:** the claim-owning supervisor heartbeats below TTL. A failed heartbeat is a fence:
   interrupt the worker and stop accepting its output.
4. **Interrupt:** graceful cancellation followed by `unclaim --if-assignee`; never `--force` in
   the ordinary supervisor path.
5. **Crash:** the granting replica reclaims after a grace comfortably above TTL and sync cadence.
   Worktree/branch preservation remains Beadhive's durable recovery mechanism.
6. **Resume/reassign:** claim anew and attach the preserved branch. If the same logical actor name
   can overlap its predecessor, mint and verify a Beadhive holder/run token; native leases do not
   distinguish them.
7. **Review:** submission still binds a SHA to the classified review gate. Approval and merge
   authority remain seat-specific and independent of the claim lease.

## Recommendation for `bh-6p2j`

**Narrow GO.** Implement native heartbeat and local-replica reclaim, and replace ad-hoc
check-then-update release paths with the available assignee/status CAS. Let native `--claim` remain
the single-issue arbitration point. Optionally expose pools only for dispatcher-created,
seat-homogeneous developer queues.

`bh-6p2j` may delete or delegate:

- any duplicate single-issue claim race arbitration around `bd update --claim`;
- unconditional supervisor unclaim/reassignment writes, in favor of guarded native mutations;
- a future custom heartbeat or local stale-claim table—do not build one.

It must not delete:

- seat, kickoff, open-gate, review-SHA, batch-set, molecule-topology, and submit-holder guards;
- branch/worktree recovery, because native reclaim preserves no workspace;
- structured merge-slot holder recovery;
- host lease and epoch fencing, which solve a different cross-host writer problem.

Implementation should be split so the safe slice does not depend on pools: (1) heartbeat owner
with failure-triggered interruption; (2) replica-local, label-scoped reclaim with metrics and no
`--any-replica`; (3) CAS hardening of provisioning rollback, abandon, and reassignment; then
(4) pool routing only after defining how reclaim restores the pool and proving atomic batch claim.

The original deferred trigger on `bh-6p2j` should remain: this spike establishes compatibility,
not operational urgency. If adopted, document explicitly that lease loss revokes lifecycle
authority but cannot kill an old process by itself.
