# Spike `bh-yber2.1` — does a guardrailed wisp + a real gate bead + measured-fact replication work mechanically?

**Bead:** `bh-yber2.1` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-yber2`'s joining decision bead — mechanism half only. The ROI /
wall-clock / token half is a **sibling spike's job and is not touched here.**

> **Taken as settled, not re-derived:** everything in
> [`bh-bomrd.1`](bh-bomrd.1-release-loop-wisp-fit.md) (E1–E12) and the
> [`bh-bomrd.3` addendum](../design/operational-workflow-substrate-adr.md#addendum--2026-08-20-bh-bomrd3-the-two-narrow-mechanical-pipelines)
> (A1–A6). A formula never executes anything; wisps are host-local and unversioned;
> `bd mol wisp gc --closed --force` is hive-wide with no `--mol` scope (E6); `bd mol squash` on an
> in-flight molecule silently deletes open children (E7).
>
> **Taken as an ACCEPTED OPERATOR RISK, not re-litigated:** *"never run
> `bd mol wisp gc --closed` or `bd mol squash` while any wisp molecule is open, hive-wide"* is a
> guardrail in the same policy category as CLAUDE.md's existing *"never run `bd compact` /
> `bd flatten`"* rule. This spike tests the **new mitigations layered on top of that guardrail**,
> not the guardrail itself.

---

## Question

**Scope note — the gate question changed mid-flight.** The bead as filed asked whether swapping
the formula's `gate: {type: human}` for `gate: {type: gh:run}` with an `await_id` changes local
`bd` persistence. The operator then dropped that line of investigation entirely: **do not test
against real GitHub Actions**, and drop `gh:run` — instead test the simpler, GitHub-free shape,
**manual bead-to-bead**. What is measured below is therefore that shape. What the `gh:run` path
would and would not have shown is stated in **M7** so the dropped question is not silently lost.

The question actually settled:

**Does this three-part mitigation stack hold up mechanically?**

1. A release-shaped wisp molecule (`attest → bump → release-preview → release`) carrying **no
   `gate:` field at all** — wisp used only for the cheap, fast, mechanical steps.
2. A **separate, real, persistent gate bead** — `bd gate create --type human --blocks <the
   one-way-door step>` — as the sign-off between "release-preview done" and "release runs".
   The hypothesis: this is `bh-gj0v9.6`'s / Decision 5's already-blessed gate, so it should be
   persistent, versioned, git-synced and never GC-eligible, and should therefore close
   `bh-bomrd.1` **E9** (*"the releaser sign-off has no audit record"*) **without any GitHub
   dependency**.
3. **Selective replication of measured facts** — a timestamped, concrete measurement (not a bare
   `done` flag) written to a persistent control bead's `--metadata` at step checkpoints, with
   **no `bd dep add`** anywhere. The hypothesis: this narrows **E11** (a wisp step is an
   unverified assertion) and cannot reintroduce `bh-gj0v9.6` / `bh-bomrd.1`'s edge-stripping bug
   shape, because no edge is ever created.

**GO or NO-GO on the MECHANISM.** Three sub-questions the acceptance criteria require answered
precisely and separately:

- **Q-A.** Does swapping the gate's **provenance** (formula-materialized → `bd gate create`)
  change **LOCAL `bd`-side persistence**, or does it merely relocate the canonical approval
  record somewhere else? *These are different claims and are answered separately below.*
- **Q-B.** Is the metadata replication write safe — does it touch the persistent bead's
  dependency graph or history, and does it survive `gc --closed --force`?
- **Q-C.** Re-confirm, self-contained, that a full run closed out completely and *then* GC'd
  loses nothing (`bh-bomrd.1` **E5** already showed this; re-run once here).

---

## Method

1. Read in full before touching anything, as required and **not re-derived**:
   [`bh-bomrd.1`](bh-bomrd.1-release-loop-wisp-fit.md) (E6, E7, E9, E11 especially),
   [`bh-bomrd.1-mol-release-run.formula.json`](bh-bomrd.1-mol-release-run.formula.json), and the
   `bh-bomrd.3` addendum (A1–A6) in
   [`operational-workflow-substrate-adr.md`](../design/operational-workflow-substrate-adr.md).
2. Read `bd gate --help`, `bd gate create --help`, `bd gate discover --help`,
   `bd gate resolve --help`, `bd mol wisp --help`, `bd formula --help` (bd **1.1.0 (dev)**).
3. **Stood up a fresh scratch hive** — `git init` + `bd init --prefix gw` under the session
   scratchpad, deliberately **not** this repo's hive — the same method `bh-gj0v9.1` / `.6` /
   `bh-bomrd.1` used.
4. Authored `mol-release-run-gateless` — `bh-bomrd.1`'s formula **verbatim except the `gate`
   object on the `release` step is deleted**; `phase: "vapor"`, `pour: true`, four linear steps.
   (Filename must end `.formula.json`; a plain `.json` is not found by `bd formula list`.)
5. Ran **three** experiments in that one hive:
   - **Run A** — mid-flight: wisp, real gate bead on the one-way door, close `attest` + `bump`,
     replicate a measured fact, add one unrelated closed patrol wisp, then run the routine
     `gc --closed --force` **while the molecule was still open** (the E6 trigger), and inspect
     every survivor. Then finish the run, resolve the gate, GC again.
   - **Run B** — clean, no GC anywhere: wisp, real gate bead, close all three predecessors, then
     interrogate the readiness surfaces **with the gate still open**. Plus three controls
     (**M4a/M4b/M4c**) to isolate the cause of what Run B found.
   - **Run C** — pristine guardrailed run: full run, all steps closed, gate resolved, measured
     facts replicated at each checkpoint, **then** `gc --closed --force` (Q-C).
6. All command output below is **verbatim** from that scratch hive. Ids: `gw-*` persistent,
   `gw-wisp-*` ephemeral.

---

## Evidence

### A. The gate bead — Q-A

**M1. A `bd gate create` gate over a wisp step is a persistent, versioned bead. It is not a
wisp.** Created against Run A's one-way-door step:

```console
$ bd gate create --type human --blocks gw-wisp-6o0 \
    --reason "releaser sign-off before the one-way door" \
    --title "Gate: releaser sign-off v0.12.0"
✓ Created gate gw-v9r (type: human)
  Blocks: gw-wisp-6o0 (just release — atomic push of main+tag, ONE-WAY DOOR)
  Reason: releaser sign-off before the one-way door

$ bd mol wisp list --all
Wisps (5):
gw-wisp-5fw  open  P2  molecule  mol-release-run-gateless
gw-wisp-6o0  open  P2  task      just release — atomic push of main+tag,...
gw-wisp-9w5  open  P2  task      just release-preview — is the path clear?
gw-wisp-m17  open  P2  task      just bump — version+changelog+local tag...
gw-wisp-ts4  open  P2  task      just attest — prove this tree green
```

The gate is **absent** from the wisp table — it got a `gw-` id, not a `gw-wisp-` id. It does not
appear in bare `bd list` either (gates are filtered from the default list view); `bd list --type
gate` and `bd gate list` both find it.

**M2. `bd history` on that gate is populated at every transition — the direct refutation of E9.**
Side by side with a wisp step in the same hive at the same moment:

```console
$ bd history gw-v9r                 # the ad-hoc gate, before resolve
📜 History for gw-v9r (1 entries)
vgjl6uiq 2026-08-20 19:57:13
  Author: root
  ○ gw-v9r: Gate: releaser sign-off v0.12.0 [P2 - open]

$ bd history gw-wisp-ts4            # a wisp step in the same molecule
No history found for issue gw-wisp-ts4
```

And the resolve is versioned, with the actor and the reason retained:

```console
$ bd gate resolve gw-v9r --reason "releaser/dev1 signed off on v0.12.0 after preview"
✓ Gate resolved: gw-v9r
  Reason: releaser/dev1 signed off on v0.12.0 after preview

$ bd history gw-v9r
📜 History for gw-v9r (3 entries)
rie8ijjq 2026-08-20 19:58:49
  Author: root
  ✓ gw-v9r: Gate: releaser sign-off v0.12.0 [P2 - closed]
s19godg4 2026-08-20 19:57:50
  Author: beads
  ○ gw-v9r: Gate: releaser sign-off v0.12.0 [P2 - open]
vgjl6uiq 2026-08-20 19:57:13
  Author: root
  ○ gw-v9r: Gate: releaser sign-off v0.12.0 [P2 - open]
```

`bd show` retains the close reason permanently, and the gate is in the **git-synced export**
while every wisp row is not:

```console
$ bd show gw-v9r
✓ gw-v9r · Gate: releaser sign-off v0.12.0   [● P2 · CLOSED]
Owner: Brian Cripe · Type: gate
Close reason: releaser/dev1 signed off on v0.12.0 after preview

$ bd export -o exp.jsonl && wc -l exp.jsonl
10 exp.jsonl                        # 10 rows, ALL persistent
$ grep -o '"id":"[^"]*"' exp.jsonl | tr '\n' ' '
"id":"gw-60h" "id":"gw-bbx" "id":"gw-5nt" "id":"gw-ln1" "id":"gw-6gq" "id":"gw-4u0"
"id":"gw-5bm" "id":"gw-mb1" "id":"gw-v9r" "id":"gw-wqx"
```

Not one `gw-wisp-*` row is exported; the only occurrences of the string `wisp` in the export are
**text references inside the gates' own descriptions**.

**M3. The gate survives `gc --closed --force` even while itself closed.** Run A, after the whole
molecule was closed out and all five wisp rows deleted:

```console
$ bd mol wisp gc --closed --force
✓ Deleted 3 issue(s)
  Removed 0 dependency link(s)
  Updated text references in 1 issue(s)

$ bd show gw-v9r
✓ gw-v9r · Gate: releaser sign-off v0.12.0   [● P2 · CLOSED]
  Ad-hoc gate blocking [deleted:gw-wisp-6o0]
  Reason: releaser sign-off before the one-way door

$ bd history gw-v9r
📜 History for gw-v9r (4 entries)
```

Note the `[deleted:gw-wisp-6o0]` **tombstone** — GC rewrote the dangling reference in the gate's
description, and that rewrite is itself a versioned entry (the 4th). This is materially better
than `bh-gj0v9.6`'s silent strip. It is **not** universal, though: `bd mol burn` leaves no
tombstone at all. Run B's gate still names a bead that no longer exists:

```console
$ grep -o '"id":"gw-\(bbx\|mb1\|v9r\)","title":"[^"]*","description":"[^"]*"' exp.jsonl
"id":"gw-bbx","title":"Gate: releaser sign-off v0.13.0","description":"Ad-hoc gate blocking
 [deleted:gw-wisp-f2r]\n\nReason: releaser sign-off before the one-way door"     # gc'd
"id":"gw-mb1","title":"Gate: releaser sign-off v0.12.1","description":"Ad-hoc gate blocking
 gw-wisp-45f\n\nReason: releaser sign-off before the one-way door"               # burned
"id":"gw-v9r","title":"Gate: releaser sign-off v0.12.0","description":"Ad-hoc gate blocking
 [deleted:gw-wisp-6o0]\n\nReason: releaser sign-off before the one-way door"     # gc'd
```

**Answer to Q-A, stated precisely, both halves separately:**

- **The provenance swap DOES change LOCAL `bd`-side persistence.** `bd history` goes from *"No
  history found"* to a full versioned record with author, timestamp, the open→closed transition
  and the sign-off reason; the bead moves out of the ephemeral wisp table into the git-synced
  export and stops being GC-eligible. **E9 is closed locally, in `bd`, on this path.** This is a
  property of **`bd gate create`**, exactly as addendum **A3** says — not of the `type: gate`
  bead shape, and not of any gate *type* (`human` / `timer` / `gh:run` / `gh:pr`). See **M7**.
- **It does NOT relocate the canonical approval record externally.** Nothing here involves
  GitHub. The canonical record stays in `bd`, on this host, in this hive's Dolt store and its
  export. That is a *different* claim from the one the bead originally posed for `gh:run`, and
  the two must not be conflated: the originally-briefed `gh:run` swap would have done the
  opposite — relocated the canonical record to GitHub — and, per M7, would have changed local
  persistence **not at all** relative to this path.

### B. THE KILLER — the persistent gate does not gate

**M4. LIVE BUG — a persistent gate that is an open `DEPENDS ON` of a wisp step does not block
it.** Run B, clean, **no GC anywhere in this run**. Gate created on the one-way door, then all
three predecessors closed, then the readiness surfaces interrogated **while the gate is still
open**:

```console
$ bd gate list
⏳ Open Gates (3):
○ gw-bbx - human
○ gw-4u0 - human
○ gw-mb1 - human                    # <- Run B's releaser gate, OPEN

$ bd show gw-wisp-45f               # the ONE-WAY DOOR step
PARENT
  ↑ ○ gw-wisp-o6i: mol-release-run-gateless ● P2
DEPENDS ON
  → ○ gw-mb1: Gate: releaser sign-off v0.12.1 ● P2      # <- OPEN blocker, present
  → ✓ gw-wisp-9pc: just release-preview — is the path clear? ● P2

$ bd mol current gw-wisp-o6i
  [done] gw-wisp-at1: just attest — prove this tree green
  [done] gw-wisp-rgu: just bump — version+changelog+local tag v0.12.1
  [ready] gw-wisp-45f: just release — atomic push of main+tag, ONE-WAY DOOR
  [done] gw-wisp-9pc: just release-preview — is the path clear?
Progress: 3/4 steps complete
Next ready: gw-wisp-45f - just release — atomic push of main+tag, ONE-WAY DOOR
  Start with: bd update gw-wisp-45f --claim

$ bd ready --mol gw-wisp-o6i
📋 Ready steps:
1. [● P2] [molecule] gw-wisp-o6i: mol-release-run-gateless [group-1]
2. [● P2] [task]     gw-wisp-45f: just release — atomic push of main+tag, ONE-WAY DOOR [group-1]
```

The unresolved releaser sign-off is a present, open, correctly-recorded dependency — and both
tracking surfaces report the one-way door **`[ready]`** and actively instruct the operator to
claim it. Reproduced a third time in Run C (`gw-bbx` open, `gw-wisp-f2r` `[ready]`).

Three controls isolate the cause. **None of them is the query, and none of them is the gate.**

**M4a — the same gate DOES block a persistent bead.** Same hive, same command, target persistent
instead of ephemeral:

```console
$ bd ready                                   # before
○ gw-wqx ● P2 Release 0.12.0 control record (PERSISTENT, no dep edge to any wisp)
○ gw-5bm ● P2 persistent work item gated by an ad-hoc human gate
Ready: 2 issues with no active blockers

$ bd gate create --type human --blocks gw-5bm --reason "control: does an ad-hoc gate block a PERSISTENT bead"
✓ Created gate gw-4u0 (type: human)

$ bd ready                                   # after
○ gw-wqx ● P2 Release 0.12.0 control record (PERSISTENT, no dep edge to any wisp)
Ready: 1 issues with no active blockers      # <- correctly blocked
```

**M4b — a formula-materialized gate DOES block the identical step, in the same hive.**
`bh-bomrd.1`'s original formula, gate object retained, wisped alongside:

```console
$ bd show gw-wisp-6ap                        # the release step
DEPENDS ON
  → ○ gw-wisp-p8u: Gate: human ● P2          # <- EPHEMERAL gate, open
  → ✓ gw-wisp-9fm: just release-preview — is the path clear? ● P2

$ bd mol current gw-wisp-0ir
  [done]    gw-wisp-ue6: just attest — prove this tree green
  [ready]   gw-wisp-p8u: Gate: human
  [done]    gw-wisp-9fm: just release-preview — is the path clear?
  [done]    gw-wisp-w1w: just bump — version+changelog+local tag v9.9.9
  [pending] gw-wisp-6ap: just release — atomic push of main+tag, ONE-WAY DOOR
Progress: 3/5 steps complete
Next ready: gw-wisp-p8u - Gate: human        # <- correctly blocked, correctly routed
```

Same hive, same query, same molecule shape, same open `type: gate` dependency. `[pending]` when
the gate is **ephemeral**; `[ready]` when the gate is **persistent**. The variable is the
blocker's ephemerality, not the query and not the gate.

**M4c — it is not gate-specific.** An ordinary persistent bead added as a blocker with plain
`bd dep add` is ignored the same way:

```console
$ bd dep add gw-wisp-45f --depends-on gw-6gq
✓ Added dependency: gw-wisp-45f (just release — ... ONE-WAY DOOR) depends on
  gw-6gq (ROOT-CAUSE PROBE: ordinary persistent blocker (not a gate)) (blocks)

$ bd show gw-wisp-45f
DEPENDS ON
  → ○ gw-mb1: Gate: releaser sign-off v0.12.1 ● P2
  → ✓ gw-wisp-9pc: just release-preview — is the path clear? ● P2
  → ○ gw-6gq: ROOT-CAUSE PROBE: ordinary persistent blocker (not a gate) ● P2

$ bd mol current gw-wisp-o6i
  [ready] gw-wisp-45f: just release — atomic push of main+tag, ONE-WAY DOOR
Progress: 3/4 steps complete
Next ready: gw-wisp-45f - just release — atomic push of main+tag, ONE-WAY DOOR
```

**Two open persistent blockers, still `[ready]`.** Root cause, stated as measured: **the
wisp/molecule-scoped readiness computation (`bd mol current`, `bd ready --mol`) honours only
blockers that are themselves inside the ephemeral set. A non-ephemeral blocker of an ephemeral
step is invisible to it.** Escalated as `hq-mek`.

The consequence is the whole point of the mitigation: **the gate becomes advisory.** It records
the sign-off beautifully (M1–M3) and enforces nothing. The failure is silently in the **unsafe**
direction — the tracking surface does not merely omit the block, it prints
`Next ready: … ONE-WAY DOOR` and `Start with: bd update … --claim`. A false green in front of an
irreversible push is worse than no gate at all, because an operator reading `bd mol current` has
been told the door is clear by the very record that exists to say it is not.

### C. Measured-fact replication — Q-B

**M5. A plain `bd update --metadata` write adds no edge, is versioned, and is git-synced.**
After closing `bump` in Run A, with the persistent control `gw-wqx` carrying **no** dependency to
anything:

```console
$ bd update gw-wqx --metadata '{"bh.release.version":"0.12.0",
  "bh.release.bump.measured_at":"2026-08-20T19:58:41Z",
  "bh.release.bump.local_tag":"v0.12.0",
  "bh.release.bump.tag_sha":"4f1c0a9e2b7d6538aa1c93fe0b2e77c41d905ab3",
  "bh.release.bump.probe":"git ls-remote --tags origin refs/tags/v0.12.0",
  "bh.release.bump.remote_has_tag":false,"bh.release.bump.exit":0}'
✓ Updated issue: gw-wqx — Release 0.12.0 control record (PERSISTENT, no dep edge to any wisp)

$ bd dep tree gw-wqx                # BEFORE and AFTER are byte-identical
🌲 Dependency tree for gw-wqx:
gw-wqx: Release 0.12.0 control record (PERSISTENT, no dep edge to any wisp) [P2] (open) [READY]

$ bd history gw-wqx                 # 2 entries before the write, 3 after
📜 History for gw-wqx (3 entries)
s19godg4 2026-08-20 19:57:50   Author: beads   ○ gw-wqx: … [P2 - open]
vgjl6uiq 2026-08-20 19:57:13   Author: root    ○ gw-wqx: … [P2 - open]
stbjjvai 2026-08-20 19:56:51   Author: beads   ○ gw-wqx: … [P2 - open]
```

The graph is untouched (no node, no edge, no change to `bd dep tree`) and the write **is** an
archived version. The value is also exported:

```console
$ bd export | tail -1                        # (line wrapped for width)
{"_type":"issue","id":"gw-wqx","title":"Release 0.12.0 control record (PERSISTENT, no dep edge
to any wisp)","status":"open","priority":2,"issue_type":"task","owner":"brian@xenophon.dev",
"created_at":"2026-08-20T19:56:51Z","created_by":"Brian Cripe","updated_at":"2026-08-20T19:57:
51Z","metadata":{"bh.release.version":"0.12.0","bh.release.bump.exit":0,"bh.release.bump.probe
":"git ls-remote --tags origin refs/tags/v0.12.0","bh.release.bump.tag_sha":"4f1c0a9e2b7d6538a
a1c93fe0b2e77c41d905ab3","bh.release.bump.local_tag":"v0.12.0","bh.release.bump.measured_at":"
2026-08-20T19:58:41Z","bh.release.bump.remote_has_tag":false},"dependency_count":0,
"dependent_count":0,"comment_count":0}
```

**M6. `--metadata` shallow-**merges**, it does not replace.** This is what makes per-checkpoint
replication work at all:

```console
$ bd update gw-60h --metadata '{"a":1}'
$ bd update gw-60h --metadata '{"b":2}'
$ bd show gw-60h --json | jq -c '.[0].metadata'
{"a":1,"b":2}
```

**But the metadata *values* have no per-version history.** `bd history --json` projects only
`created_at / created_by / id / issue_type / owner / priority / status / title / updated_at` —
no `metadata` — and `bd show --as-of <commit>` returns `null` for it at **every** commit in the
row's history:

```console
$ bd history gw-5nt --json | jq -r '.[0].Issue | keys | join(" ")'
created_at created_by id issue_type owner priority status title updated_at

$ for i in 0 1 6; do
    H=$(bd history gw-5nt --json | jq -r ".[$i].CommitHash")
    bd show gw-5nt --as-of $H --json | jq -c '.[0].metadata'
  done
null
null
null
```

So the **current merged value** is durable and archived-by-row, but a key that is *overwritten*
loses its prior value irrecoverably — there is no bd surface that reads it back. Practical rule:
**one distinct key per checkpoint measurement; never reuse a key.** With that rule the record is
effectively append-only, which is how Runs A and C wrote it.

**M7 — the `gh:run` question the operator dropped, and what is/isn't answerable without it.**
Not tested, by explicit instruction (no real GitHub Actions). What the help text settles without
a live connection: `--type` and `--await-id` are flags on **`bd gate create`** *and* fields on a
formula `gate:` step — i.e. **type is orthogonal to provenance**. `bd gate check` evaluates open
gates and `bd gate discover` fills in a missing `await_id` by matching branch / SHA / time
proximity against recent GitHub runs; both operate on the *same* bead row either way. Since M1–M3
show persistence is decided entirely by **which command created the bead** (ephemeral wisp table
vs. persistent table), and no gate *type* moves a bead between those tables, the load-bearing
prediction is: **`gh:run` would change local `bd` persistence not at all.** A `gh:run` gate
materialized by a formula inside a wisp would still be ephemeral and still return *"No history
found"* (E9 unchanged); a `gh:run` gate made by `bd gate create` would be persistent exactly like
`gw-v9r` above. Its only *additional* effect is to relocate the **canonical approval decision**
to GitHub, where `environment: pypi-prod` already holds it (`bh-bomrd.1` **E12**). **This
prediction is stated as a prediction, not as a measurement** — it is the one claim in this
document not backed by live output, and the one thing a future spike with a live GH connection
would need to confirm. Note it would not rescue **M4**: `bd gate create --type gh:run --blocks
<wisp-step>` produces the same persistent-blocker-of-an-ephemeral-step configuration that M4c
shows is ignored regardless of the blocker's type.

### D. The accepted risk, re-measured — and Q-C

**M8. E6 reproduces exactly, on this shape, with the new mitigations in place.** Run A, mid-flight
(`attest` + `bump` closed) plus one unrelated closed patrol wisp:

```console
$ bd mol current gw-wisp-5fw                 # BEFORE
  [done]    gw-wisp-ts4: just attest — prove this tree green
  [ready]   gw-wisp-9w5: just release-preview — is the path clear?
  [pending] gw-wisp-6o0: just release — atomic push of main+tag, ONE-WAY DOOR
  [done]    gw-wisp-m17: just bump — version+changelog+local tag v0.12.0
Progress: 2/4 steps complete

$ bd mol wisp gc --closed --force
Found 3 closed wisp(s)
✓ Deleted 3 issue(s)
  Removed 0 dependency link(s)

$ bd mol current gw-wisp-5fw                 # AFTER
  [ready]   gw-wisp-9w5: just release-preview — is the path clear?
  [pending] gw-wisp-6o0: just release — atomic push of main+tag, ONE-WAY DOOR
Progress: 0/2 steps complete

$ bd show gw-wisp-9w5
PARENT
  ↑ ○ gw-wisp-5fw: mol-release-run-gateless ● P2
BLOCKS
  ← ○ gw-wisp-6o0: just release — atomic push of main+tag, ONE-WAY DOOR ● P2
                                              # ^ no DEPENDS ON — `needs: bump` is gone
```

`2/4` → `0/2`, both completed steps erased, `needs: bump` stripped, `Removed 0 dependency
link(s)` reported. **The guardrail is load-bearing, not theoretical.** The mitigations do not
change this, and are not claimed to.

**M9. What the mitigations DO protect from that same mid-flight GC** — measured in the same
breath, immediately after M8:

```console
$ bd show gw-wqx                             # persistent control
○ gw-wqx · Release 0.12.0 control record (PERSISTENT, no dep edge to any wisp)  [● P2 · OPEN]
$ bd show gw-wqx --json | jq -c '.[0].metadata'    # all 7 measured keys intact
{"bh.release.version":"0.12.0","bh.release.bump.exit":0,"bh.release.bump.probe":"git ls-remote
--tags origin refs/tags/v0.12.0","bh.release.bump.tag_sha":"4f1c0a9e2b7d6538aa1c93fe0b2e77c41d
905ab3","bh.release.bump.local_tag":"v0.12.0","bh.release.bump.measured_at":"2026-08-20T19:58:
41Z","bh.release.bump.remote_has_tag":false}      # (wrapped for width; one line as emitted)
$ bd dep tree gw-wqx
gw-wqx: … (open) [READY]                     # unchanged
$ bd history gw-wqx
📜 History for gw-wqx (3 entries)             # unchanged

$ bd gate list
⏳ Open Gates (1):
○ gw-v9r - human                             # gate survived mid-flight GC, still open
$ bd history gw-v9r
📜 History for gw-v9r (2 entries)
```

So the **replicated measured facts and the sign-off record are exactly the two things that
survive** the bug the guardrail exists for. That part of the design works.

**M10. The `bh-gj0v9.6` edge-stripping shape — which direction is dangerous.** Tested both
directions live rather than assumed:

- **`persistent DEPENDS ON wisp`** (gj0v9.6's exact shape) — **reproduces, silently, no
  tombstone:**

  ```console
  $ bd dep add gw-ln1 --depends-on gw-wisp-ue6
  ✓ Added dependency: gw-ln1 (DIRECTION PROBE…) depends on gw-wisp-ue6 (just attest…) (blocks)
  $ bd show gw-ln1                            # BEFORE
  DEPENDS ON
    → ✓ gw-wisp-ue6: just attest — prove this tree green ● P2

  $ bd mol wisp gc --closed --force
  Found 6 closed wisp(s)
  ✓ Deleted 6 issue(s)
    Removed 0 dependency link(s)
    Updated text references in 0 issue(s)

  $ bd show gw-ln1                            # AFTER — the edge is simply gone
  Owner: Brian Cripe · Type: task
  DESCRIPTION
    (none)
  ```

- **`persistent BLOCKS wisp`** (the gate's direction) — the edge also disappears when the wisp
  step is GC'd, but the description reference is rewritten to `[deleted:…]` (M3), so there is a
  tombstone. Not for `bd mol burn`, which leaves the reference dangling.
- **The metadata replication write creates neither** — `bd dep tree gw-wqx` is byte-identical
  before and after (M5). **The bug shape is not reintroduced by the replication design at all.**
  Confirmed live, not assumed. It *is* reintroduced, mildly, by the **gate**, which necessarily
  does create a persistent↔ephemeral edge.

**M11 — Q-C: the guardrail, re-confirmed self-contained.** Run C, pristine: full run walked to
completion, measured facts replicated at every checkpoint, gate resolved, root auto-closed —
**then** GC:

```console
$ bd mol current gw-wisp-0un
  [done] gw-wisp-2hx: just attest — prove this tree green
  [done] gw-wisp-7b7: just release-preview — is the path clear?
  [done] gw-wisp-f2r: just release — atomic push of main+tag, ONE-WAY DOOR
  [done] gw-wisp-ahe: just bump — version+changelog+local tag v0.13.0
Progress: 4/4 steps complete

$ bd mol wisp gc --closed --force
Found 5 closed wisp(s)
✓ Deleted 5 issue(s)
  Removed 0 dependency link(s)
  Removed 0 label(s)
  Removed 0 event(s)
  Updated text references in 1 issue(s)

$ bd show gw-5nt --json | jq '.[0].metadata'   # all 14 measured keys intact
{
  "bh.release.version": "0.13.0",
  "bh.release.bump.probe": "git ls-remote --tags origin refs/tags/v0.13.0",
  "bh.release.signoff.at": "2026-08-20T20:02:10Z",
  "bh.release.attest.tree": "a91f0c3",
  "bh.release.bump.tag_sha": "c0ffee11deadbeef22334455667788990011aabb",
  "bh.release.signoff.gate": "gw-bbx",
  "bh.release.bump.local_tag": "v0.13.0",
  "bh.release.release.tag_sha": "c0ffee11deadbeef22334455667788990011aabb",
  "bh.release.bump.measured_at": "2026-08-20T20:01:20Z",
  "bh.release.attest.measured_at": "2026-08-20T20:01:00Z",
  "bh.release.bump.remote_has_tag": false,
  "bh.release.release.measured_at": "2026-08-20T20:02:30Z",
  "bh.release.attest.ledger_verdict": "green",
  "bh.release.release.remote_has_tag": true
}
$ bd dep tree gw-5nt
gw-5nt: Release 0.13.0 control record (PERSISTENT) [P2] (open) [READY]
$ bd history gw-5nt
📜 History for gw-5nt (7 entries)

$ bd show gw-bbx
✓ gw-bbx · Gate: releaser sign-off v0.13.0   [● P2 · CLOSED]
Close reason: releaser/dev1 signed off on v0.13.0
$ bd history gw-bbx
📜 History for gw-bbx (6 entries)
```

**Zero data loss**, confirming `bh-bomrd.1` **E5** on this spike's own evidence, with the two new
mitigations in place. Everything the guardrailed design intends to keep, it keeps.

**M12 — E11 is narrowed, not closed.** The replicated fact is a *timestamped measurement*
(`bh.release.bump.tag_sha`, `bh.release.bump.probe`, `bh.release.bump.remote_has_tag: false`),
strictly more than the bare `done` flag E11 objects to — a reader can at least see *what* was
measured, *when*, and *by which probe*. But it is still a **remembered** measurement, read back
without re-measuring, and `bd show` will print `remote_has_tag: false` forever regardless of what
the remote now says. `release.py`'s design re-measures at read time and distinguishes `3 = COULD
NOT MEASURE` from `1 = refused` (`release.py:139-148`). The replication pattern moves the record
from *unverified assertion* to *stale-able snapshot* — a real improvement, but E11's core point
(the shipped flow derives position from the world) survives it.

---

## Verdict — **NO-GO on the mechanism**

**NO-GO** — and, importantly, **not for the reason the epic anticipated.** The accepted risk
behaved exactly as the operator predicted, and one of the two new mitigations is genuinely sound.
The stack fails on a **third, previously unrecorded, un-accepted defect** found in the seam the
mitigation itself creates.

| Claim under test | Result |
|---|---|
| `bd gate create` gate over a wisp step is persistent, versioned, git-synced, GC-proof | **Holds** (M1–M3) — E9 closed locally, no GitHub needed |
| …and therefore **gates** the one-way door | **FAILS** (M4) — reported `[ready]` with the gate open |
| Measured-fact replication touches no edge, survives GC | **Holds** (M5, M9, M10) |
| Replication reintroduces the gj0v9.6 edge-strip bug | **No** (M10) — confirmed live, no edge exists |
| Full run → then GC → zero loss (guardrail, Q-C) | **Holds** (M11), confirming E5 |
| Mid-flight GC still destroys in-flight progress (E6) | **Reproduces** (M8) — guardrail is load-bearing |

**The disqualifier: the persistent gate does not gate (M4).** `bd mol current` and
`bd ready --mol` honour only blockers inside the ephemeral set, so an open persistent gate on a
wisp step is silently ignored — while `bd show` on the same step correctly lists it as an open
`DEPENDS ON`. Isolated to the blocker's ephemerality by three controls in the same hive: the same
gate blocks a persistent bead (M4a); a *formula-materialized* gate blocks the identical step
(M4b); and a plain non-gate persistent blocker is ignored the same way (M4c) — so it is neither
the query nor the gate type.

That converts the mitigation into a strict trade rather than a win: **you buy the sign-off's
audit trail with the sign-off's enforcement.** The formula gate enforces and leaves no record
(E9); the real gate leaves a perfect record and enforces nothing. And this failure mode is worse
than the one it replaces, because it is a **false green** rather than a missing record: the
tracking surface prints `Next ready: just release — ONE-WAY DOOR` and `Start with: bd update
… --claim` while the releaser sign-off is unresolved. Per
[`attested-green-adr.md`](../design/attested-green-adr.md), the material fact here is that the
push past this point is irreversible; a record that confidently says "clear" when it is not is
precisely the `0.11.5`-incident failure shape that `release.py`'s exit contract exists to
prevent.

**Scope of this NO-GO.** It is a verdict on the **mechanism only**. It moves nothing in the ADR's
Decision-4 table (the release cut stays *"Neither"*), it does not touch Decision 5, and it says
**nothing** about the ROI claim, which is the sibling spike's to measure. It also does not
re-open the accepted risk: the guardrail held (M11).

**Two findings are salvage, and should not be lost in the NO-GO:**

1. **Addendum A3's table gains a measured third column and a caveat.** `bd gate create` really
   does deliver all four properties over a wisp step *as a record* — persistent, versioned,
   git-synced, GC-proof (M1–M3), with a `[deleted:…]` tombstone on GC that `bh-gj0v9.6` did not
   get. The caveat A3 does not yet carry: **those four properties do not include being
   enforced**, once the blocked bead is ephemeral.
2. **Selective measured-fact replication is sound and is independently useful.** It is a plain
   `bd update --metadata` on a persistent bead: no edge, no graph change, versioned per write,
   shallow-merged so per-checkpoint keys accumulate, exported, and untouched by any GC (M5, M6,
   M9, M11). Critically, **it needs no wisp.** Nothing in M5/M6/M9/M11 depends on a molecule
   existing; the wisp contributed nothing to the replication result. If this pattern is the
   value, it can be taken without adopting any part of the wisp mechanism.

---

## Recommendation

1. **Carry this to `bh-yber2`'s joining decision bead as a mechanism NO-GO, with the two salvage
   findings intact.** Do **not** average it with the sibling ROI spike: if ROI lands GO, the
   correct conclusion is *"the payoff is real but this mechanism cannot deliver it safely"*, not
   *"ship it"*. The decision bead's own framing already allows a split.

2. **Do not adopt the wisp + persistent-gate hybrid in front of `just release`.** M4 is
   disqualifying on its own for a one-way door, independent of E6, E11 and the guardrail.
   `environment: pypi-prod` on `.github/workflows/release.yml:20` already has a real executor, a
   real identity and an audit log GitHub retains (E12/A3) — it enforces *and* records, which is
   exactly the combination M4 shows this hybrid cannot offer.

3. **Take the measured-fact replication pattern on its own merits, if it is wanted — separately,
   and without a wisp.** It is `bd update --metadata` on a persistent bead, needs no new
   mechanism, and is safe (M5, M9, M10). Two rules if it is adopted: **one distinct key per
   checkpoint measurement, never reuse a key** (M6 — overwritten values are unrecoverable, as
   metadata is projected into neither `bd history --json` nor `bd show --as-of`), and it stays a
   *snapshot beside* the measured verbs, never a substitute for them (M12 — E11 is narrowed, not
   closed). **This spike files no beads for it**, per the spike-loop rule; it is a `/bh:replan`
   input if the decision bead wants it.

4. **Escalated, not fixed here** — filed via `bh escalate` as **`hq-mek`**: *a persistent blocker
   of an ephemeral wisp step is ignored by wisp-scoped readiness.* `bd gate create --type human
   --blocks <wisp-step>` leaves the step reported `[ready]` by `bd mol current` and
   `bd ready --mol` while the gate is open; the same gate blocks a persistent bead, a
   formula-materialized gate blocks the same step, and a plain `bd dep add` persistent blocker is
   ignored identically. Worth flagging at the severity `bd mol wisp gc --closed` got (addendum
   A5, `hq-9le`): it is silent, it is reachable under entirely normal use of two documented
   commands, and it fails toward "go" in front of an irreversible operation. This spike's verdict
   does not depend on it being fixed.

5. **Reopen bar — Decision 2's list gains a fifth condition.** Alongside A4's four (git sync,
   ready visibility, GC that does not strip edges, and release-step completion becoming something
   `bh` verifies), add: **wisp-scoped readiness must honour non-ephemeral blockers.** Without it,
   no gate placed on a wisp step — of any type, from any provenance — actually gates. As with the
   existing bar: all of them, not any one.
