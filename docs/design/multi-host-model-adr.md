# Multi-host model ADR — `host`, exclusive primary, backend portability

**Status:** proposed · **Date:** 2026-07-24 · **Supersedes:** nothing · **Amends:** nothing

Establishes vocabulary for the physical-machine axis, decides the concurrency model for a hive
across machines, and records the limitations and upstream dependencies that decision rests on.

---

## Context

`bh` models a **hive** as a repo's beads DB — a logical identity (stable prefix + identity
labels) whose history rides the repo's own git remote. That model has no term for *the machine
the hive is checked out on*. From the CLI's perspective it is always "the current host with the
current hives," and even Factory HQ is host-bound: `~/.beadhive/hq/` is a local path, so what
presents as a singleton is in fact **N replicas converging over git**.

Three pieces of evidence say this gap is already costing us.

**1. The concept exists in code, unnamed, three times.** `socket.gethostname()` is load-bearing
in three modules, always in the same shape — `{host, pid, ts|pid_start}` as a liveness token:

| location | role |
|---|---|
| `work_group.py:45` | merge-slot holder token, *"embedding host+pid+acquire-time so a later acquirer can tell a live holder from an orphan"* |
| `worktree.py:966` | verify-dir marker, explicitly *"the merge-slot HolderToken analog"* |
| `validation_ledger.py:87` | stamps `host` on cached validation verdicts |

The code has also already derived the correct trust posture and written it down:

> *"a live same-host pid (with matching start-time) is NEVER reaped; a **cross-host** or
> unreadable dir falls back to the grace/TTL windows only."* — `worktree._verify_dir_is_orphan`

That is exactly right: within a host, liveness is checkable (pid + pid-start); across hosts,
only time. Derived twice, independently, never named.

**2. The docs need a disambiguation block.** `CONTROL-PLANE.md:164` opens with *"This is a
third, distinct meaning of 'another host' — don't confuse it with either existing one"* and then
enumerates custodian hand-off vs. BEADS-SYNC developer bootstrap vs. fleet relocation. When a
doc needs a disambiguation section for a word, the word is overloaded.

**3. The current model is serial, enforced by ritual.** `CONTROL-PLANE.md`'s
"pack-up-before-host-switch" flow relocates *the whole fleet's* state to another machine so work
"resumes there exactly where it left off" (epic `bh-59q1` covers the preflight). One host at a
time, by procedure, with no mechanism.

---

## Decision 1 — vocabulary

**Keep the word `host`.** It is already the term in `BEADS-SYNC.md`, `CONTROL-PLANE.md`, three
code modules, and a filed epic. A rename to `site`/`node` would be churn, and this repo has
already paid for one rename (`docs/design/rig-to-hive-rename.md`).

> **host** — one `~/.beadhive` config home. Usually one machine, but precisely: the unit that
> owns a hive registry, a primary set, and a compute budget. Defining it as the config home
> rather than the machine makes containers, VMs, and two-workspaces-on-one-laptop fall out
> correctly.
>
> **host id** — a stable UUID in `~/.beadhive/host.yaml`, minted at `bh config init`. The
> hostname demotes to a human label.
>
> **primary** — the one host currently permitted to *write* a given hive.
>
> **follower** — any other host: syncs and reads, does not dispatch, plan, or merge.
>
> **adopt / release / packup** — become primary · yield one hive · yield all (mechanizing the
> existing "pack-up-before-host-switch" ritual).

### Names deliberately NOT used

| rejected | why |
|---|---|
| **`lease`** | **beads owns it.** `lease_expires_at`, `issueops.ManageLeaseOnUpdate`, `bd heartbeat`, `bd reclaim` — a TTL lease held by a *worker* on an *issue*. Upstream #4716 is a bug report about it misfiring. Reusing it for host↔hive would collide with a live concept. |
| `claim` | taken — `bh work claim`, bead-level |
| `hold` | taken — the `release-hold:` gate in `guard.py` |
| `slot` | taken — merge-slot |
| `owner` | taken — a `bd` issue field |
| `home` | taken — `BH_HOME` / `~/.beadhive` |
| `rig` | retired in the rig→hive rename; never resurrect |
| `fleet` | means the set of managed *repos*, not hosts |

Note the stable-host-id change fixes a live bug class: comparing `gethostname()` means a renamed
machine orphans its own merge-slot locks, and a *reused* hostname makes another machine's live
holder look like your own dead one — the reclaim path then steals a live merge slot.

---

## Decision 2 — exclusive primary is the default, and it is not a preference

`docs/BEAD-BACKENDS.md`'s comparison matrix states the bd backend's multi-writer story flatly:

> **bd/Dolt** — *"embedded = single writer; server mode for true concurrency"*

Concurrent multi-host writes to one hive are **outside the current backend's stated model**, not
merely expensive. Upstream confirms this is not theoretical — see the watch table below,
particularly **#4796**, which reproduces in exactly this configuration (bd 1.1.0, embedded Dolt,
two Macs, `bd dolt push`/`pull` over `refs/dolt/data`) and blocks sync indefinitely.

### Implementation

**The primary record is a git ref, not a bead.** `refs/bh/primary` on the hive's own remote,
containing `{host_id, label, adopted_at, expires_at}`.

This is the load-bearing insight. Eventual convergence of *Dolt data* provides no mutual
exclusion, but **the git remote is a linearization point**: ref updates are atomic, and
`git push --force-with-lease=<ref>:<expected-sha>` is a genuine compare-and-swap against a single
authority that every hive already has. The ref lives outside `refs/dolt/data`, so it never
participates in a Dolt merge.

```
adopt     git push --force-with-lease=refs/bh/primary:<expected>   → atomic CAS; loser is rejected
renew     same CAS with a new expires_at, piggybacked on any bh write verb (no daemon)
takeover  CAS from the expired value; refuses an unexpired primary unless --force (logged)
```

**Gate writes, never reads.** `bh work assign|claim|start|submit|merge` and — critically —
**`bh plan file`** refuse when this host is not primary. `ready`, `list`, `show`, `brief`, and
`sync` work from anywhere; looking is always safe.

Gating `plan file` is the non-obvious requirement and the most important one: creating children
under a shared parent is *literally* the #4796 trigger. Planning from a follower is the
known-broken path, not an edge case.

**Collision detection already half-exists.** `bd federation status --json` returns per-peer
`Reachable` / `LocalAhead` / `LocalBehind` / `HasConflicts`, and `bh` already consumes it via the
`Engine` seam (epic `bh-wty3`, landed 2026-07-24). Exclusive mode reframes the signal: under this
policy a `HasConflicts` is a **policy violation to escalate**, not a merge to resolve.

**Orphan recovery.** Primary dies → ref expires → another host CASes it. Uncommitted work on the
dead machine is unrecoverable by construction, so `bh work reclaim` means: abandon that
worktree's state, reset the bead, re-dispatch. This requires `claim_authority` to record the
**host id** — today it records only the seat, so orphans are not even detectable.

**Degrade honestly.** Remote unreachable ⇒ cannot adopt ⇒ read-only, or `--force` with a loud
logged override. Never silent optimism.

---

## Limitations

Recorded explicitly so none of these is rediscovered as a surprise.

1. **CAS requires reaching the remote.** Offline hosts cannot adopt. A partitioned host that
   already holds the primary keeps working until expiry, then must stop or force. There is no
   offline-safe acquisition and there cannot be one.

2. **Exclusion covers writes, not side effects.** The ref stops a follower from *writing beads*.
   It does not stop it from running tests, burning tokens, or pushing code branches. Token
   headroom is account-scoped and remains shared across hosts regardless of who is primary.

3. **`--force` is unavoidable and dangerous.** A dead primary with an unexpired ref would
   otherwise block the fleet until expiry. The escape hatch must exist, and using it is exactly
   how split-brain happens. Mitigation is loud logging and escalation, not prevention.

4. **TTL choice is a real trade-off with no good answer.** Short TTL ⇒ frequent renewal and a
   dead host frees up fast, but a slow network looks like death. Long TTL ⇒ stable, but a dead
   host blocks work for the whole window.

5. **`bd update --claim` is not a hard CAS** (upstream #4657) — despite the help text saying
   "Atomically claim." Even *single-host* concurrent claims can race. Host-level exclusion does
   not fix bead-level exclusion.

6. **HQ remains a replicated store presented as a singleton.** Nothing here changes that. Any
   cross-host fact read from HQ is a *reading* with an `as_of`, never a truth — the same
   contract the provider-headroom design uses.

7. **The `host:` dimension cannot be expressed in a molecule spec.** `plan.py:58`'s
   `_DIMENSION_FIELDS` has no `host` (nor `tag`) — see idea bead `bh-0a6g`.

8. **This buys no tokens.** Adding a host multiplies compute and contention and adds *zero*
   token capacity, because tokens are account-scoped. A fleet at 91% of its weekly token budget
   is token-bound, not compute-bound; a second machine mainly enables running work it cannot
   afford.

---

## Upstream watch — keep tabs

All against **github.com/gastownhall/beads**. Checked 2026-07-24 against bd 1.1.0 (Homebrew).

| issue | state | what it blocks | why we care |
|---|---|---|---|
| [#4796](https://github.com/gastownhall/beads/issues/4796) | **open** | any concurrent bead *creation* | Two machines each `bd create --parent <epic>` before syncing allocate the **same child id**; next `bd dolt pull` hits an add/add PK collision on `issues` **plus** a both-changed conflict on `child_counters`. Neither auto-resolves; sync blocks indefinitely, recovery is heavy manual work. Reproduces in our exact topology. **This is why `bh plan file` must be gated.** |
| [#4974](https://github.com/gastownhall/beads/issues/4974) | open | JSONL-interchange sync | `bd import` is an unconditional upsert with no `updated_at` gate — an older record silently overwrites a newer one. Requests `--on-conflict {newer-wins,skip,fail}`. |
| [#4657](https://github.com/gastownhall/beads/issues/4657) | open | bead-level exclusion | `bd update --claim` is not a hard CAS under sub-second concurrency (load-tested), contradicting its own help text. |
| [#3791](https://github.com/gastownhall/beads/issues/3791) | open | clean host-local state | Schema mixes globally-shared issue state with per-host operational state (`repo_dolt_remote_status`, `repo_mtimes`, `local_metadata`, `federation_peers`). Asks for local-only tables. Directly relevant: a host model wants per-host rows that never replicate. |
| [#4716](https://github.com/gastownhall/beads/issues/4716) | closed | — | Worker-lease semantics; the reason `lease` is not available as vocabulary here. |
| [#4698](https://github.com/gastownhall/beads/issues/4698) | closed | — | Last-write-wins auto-resolve for modify/modify `issues` conflicts. Landed; does not cover #4796's add/add or counter conflicts. |

**Revisit this ADR only if:** #4796 closes (unblocks finer-grained partitioning), **or** #4657
closes (makes bead-level exclusion viable), **or** we adopt a backend whose multi-writer story
differs from bd embedded's single-writer model.

---

## Backend portability

Multi-host behaviour is **backend-dependent**, and the `Engine` seam (`engine.py`, config key
`beads.engine`) already anticipates swapping. From `docs/BEAD-BACKENDS.md`:

| backend | multi-writer story | multi-host conflict profile | exclusive primary still needed? |
|---|---|---|---|
| **bd** (Dolt on `refs/dolt/data`) | *"embedded = single writer; server mode for true concurrency"* | #4796 add/add PK + `child_counters`; cell-level Dolt merge | **Yes** — required by the backend, not just by policy |
| **br** (in-branch JSONL) | *"single local writer; concurrency = git branches"* | state diverges per branch/worktree until merged; explicit `--force-db/--force-jsonl` policy | Yes, and worse — no shared live view at all |
| **bw** (orphan branch, intent replay) | *"file-per-issue → concurrent agents rarely conflict"* | rebase + deterministic intent replay, no merge drivers or lock files | **Partially** — structural conflicts largely designed away |
| **nodb** (JSONL only) | single writer | git line-merge | Yes |

**`bw` is the interesting one.** One JSON file per issue plus zero-byte marker files means *"two
agents working on the same repo never touch the same file,"* and sync is fetch → rebase → replay
intents from commit messages. That is an op-based model, and it dissolves the entire #4796 class:
there is no shared counter row to conflict on.

But its documented edge case is the exact boundary that matters:

> *"bw: rebase-replay is last-writer-wins per intent. Replay is deterministic, but two agents
> editing the same field of the same issue resolve by intent order, not by a merge policy you
> choose. Attachments/comments (separate files) are safe; **scalar field races are silent**."*

So even the most concurrency-friendly existing backend converges *structurally* while losing
data *semantically*, without saying so.

---

## Should we build a CRDT backend?

Worth evaluating; not yet worth building. The honest analysis:

### What CRDTs would genuinely fix

- **Id allocation (#4796).** A shared incrementing counter that must produce unique values is
  the single worst structure for concurrent writes. Replace `child_counters` with per-replica
  allocation (host-scoped id namespace, or a dense/lexical ordering) and the conflict is gone
  *by construction* — not merged, never created. This is the canonical CRDT win.
- **Labels, dependencies, comments, notes.** OR-Set semantics. Concurrent adds commute;
  add/remove needs tags but is thoroughly solved.
- **Event/history append.** Grow-only set; trivially conflict-free.

### What CRDTs fundamentally cannot fix

- **Mutual exclusion.** "Only one worker may hold this bead" is a **consensus** problem, not a
  convergence problem. A CRDT converges to *both claimed* or to an arbitrary winner; neither is
  exclusion. #4657 survives a CRDT rewrite untouched.
- **Status transitions.** `open → in_progress → closed → reopened` is a state machine with
  non-monotonic transitions; concurrent close+reopen has no principled join.
- **Scalar field edits.** An MV-Register can *preserve* both values, which converts bw's silent
  loss into a visible conflict — a real improvement — but something still has to choose. The
  decision moves to a human or a policy; it does not disappear.

### The conclusion that matters

**CRDTs would move the exclusive-primary boundary, not remove it.** Concretely: they would make
*planning and filing* safe from any host — the most painful restriction in Decision 2, since
`bh plan file` is precisely what must be gated today — while *dispatch and claim* would still
require the ref-CAS primary. That is a meaningful prize, and it is a smaller prize than "CRDTs
solve multi-host."

### The constraint on "conforms to the protocol"

`BEAD-BACKENDS.md` is explicit that *"the interchange is the lowest common denominator — anything
an engine stores beyond the JSONL schema does not survive a round-trip through another engine."*
CRDT metadata (vector clocks, causal context, OR-Set tags) is exactly that kind of state. A CRDT
backend conforming to the JSONL interchange would **lose its causal context on every export/import
round-trip**, degrading to last-write-wins at precisely the boundary where it was supposed to
help. Either the interchange grows a causal-metadata extension, or CRDT guarantees hold only
within a single-engine fleet.

### Recommendation

**Do not build one yet.** Rank the cheaper options first: (a) fix `bh plan file` gating and ship
exclusive primary — days, no new backend; (b) push #4796 upstream, where per-replica id
allocation is a contained fix to one counter and benefits every beads user; (c) evaluate `bw` as
an `Engine` adapter, since it already dissolves the structural conflict class without us writing
a storage engine. Revisit a CRDT engine only if all three fail *and* concurrent multi-host
planning is demonstrably the binding constraint on throughput — which, per Limitation 8, it is
not while the fleet is token-bound.

---

## Consequences

- `bh host` becomes a new CLI group (Fleet / HQ panel), requiring an amendment to
  `cli-mcp-naming-conventions-adr.md` §5a and §5c.
- `claim_authority` gains a `host_id` field in its `ClaimRecord`.
- `bh config init` mints a host id; `home_migration` is the precedent for the one-time step.
- Provider-headroom allowances gain a host dimension: account-absolute readings stay
  freshest-wins, but **per-host contribution records are additive** — the two must never be
  merged by the same rule.
- Cost-corpus records should carry `host_id`, and capability-cohort keys should include it: a
  slower machine changes wall-time without changing tokens, and conflating the two would
  corrupt drift detection.
- `docs/CONTROL-PLANE.md`'s pack-up flow gains a mechanism (`bh host packup`) behind its
  existing procedure.
