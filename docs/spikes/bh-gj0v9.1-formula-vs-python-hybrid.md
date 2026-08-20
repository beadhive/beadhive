# Spike `bh-gj0v9.1` — formula vs `onboard.py`'s Python step DAG

> Can a formula express `onboard.py`'s step DAG, and is a wisp-tracked operational run
> worth the double-bookkeeping?

**Bead:** `bh-gj0v9.1` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-gj0v9` — SPIKE: does beadhive need beads' formula/wisp at all, given
Guide is already its adopted operational-workflow substrate?

Artifact: [`bh-gj0v9.1-mol-onboard-hive.formula.json`](bh-gj0v9.1-mol-onboard-hive.formula.json)
— the real formula authored and run for this spike. It is a spike artifact only: it lives in
`docs/spikes/`, not on any `bd formula` search path, and nothing in `src/` references it.

## Question

Given the epic's already-settled finding that **a formula never executes anything** (a `Step`
has no `action` / `script` / `command` field, so the Python must keep doing the work), is there
real value in using a formula + wisp to *materialize tracking beads* for an operational run —
specifically `src/beadhive/onboard.py`'s step DAG, the strongest candidate in the repo?

Critically NOT asking: whether a formula could *run* onboarding (settled: no), nor whether
formula/wisp is useful anywhere in beadhive at all (that is spike `c` / the joining decision
bead). This spike asks only whether the *hybrid* — declarative formula for the DAG, Python for
the doing — beats today's status quo by enough to justify maintaining two descriptions of one
DAG.

## Method

1. Read the executor and DAG in full: `src/beadhive/onboard.py` — `Step` (`:89-106`),
   `_topo_order` (`:194`), `_evaluate` / `_gate` / `_run_action` (`:217-272`), `run_onboard`
   (`:274`), `build_steps` (`:1328-1562`), `_plugin_step` (`:1233`), `_act_bd_init` (`:730`),
   `_run_bd_mint` (`:699`).
2. Dumped the full formula schema — all 18 exported structs — with `bd formula schema <name>`
   (`bd version 1.1.0 (dev)`), looking for any execution, branching, or rollback primitive.
3. Authored a real `mol-onboard-hive.formula.json` transcribing `build_steps()` node-for-node
   (19 steps, every `requires` edge, every flag-shaped `enabled` predicate as a `condition`).
4. Stood up a scratch hive (`bd init --prefix spk`, embedded Dolt) under the session scratchpad
   — deliberately **not** this repo's hive — and ran `bd formula list` / `show` / `bd cook`
   (compile + runtime) / `bd mol seed` / `bd mol wisp` / `bd dep tree` / `bd mol squash` /
   `bd mol burn` against it. This is the first live exercise of the wisp path in this
   workspace; the epic predicted it might not work first try, and two failures below are
   recorded as evidence rather than worked around.
5. Wrote two throwaway probe formulas (`mol-probe-hard`, `mol-probe-cond`, `mol-probe-tri`) to
   isolate each hard construct: compound `condition` expressions, `loop` with a variable range,
   and `on_complete.for_each` runtime fanout.
6. Compared against the two negative cases the epic named: `src/beadhive/doctor.py` and the
   `local` patrol loop (`docs/design/runtime-tiers-molecule.yaml:141-179`).

## Evidence

### A. The static skeleton *is* expressible — and it works better than expected

**1. The 19-node DAG parses and renders intact.** `bd formula show mol-onboard-hive` reproduced
every node and every edge from `build_steps()`:

```console
🌲 Steps (19):
   ├── resolve: resolve triplet {{hive}}
   ├── clone: clone {{hive}} if absent [needs: resolve]
   ├── identity: workspace identity [needs: clone]
   ├── classify: classify hive [needs: identity]
   ├── prefix: derive prefix [needs: classify]
   ├── worktree-clean: working tree clean [needs: identity]
   ├── bd-init: bd init ({{hive}}) [needs: prefix, worktree-clean]
   ├── register: register hive [needs: bd-init]
   ...
   ├── hub-sync: sync hub [needs: register, claude, agents, skills, opencode, codex, observaloop]
   └── footprint: settle declared footprint [needs: register, claude, ..., hub-sync]
```

**2. `condition` filtering and dangling-edge repair match `_topo_order`'s semantics exactly.**
This is the single most positive result. `onboard.py:194-208` documents that the sort "only
counts `requires` edges to steps that are *present* in this set, so filtering out disabled steps
never deadlocks the sort". Beads does the same thing:

```console
$ bd mol wisp mol-onboard-hive --var hive=github/beadhive/beadhive --var target_exists=true --var claude=true
✓ Created wisp: 14 issues
  Root issue: spk-wisp-ola
```

19 steps → 13 children + 1 root: `clone` dropped (`!{{target_exists}}` false) and the five
un-flagged installers dropped. The critical check is `identity`, whose only edge was to the
now-absent `clone`:

```console
$ bd dep tree spk-wisp-bvd
spk-wisp-bvd: workspace identity [P2] (open) [READY]
    └── spk-wisp-ola: mol-onboard-hive [P2] (open) [parent-child]
```

The edge to the filtered step vanished and the node is `READY`, not deadlocked. `hub-sync`
likewise kept only its surviving edges (`register` + `install .claude`), dropping the five
filtered installers. No hand-holding required.

**3. `{{var}} != value` handles the tri-state `hub_sync` correctly.** `onboard.py:1516` gates
hub-sync on `c.hub_sync is not False` — a three-valued flag (`None` = deferred default, `True`,
`False`). `condition: "{{hub_sync}} != false"` reproduced it: the step materialized for
`hub_sync=unset` (3 issues) and `hub_sync=true` (3 issues), and was filtered for
`hub_sync=false` (2 issues).

### B. `bd cook` is not the filtering stage, and needs a database

**4. `--mode=runtime` substitutes titles but does **not** evaluate conditions.** Despite `bd cook
--help` describing runtime mode as "a fully-resolved proto with variables substituted", the
cooked JSON still carried all 19 steps with their raw condition strings:

```console
  clone  | needs= ['resolve'] | cond= !{{target_exists}} | title= clone github/beadhive/beadhive if absent
  claude | needs= ['register'] | cond= {{claude}}        | title= install .claude
```

`{{hive}}` was substituted in titles; `{{claude}}` inside a `condition` was not. Filtering is a
pour/wisp-time step (`FilterStepsByCondition`), so `cook` output cannot be used to preview the
real step set.

**5. `bd cook` requires a beads database even in its documented ephemeral mode.** Against a
directory with a formula but no store: `Error: no beads database found`. So "inspect the
resolved formula as JSON to stdout" is not a standalone operation.

### C. The three hard constructs

**6. `Step.enabled` membership — EXPRESSIBLE WITH CAVEAT.** Schema field considered:
`Step.condition` (`Formula` → `Step.condition`; grammar `{{var}}` / `!{{var}}` / `{{var}} ==
value` / `{{var}} != value`, "Evaluated at cook/pour time via FilterStepsByCondition").

The grammar is strictly single-term. Compound expressions are rejected outright:

```console
$ bd mol seed mol-probe-cond
Error: formula "mol-probe-cond" not accessible: filtering steps by condition:
  step "compound-and": invalid step condition format:
  "{{target_exists}} && !{{cloned}}" (expected {{var}} or {{var}} == value)

$ bd mol seed mol-probe-cond    # after removing the && step
Error: ... step "compound-or": invalid step condition format:
  "{{target_exists}} || {{cloned}}" (expected {{var}} or {{var}} == value)
```

Measured against onboard's eight distinct membership predicates:

| predicate | site | expressible as `condition`? |
|---|---|---|
| `c.claude` … `c.observaloop` (6 installers) | `onboard.py:1467,1475,1483,1491,1499,1507` | **yes** — plain scalar flags |
| `c.hub_sync is not False` | `:1516` | **yes** — `{{hub_sync}} != false` (evidence 3) |
| `not c.target_exists` | `:1356` | **only after Python probes the filesystem** and passes a bool |
| `registry.find_entry(cfg, provider, org, repo) is None or c.force` | `:1393` | **no** — a registry I/O call `or`'d with a flag; must collapse to one bool in Python |
| `_p.name in c.plugins or _p.enabled(c.cfg, c.existing)` | `:1255` | **no** — compound, and arbitrary per-plugin Python |
| `unclean_applies` = `c.target_exists and not c.cloned and (c.base/".git").exists()` | `:1339-1341` | **no, and not at any time** — `ctx.cloned` is only assigned *mid-Phase-A*, after the clone step runs (`run_onboard`, `:300-303`). `condition` is evaluated once, before any step exists. |

The last row is the structural one: onboard's check-applicability predicates depend on state
produced by an earlier step in the same run. A formula's `condition` is a pour-time constant.

**7. Runtime plugin-registry node generation — NOT EXPRESSIBLE.** Schema fields considered:
`Step.loop` (`LoopSpec.count` / `.until` / `.range`), `Step.expand` / `expand_vars`
(`ExpandRule`), `Step.on_complete` (`OnCompleteSpec`).

`onboard.py:1544` builds one node per registered plugin at call time:
`plugin_steps = [_plugin_step(p) for p in _plugins.registry() if p.on_onboard is not None]`.

- **`loop` with a variable range does not work.** `LoopSpec.range` documents "Variables:
  `{start}..{count}` (substituted from Vars)". Both spellings fail:

  ```console
  $ bd cook mol-probe-hard --mode=runtime --var plugin_count=3   # range "1..{{plugin_count}}"
  Error: applying control flow: applying loops: loop "plugin-loop": invalid range
    "1..{{plugin_count}}": invalid end expression: unexpected character '{' in expression

  $ ... # range "1..{plugin_count}"
  Error: ... evaluating range end "{plugin_count}": unexpected character '{' in expression
  ```

  A **literal** range works and expands at cook time:
  `plugin-loop.iter1.plugin-run` / `.iter2.` / `.iter3.`. So the iteration count must be baked
  into the formula file — which is exactly the thing the plugin registry makes dynamic.
- **`on_complete.for_each` is inert.** `OnCompleteSpec.for_each` reads `output.<field>` from a
  step's output. A bead has no output field, and closing the step produced no fanout: the wisp
  count went from 5 to 4 (the closed step) with no bonded molecule created. The schema notes
  `GateRule` conditions are "evaluated at runtime by the patrol executor" — no such executor is
  reachable from the `bd` CLI here.
- **`expand` / `ExpandRule`** target a named step with a named expansion formula. Both names are
  static; neither is driven by runtime data.

**8. Probe-branching and rollback — NOT EXPRESSIBLE, confirmed against all 18 structs.**
`_act_bd_init` (`onboard.py:730-825`) branches three ways on live filesystem/remote probes —
furnished `bd init --shared-server`, `bd bootstrap` from `origin refs/dolt/data`, or
`bd init --setup-exclude` — and `_run_bd_mint` (`:699-728`) runs a **compensating cleanup**
(`hive.cleanup_failed_bd_init`) on failure so a retry is not blocked by wreckage.

Fields considered, and what they are instead:

- `BranchRule` (`from` / `steps` / `join`) is a **fork-join for parallelism** — all branches run;
  it is not a selection.
- `Step.gate` / `Gate` (`type`: `gh:run` / `gh:pr` / `timer` / `human` / `mail`) is an async
  *wait*, not a branch.
- `AdviceRule` / `AroundAdvice` insert steps around a target; `AdviceStep` has `args` and
  `output` maps — the closest thing to execution anywhere in the schema — but they are opaque
  `map[string]string`, not commands.
- Searching all 18 structs (`Formula`, `Step`, `VarDef`, `LoopSpec`, `ExpandRule`, `MapRule`,
  `OnCompleteSpec`, `Gate`, `GateRule`, `BranchRule`, `BondPoint`, `ComposeRules`, `Hook`,
  `Pointcut`, `AdviceRule`, `AdviceStep`, `AroundAdvice`, `WaitsForSpec`) turns up **no**
  `rollback`, `compensate`, `undo`, `retry`, or `on_failure` field. The epic's prediction is
  confirmed: there is no rollback primitive.

### D. What the materialized wisp is actually worth

**9. Wisp issues are invisible to `bd list` and `bd ready`.** With 8 open wisp issues live in the
scratch hive:

```console
$ bd mol wisp list
Wisps (8):

$ bd ready
✨ No open issues

$ bd list
No issues found.
```

This kills the strongest hypothetical payoff. The reuse argument for materializing an
operational run as beads is "beads IS the state" — but a wisp is not on the ready surface, so
`bh work ready` / `bd ready` / any dispatch loop cannot see it. It is a private side-table that
happens to be stored in the issues table.

**10. The durable output of a run is one prose markdown blob, not queryable rows.**

```console
$ bd mol squash spk-wisp-ola
✓ Squashed molecule: 13 children → 1 digest
  Digest ID: spk-4z3
  Deleted: 13 wisps
```

The digest (`bd show spk-4z3`) is a `## Molecule Execution Summary` markdown body listing
`**[open]** set host node_id`, `**[open]** register hive`, … — in **storage order, not
topological order**, with no per-step timing and no failure detail. `bd mol burn` deletes
without a digest. So a wisp-tracked onboard run yields either nothing, or a paragraph that
records less than `OnboardPlan` already holds in memory (`onboard.py:109-126`: `steps_run`,
`checks`, `skipped_checks`, `cloned`, `reconfigure`, …).

**11. Two `bd` defects found while doing this (recorded, escalated, not worked around):**

- `bd mol wisp <formula>` reports a **misleading** `Error: '<name>' not found as formula or
  proto` when the real failure is a formula validation error. `bd formula list` listed the
  formula and `bd mol seed` gave the actual message (evidence 6). Diagnosing this cost more
  than the experiment it blocked.
- `LoopSpec.range` variable substitution is documented but non-functional (evidence 7).

### E. The negative cases hold

**12. `doctor.py` is not formula-shaped: no edges to declare.** `doctor()`
(`doctor.py:2350-2384`) is 20 flat `_render_*(data["<key>"])` calls over a dict assembled by
`_collect`, keyed by section. There is not one ordering constraint between sections — reorder
them and the report is equally correct. A formula's entire contribution is `depends_on` /
`needs`; against a step list with zero edges it contributes nothing but a second copy of the
list. The sections are also pure reads (`_section_dimensions` … `_section_observability`,
`:217-1401`), so there is no failure to gate and no mutation to roll back either.

**13. The `local` patrol loop is not a finite step list at all.**
`docs/design/runtime-tiers-molecule.yaml` is banner-marked `# SNAPSHOT — not the current plan`
(line 1), and the tier is five lines of pseudocode (`:150-154`): `while True: bd gate check;
bd reclaim; spawn role binaries for newly-ready beads; sleep(interval)`. A formula materializes
a *fixed* set of beads at pour time. An unbounded loop whose fanout is `bd ready`'s result on
each tick, with SIGTERM-driven checkpoint/unclaim (`:167-169`), has no finite materialization.
`LoopSpec` even requires `max` whenever `until` is set, "to prevent unbounded loops" — the
schema explicitly refuses this shape.

### F. The drift cost is measurable

**14. The Python DAG is already guarded by 813 lines of tests; a formula copy would be guarded
by nothing.** `tests/test_onboard_dag.py` (813 lines) asserts execution order
(`test_existing_clean_folder_runs_full_dag_in_order`), per-flag installer gating, the
gate-before-`bd-init` ordering, `--skip-check` downgrades, dry-run mutation-freedom, and
fresh-clone check applicability. Every one of those assertions is against `build_steps()`. A
parallel `.formula.json` would have to restate ~19 nodes and ~30 edges with no test that fails
when the two disagree — and per evidence 6, its condition set is only a *lossy projection* of
the Python predicates, so exact equivalence is not even checkable in principle.

## Verdict — **NO-GO**

**NO-GO.** The formula can express onboard's static skeleton — genuinely well (evidence 1-3);
dangling-edge repair on condition-filtered steps matches `_topo_order` semantics for free. But
the blocker is that **every runtime-dependent construct must be resolved in Python *before*
`cook`**, which means the Python already knows the full step membership and order at the moment
it would hand them to the formula (evidence 6-8). The formula is therefore never the source of
the DAG; it is a downstream copy of a decision Python has already made.

And the copy buys nothing measurable: the materialized wisp is invisible to `bd list` / `bd
ready` (evidence 9), so it is not reusable dispatch state, and its only durable trace is an
unordered prose digest that records strictly less than the in-memory `OnboardPlan` (evidence 10)
— against 813 lines of existing tests that would silently stop covering the real DAG description
(evidence 14). That is the definition of double-bookkeeping with no payoff.

The two counterexamples bound the claim from the other side (evidence 12-13): `doctor.py` has no
edges for a formula to own, and the patrol loop has no finite step list to materialize. So
onboard.py — the *best* candidate in the repo, and the only Python in it already written as a
declarative DAG — is also the high-water mark. Nothing weaker clears the bar.

## Recommendation

1. **Close question (a) of `bh-gj0v9` NO.** No implementation bead should be filed to convert
   `onboard.py`, `doctor.py`, `release.py`, or the patrol loop to a formula, and none should be
   filed to wisp-track an operational run. Fold this into the epic's ADR rather than a separate
   record.
2. **Do not touch `onboard.py`.** Its hand-rolled `Step` + Kahn sort + two-phase gate-then-mutate
   executor is doing three things the formula schema has no field for at all — executing,
   branching on a live probe, and compensating on failure — and one thing it does *better*:
   `Step.enabled` is arbitrary Python evaluated against a live `Ctx`, where `condition` is a
   single-term pour-time constant.
3. **If run telemetry is what was actually wanted, take it from `OnboardPlan`, not from beads.**
   `run_onboard` already returns `steps_run` (the topological plan), `checks`, `skipped_checks`,
   `cloned` and `reconfigure` (`onboard.py:109-126`). One structured line through the existing
   `jsonout` envelope is strictly more data than a squash digest, costs no second DAG
   description, and is already covered by the tests above. That is a one-bead change if anyone
   wants it — not a substrate adoption.
4. **The two `bd` defects (evidence 11) are filed via `bh escalate`**, and are worth reporting
   upstream regardless of this verdict: `bd mol wisp`'s "not found as formula or proto" masking
   a validation error, and `LoopSpec.range` variable substitution being documented but broken.
5. **Carry one caveat to the joining decision bead.** Evidence 2 (condition filtering with
   automatic dangling-edge repair) is a real, working primitive. It is worth nothing *here*
   because onboard's membership is runtime-computed — but if some future beadhive workflow ever
   has a step set that is genuinely determined by static flags at plan time, this is the piece
   that would carry it. Do not generalize this NO-GO into "beads' formula DAG filtering does not
   work"; it does.
