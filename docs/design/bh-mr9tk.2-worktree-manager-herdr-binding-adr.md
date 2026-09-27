# Worktree manager and Herdr binding contract ADR

**Status:** decided — **GO** · **Date:** 2026-09-27 · **Decision bead:** `bh-mr9tk.2` ·
**Supersedes:** the "Beadhive must always execute worktree mechanics" premise of
[herdr-integration-adr.md](herdr-integration-adr.md) §2 (already flagged partially superseded
there, pending this ADR) · **Amends:**
[lifecycle-owned-agent-herdr-space-contract.md](lifecycle-owned-agent-herdr-space-contract.md)
(teardown saga, §"Teardown saga variant" below) · **Related:**
[worktree-provider-contract-spike-molecule.yaml](worktree-provider-contract-spike-molecule.yaml)
(original spike framing, superseded on specifics by the epic design below),
`docs/spikes/bh-5kg8w-worktree-provider-mechanics.md` (E1–E27),
`docs/spikes/bh-mr9tk.1-worktree-manager-herdr-contract.md` (E28–E49)

Decided by the operator via the `bh-mr9tk` epic design (confirmed 2026-09-26), ratified against
spike evidence in this ADR. Fixes the two-role worktree contract, selection, composition,
failure policy, recovery, teardown-saga variant, sequencing, and bead disposition so an
implementation molecule can be filed next.

## Decision: GO

Beadhive splits worktree lifecycle into two independently composable roles instead of one fused
"provider": a **WorktreeManager** (exactly one per hive: `create`/`attach`/`remove`) and a
**WorkspaceBinding** (zero or one: `bind`/`release`), with **native Git as the default and only
built-in `WorktreeManager`**, and **Herdr composable only as a `WorkspaceBinding`**. Herdr is
deferred, not scheduled, as a `WorktreeManager` (Option B): promoting it to manager is
re-evaluated only if Option A shows problems that were not anticipated here — see "Sequencing:
Option A now, Option B deferred" below.

### Why GO

`bh-5kg8w` (E1–E27) settled the underlying question — the worktree allocator and the Herdr
process owner do not have to be the same tool, because Herdr's only worktree state is a
workspace-to-checkout binding (E2) that `herdr worktree open --path` produces idempotently for
*any* existing worktree regardless of who created it (E5, E7). `bh-mr9tk.1` (E28–E49) then proved
every remaining lifecycle row this recommendation depended on, for both valid compositions:

- **Option A** (native manager + Herdr binding): bind-after-create (E28), bind-after-attach
  (E29), clean release-then-remove (E30), the exact partial-failure shape when release is skipped
  (E31, extending bh-5kg8w E17), dirty refusal (E32), Herdr-unavailable-at-bind safety (E33),
  crash-then-idempotent-rebind across a real server restart (E34), and the binding-reference
  round trip (E35).
- **Option B** (Herdr as exclusive manager): exact-path/exact-branch create with implicit bind
  (E36), the `--base`-is-silently-ignored guard Beadhive must own itself (E37, extending bh-5kg8w
  E9), hook-shelling proof needed for the crash technique to be valid (E38), the structural (not
  conventional) proof that classification must precede the fused remove call (E39), total refusal
  with nothing created when Herdr is unavailable (E40), partial-failure recovery via the same
  primitive as Option A (E41), full crash/restart durability for clean sequences (E42), and the
  binding-reference round trip (E43).

No evidence in either spike contradicts the epic's two-role design, and E39 turns what the epic
described as a plausible saga variant into a structural necessity. This ADR is GO on that basis.

## The contract

### WorktreeManager (capability slot `worktree.manager`, on `beadhive-plugins`)

Exactly one configured manager per hive. Beadhive policy — never the manager — computes the
`WorktreeSpec` (main clone, branch, exact path, start point); the manager only executes it.

| Method | Signature | Behavior, cited |
| --- | --- | --- |
| `create` | `create(spec: WorktreeSpec) -> WorktreeHandle` | New branch at the exact path Beadhive computed (E8, E36). Never chooses the path or branch name itself. |
| `attach` | `attach(spec: WorktreeSpec) -> WorktreeHandle` | Existing branch at the exact path; must not move the branch tip even when `spec.base` has diverged — Beadhive pre-checks start-point intent itself rather than trusting the manager's `--base`/equivalent flag, because at least one real manager silently ignores it on an existing branch (E9, E37). |
| `remove` | `remove(handle: WorktreeHandle, force: bool) -> WorktreeRemoved` | Removes the linked worktree only; never deletes the branch (E14, native `git worktree remove` semantics). `force` bypasses the dirty refusal (E23/E32); dirty refusal without `force` is the default (E32). |

Native Git is the built-in default `WorktreeManager`, implemented in `beadhive-worktrees`. A
Herdr `WorktreeManager` (Option B) would be a distinct adapter, filed only if a later
re-evaluation adopts Option B (see "Sequencing: Option A now, Option B deferred" below), and it
must pass the same interface plus the `bh-qdezo.9` conformance kit before it may be selected.

### WorkspaceBinding (capability slot, zero or one)

| Method | Signature | Behavior, cited |
| --- | --- | --- |
| `bind` | `bind(handle: WorktreeHandle) -> WorktreeHandle` | Called after `create` or `attach` returns; agnostic to which one preceded it (E28, E29). Idempotent — a repeated `bind` on an already-bound handle returns the same binding reference rather than erroring (E34). |
| `release` | `release(handle: WorktreeHandle) -> None` | Called before `remove`. Must complete before `remove` runs; a `remove` issued without a prior `release` leaves a stale, orphaned binding on the presentation side (E31). |

Herdr implements `WorkspaceBinding` with `herdr worktree open --path <exact>` (bind) and
`herdr workspace close <id>` (release) — exactly the primitives bh-5kg8w's Recommendation §3 and
`bh-mr9tk.1`'s E28/E30/E34/E41 exercised.

### `WorktreeHandle` shape

`WorktreeHandle` carries: the bead/worktree correlation identity, the exact `path`, the exact
`branch`, the recorded start point (`base`, for the E37 guard), and a `bindings: dict[str, str]`
map (for example `{"herdr": "w4"}`) filled by whichever component performed the bind — the
manager itself under Option B's implicit bind (E36, E43), or the separate `WorkspaceBinding`
under Option A (E28, E35). Every binding reference recorded here must be independently
re-derivable from the manager's own inventory read (`herdr worktree list` →
`open_workspace_id`, E35/E43) — the handle is a cache of that fact, never its sole source of
truth, so a lost/stale handle is always repairable by re-reading inventory.

### Capability declarations

- **`binds: list[str]`** — presenters a `WorktreeManager` binds as a side effect of its own
  `create`/`attach`. Native declares `binds: []`. A future Herdr `WorktreeManager` (Option B)
  declares `binds: ["herdr"]`, because its `create` call already returns a `workspace_id` inline
  with no separate bind step (E36).
- **`remove_releases_bindings: bool`** — whether the manager's `remove` also performs the
  binding release as a side effect. Native declares `false` (it has no binding concept). A Herdr
  `WorktreeManager` declares `true`, because `herdr worktree remove` fuses close-and-remove into
  one call (E13, reconfirmed E39).

### Composition rule

A `WorkspaceBinding` for a given presenter is composed **only when the selected
`WorktreeManager`'s `binds` list does not already contain that presenter.** Concretely:

- native manager alone → no binding composed (no presenter enabled);
- native manager + Herdr binding enabled → **Option A**, the binding is composed because
  `binds: []` does not contain `"herdr"`;
- Herdr manager (Option B) → **no separate binding is composed**, because `binds: ["herdr"]`
  already contains it; the manager's own `create`/`remove` fulfil bind/release.

This is what stops Herdr launch from creating a second, redundant workspace once a manager
already produces one (the exact defect E48 documents in the current code: `_workspace()` calls
`workspace create --cwd`, a **plain**, unbound workspace — bh-5kg8w E6 — instead of adopting
whatever the configured manager already produced).

### Ownership, assigned once each

| Concern | Owner | Why |
| --- | --- | --- |
| Naming (branch/path policy) | `beadhive-worktrees` (Beadhive policy) | The manager only executes a spec Beadhive computed; never invents names or paths (epic design; E37 shows a manager cannot be trusted to protect intent even on an existing branch). |
| Safety (dirty/unmerged/unpushed/landed-rebased classification) | `beadhive-worktrees` safety classifier, exclusively | No probed manager has this logic — native and Herdr each expose only a generic dirty check (E15, E23/E32), which bh-5kg8w's Recommendation §7 and `bh-mr9tk.1` E39's gating counter-case both show is not a substitute. Classification is never delegated to a manager or binding. |
| Mechanics (create/attach/remove git-level effect) | The one selected `WorktreeManager` | Definitionally exclusive per hive (selection rule below). |
| Presentation (visible workspace/pane grouping) | The composed `WorkspaceBinding`, or the manager itself when `binds` already covers it | Composition rule above. |
| Cleanup (reconciling a stale/orphaned binding after an out-of-order remove) | Whichever component still holds the binding | E31's orphaned `"<label> (deleted)"` workspace is repaired by `workspace close`, never by `worktree remove --force` — the binding owner cleans up its own record; the manager never reaches into it. |
| Observation (cross-cutting notification, e.g. Repowise's `wt_creating`/`wt_created`) | A separate observer list, decoupled from the manager/binding contract | `bh-mr9tk.1` Recommendation item 4: observers stay a separate list, unaffected by which manager or binding is selected. |

## Selection

Explicit configuration, `worktrees.manager`, with `native` as the default. No auto-detection,
no plugin-registry-order race (retiring the current "ask the plugin first, native runs only if
the plugin reports `unhandled`" composition — E44 — which is exactly the shape that makes a
silent Option-B fallback possible today). Setting `worktrees.manager: herdr` selects the Option B
manager once it exists; until then only `native` is a legal value.

## Failure policy

- **Option A:** the selected manager (native) is authoritative. Herdr unavailable at bind time
  never rolls back or blocks a successful native `create`/`attach` — the claim is retained, and
  the missing binding is reported and re-bindable later (E33). A Herdr failure is always a
  presentation-layer gap, never a mechanics-layer one.
- **Option B:** Herdr unavailable at claim time is a **total** refusal — nothing is created, no
  worktree, no branch touched (E40). There is no native fallback, because Option B has no
  separate native step to fall back to; adding one back in would itself be the forbidden silent
  fallback / mixed-manager hive. No hive may have more than one manager selected at a time — this
  is enforced structurally by the single `worktrees.manager` config value, not by convention.

## Partial failure, crash recovery, and rollback

Both modes converge on **one reconciliation primitive**, `herdr worktree open --path <exact>`:

- **Option A**, crash between create and bind (Herdr down or killed mid-request): the native
  worktree exists, unbound; `open --path` re-binds it idempotently whether or not the server
  restarted in between (E33, E34).
- **Option A**, remove issued without release first: leaves an orphaned, unbound-but-labelled
  `"(deleted)"` workspace; the only working repair is `workspace close`, not a forced native
  remove of something already gone (E31).
- **Option B**, partial failure (git-level create lands, Herdr's own bookkeeping does not,
  because the manager process was killed mid-request): recovery is the **identical** `open
  --path` call Option A uses for its ordinary bind step — Beadhive needs exactly one repair path
  regardless of which manager is selected (E41).
- **Crash/restart, general case:** a clean create→bind→remove sequence is durable across a
  manager restart in both modes; only a sequence interrupted mid-step loses its binding, and only
  that binding — nothing else in the session is corrupted (E42).
- **Rollback:** neither option ever unwinds a successful git-level effect because a
  presentation-layer step failed. A failure is always recorded as a re-bindable/re-classifiable
  gap, never as a reason to undo work that already landed. This generalizes the "no silent
  fallback" principle to the crash case: recovery adds a missing binding, it never retroactively
  invents or removes a worktree.

## Teardown saga variant

`docs/design/lifecycle-owned-agent-herdr-space-contract.md` §"Completion and teardown contract"
already records the accepted saga:

```text
awaiting_authoritative_completion -> completion_accepted -> stopping_agents
  -> agents_stopped -> lease_verified -> space_closed -> cleanup_classified -> complete
```

with the physical order fixed as *stop agents → final lease read → close only the exact Space →
classify → one-target cleanup if `SAFE`/`LANDED_REBASED`*. `bh-mr9tk.1` E39 demonstrates this
order is correct **as written, unmodified, for Option A** — release (Herdr's `workspace close`)
and the native `remove` are two independent effects, so classification has a real gap to sit in
between them.

**Amendment required for Option B only:** `herdr worktree remove` fuses close-and-remove into one
call (`remove_releases_bindings: true`). E39 shows this is not a style question — once that call
returns, the working tree is gone and a `git status --porcelain`-based classification of it
becomes **permanently unobtainable**, not merely stale (`UNKNOWN (path unreadable)` in the
probe). A "close → classify → remove" order is therefore structurally impossible for Option B.
The Option B variant must be:

```text
… -> agents_stopped -> lease_verified -> classify -> remove -> complete
```

i.e. `classify` moves to immediately before the single fused `remove` call, and `remove`
performs the close as its own side effect instead of a separate `space_closed` step. Main/shared
checkout, dirty, unmerged, unknown, foreign, stale-generation, or reference-bearing classification
outcomes are retained exactly as today — only their position in the saga, and the fact that
`remove` now also closes the Space, changes. This ADR records the amendment; the actual doc edit
is a small, clearly-marked note added below (see "Amendment applied" at the end of this ADR) —
the file's dense narrative structure is otherwise left untouched, since Option B does not exist
yet and the base saga (Option A, and non-Herdr hives) is unaffected.

## Sequencing: Option A now, Option B deferred

File the **Option A implementation molecule now** (native manager in `beadhive-worktrees`, Herdr
binding replacing `_workspace()`'s `workspace create --cwd` call in launch, per `bh-mr9tk.1`
Recommendation items 1–5).

**Option B is deferred, not sequenced.** There is no numeric proof bar and no molecule scheduled
or planned for it. Option B is re-evaluated only if Option A shows problems that were not
anticipated in this decision — for example, if the E31-shaped orphaned-binding reconciliation or
the E34-shaped crash-recovery path from the "Partial failure, crash recovery, and rollback"
section above turns out not to hold up under real dispatch. Absent such a trigger, Option A is
the whole worktree-manager story indefinitely; nobody owes a follow-up planning pass on a timer
or a lifecycle count.

If that re-evaluation is ever opened, it starts from the Option B contract already recorded in
this ADR — the method shapes, the refusal-when-Herdr-unavailable policy, the no-silent-fallback /
no-mixed-manager-hive rule, and the `classify → remove` saga variant above — rather than
re-deriving them from scratch.

## Bead disposition

| Bead(s) | Status | Disposition under this decision |
| --- | --- | --- |
| `bh-7oo93.4`, `bh-7oo93.14`, `bh-7oo93.20`, `bh-7oo93.23` | closed | **Confirmed superseded, stay closed.** Each was already closed with a note flagging it as an "expected SUPERSEDE candidate" pending this ADR, because moving worktree mechanics behind the legacy `modules/worktrees` boundary first would have been a double move ahead of the `beadhive-worktrees` package extraction. This ADR confirms that judgment: the two-role contract lands in `beadhive-worktrees` directly (via `bh-xh8ku`/`bh-qdezo`'s later phases), so nothing in these four beads needs to be reopened or redone. |
| `bh-ym56t` (epic) | open | **Partially unblocked.** Its own note says Herdr implementation work was blocked on this decision so it would not encode the now-superseded "Herdr always owns mechanics" assumption. Non-worktree transport/bootstrap consolidation (below) is unblocked immediately; the two worktree-touching children need the amendment noted against them. |
| `bh-ym56t.1` | open | **Unblocked, informed.** Its note ties Herdr worktree compatibility to this decision; it should now assume native-default + Herdr-as-binding-only (Option A), not Herdr-as-mechanics-owner. |
| `bh-ym56t.5` | open | **Unblocked, amend.** Its note forbids decomposing Herdr worktree ownership before this decision. It may now proceed, but must decompose Herdr's worktree-adjacent code around the `WorkspaceBinding` role (bind/release) only — Herdr does not get a manager role unless a later Option B re-evaluation adopts it. |
| `bh-ym56t.17` | open | **Confirmed amend** (already tagged "expected AMEND" in its own note). Replace `_workspace()`'s `workspace create --cwd` call with `herdr worktree open --path <exact>` and thread the returned `workspace_id` onto `WorktreeHandle.bindings["herdr"]` (E48; `bh-mr9tk.1` Recommendation item 1). This edit belongs to the Option A implementation molecule filed from this decision, not to this ADR. |
| `bh-ym56t.2`, `.3`, `.4`, `.6`–`.16` (excl. `.5`), `.18`, `.19` | open | **Unblocked, unaffected.** Pure CLI/MCP/host-daemon/Frame Bridge transport and bootstrap consolidation with no worktree-mechanics content; proceed independently of this decision. |
| `bh-sy36q.1` | open | **Unblocked.** Its note already anticipates consuming "the beadhive-worktrees library package" as its worktree capability; this decision is exactly that selection (WorktreeManager + WorkspaceBinding, native default). It may proceed once its own dependency chain (`bh-l5sxi`, etc.) clears — no longer gated on an undecided worktree contract. |
| `bh-qdezo.4` | **closed, but not as merged/scaffolded** | **Correction to this bead's own acceptance-criteria assumption.** `bh-qdezo.4` was closed 2026-09-27 as *superseded*, not landed: the operator's 2026-09-27 replan of `bh-qdezo` split its scope into sub-epics, and `bh-xh8ku.3` ("Scaffold beadhive-worktrees with the pure worktree domain, the manager slot, and the native provider") carries **identical title, description, design, and acceptance criteria** and is still **open**. The real Option A dependency is **`bh-xh8ku`** (which superseded `bh-qdezo.4`), not `bh-qdezo.4` itself. Good news: `bh-xh8ku`'s own blocking dependency, the phase-0a package ADR sub-epic `bh-qo63b`, has already landed on `main` (see `chore(merge): molecule bh-qo63b` / `chore(merge): bead bh-qo63b.1` in this repo's recent history) — so `bh-xh8ku` is unblocked and ready to start. |
| `bh-qdezo.9` | open | **Dependency still to land, but can proceed independently.** The conformance kit (exact path, exact branch, attach, remove-keeps-branch, dirty refusal, handle round trip) exercises exactly the methods this ADR fixes, so its shape needs no amendment. It does not itself depend on this ADR's verdict and can be built in parallel with `bh-xh8ku.3`, but every Option A/B adapter (the native provider first, a future Herdr adapter later) must pass it before being considered conformant — sequence it to land at or before the point the Option A molecule's native manager is exercised in CI. |
| `bh-xh8ku` (epic), `bh-xh8ku.3` | open | **Matches this ADR's placement, ready to start.** `bh-xh8ku.3`'s existing acceptance criteria (worktree domain in `beadhive-worktrees`, `worktree.manager` slot on `beadhive-plugins`, native provider as built-in default, `beadhive.modules.worktrees` becomes a forwarding facade) already match this decision's placement exactly. Its note that "the plugin-first fallback seam... replacing it belongs to bh-mr9tk.2" is now resolved: replace it, per the Selection and Composition sections above and `bh-mr9tk.1` Recommendation item 2. No amendment to `bh-xh8ku.3`'s own shape is needed. |

## Placement

The `worktree.manager` capability slot and the `WorkspaceBinding` slot are declared on
**`beadhive-plugins`** (the stdlib-only capability-slot/binding-contract package). Their native
implementation — naming policy, `WorktreeSpec`/`WorktreeHandle` types, the safety classifier, and
the native Git manager — lives in **`beadhive-worktrees`**, which depends only on
`beadhive-plugins`. This matches the epic's `bh-qdezo` amendment and introduces no additional
provider-framework package and no generic event bus, per the epic design's explicit prohibition.

The Option A implementation molecule depends on `bh-xh8ku` landing the scaffold (see Bead
disposition above — the dependency named in this bead's original acceptance criteria,
`bh-qdezo.4`, was itself superseded by `bh-xh8ku`). Every Option A and, later, Option B adapter
must pass the `bh-qdezo.9` conformance kit before it is considered conformant; that kit can be
built in parallel and does not block on this ADR's verdict.

## Scope: no implementation filed here

Per the epic's own scope and this bead's acceptance criteria, this ADR files no beads. A
follow-up replan (`/bh:replan` on `bh-mr9tk`, in the planning seat) files the Option A
implementation molecule from the Recommendation items in `bh-mr9tk.1`, informed by the bead
disposition table above. No speculative Option B adapter beads are filed anywhere in this
molecule.

## Amendment applied

A short amendment note has been added to the top of
[`lifecycle-owned-agent-herdr-space-contract.md`](lifecycle-owned-agent-herdr-space-contract.md),
pointing here for the Option B `classify → remove` saga variant, without altering that document's
existing accepted Option A / non-Herdr saga text or structure.
