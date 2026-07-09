# br under AGF — the atomic-commit thesis, its limits, and invented state compat layers (design)

> Status: **assessment / design intent.** Reviews how beads_rust (`br`)'s "issue state commits
> atomically with code" thesis behaves under AGF, renders a verdict on whether its constraints
> are by design, defines the envelope for using br in limited capacity, and proposes invented
> compatibility layers over `bd` / `br` / `bw`. Extends
> [BEAD-BACKENDS](../BEAD-BACKENDS.md) §3–4 and feeds
> [bead-backend-abstraction](bead-backend-abstraction.md) (epic).

Sources: beads_rust repo docs (README, SYNC_SAFETY.md, VCS_INTEGRATION.md, AGENTS.md,
AGENT_INTEGRATION.md, SWARM_SCALE_TUNING.md, CLI_REFERENCE.md), its issues #212/#337/#338/#285/#286,
the author's Agent Flywheel methodology (agent-flywheel.com/complete-guide), and DoltHub's
"common beads workflows" post (2026-04-15); fetched 2026-07-07.

---

## 1. Where the atomic-commit thesis breaks under AGF

br's thesis: one commit = code change + its bead update → alignment, bisectability, reviewable
provenance. Its **hidden premise**: *the branch you commit on is the place everyone reads state
from.* True in br's intended topology (everything on `main`, one checkout); false at almost
every step of AGF's choreography ([BEADS-SYNC](../BEADS-SYNC.md)):

| AGF step | Actor | Under br, the write lands… | Problem |
|---|---|---|---|
| create | planner | on `main` directly (no code change exists) | planning beads have no branch to ride; PR-gated repos have **no sanctioned path** (br issue #212, closed unanswered) |
| assign | dispatcher | on `main` (developer's worktree doesn't exist yet) | dispatcher needs direct-main push; assignment can't be pushed as a ref the developer pulls |
| claim | developer | first commit on `wt/bead/<id>` | invisible to dispatcher/HQ until branch push; git has no cheap "latest state of file X across all refs" → stale ready-views, double-assignment risk |
| notes / concurrent edits | dispatcher + developer | same JSONL **line** on two branches | guaranteed same-line conflict at merge — the benign "keep both lines" rule only covers *distinct* issues; with N parallel beads every merge touches `issues.jsonl` and the refinery becomes a JSONL surgeon |
| review gate | reviewer | no clean home | committing to the developer's branch mixes authorship; committing to `main` splits state again |
| close | merger | **the `--no-ff` merge commit itself** | ✓ the one step where the thesis shines: "merge = done" is atomic and archaeologically perfect |
| revert / bounce | anyone | rides the revert | `git revert` silently un-closes beads; cherry-picks double-apply state; a bounced batch bubble oscillates bead state with it |

**Distilled:** beads have two natures — **coordination records** (status, assignee, gates:
fast-changing, must be globally current) and **provenance records** (description, acceptance,
close: slow, genuinely code-aligned). br's atomic commit is the right mechanism for provenance
and the wrong medium for coordination — and AGF fanout traffic is mostly coordination. Code and
status also want opposite merge semantics: code wants revertability, status history wants
monotonicity; coupling them in one commit means you can't have both.

## 2. Verdict: by design — but the rationale lives outside the repo

**The single-branch, trunk-based constraint is deliberate; the beads_rust repo never states
it.** Evidence:

- The repo's own docs systematically avoid branch semantics (README, SYNC_SAFETY.md,
  VCS_INTEGRATION.md, AGENT_INTEGRATION.md — checked term-by-term: no "worktree", no
  sync-branch, no branch-divergence discussion, no rebase interaction with
  `beads.base.jsonl`, no commit-cadence rule beyond session-end
  `br sync --flush-only && git add .beads/ && git commit`). Every doc answers "what happens
  between SQLite and JSONL in one working directory" and defers all git behavior to the user.
- The freeze rationale is dependency stability — *"Rather than ask Steve to maintain a legacy
  mode for my niche use case, I created this Rust port that freezes the 'classic beads'
  architecture I depend on"* — not a rebuttal of the branch-divergence problem that pushed
  upstream to Dolt.
- The acknowledgment lives in the author's **Agent Flywheel** methodology: *"I really think
  worktrees are a bad pattern and not worth the trouble"*; *"All agents commit directly to
  `main`… branch-per-agent creates merge hell with 10+ agents."* His multi-agent answer is one
  **shared checkout** with Agent Mail file reservations, a pre-commit guard, and br's
  `.beads/.write.lock` serialization (SWARM_SCALE_TUNING.md). In that topology the
  branch-divergence failure mode structurally cannot occur — which is why the repo never
  documents it.
- **Issue #212** is the closest thing to an acknowledged gap: a user combined worktrees with a
  `.beads/redirect` file, then asked how JSONL is supposed to be updated in PR-gated repos —
  closed with no documented answer. All other concurrency issues (#337, #338, #285, #286) are
  dual-store (SQLite↔JSONL) or shared-checkout races, never two-branches-diverging.

So br doesn't "break at edge cases" under AGF — it targets the **opposite topology**. AGF and
Flywheel answer the same question (how do many agents avoid trampling each other) with mutually
exclusive mechanisms: branch isolation + serialized merges vs. shared checkout + advisory
locks. Both are self-consistent; they don't compose.

Two artifacts from the dig matter for §4: br honors `.beads/redirect` (worktrees *can* share
one DB — user-discovered, undocumented) plus `BEADS_DIR` / `BEADS_JSONL` overrides; and Yegge's
endorsement of br frames beads as *"an interface/protocol, not a single implementation"* —
direct support for the interchange-contract direction.

## 3. The limited-capacity envelope for br rigs

Three defensible tiers, in increasing intimacy:

- **Tier A — interchange-only (safe today).** HQ hydrates from `main`'s tracked JSONL
  ([bead-backend-abstraction](bead-backend-abstraction.md) phase 1); no bh lifecycle verbs
  touch the rig. Visibility is at most one merge stale.
- **Tier B — batch-collapsed, single writer.** Collapsed dispatch mode only: beads driven
  sequentially on one `wt/batch/<group>` branch, so exactly one live branch ever modifies
  `.beads/`; the merger resolves the single JSONL merge with a pinned `br sync --merge`
  policy; closes ride the merge bubble ("merge = done"). The guardrail bead
   should **hard-error**, not warn, when more than one open branch
  touches `.beads/`.
- **Tier C — br's native swarm, locally.** All seats share one checkout's `.beads/` via
  `redirect` + the write lock, keeping `wt/` isolation for code only. This abandons in-branch
  state per worktree and is the gateway to compat layer (i) below.

Hard limits at every tier: no remote-factory dispatch (an assignment cannot cross hosts
without pushing `main`); PR-gated repos unsupported (per #212); no clean home for
reviewer/gate writes; revert/cherry-pick hygiene stays manual. **Fanout mode is out, full
stop.**

## 4. Invented compatibility layers

Ranked by leverage. (i) is the unifying abstraction; (ii)–(v) compose with it.

### (i) The State-Ref Contract + hidden state worktree — "give br the Dolt ref it never had"

Generalize what all backends secretly share into a per-rig contract:

```text
(state_ref, layout, merge_policy, local_materialization)
bd  = (refs/dolt/data,      dolt chunks,        cell-merge,     embedded DB)
bw  = (refs/heads/beadwork, file-per-issue,     intent replay,  git object DB)
br  = (refs/beads/state,    .beads tree (JSONL), 3-way / op-log, hidden state worktree)  ← invented
nodb= (refs/beads/state,    issues.jsonl,        union / fold,   hidden state worktree)  ← invented
```

For br/nodb, bh provisions a **hidden linked worktree** checked out to a dedicated state ref
(`refs/beads/state`, or an orphan branch for forge visibility à la bw), and points every code
worktree's `.beads/redirect` at it. br runs **completely unmodified** — it already honors
redirect, and `.beads/.write.lock` was built for concurrent writers in one `.beads/`. Every
engine then gets: issues-without-code sync, one shared local truth across worktrees, PR-gated
repo compatibility, and a scopeable credential. This is upstream bd's removed
sync-branch / `.git/beads-worktrees/` machinery rebuilt at the bh layer, where it belongs.
`Engine.state_channel` in [bead-backend-abstraction](bead-backend-abstraction.md) becomes the
contract, not a description. Cost: the atomic-commit thesis is fully abandoned — restore the
association benefit via (iii).

### (ii) The op-log ledger — snapshot → events (the root fix)

In-branch beads hurt not because of *where* they're stored but because `issues.jsonl` is a
**state snapshot**, so concurrent edits collide on the same line. Store per-bead append-only
op shards instead — `.beads/ops/<bead-id>.jsonl` with a `merge=union` gitattribute — and state
becomes a deterministic fold: field-level last-write-wins by (timestamp, actor), first-wins
for claims, tombstones for deletes, periodic fold-and-compact. Consequences:

- Same-line conflicts become structurally impossible; merges are automatic.
- A revert removes ops and the fold degrades gracefully instead of silently un-closing beads.
- The atomic-commit benefit is **restored safely**: the op rides the code commit *and* merges.

The ecosystem already contains both halves — bd's `events.jsonl` export and bw's
commit-message intent log; the invention is promoting the audit log to the storage primitive.
Candidate spike; potentially upstreamable to br as a mode.

### (iii) Commit-trailer association — logical atomicity for every backend

`Bead: <id> <transition>` trailers on code commits (the `Fixes #123` / Gerrit `Change-Id`
pattern); the merger or a hook replays trailers into whichever engine is authoritative at
merge time. Keeps the archaeology wins of br's thesis — bisect, PR-visible closure, provenance
in `git log` — with **bd today**, at trivial cost. Cheapest item; do first.

### (iv) Split-plane storage — the end-state north star

Split the bead by nature: the **definition plane** (description, acceptance, design) as
file-per-bead in-branch — reviewers see acceptance criteria evolve atomically with the code in
the PR — and the **coordination plane** (status, assignee, gates) on the state ref. Overlay at
read time. Gerrit's NoteDb move applied to beads; the only option that keeps br's genuine
benefit *and* AGF's disjoint-namespace invariant simultaneously.

### (v) Mirror, don't migrate — consumer-side compat

Keep bd authoritative; bh emits derived mirrors — br-compatible JSONL committed to `main` at
merge, optionally a bw-format branch — so Flywheel/bw ecosystem tooling can read the rig
without bh changing backends. Zero risk.

## 5. Recommendation

1. Make **(i)** the unifying abstraction in
   [bead-backend-abstraction](bead-backend-abstraction.md): it redefines the Engine seam bead
    and turns the br adapter into "hidden state
   worktree + redirect".
2. Ship **(iii)** as an immediate standalone bead (works with bd now).
3. File **(ii)** as a spike bead.
4. Hold **(iv)** as the long-term direction; note it in the design doc's open questions.
5. Add **(v)** as a small consumer-compat bead.
6. Tighten: hard-error (not warn) on >1 open branch modifying `.beads/`
   for tracked-state engines, per the Tier B envelope.

## 6. Discussion — collapsed AGF vs Agent Flywheel, repo shapes, and the merge problem underneath

### 6.1 Collapsed mode is AGF's closest point to Flywheel

The two methodologies read as opposites in §2 (branch isolation vs shared checkout), but AGF's
**batch-collapsed dispatch** converges on most of what makes Flywheel work:

- **Single-threaded state.** A collapsed dispatcher drives beads *sequentially* on one
  `wt/batch/<group>` branch — one writer, one line of development, no concurrent tracker
  mutation. Flywheel reaches the same regime by different means: parallel agents, but writes
  serialized through `.beads/.write.lock` and Agent Mail file reservations. Collapsed AGF
  partitions **temporally** (one bead at a time); Flywheel partitions **spatially** (one file
  owner at a time). Both avoid merge hell by not merging concurrent tracker state at all.
- **Coordination by messaging over a shared ledger, not by merge.** Flywheel's coordination
  medium is explicit messaging (Agent Mail reservations, advisory hints with TTLs) beside the
  tracker. AGF's coordination medium *is* the tracker — assign/claim/gate transitions are the
  messages, and in collapsed mode they never even leave the dispatcher's session. In both
  systems, agents learn what not to touch from a ledger, not from discovering a conflict.
- **Where they still differ:** collapsed AGF keeps the integration gate — the batch lands as
  one reviewable `--no-ff` bubble that can bounce as a unit — while Flywheel commits straight
  to `main` and reviews post-hoc. Collapsed mode is, in effect, *Flywheel with a merge gate
  and temporal instead of spatial partitioning*.

### 6.2 Repo shape decides which regime holds

Flywheel's topology aligns with a specific repo class: the **single-artifact, trunk-developed
service repo** — continuous deploy, no PR gate on `main`, and rarely (never) maintenance
branches. Every one of its mechanisms assumes that shape: state on `main` is only "current"
if `main` is the only line of development; file reservations only stay cheap while the
working set is small and file boundaries approximate task boundaries; and with no long-lived
branches there is no backport story to break. Notably, AGF theory already concedes half of
this: the integration plane is itself trunk-shaped — *"each bead gets a worktree off the
integration tip, and lands on an always-green line… Merging is not releasing"*
([AGF](../AGF.md)) — maintenance/release branching is pushed out of the integration loop into
the separate release act. The difference is that AGF gets its trunk discipline *at the merge
boundary* while allowing parallel in-flight branches; Flywheel gets it by forbidding the
branches.

The same shape-dependence predicts the breakdown: **monorepos at scale.** File-hint
coordination degrades superlinearly there because monorepos concentrate *hot files* — shared
configs, lockfiles, generated registries, central routing tables, `BUILD`/`go.mod`/barrel
`__init__` files — that many unrelated tasks must touch. Reservation contention on those
files becomes the steady state; advisory TTL hints harden into a de-facto lock manager with
the classic pathologies (starvation, deadlock across multiple hints, dead-agent TTL churn).
The only relief is **constant refactoring to split files** so ownership boundaries re-align
with task boundaries — Conway's law enforced file-by-file, and real ongoing work that fights
several languages' idioms (single large module files, package-level re-exports). The tracker
itself is the reductio: a monorepo-wide `.beads/issues.jsonl` is the *hottest file in the
repo* — every task in every subtree contends on it — which is exactly the observation behind
the op-log sharding in §4(ii). AGF's branch isolation carries monorepos further for *code*
(worktrees partition by task, not by file), but it pays at the merge boundary instead — which
is where the next subsection lives.

### 6.3 Line-based 3-way merge, language by language — and the semantic-merge lesson

Both regimes are, at bottom, strategies for coping with the same weak joint: **3-way textual
merge is language-blind — it merges texts, not programs.** Its failure severity varies by
language and format:

- **Structure-positional languages** suffer most. Python can take a textually clean merge to
  wrong *nesting* (indentation is semantics). JSON object literals make two independent
  additions collide on the same brace/comma lines — or merge into invalid syntax. Import
  blocks conflict (or silently duplicate) because both sides append at the same anchor.
  Lockfiles and generated files are semantically mergeable but textually catastrophic.
- **The adjacent-line trap is the dangerous case**: 3-way merge *cleanly* auto-merges
  semantically conflicting changes — both branches add a case to the same switch, one renames
  a variable the other adds a use of, two migrations take the same sequence number. No
  conflict marker, broken program. The visible conflict is the safe failure; the clean wrong
  merge is the expensive one.
- **JSONL sits in a sweet-but-narrow spot**: line-per-record makes conflicts legible, but any
  two edits to the *same record* are same-line conflicts, and "keep both lines" resolution
  manufactures duplicate IDs that only survive because bd treats same-ID import as an update —
  a semantic-merge hack smuggled in through the importer.

The industry has attacked this for decades with **semantic merge**: AST/structure-aware tools
(Codice's SemanticMerge, GumTree in research, tree-sitter-era difftastic/diffsitter for diff,
Mergiraf for structural merge), git's own knobs (union/ours merge drivers, `xfuncname` diff
hunks, rerere), and the patch-theory lineage (Darcs/Pijul commutation). None became the
default, for consistent reasons: per-language grammar maintenance, formatting round-trip
fidelity (a merge must reprint code without reflowing it), and the hard ceiling that even a
perfect AST merge can't see *behavioral* conflicts — rename-plus-new-call-site merges cleanly
at the tree level and breaks at runtime. In practice the test suite became the real semantic
gate: CI is the merge oracle.

The lesson that survived forty years of attempts: **don't make merge smarter — make the data
merge-trivial.** Every bead backend that matured has already fled textual merge in exactly
this direction: bd went to Dolt, whose cell-level merge is a *successful* semantic merge
precisely because tabular data has a real schema (rows and columns are the AST); bw went to
operation replay (don't merge states — replay intents, the CRDT move); and §4(ii)'s op-log
ledger is the same move for in-branch storage (append-only ops + union merge = conflicts
structurally impossible). For *code*, where the data can't be made merge-trivial, the two
methodologies pick the two remaining strategies: AGF **serializes** the merge (one refinery,
one `--no-ff` bubble at a time, always-green + tests as the oracle); Flywheel **avoids** it
(no branches to merge). Issue *state* is the lucky case — it is structured enough that the
semantic-merge dream actually works — which is the deepest argument for keeping bead state
out of language-shaped text files on code branches.

### 6.4 CRDTs — the step past Dolt: merge that cannot conflict

bd's move to Dolt was a commitment to *schema-aware* merge: cell-level resolution reduces
conflicts, but Dolt can still surface one for a decider. A CRDT (conflict-free replicated
data type) is the stronger commitment on the same axis: **merge always succeeds by
construction**, because every field's resolution policy is decided *in advance* by its data
type rather than at merge time by an agent. For a coordination-plane store written
concurrently by agents that cannot stop to resolve conflicts interactively, that trade is
usually right — a deterministic outcome plus a bounce beats an interactive merge. The
question is whether bead state structures cleanly into CRDT primitives. It does, almost
suspiciously well:

| Bead field | CRDT shape | Policy it encodes |
|---|---|---|
| status | monotonic state-machine lattice (terminal states win) or MV-register | concurrent transitions converge; a genuine race (two closes with different resolutions) surfaces as a multi-value for policy |
| assignee / claim | LWW register with **deterministic first-writer-wins** tiebreak (lamport clock, then actor id) | both replicas converge on the same winner; the loser is *deterministically bounced* — acceptable because AGF already has re-dispatch |
| labels, deps, parent links | OR-Set (add-wins observed-remove set) | concurrent add+remove of the same label resolves add-wins; a textbook fit |
| comments, events, audit | grow-only append log | trivially conflict-free |
| description / design / notes | LWW register (or a text CRDT if collaborative editing ever matters — likely overkill) | last edit wins, history retained in the event log |

One thing CRDTs **cannot** give: mutual exclusion. A claim wants a lock; convergence is not
consensus. The honest framing is that a CRDT turns a double-claim from a merge conflict into
a *deterministic loss* one side discovers at sync — AGF's serialized merger and re-dispatch
loop remain the coordination layer for the operations that genuinely need exclusivity.

Concrete options, in ascending weight:

1. **Formalize §4(ii)'s op-log ledger as an op-based CRDT** (recommended first step). The
   ingredients are already there: union merge gives at-least-once delivery, op-ids give
   idempotence — what's missing is pinning the fold rules to be commutative (the table
   above). Do that and the ledger has *provable* convergence with zero new dependencies,
   stays textual/greppable, and git remains the only transport. The op-log spike bead should
   absorb this as its spec.
2. **cr-sqlite — the true CRDT analogue of the Dolt move.** bd chose versioned-SQL-with-merge
   (Dolt); cr-sqlite is replicated-SQL-without-merge — a SQLite extension that makes tables
   CRDTs (per-column LWW, causal-length sets), syncing by exchanging changesets over any
   transport, including blobs on the state ref. Directly relevant because **br is already
   SQLite**: a `br`-compatible variant on cr-sqlite would keep the classic local-DB
   architecture while making replicas mergeable by construction — the backend experiment
   worth a bead if a br-family engine is ever promoted past Tier B.
3. **Document CRDTs — Automerge (or Loro/Yrs), one doc per bead** on the state ref. The
   richest option (full JSON CRDT with sync protocol and history), and the only one that
   handles collaborative *text* editing of descriptions. Costs: binary at-rest format
   sacrifices greppability/reviewability (mitigate with a materialized JSONL projection, the
   §4(v) mirror pattern), and CRDT metadata (tombstones, version vectors) needs the library's
   compaction story. Reach for this only if concurrent free-text editing becomes real.

**Framework landscape, evaluated for this situation.** The constraints that matter here are
not the generic CRDT-shootout ones. In order: (1) the **transport is git refs** — the
serverless bet means the sync unit must be a self-contained blob/changeset that can ride the
state ref, not a live socket protocol; (2) **audit history is mandatory** — a factory needs
"who set this and when" forever, so libraries that garbage-collect tombstones by default are
disqualified from being the system of record; (3) **bindings**: `bh` is Python, br and the
Rust ecosystem tools matter, so Rust-core-with-Python-bindings is the sweet spot;
(4) **structured records, not rich text** — beads are field/set/log shaped, so document and
table CRDTs beat sequence CRDTs; (5) **custom merge policy** — the status state-machine and
first-writer-wins claims need more than blanket LWW, so the conflict-surfacing API matters;
(6) **format durability** — this data outlives any one tool version.

| Framework | What it is | Pros here | Cons here |
|---|---|---|---|
| **Automerge** (+automerge-repo) | JSON document CRDT; Rust core, JS/Python/Swift bindings; columnar binary format | doc-per-bead maps naturally; **full history retained** (audit for free); multi-value conflict API lets us implement the status lattice and FWW claims on top of its per-key resolution; updates are self-contained blobs → git-ref transport works; format spec is stable and versioned | binary at rest (greppability lost — needs the JSONL mirror); metadata growth needs its compaction story managed; Python bindings are second-tier citizens vs JS; doc-per-bead means thousands of small docs to index ourselves |
| **Yjs / Yrs** (y-crdt) | Sequence-optimized CRDT; the most-deployed ecosystem (collaborative editors); Rust core, `ypy` Python bindings | raw performance; tiny incremental updates; battle-tested at editor scale | built for live editing sessions, not records: **tombstone GC discards history by default** (fails the audit constraint as system of record); `ypy` maintenance has lagged; sequence orientation is the wrong shape for field/set data — you'd fight it |
| **Loro** | Modern Rust CRDT: JSON + rich text + **movable tree**; shallow snapshots; time-travel history | best-in-class compaction (shallow snapshots answer the metadata-growth problem); history/time-travel native; the movable-tree type could model the dep/parent DAG directly — no other library offers that; Python bindings exist | youngest of the set — smaller community, format stability risk for data meant to outlive tools; less deployment evidence for durable-storage (vs live-collab) use |
| **cr-sqlite** (vlcn.io) | SQLite extension making tables CRDTs (per-column LWW, causal-length sets); sync = changesets | **br is already SQLite** — closest to a drop-in for a br-family engine; keeps a SQL query surface; changesets are transport-agnostic blobs → state-ref friendly; per-column LWW matches our field-policy table almost 1:1 | effectively single-vendor with fluctuating activity — a maintenance bet; resolution is LWW-flavored, so the status lattice and FWW-claim policies need app-level enforcement anyway; adds a third database technology to a stack that already has Dolt and SQLite stories |
| **Hand-rolled op-log CRDT** (§4(ii) formalized) | Append-only per-bead op shards, `merge=union`, deterministic commutative fold | zero dependencies; **textual and greppable** — the only option where `git log -p .beads/ops/` is the audit UI; policies are exactly ours (status lattice, FWW, OR-Set semantics coded in the fold); JSONL interchange is a trivial projection; transport is plain git | we own correctness: commutativity, clock discipline, tombstones, and compaction are ours to prove (property-based tests are non-negotiable); no ecosystem tooling; easy to get subtly wrong in the ways CRDT papers exist to warn about |
| *(non-starters)* ElectricSQL, PowerSync, Ditto, Replicache; json-joy | sync-service platforms; TS-only JSON CRDT | — | server-dependent or license-encumbered (fails the serverless bet) or wrong runtime; listed to record they were considered |

**Verdict for this situation:** the hand-rolled op-log wins on every constraint except "who
proves correctness" — which is why it should be *formalized against* the literature (op-based
CRDT with causal ordering from lamport clocks) rather than improvised, and property-tested for
commutativity. If the spike outgrows hand-rolled, **Automerge** is the graduation path for
doc-shaped state (history + conflict API + stable format) and **Loro** the one to re-evaluate
at that time (shallow snapshots and the movable tree are genuinely attractive; maturity is the
only reservation). **cr-sqlite** is worth a bead only in the specific future where a br-family
engine is promoted past Tier B and wants multi-writer replicas. **Yjs** is the right answer to
a different question (live co-editing) and the wrong one here.

Two caveats to record. First, deterministic is not the same as *unsurprising*: LWW quietly
discards a write that a human might have preferred — which is why the append-only event log
underneath (already the audit layer in every option above) is not optional; it is what makes
silent resolution recoverable. Second, CRDT benefits are specific to **multi-writer,
asynchronous** regimes: remote factory hosts syncing on their own schedule, fanout at scale,
cross-rig federation. A single-writer collapsed rig gets nothing from a CRDT it doesn't
already get from a lock — matching §6.1's observation that the two coordination regimes only
diverge when writers go parallel.

Placed on the spectrum this section of the doc has been walking: textual 3-way merge (br) →
schema-aware merge with conflicts (bd/Dolt) → operation replay (bw) → CRDT (merge cannot
conflict). Each step trades merge-time human judgment for pre-committed policy — and the
further right the factory operates (more writers, more async, less supervision), the further
right the storage should sit.

See also: [BEAD-BACKENDS](../BEAD-BACKENDS.md) ·
[bead-backend-abstraction](bead-backend-abstraction.md) · [BEADS-SYNC](../BEADS-SYNC.md) ·
[AGF](../AGF.md) (integration-vs-release tenet).
