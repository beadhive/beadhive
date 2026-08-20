# Spike `bh-bomrd.1` — does a wisp fit release-loop tracking (attest → bump → release-preview → release)?

**Bead:** `bh-bomrd.1` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-bomrd.3` — *"does either mechanical loop change the
operational-workflow-substrate verdict?"*

> Sibling findings taken as settled and **not** re-derived: a formula never executes anything
> (`bh-gj0v9.1`, and Context of
> [`operational-workflow-substrate-adr.md`](../design/operational-workflow-substrate-adr.md));
> the `condition` grammar is single-term; no `rollback` / `retry` / `on_failure` in any of the 18
> schema structs; `LoopSpec.range` variable substitution is documented but broken;
> `on_complete.for_each` needs a step `output` ordinary beads never populate.

Artifact: [`bh-bomrd.1-mol-release-run.formula.json`](bh-bomrd.1-mol-release-run.formula.json)
— the real formula authored and wisped for this spike. Spike artifact only: it lives in
`docs/spikes/`, is on no `bd formula` search path, and nothing in `src/` references it.

## Question

The closed ADR declines `formula` / `wisp` as an operational-workflow substrate and lists the
release cut in its Decision-4 table as **"Neither"**. Beads' own narrative docs
(<https://beads.gascity.com/workflows/wisps>) say the opposite for exactly this shape — *"Use
case … release runs, operational loops, health checks"* — and the four `bh-gj0v9` spikes worked
from `bd formula schema` + Go source, not from that page. So the operator's narrower question:

**Does materialising this hive's real release loop — `just attest` → `just bump` →
`just release-preview` → `just release`, four linear steps with one human gate before the
one-way door — as a wisp molecule add anything over the already-shipped
`bh release preflight/attest/await/preview/recover` orchestration? GO or NO-GO.**

The load-bearing sub-question, and the actual differentiator from `bh-gj0v9.6`: that spike
reproduced a live bug where `bd mol wisp gc --closed --force` silently strips a dependency edge
off a **persistent** bead a wisp was blocking. A release run is *self-contained* — nothing
persistent depends on it. **Is that bug even reachable in this shape?** The acceptance criteria
require this be reproduced live, not argued.

Critically **NOT** asking: whether a formula could *run* a release (settled: no); whether an
agent guide should own the release (out of scope per the epic); whether `bh-87ktb`'s
zero-commit refusal should soften (settled NO by `bh-gj0v9.6`); nor re-litigating the ADR as a
whole — this spike may only move the release row of its Decision-4 table.

## Method

1. Read the shipped alternative in full before judging: `src/beadhive/release.py` (934 lines —
   `preflight` `:235`, `attest` `:281`, `await` `:400`, `pending` `:501`, `recovery_decision`
   `:538`, `recover` `:629`, `preview` `:778`, the marker helpers `:175-233`), the six release
   recipes in `justfile:511-652`, and
   [`attested-green-adr.md`](../design/attested-green-adr.md)'s *"Release orchestration"*
   section (`:223-261`).
2. Fetched and read the two narrative pages the original spikes did not:
   `/workflows/wisps` and `/workflows/molecules`. Re-read `bd mol wisp --help` and
   `bd mol wisp gc --help` (bd 1.1.0 (dev)) for what the CLI actually promises.
3. **Stood up a scratch hive** — `git init` + `bd init --prefix rl` under the session
   scratchpad, deliberately not this repo's hive — the same method `bh-gj0v9.1` / `.6` used.
4. Authored a real release-shaped formula (`mol-release-run`, `phase: "vapor"`, `pour: true`,
   4 linear steps + a `gate: {type: human}` on the last) and instantiated it with
   `bd mol wisp mol-release-run --var version=0.12.0`.
5. **Walked a full release run end to end** through `bd ready --mol` / `bd update --claim` /
   `bd close` / `bd gate resolve`, checking visibility and the "where did it stop" surfaces at
   each step, then ran the acceptance test: `bd mol wisp gc --closed --force` with **nothing
   persistent depending on the wisp**, against a control persistent bead (`rl-8mn`).
6. **Then walked a second run and stopped it mid-flight** (attest + bump closed, 2/5) alongside
   one unrelated closed patrol wisp, and ran the same routine `gc` the wisps page recommends —
   the case step 5 does not cover. Then exercised the documented escape hatch,
   `bd mol squash`, on that in-flight molecule.

All command output below is verbatim from that scratch hive.

## Evidence

### A. What genuinely works — and corrects the closed spikes

**E1. `bd ready --mol <wisp-id>` DOES surface wisp steps.** `bh-gj0v9.1` evidence 9 and
`bh-gj0v9.6` E1 both measured *bare* `bd ready` and concluded wisps are invisible to the ready
surface. Scoped, they are not:

```console
$ bd ready                       # unchanged from the closed spikes
✨ No open issues

$ bd list
No issues found.

$ bd ready --mol rl-wisp-5q0
   ID: rl-wisp-5q0
   Total: 6 steps, 3 ready
📋 Ready steps:
1. [● P2] [molecule] rl-wisp-5q0: mol-release-run [group-1]
2. [● P2] [task] rl-wisp-sqh: just attest — prove this tree green [group-1]
3. [● P2] [gate] rl-wisp-v3l: Gate: human [group-1]
```

**E2. Sequencing and the human gate work end to end.** `bd mol wisp` produced 6 issues (root +
4 steps + a materialised `type: gate` bead). Closing `attest` promoted `bump` to ready; the gate
really blocks:

```console
$ bd show rl-wisp-1cs           # the `release` step
DEPENDS ON
  → ○ rl-wisp-v3l: Gate: human ● P2
  → ○ rl-wisp-1en: just release-preview — is the path clear? ● P2

$ bd gate resolve rl-wisp-v3l --reason "releaser/dev1 signed off on v0.12.0"
✓ Gate resolved: rl-wisp-v3l
$ bd ready --mol rl-wisp-5q0
   Total: 6 steps, 2 ready
2. [● P2] [task] rl-wisp-1cs: just release — atomic push of main+tag, ONE-WAY DOOR
```

**E3. `bd mol current` is a genuinely readable "where did the release stop" report.** Stopping
the run after the bump:

```console
$ bd mol current rl-wisp-5q0
  [done]    rl-wisp-sqh: just attest — prove this tree green
  [ready]   rl-wisp-v3l: Gate: human
  [done]    rl-wisp-xfl: just bump — version+changelog+local tag v0.12.0
  [ready]   rl-wisp-1en: just release-preview — is the path clear?
  [pending] rl-wisp-1cs: just release — atomic push of main+tag, ONE-WAY DOOR
Progress: 2/5 steps complete
Next ready: rl-wisp-1en - just release-preview — is the path clear?
```

**E4. bd's own schema nominates this exact use case.** `bd formula schema Formula`:
`phase` — *"Patrol and **release** workflows should typically use 'vapor'"*; `pour` —
*"Reserve pour=true for critical, infrequent work (**e.g. releases**) where step-level tracking
is worth the DB overhead."* The wisps page agrees. This spike's premise is not a stretch of the
docs; it is the case the docs advertise.

### B. THE ACCEPTANCE TEST — `bh-gj0v9.6`'s bug is **NOT** reachable in this shape

**E5.** Full run closed out, `rl-8mn` a persistent bead with **no** edge to any wisp:

```console
$ bd mol wisp list --all
Wisps (6):
rl-wisp-1cs  closed  P2  task      just release — atomic push of main+tag,...
rl-wisp-5q0  closed  P2  molecule  mol-release-run
rl-wisp-v3l  closed  P2  gate      Gate: human
rl-wisp-1en  closed  P2  task      just release-preview — is the path clear?
rl-wisp-xfl  closed  P2  task      just bump — version+changelog+local tag...
rl-wisp-sqh  closed  P2  task      just attest — prove this tree green

$ bd mol wisp gc --closed --force
Found 6 closed wisp(s)
✓ Deleted 6 issue(s)
  Removed 0 dependency link(s)
  Removed 0 label(s)
  Removed 0 event(s)
  Updated text references in 0 issue(s)

$ bd show rl-8mn
○ rl-8mn · Release 0.12.0 notes (persistent, UNRELATED to the wisp)   [● P2 · OPEN]
$ bd dep tree rl-8mn
rl-8mn: Release 0.12.0 notes (persistent, UNRELATED to the wisp) [P2] (open) [READY]
```

`Removed 0 dependency link(s)`, the control bead's graph intact. **The spike's central
hypothesis is confirmed: `bh-gj0v9.6`'s edge-stripping bug does not fire when nothing persistent
depends on the wisp.** That objection does not carry over to the release loop.

### C. What kills it instead — a strictly worse bug, reachable with zero persistent beads

**E6. LIVE BUG — routine `gc --closed` destroys the progress of an *in-flight* release run.**
`bd mol wisp gc` is **hive-wide and step-granular**; it has no `--mol` scope flag. Second run
mid-flight (attest + bump closed, `release-preview` next), plus one unrelated closed patrol
wisp — the everyday reason an operator types the command the wisps page recommends
(*"Garbage collect regularly — `bd mol wisp gc` or `bd purge --force`"*):

```console
$ bd mol current rl-wisp-amf                       # BEFORE
  [ready]   rl-wisp-dxe: Gate: human
  [done]    rl-wisp-e42: just attest — prove this tree green
  [done]    rl-wisp-bku: just bump — version+changelog+local tag v0.12.1
  [ready]   rl-wisp-o24: just release-preview — is the path clear?
  [pending] rl-wisp-a9l: just release — atomic push of main+tag, ONE-WAY DOOR
Progress: 2/5 steps complete

$ bd mol wisp gc --closed --force
Found 3 closed wisp(s)
✓ Deleted 3 issue(s)

$ bd mol current rl-wisp-amf                       # AFTER
  [ready]   rl-wisp-o24: just release-preview — is the path clear?
  [ready]   rl-wisp-dxe: Gate: human
  [pending] rl-wisp-a9l: just release — atomic push of main+tag, ONE-WAY DOOR
Progress: 0/3 steps complete

$ bd show rl-wisp-o24
○ rl-wisp-o24 · just release-preview — is the path clear?   [● P2 · OPEN]
PARENT
  ↑ ○ rl-wisp-amf: mol-release-run ● P2
BLOCKS
  ← ○ rl-wisp-a9l: just release — atomic push of main+tag, ONE-WAY DOOR ● P2
```

It found 3 closed wisps — the patrol **and both completed steps of the running release** — and
deleted all 3. A live release went from *"2/5, attest done, bump done"* to **`Progress: 0/3`**
with no completed steps, and `release-preview` lost its `needs: bump` edge entirely (no
`DEPENDS ON`, now `READY`). The erased fact is the load-bearing one:
[`attested-green-adr.md:230-234`](../design/attested-green-adr.md) — *"the bump is the last
safely reversible moment"* — so **"we already bumped, a local tag exists"** is precisely the
state the record exists to hold, and it is erased silently, at the one moment it is consulted
(a stopped release).

This is the same root cause as `bh-gj0v9.6` E3 (GC deletes closed wisps regardless of graph
membership) with a worse victim and a lower bar: **no persistent bead need be involved at all.**
It is also not an oversight nobody thought about — `bd mol wisp gc --help` documents deliberate
live-work protection, *"blocked steps …, pinned beads, and any step whose status category is wip
… are never reclaimed by age … (GH#4394). … If the blocked set … cannot be read, the GC aborts
rather than risk reclaiming live steps."* **`--closed` bypasses all of it**, because a *closed
step of a running molecule* is not in the protected set. `--exclude-type` is no mitigation: it
filters issue type, and release steps are `task`.

**E7. `bd mol squash` on an in-flight molecule does not refuse — it completes the release.**
The wisps page's best practice is *"Squash before you delete."* Applied to the same still-open
run (unresolved human gate, unrun one-way door):

```console
$ bd mol squash rl-wisp-amf
✓ Squashed molecule: 3 children → 1 digest
  Digest ID: rl-6mr
  Deleted: 3 wisps
  Root auto-closed: rl-wisp-amf
```

Three open steps — including the unresolved releaser gate and `just release` itself — deleted,
and the root marked complete. No confirmation, no refusal.

**E8. The one durable artifact records the opposite of what happened.** `bd mol squash` *does*
promote to persistent (the narrative page's claim holds, refining `bh-gj0v9.1` evidence 10:
`rl-6mr` has a `bd history` entry and survives the next `gc`, which reported *"No closed wisps to
delete"*). But its content:

```console
$ bd show rl-6mr
✓ rl-6mr · Digest: mol-release-run   [● P2 · CLOSED]
  ## Molecule Execution Summary
  **Molecule**: mol-release-run
  **Steps**: 3
  **Completed**: 0/3
  ### Steps
  1. **[****open]** just release-preview — is the path clear?
  2. **[****open]** just release — atomic push of main+tag, ONE-WAY DOOR
  3. **[****open]** Gate: human
```

**`Completed: 0/3`** for a run whose `attest` and `bump` genuinely completed — E6 deleted them,
so they are absent from the summary and from the count. The permanent record of the release
therefore asserts that no attestation and no bump occurred. Steps are in storage order, not
topological order, and the markdown is malformed (`**[****open]**`). The digest's `PARENT` is
`rl-wisp-amf`, which squash left in a limbo state: not a wisp (`bd mol wisp list --all` → *"No
wisps found"*), absent from `bd list --status closed`, and `bd history` → *"No history found"* —
reachable only by `bd show`.

**E9. The releaser sign-off has no audit record.** The formula's `gate` materialises as an
*ephemeral* `type: gate` bead, so it inherits every wisp property:

```console
$ bd history rl-wisp-v3l            # the resolved human gate
No history found for issue rl-wisp-v3l

$ bd history rl-8mn                 # a persistent bead, for contrast
📜 History for rl-8mn (1 entries)
pfe40sa5 2026-08-20 18:27:02
  Author: beads
  ○ rl-8mn: Release 0.12.0 notes … [P2 - open]
```

This collides head-on with the ADR's **only positive finding**. Decision 5 / `bh-gj0v9.6`
E10-E12 rest on gate beads being *"persistent, versioned … git-synced, and **never
GC-eligible**"*. A formula-materialised gate inside a wisp is none of the three. A releaser
sign-off before a one-way door is the highest-audit-value gate this repo has, and it is the one
that leaves no trace.

**E10. Local-only kills the payoff that motivated the question.** The candidate benefit was
multi-operator handoff visibility. Wisps are *"excluded from federation push by default
(`federation.exclude_types` defaults to `[wisp]`) and not part of the shared audit trail"*
(wisps page) / *"stored locally but NOT synced via git"* (`bd mol wisp --help`). A second
operator on a second machine sees nothing. Discovery is doubly blocked: E1's scoped query needs
an id that appears on no list surface, and `bd mol wisp list` shows only **open** wisps — it
printed *"No wisps found"* the instant the run closed, **before** any GC ran.

### D. The architectural point, independent of every bug above

**E11. The shipped flow derives release position from the world; a wisp asserts it.** Every
release verb in `release.py` re-measures rather than remembers:

- `recover` (`:629`) — *"Read-only: it looks, it decides, it names the next command"*, deciding
  on **one measured fact**, *"ls-remote against the actual remote, not a local tracking ref"*
  (`:620`).
- `preview` (`:778`) — three measurements: the ledger verdict, `git ls-remote` for the tag, a
  PyPI request for the artifact — *"every line here is measured and printed"* (`:798`).
- `preflight` (`:235`) — reads a verdict keyed on the **tree hash**, and *"There is no flag that
  turns a refusal into a pass"* (`:251`).
- `pending` / `await` — read a marker keyed on `validation_ledger.tree_of(entry, sha)` (`:382`);
  a marker for a different tree is not a marker.

None can go stale, and all four share an exit-code contract in which **`3` = COULD NOT MEASURE
is never folded into `1` = refused** (`release.py:139-148`) — a contract that exists because the
0.11.5 incident was caused by a confident wrong sentence.

A wisp step is the opposite: a hand-closed assertion, unverified against anything. Close `bump`
then `git reset --hard` and the wisp still reports bumped, while the ledger, `ls-remote` and the
marker all report otherwise. So the wisp does not supply a durable "where did the release stop"
record **distinct from log output** — it supplies a **fifth, non-authoritative, unmeasured** one
beside four measured ones, and E6/E8 show it is the one that silently goes wrong.

**E12. The human gate already exists, with an executor behind it.** `just release` →
`scripts/push-main.sh` pushes the tag; `.github/workflows/release.yml` fires on `push: tags:`
(`:9-10`) and publishes under `environment: pypi-prod  # approval gate; configured under repo
Settings → Environments` (`:20`). That approval is exactly the shape
formula's `gate: gh:run` *models* — with a real executor, real identity, and a real audit log
GitHub retains. The ADR already recorded this (Decision 4 table: release cut → *"Neither …
its human gate is already `environment: pypi-prod` — the `gate: gh:run` shape formula models,
with a real executor behind it"*). E9 is the measurement of what swapping it for a wisp gate
costs: the sign-off stops being auditable.

## Verdict — **NO-GO**

**NO-GO.** A wisp does not fit release-loop tracking — but for reasons that are **not** the
ADR's, and the ADR's two headline disqualifiers do **not** apply here:

| `bh-gj0v9` disqualifier | Holds for a self-contained release run? |
|---|---|
| Invisible to the ready surface | **No** — `bd ready --mol` works (E1) |
| GC strips edges off persistent beads | **No** — verified live, `Removed 0 dependency link(s)` (E5) |
| Squash yields only a throwaway prose blob | **Partly** — it does promote to persistent (E8) |

What kills it is specific to this use case and worse. Routine `bd mol wisp gc --closed --force`
— hive-wide, step-granular, with no `--mol` scope and with its documented live-work protection
bypassed — **erased the completed steps of a running release**, taking `Progress: 2/5` to
`0/3` and deleting the `needs: bump` edge (E6). "The bump already happened, a local tag exists"
is precisely the fact the record is for, per `attested-green-adr.md`'s own reversibility rule,
and it is the fact that vanishes. `bd mol squash` on the same run silently deleted the
unresolved releaser gate and the unrun one-way-door step and marked the release complete (E7).
The one persistent artifact left behind then asserts `Completed: 0/3` about a run that attested
and bumped (E8), and the releaser sign-off has no `bd history` at all (E9) — collapsing the
single property the ADR's one **GO** (gate beads) is built on.

And even with every one of those defects fixed upstream, the fit would still fail on E11: the
shipped flow answers *"where did the release stop"* by **measuring** the remote, the ledger and
PyPI, with `COULD NOT MEASURE` as a distinct answer. A wisp answers it by remembering what
somebody clicked. Adding a record that can disagree with four measured ones, in front of a
one-way door, is a net loss regardless of GC.

**Scope of this NO-GO.** It moves nothing in the ADR's Decision-4 table — the release cut stays
**"Neither"** — but it changes the *reasons* on that row and supplies two corrections (E1, E5,
E8) that a future reader would otherwise re-derive wrongly from the closed spikes.

## Recommendation

1. **Carry this verdict to `bh-bomrd.3`, the joining decision bead — that is the concrete next
   step, and it is already filed.** Per the epic's design, `.3` appends a **dated addendum** to
   [`operational-workflow-substrate-adr.md`](../design/operational-workflow-substrate-adr.md)
   rather than authoring a new record. The addendum should carry four things this spike
   measured that the ADR currently states otherwise or not at all:
   - Decision 2's *"invisible to the work queue"* is **scoped-query-false**: `bd ready --mol
     <wisp-id>` lists wisp steps (E1). The real barrier is **discovery** (the id appears on no
     list surface, and `bd mol wisp list` hides closed wisps) and **locality** (E10) — state
     it that way, because the current wording is refutable in one command.
   - Decision 2's GC bullet should record the **stronger, self-contained** form found here
     (E6): `--closed` GC destroys the completed steps of an **in-flight** molecule and its
     dependency edges, with **no persistent bead involved**. The `bh-gj0v9.6` shape is a
     special case of this, and E5 shows the persistent-edge form is *not* reachable in a
     self-contained run — so the ADR's current framing understates the hazard while
     overstating its precondition.
   - `bd mol squash` **does** clear the ephemeral flag and yield a persistent, versioned digest
     (E8), correcting `bh-gj0v9.1` evidence 10 — but on an in-flight molecule it deletes open
     steps and auto-closes the root (E7), and its summary can be flatly wrong.
   - A formula-materialised `gate` inside a wisp is **ephemeral**, therefore unversioned and
     GC-eligible (E9). Decision 5's guarantee — gate beads are persistent, versioned, never
     GC-eligible — is a property of **`bd gate create`**, not of the `type: gate` bead shape.
     Worth saying explicitly so nobody reaches for a formula gate expecting Decision 5's
     properties.

   **No implementation beads are proposed by this spike**, and none should be filed from it. The
   epic's rule stands: if `bh-bomrd.2` returns GO, that is a `/bh:replan` into an implementation
   molecule, not beads filed from a verdict.

2. **Do not touch `release.py` or the release recipes.** The measure-don't-remember design
   (E11) and its 0/1/2/3 exit contract are what a wisp layer would dilute, not extend. There is
   no partial adoption worth taking either: the gate is the one piece that looked portable, and
   E9/E12 show moving it off `environment: pypi-prod` loses the executor *and* the audit record.

3. **Escalated, not fixed here** (filed via `bh escalate`, upstream `bd` defects — they change
   no verdict above, since E11 stands without them):
   - `bd mol wisp gc --closed` reclaims **closed steps of an in-flight molecule**, deleting their
     dependency edges and resetting the molecule's progress — bypassing the live-work protection
     `gc --help` documents for the `--age` path (GH#4394). There is no `--mol` scope flag and
     `--exclude-type` does not cover it.
   - `bd mol squash <id>` on a molecule with open steps deletes them and auto-closes the root
     without refusing or confirming.

4. **Reopen only on a measured change**, matching the ADR's own reopen bar: `bd mol wisp gc`
   gains molecule scoping *and* extends its live-work protection to the `--closed` path, wisps
   gain git sync and version history, **and** a release step's completion becomes something
   `bh` verifies against the ledger/remote rather than accepts on a close. All three, not any
   one — the first two only remove the bugs, and E11 is the reason the fit fails without them.
