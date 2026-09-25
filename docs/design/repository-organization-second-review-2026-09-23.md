# Repository organization plan: second review

Review date: 2026-09-23 UTC, about 17:00. This is an advisory second opinion on the repository
organization workstream (`bh-cgfj0`) and on the first review,
[`repository-organization-review-2026-09-23.md`](repository-organization-review-2026-09-23.md).
It is not a lifecycle approval. No bead, source, test, or existing document was changed.

## Executive summary

**The architecture is right. The plan as filed can't reach the finish line the ADR sets, and
the first review's fix for that is the wrong way round.**

I independently re-checked the first review's seven findings. Every factual claim I tested
holds (details in section 2). Where I part ways is the remedy for the central problem, F1
(delivery ownership). The first review recommends keeping repository-wide completion as this
workstream's goal and filing the uncovered work before execution resumes. The numbers argue for
the opposite:

- **About 83% of production code is still at the package root.** The root holds 115,624 of
  139,050 production lines. The capability, kernel, adapter, integration, bootstrap, and testing
  packages together hold the other 23,426.
- **The filed leaves address at most about 43% of the root lines.** By my family-level
  allocation (section 3.1), roughly 57% has no filed move: 42% has no owner at all, and 15%
  only gets import-edge cuts in phase two.
- **The critical path is already about 32 serial leaves.** Each one needs its own validation,
  review, and merge. Two open external prerequisites (`bh-yml5h` and `bh-ecmmo.1`) sit in
  front of it.
- **All of that work accumulates on one container branch that lands on `main` only at the end.**
  Meanwhile `main` has had 387 commits in the last 14 days and touched 266 distinct package-root
  paths in the last 30.

Filing another roughly 60% of scope into the same long-lived container multiplies the part of
the plan that is already the riskiest. My recommendation:

1. **Land phase one on `main` now.** Today it merges cleanly: the two sides' changed files
   don't overlap, and the phase-one import checker and closure check both pass on the merged
   tree. Until it lands, the new-root guard doesn't protect the branch where most development
   happens.
2. **Narrow this workstream's finish claim to the filed increment:** config, work, worktrees,
   planning, transports, and Herdr. Make repository-wide completion a ratchet enforced on
   `main`, not a closeout condition of this container.
3. **Fix enforcement before the first move.** The first review's F2 and F3 are right, and I add
   three ratchets it doesn't cover: a forwarding-only check for facade bodies, a budget on
   boundary exceptions, and a zero-root-import rule for moved code.
4. **Decide the patch-seam policy explicitly.** Tests contain 2,411 `setattr` patches aimed at
   112 root modules. That is the dominant hidden cost of every move, and the current ordering
   pays it twice.
5. **Run one capability all the way through the ADR's definition of done first,** config being
   the obvious pilot. Measure its cost, then decide how many small leaves to fan out.

## 1. State at review time

Refreshed after the first review. The live bead reads and repository measurements were taken
around 17:00 UTC.

| Surface | Value |
| --- | --- |
| `main` | `925886bc` (first review saw `40f3b23a`) |
| Workstream container `wt/bead/epic/bh-cgfj0` | `cd80d16f`, clean worktree |
| Divergence `main...container` | **18** main-only / 14 container-only (first review: 16 / 14) |
| Files changed on both sides since the merge base | none |
| In-memory merge `main` + container | clean, tree `076a58e3` |
| Import checker on that merged tree | OK: 337 files, 3,028 edges, 8 owned cycles / 155 edges |
| Closure registry on that merged tree | OK: 24 present, 0 absent |
| Phase beads | `bh-50gsn` closed; `bh-31p2s`, `bh-7oo93`, `bh-ym56t`, `bh-4kmy0`, `bh-cgfj0.1` open and blocked |
| External blockers of phase two | `bh-yml5h` (open, itself behind open `bh-ywew7`); `bh-ecmmo.1` (open, behind `bh-068zo`) |

No new package-root Python file has appeared on `main` since the fork, so the frozen manifest is
still accurate. It won't stay accurate by itself.

## 2. Verdicts on the first review's findings

| Finding | My verdict | Notes from re-verification |
| --- | --- | --- |
| F1: root inventory has no delivery ownership | **Confirmed, and larger than stated** | All 202 legacy paths share one collective owner string. Section 3.1 quantifies the gap. My remedy differs; see section 4. |
| F2: checker doesn't model the new roles | **Confirmed, and already biting** | `_role` only recognizes `domain`, `contracts`, and `application` under `modules/`. Local adapters, `api.py`, capability `__init__`, and `testing` all resolve to `legacy`. Since `_direction_allowed` rejects every import of a `legacy` module, bootstrap can't wire a capability-local adapter without an exception (see N7). |
| F3: root guard and facade lifecycle are weaker than the ADR | **Confirmed** | `baseline_commit` is never read. The `legacy_implementation` list accepts additions. Only `active` facades are accepted. `check()` requires every facade path to exist, whatever its status. I add a body check (N2). |
| F4: no delivery foundation for Pants topology | **Gap confirmed; remedy should be lighter** | See section 4.2. |
| F5: cycle successors and the `.6 -> .15` edge | **Confirmed** | The transitive predecessors of `bh-31p2s.15` include every sibling except `.6`. The closeouts of the other three phases cover all their siblings. The cycle simulation in N6 sharpens this. |
| F6: MODULES tree mixes actual and intended layout | **Confirmed** (spot-checked) | The illustrative tree still shows `adapters/gateway`, `bootstrap/gateway.py`, and `kernel/schemas`. |
| F7: judgment-envelope amendment is parked on a closed bead | **Confirmed** | The `bh-50gsn.5` notes carry the operator decision to fold it in, and I found no disposition in the phase-one ADR. |

I also agree with the first review's positive assessments. The phase order is sound. Cutting
only the cycles each move needs is better than demanding zero cycles up front. The test audit's
"evidence, decision, then replan" boundary should stay.

## 3. New findings

### N1: scale. Most of the code isn't in the architecture yet

| Area | Python files | Lines |
| --- | ---: | ---: |
| Package root (`src/beadhive/*.py`) | 213 | 115,624 |
| `modules/` | 71 | 8,851 |
| `integrations/` | 13 | 7,194 |
| `kernel/` | 24 | 5,208 |
| `adapters/` | 8 | 1,590 |
| `testing/` | 2 | 481 |
| `bootstrap/` | 6 | 102 |

Because the checker skips direction rules for every `legacy` importer, **dependency direction
is enforced over about 17% of the code**. Governed packages import root modules through only
five edges: four from bootstrap and one from contracts, all ledgered. So the governed region is
genuinely clean, and the legacy region is unconstrained. Both `MODULES.md` ("implemented
modular architecture") and the workstream description ("the capability-first and
inward-dependency architecture already exists") are true of the governed region only. Anyone
sizing this workstream should start from the table above, not from the file counts.

#### 3.1 How much of the root the filed leaves actually address

I read every one of the 61 substantive leaves in `bh-31p2s`, `bh-7oo93`, and `bh-ym56t`.
Only 17 of the 212 non-marker root files are named literally; the rest are described by family.
So I allocated root files to filed work by family, generously. Any file in a family a leaf
plausibly covers counts as covered.

| Allocation | Files | Lines | Share of root lines |
| --- | ---: | ---: | ---: |
| Phase 3: work chain (`work*`, `schedule`, `claim_authority`) | 17 | 9,387 | 8% |
| Phase 3: worktree chain (`worktree*`, `wt_status`) | 7 | 6,473 | 6% |
| Phase 3: config chain (`config*`) | 14 | 3,528 | 3% |
| Phase 3: planning (`plan`, `plan_repair`, `planning_services`) | 3 | 1,673 | 1% |
| Phase 4: host daemon, `operator_*`, `daemon_*` | 24 | 15,300 | 13% |
| Phase 4: CLI, MCP, Frame Bridge, Herdr roots | 14 | 12,942 | 11% |
| **Subtotal: some filed move** | **79** | **49,303** | **~43%** |
| Phase 2 cycle cut only, no move filed (`bd`, `otel`, `hive`, `hub`, `hq`, `backup`, `registry`, `setup`, `run`, …) | 22 | 17,793 | 15% |
| **No filed owner** | **111** | **48,527** | **42%** |

The largest unowned files are `doctor` (2,817 lines), `localloop` (2,683), `contract_release`
(2,653), `release` (1,799), `safety` (1,659), `host_cli` (1,572), `dolt_health` (1,268),
`gateway_read` (1,173), `public_readers` (1,155), and `validation_ledger` (1,013). The unowned
families include release/contracts, host provisioning, lease, fence, and retirement,
validation records and admission, role execution and seat runs, dispatch, the Orca, Hitch,
Observaloop, and RepoWise runtimes, and state and agent readers.

This is a family-level estimate, not a symbol-level proof. It's still enough to show that
executing every filed leaf leaves on the order of 130 root implementation files behind: the 111
unowned files plus the 22 cycle-cut-only ones.

### N2: two "facades" are 3,800 lines of implementation

The root manifest classifies `work.py` and `worktree.py` as `public_facade`, and the ledger
registers them as active facades. Their bodies:

| File | Lines | Top-level definitions | Definitions over 10 lines | Ledger `preserved_symbols` |
| --- | ---: | ---: | ---: | ---: |
| `work.py` | 1,995 | 108 | 39 | 14 |
| `worktree.py` | 1,817 | 141 | 31 | 10 |
| `config.py` (for contrast) | 387 | 69 | 1 | 11 |

Decision 8 accepts any root file that is "an active registered facade". With the current
labels, the two largest implementation families already meet the root-ownership condition. The
fix is planned: `bh-7oo93.22` and `.23` reduce them. But nothing executable stops a closeout from
declaring success if those reductions stop short. `bh-7oo93.27` only asks in prose that "facade
bodies … match the ADR".

**Recommendation.** Reclassify both files as `legacy_implementation` until they are actually
reduced. Add a facade-body AST check: module-level imports, constants, aliases, and functions
whose bodies only delegate, with a small reviewed allowance for call-time collaborator lookup.
Check the defined public names against `preserved_symbols`. Put this in the same checker
follow-up as F3.

### N3: the container model amplifies drift and delays enforcement

Under AGF's `integration_base` climb, leaves land on their epic, epics land on the workstream,
and only the workstream lands on `main`. For this workstream that means:

- **The phase-one guard, ledger repair, and documentation fixes aren't on `main`.** The root
  guard exists precisely to stop new root files, yet it doesn't guard the branch where daily
  development happens. Any root file added on `main` in the meantime becomes a merge-time
  "unowned package-root implementation" error, discovered weeks later by whoever integrates.
- **The serial critical path is long.** The longest chains of blocking edges are 5 leaves in
  phase two (`.1 → .3 → .11 → .13 → .15`), 12 in phase three (the config chain
  `.1 → .2 → .6 → .9 → … → .26 → .27`), 9 in phase four (the Herdr chain
  `.1 → .5 → .10 → .14 → … → .18 → .19`), 5 in phase five, plus the closeout. That totals
  about 32 sequential leaves, several requiring `just check-all`, before anything reaches
  `main`.
- **`main` moves fast on exactly these files.** In the last 30 days, the most-touched root
  files include `herdr_plugin` (33 commits), `cli` (27), `worktree_verify` (18), `worktree` (16),
  `host_daemon` (15), `herdr_views` (14), `work_logic` (12), and `work` (10). All of them are
  scheduled moves. Pace varies: there were 203 commits in the last 7 days, but only 18 since the
  fork two days ago.

Physically moving files is the change most sensitive to concurrent edits. A move conflicts with
every edit to the moved code, not just edits to overlapping lines. Holding 61 such changes on
one branch for weeks is the plan's largest schedule risk, and neither the plan nor the first
review addresses it.

**Recommendation.** Let the phase epics land on `main` one by one. One way is to make them
dotless epics linked by `blocks` edges, keeping `bh-cgfj0` as a tracking parent in name only.
Another is to finish each phase epic up the chain as it closes. At minimum, land phase one now,
while the merge is clean and checker-green. This changes how the workstream uses AGF, so it's an
operator decision (section 5, D1).

### N4: patch-seam preservation is the dominant cost, and the ordering pays it twice

Tests contain 3,855 `setattr` patch calls; **2,411 target 112 package-root modules**. The
biggest targets are `bd` (288), `config` (230), `herdr_plugin` (205), `otel` (165), `registry`
(158), and `worktree` (135).

Three rules in the plan combine badly:

1. Governed code may not import root modules. `_direction_allowed` rejects every `legacy`
   target, including from bootstrap and adapters.
2. Phase three must "preserve every … patch point" until a consumer ledger proves removal safe.
3. Test refactoring is deferred until after the `bh-4kmy0` audit.

With all three in force, moved code can't honor a test patch on `beadhive.bd` directly. The only
way to keep the seam is for the root facade to look collaborators up at call time and pass them
inward. `config.py` already does this ("pass this module as a collaborator"), and `bh-7oo93.17`
plans the same for `worktree.py`. That works, but it makes each facade the de facto composition
point for its family. That sits awkwardly with Decision 6's "no process-wide assembly" clause.
Then, after the audit, many of those same seams are rewritten again to target ports and fakes.

**Recommendation.** Before phase two starts, decide a seam policy per family:

- **Supported seam:** a documented or plausibly external patch target. Preserve it through
  facade-side call-time lookup, and state in Decision 6 that this wiring is allowed.
- **Internal test seam:** retarget the test to the new owner's port or fake in the same change
  as the move. Treat it as consumer migration, which Decision 5 already permits for internal
  callers, not as audit-gated test refactoring.

This moves only the mechanical retargeting forward. Deciding which tests to delete stays with
the audit.

### N5: moves only pass if each family depends on nothing at the root

Code that moves from the root (unchecked) into `modules/<capability>/application` (checked)
immediately becomes subject to the rule against importing `legacy`. The fan-out of the
scheduled families to other root modules is large: `work` 41, `cli` 63, `mcp` 23,
`herdr_plugin` 20, `worktree` 19, `plan` 12. The low-level modules everything depends on have
large fan-in: `config` 76, `bd` 37, `log` 23, `otel` 21. Phase three's config chain moves
`config`; the other three have no filed move.

Every movement leaf therefore has to either inject a port for each root collaborator or add a
boundary exception. The ADR forbids hiding debt "by broadening an exception", but the checker
accepts any well-formed new `boundary_exception` row. Nothing mechanical holds the budget of six.

**Recommendation.** Make two acceptance criteria shared by every movement leaf:

- Moved code has **zero imports of package-root modules**.
- The count of active boundary exceptions **never increases**, except through an ADR
  amendment. The checker should enforce this against a recorded baseline count and ID set, the
  same way it already pins the cycle digest.

Then name the owners for the high fan-in collaborators (`bd`/engine, `otel`, `log`) as ports
early. Phase two's `.5`, `.7`, and `.10` start this work, but only for the edges that are cycles.

### N6: the cycle ledger is a complete cut set; three edges have no owner

This is a positive check that the first review didn't make. I removed the 38 active cycle
exception edges from the live import graph, and **all eight SCCs disappear**. The ledgered edges
form a complete feedback-arc set, so "zero active cycle exceptions" really does imply "zero
cycles" at today's graph.

Simulating the filed plan in order:

| After | Remaining SCCs |
| --- | --- |
| Phase 2's 25 cuts (every active edge whose planned removal is in phase two) | 5: the worktree cluster (9 modules, including `converge`, `observaloop`, `otel`, `validation_*`), host/MCP (8), `work`/`work_show`, `localloop`/`runtime`, `herdr_plugin`/`herdr_views` |
| All filed cuts through phase 4 | 2: `localloop`/`runtime`, and host/MCP (7 modules: `alerts`, `doctor`, `host_cli`, `host_daemon`, `host_provision`, `host_retire`, `mcp`) |

The two survivors are exactly the unassigned edges `cycle-edge-006`, `007`, and `026` that F5
names. Without an owner for them, `bh-ym56t.19` can't meet "composition-root cleanliness". The
simulation also shows phase three moving worktree families while `worktree` still sits in a
9-module cycle. `bh-7oo93.17` intends this ("after the worktree families already moved"). It's
safe only if N5's zero-root-import rule applies to each of those moves.

### N7: the one existing capability API exports concrete adapters

`modules/worktrees/__init__.py` re-exports `NativeGitWorktreeProvisioner`,
`PluginWorktreeProvisioner`, and `CallbackWorktreeInventory` in `__all__`. Importing the
capability's public entry point therefore imports concrete Git and plugin effects. That
contradicts Decision 3, under which a local adapter is not itself a public surface. Because
capability `__init__` files and local adapters are both classified `legacy`, the checker can't
see it. And once bootstrap needs to wire these adapters, the `legacy` classification forbids it.

**Recommendation.** In the F2 amendment, state explicitly that a capability root exports
contracts, domain types, and use-case entry points. Concrete adapters are exported only from
`modules/<capability>/adapters` and imported only by bootstrap or composition code. Fix
`modules/worktrees` when the checker learns the new roles.

### N8: stale and contradictory records

- **Facade expiry triggers name closed beads.** `facade-config-schema`, `-partition`, `-store`,
  and `-policy` still say they expire when `bh-18hud.5` and `bh-18hud.6` finish. Both are closed,
  yet the facades are correctly still active. Phase one repaired the successors but not the
  triggers, so the triggers now read as already satisfied. The first review raised the same
  problem for the cycle records' `bh-inqwc` wording. It applies to the facade records too.
- **The baseline and the ledger disagree on overlap dispositions.**
  `repository-physical-layout-baseline.md` says to *adopt* `bh-62rm` and *exclude* `bh-7ks1c`,
  `bh-nc0p`, and `bh-gfvt`. The ledger's `overlap_disposition` says *exclude* `bh-62rm` and
  *depend* on the other three. The baseline also defers `bh-vylp0` to its spike, while the ledger
  excludes it. The baseline is historical evidence, so it shouldn't be edited. But `bh-7oo93.1`
  should cite the ledger as authoritative and resolve `bh-62rm` against the current `plan.py`,
  which has 1,549 lines. The ledger's stated rationale, "the former workspace plan
  implementation", may not match the file as it is today.

## 4. Assessment of the first review's proposed amendments

| First review's proposal | My position |
| --- | --- |
| Exhaustive 212-row mapping (per path or symbol) before implementation resumes | **Adopt, with changes.** Don't create a new artifact. Add `destination_owner`, `role`, and `delivery_bead` (or the literal `unallocated`) columns to the existing `root-module-ownership.toml`, one row per file. Symbol-level mapping belongs in each family's own inventory leaf, not up front. The checker reports the unallocated count, which must be zero only for a global-completion claim. This takes hours rather than a planning phase, and it gives the operator the scope decision in numbers. |
| New build-topology prerequisite before the first move | **Adopt, lighter.** See 4.2. |
| Amend the direction ADR (local adapters, API modules, `testing`, export semantics) | **Adopt.** Add N7's export rule. |
| Status-aware facade lifecycle, frozen root allowances | **Adopt,** plus the facade-body check (N2) and the exception budget (N5). |
| Add `.6 → .15`; allocate `006`, `007`, and `026`; refresh cycle destinations and expiry wording | **Adopt.** Refresh the facade triggers too (N8). |
| One shared acceptance checklist referenced by every movement leaf | **Adopt, and make it checkable.** Leaves sized for Luna need checks that tooling can verify. The nine-row checklist should compile down to a handful of commands (architecture check, facade-body check, exception budget, mapping check, focused closure, full gate), not prose each leaf re-interprets. |
| `bh-cgfj0.1` requires zero exceptions and zero root debt if it claims global completion | **Agree with the condition,** and recommend that it not claim global completion (D2). |
| Keep the developer-feedback block unless its owner changes it | **Agree.** I'd ask more directly: `bh-ecmmo.1` sits behind a human gate and `bh-068zo`, and nothing in phase two is a feedback-loop change. |

### 4.1 Where I'd take the plan instead

Order of work, replacing "repair the whole plan, then resume":

1. **Land phase one on `main`** (D1). Cheap today.
2. **Enforcement epic** (one small epic, before `bh-31p2s.1`):
   - role table for local adapters, API entry modules, and `testing` (F2, N7);
   - status-aware root guard pinned to its baseline (F3);
   - facade-body check, and reclassify `work.py` and `worktree.py` (N2);
   - boundary-exception budget (N5);
   - allocation columns plus an unallocated count (section 4 table).
3. **Seam-policy decision** (N4, D3). Record it once in Decision 6 rather than per leaf.
4. **Pilot: config, end to end.** It is the smallest scheduled family (3,528 root lines), the
   most already migrated, and the lowest in the dependency graph. Run it through the ADR's full
   definition of done: exports, facade bodies, role-scoped targets, mapping check, seam
   retargeting, and installed-artifact proof. Record the elapsed time and validation cost.
5. **Re-estimate from the pilot,** then fan out the work, worktree, and transport chains. If the
   pilot shows each small leaf costs a full gate plus review for little movement, merge adjacent
   leaves in the same chain rather than weakening gates.
6. **Test audit** as planned, on whatever tree the narrowed increment produces.

### 4.2 Pants topology: keep Decision 7's goal, lighten its mechanism

`src/beadhive/BUILD` is still one `python_sources(name="lib", sources=["**/*.py"])`. The first
review is right that nothing filed delivers Decision 7. But Decision 7 asks for a
repository-owned mapping with exact addresses for seven categories per owner, plus a check that
every import edge has a matching target dependency. Pants already infers dependencies from
imports. A stored mapping would duplicate that graph and drift from it.

Satisfy the decision with **property checks over the live Pants graph** instead of a stored
address list:

- every source and test file is owned by exactly one target, and that target's owner/role tags
  match its path's role;
- no target's sources span two owners;
- every closure selector in `tests/closures.toml` resolves to existing addresses;
- explicit non-import inputs (schemas, fixture plugins, templates, generated assets) are declared
  and resolve;
- the stateful fixture and harness targets are reachable only from targets tagged stateful.

Store only what inference can't know: tags, non-import inputs, and cross-owner scenario owners.
This still gives Decision 7's "resolve a capability-role target and enumerate its complete test
subset from one checkout". And it turns the "every movement leaf updates the mapping" burden into
"every movement leaf keeps the property check green".

## 5. Operator decisions

| # | Decision | My recommendation | First review's position |
| --- | --- | --- | --- |
| D1 | Should phases land on `main` one at a time instead of accumulating in `bh-cgfj0`? | **Yes. Land phase one now,** and restructure so each phase epic lands when it closes. | Not raised; it treats container-only landing as expected staging. |
| D2 | Completion scope | **Narrow this workstream to the filed increment.** Record repository-wide completion as a ratchet on `main`: frozen root, allocation columns, non-increasing budgets. Unowned families (about 42% of root lines) get their own later molecules. | Keep repository-wide completion; file uncovered work before execution. |
| D3 | Patch-seam policy | Supported seams keep facade-side call-time lookup, explicitly allowed in Decision 6. Internal test seams are retargeted in the same move. | Not raised as a policy question. |
| D4 | Public API granularity | As the first review says, plus: a capability root never exports concrete adapters. | Preserve existing API modules; explicit cross-owner exports. |
| D5 | Facade retention, the `bh-62rm`/`bh-bo5` overlaps, the judgment envelope, developer-feedback ordering | Agree with the first review's framing of all four. | As in its section 7. |

If the operator chooses repository-wide scope anyway (rejecting D2), then the first review's
advice to file everything before resuming stands. In that case D1 becomes more urgent, not less.

## 6. Method, evidence, and limitations

**Sources read:** the first review; the phase-one ADR, baseline, root manifest, exception
ledger, checker, and `MODULES.md` at container `cd80d16f`; `docs/AGF.md` on `main`; the live
records for all 99 child beads of `bh-cgfj0`, `bh-50gsn`, `bh-31p2s`, `bh-7oo93`, `bh-ym56t`,
and `bh-4kmy0`, plus the referenced external beads (via `bh work issue --json` and
`bh work list --all --limit 0 --include-gates --json`).

**Computations** (all local and read-only; scratch output wasn't kept in the repository):

- line and file counts per top-level area, from `wc -l` over the container tree;
- facade body shape, from a standard-library AST walk counting top-level definitions and
  definitions over 10 lines;
- import graph, role classification, fan-in/fan-out, and SCCs, from the phase-one checker's own
  `collect_imports` and `_cyclic_edges`. Cut simulations remove the ledger's exact
  `(importer, imported_module)` pairs;
- blocking-edge closures and critical paths over the live bead JSON, ignoring `parent-child`;
- test patch targets, from an AST walk over `tests/`. It resolves `setattr` first arguments that
  are root-module aliases, `"beadhive.<root>..."` strings, or `beadhive.<root>` attributes.
  Patches through other indirections aren't counted, so 2,411 is a lower bound;
- churn, from `git log --since` over `src/beadhive/*.py` on `main`;
- merge viability, from `git merge-tree --write-tree main wt/bead/epic/bh-cgfj0`, with the
  checker and closure registry run on an extracted copy of the resulting tree in scratch space.

**Confidence.** High: the counts, graph simulations, checker behavior, bead ordering, and the
merge result as of the time read. Moderate: the family-level allocation in section 3.1. Leaves
describe families in prose, so another reader could allocate a few families differently, but not
enough to change the conclusion. My recommendations in D1 through D3 are judgment calls for the
operator, not facts.

**Not done:** no `just check`, `just check-all`, pytest, Pants resolution, installed-package, or
timing run. So nothing here establishes the actual per-leaf validation cost; the pilot in 4.1
exists to measure it. No remote fetch was performed. Live bead state can change after this read.

Representative reproduction, from the container worktree:

```sh
git rev-list --left-right --count main...wt/bead/epic/bh-cgfj0
git merge-tree --write-tree --name-only main wt/bead/epic/bh-cgfj0
wc -l src/beadhive/*.py | tail -1
PYTHONDONTWRITEBYTECODE=1 python3 scripts/check_import_boundaries.py
bh work list --all --limit 0 --include-gates --json
bh work issue bh-31p2s.15 --json
```
