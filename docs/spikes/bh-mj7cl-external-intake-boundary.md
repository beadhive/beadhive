# Spike `bh-mj7cl` — the external-intake module boundary: queue, report binding and link to work

**Bead:** `bh-mj7cl` · **Seat:** `dev/mj7cl` · **Type:** research-only (no product code)
**Feeds decision on:** epic `bh-wjvq9` (external-tracker intake module, prerequisite 3 of the
public GitHub issue to internal work loop `bh-33d5a`). The outbox, milestone and dimension
spike `bh-6wj9y` builds on the module, port and layout named here. The conversation-model spike
`bh-fptob` runs alongside; this document does not depend on it.
**Builds on:** [`tracker-adapter-port-adr.md`](../design/tracker-adapter-port-adr.md) (accepted,
GO), [`external-issue-intake.md`](../design/external-issue-intake.md) sections 1, 2, 4, 5 and 7,
[`github-issue-projection.md`](../design/github-issue-projection.md),
[`two-store-scope-adr.md`](../design/two-store-scope-adr.md) and [`TWO-STORES.md`](../TWO-STORES.md).

## Question

Can **external intake** be an explicit internal module with these properties?

- It has one named, consumer-facing port. The CLI, the work layer, and the merge and release
  hooks call this port and nothing behind it.
- It depends only on the tracker port (`beadhive.modules.tracker.contracts`) and on a bead-store
  port.
- It imports no tracker adapter, no CLI and no work layer, and existing tooling enforces that
  rule.
- It owns the **one** intake queue that `bh report` / `bh escalate` already fill, so imported
  issues and internal reports share one queue.

The question includes the behaviour the boundary must carry, consistent with operator decisions
D1 and D3:

- **Import** an issue by URL. The key is the issue URL, closed issues are in scope, and the
  import lands in `intake:untriaged` only, under a verb that is not `promote`.
- **Refuse** four kinds of input: another repository, a pull request, an unknown URL, and a
  public-store id.
- **Link** many reports to one canonical work bead through a native edge inside the internal
  store. The spike chooses the edge type.
- **Scheduled discovery** calls the module, never the adapter.

Explicitly **not** asked:

- the conversation and comment model (`bh-fptob`);
- the outbox, the supervised sender (D2), the milestone triggers and the report dimensions
  (`bh-6wj9y`);
- the intake state-machine redesign (`bh-elkv`), which this design must survive unchanged;
- plugin wiring and the tracker config namespace (`bh-mobxo`);
- the bd projection, which stays on the bd path per the ADR, including `bh-cm4er`.

This spike sent no request to GitHub. It wrote nothing to any bead store, and it changed no
product code.

## Method

1. **Read the binding inputs.** These were the tracker port ADR (all sections, plus the
   `bh-gj1hb` prototype `docs/spikes/artifacts/bh-gj1hb/tracker_contracts.py`), the intake design
   (sections 1, 2, 4, 5, 7), the projection design, the two-store ADR (sections 1, 3, 5, 8),
   `docs/MODULES.md`, `docs/design/capability-module-boundaries.md`, the epic `bh-wjvq9` and the
   beads `bh-6wj9y`, `bh-elkv`, `bh-o2mjz`, `bh-nkssm` and `bh-cm4er` (`bh bd show`).
2. **Read the existing intake code end to end.** This covered `report.py`, `triage.py`,
   `work_intake.py`, `state.py`, `adopt.py` and `escalate.py`, and every call site found by grep:
   `cli.py`, `work.py`, `mcp.py`, `contributor.py`, `plan.py`, `hub.py`, `hq.py` and
   `registry.py`.
3. **Read the module convention as code**, not as prose: `scripts/check_import_boundaries.py`
   (`_role`, `_direction_allowed` and the forbidden-runtime rule), the five existing
   `src/beadhive/modules/*` packages, `adapters/impact_git.py` with `bootstrap/impact.py` (the
   work-impact port, adapter and root precedent), `hive_services.py` (legacy composition of a
   module), `tests/BUILD` (unit versus legacy-stateful targets) and
   `tests/unit/modules/hives/test_hive_independence.py`.
4. **Probed the checker** by calling `_role` and `_direction_allowed` on every edge this design
   proposes (E8). The script did not run `check()` against a modified tree, because no
   source file was created.
5. **Measured the current closure from real imports.**
   - The repo's own AST collector (`check_import_boundaries.collect_imports`) gave forward and
     reverse transitive closures over the 456-file production graph (E9), plus a static reach
     analysis over the 641 `tests/**/test_*.py` files.
   - A runtime `sys.modules` count measured what actually loads.
   - A timed run of the four current intake test files was compared against the existing
     `modules/hives` unit suite (E10).
   - `pants dependents --transitive` was attempted and **failed** on this tree, for a reason
     unrelated to intake (E10). No closure figure below comes from Pants.
6. **Read bd's dependency-type semantics.** Sources were the installed bd 1.3.0 `bd dep add
   --help` and `bd list --help`, the bd source (`internal/types/types.go`, `cmd/bd/show_refs.go`,
   `cmd/bd/dep_relation.go`) and the `issues` schema migration. Every bh reader of dependency
   types was found by grep (E5, E6).

## Evidence

### Today's intake code

**E1. One queue, three writers, one vocabulary owner.**

- `report.file_report` (`src/beadhive/report.py:94-152`) is the single write path. It creates a
  born-native bead (`bd create`, with no `external_ref`), then runs `set-state origin=<channel>`
  and `set-state intake=untriaged`. It pushes when the target hive is a clone-on-demand cache.
- `escalate.file_escalation` reuses it with `origin=ORIGIN_ESCALATION`.
- `triage.reroute` reuses it to re-file elsewhere (`triage.py:200-205`).
- Queue membership is the label `intake:untriaged`, and the channel is the closed `origin`
  dimension (`state.py:100-124`).
- `triage.list_intake` (`triage.py:65-77`) keys only on the label, so any channel shares the
  queue. That includes `origin:github`, which is **already registered** (`state.py:117`, constant
  `ORIGIN_GITHUB` at `:191`).
- The dispositions accept, reject, reroute and promote (`triage.py:138-228`) move `intake` to a
  terminal value through `bd set-state`. `adopt.py` / `plan.py:927-939` read `intake:promoted`
  as the planner's adopt queue.

**E2. `state.py` mixes three owners.**

- `STATE_DIMENSIONS` holds the following:
  - intake vocabulary: `intake`, `origin` and their predicates and channel derivation;
  - contributor outbound vocabulary: `outbound`, `publish`;
  - the dispatch loop's vocabulary: `escalation`, `dispatch` and the 14 `CAUSE_*` values.
- It imports nothing (forward closure 0), but has **13 production importers and a 238-module
  reverse closure** (E9).
- `registry.closed_dimensions` (`registry.py:339-353`) merges it into what `bh label validate`
  accepts, so moving the intake vocabulary must keep that merge working.

**E3. `report` and `triage` form an owned legacy cycle.** `triage.py:200` lazily imports
`report`, and `report.py:161` lazily imports `triage`. The edge is recorded as `cycle-edge-036`
in `docs/design/import-boundary-exceptions.toml:676`, which expires on the `bh-inqwc`
legacy-package migration. It is one of the 8 owned legacy cycles (`import-boundary-check: OK
(456 files, 4706 import edges, 8 owned legacy cycles/147 edges)`).

**E4. `external_ref` is free on internal report beads today, and settable at creation.**

- `report.py:22-23` reserves `source_system` / `external_ref` for external mirrors and leaves
  them empty on born-native reports.
- `adopt.py:12-15` records that `source_system` is settable only at bead birth (through `bd
  import`). `external_ref` is not so restricted:
  - `bd create --external-ref` exists in bd 1.3.0;
  - `bd update --external-ref` is already routed as `CliRoutes.issue_set_external_ref`
    (`packages/beadhive-bd-cli/src/beadhive_bd_cli/routes.py:239`);
  - `bd list --external-ref <exact>` exists.
- The bh routes lack both of the last two: `create_report` (`routes.py:296-308`) takes no
  `external_ref`, and `issue_list` (`routes.py:42-70`) has no `--external-ref` filter and is
  status-filtered by default.
- **The bd index on `external_ref` is not unique** (`INDEX idx_issues_external_ref
  (external_ref)`, bd `internal/storage/schema/migrations/0001_create_issues.up.sql:59`).
  Idempotence on the URL is therefore Beadhive's job, not the store's.
- The public projection keys its own rows on the same URL (`github-issue-projection.md`,
  "Identity and idempotence"). Those rows live in a different database with a different prefix
  (two-store ADR §4, §5).

### Edge semantics

**E5. bd's dependency types and what affects readiness.**

- In bd source, `AffectsReadyWork` is true only for `blocks`, `parent-child`,
  `conditional-blocks` and `waits-for` (`internal/types/types.go:1335-1337`).
- `tracks` is declared `// Convoy → issue tracking (non-blocking)` (`types.go:1271`). Grep shows
  that bd gives it no special behaviour beyond display: `"TRACKS" / "TRACKED BY"` in
  `cmd/bd/dep_relation.go:46`, and a "watching" glyph in `cmd/bd/show_refs.go:121`.
- `bd dep add --help` (1.3.0) lists `blocks|tracks|related|parent-child|discovered-from|until|
  caused-by|validates|relates-to|supersedes`. `duplicates` is created only by `bd duplicate`,
  which also **closes** the duplicate.

**E6. bh already gives some edge types meanings, and one reader treats `tracks` as blocking.**

- `adopt.py:16-20`: an adopted report is `parent-child` of its epic.
- `beadhive_worktrees/policy/bead_state.py:110-126` (and `worktree.py:1631-1634`): `relates-to`
  and `supersedes` carry retained and replaced disposition evidence.
- **No bh code uses `tracks`** (grep over `src/` and `packages/`).
- However, `operator_contract.py:44` and `operator_work_items.py:43` treat every type **not** in
  `{"parent-child", "related", "discovered-from"}` as a blocker when they count blockers for
  operator views (`operator_contract.py:390-396`, `operator_work_items.py:229`). A `tracks` edge
  would therefore count as a blocker on the report in those views (not on the work).

### The module convention as enforced

**E7. The layout and its rules.**

- `_role` (`scripts/check_import_boundaries.py:202`) classifies
  `beadhive.modules.<owner>.{domain,contracts,application}` as checked layers.
  - `beadhive.adapters.<owner>` and `beadhive.integrations.<owner>` are adapters and
    integrations.
  - `beadhive.bootstrap` is the composition root.
  - Everything else under `beadhive.*` is `legacy`, an **unchecked importer that no checked
    layer may import**.
- `_direction_allowed` (`:243`) sets these rules:
  - `domain` imports only its own domain.
  - `contracts` imports its own domain and contracts, or any public `contracts`.
  - `application` imports its own three layers, or any public `contracts`.
  - Adapters import any public module layer.
- `_is_forbidden_runtime` (`:448`) bans `typer`, `httpx`, `fastmcp` and others in `domain` and
  `application`.
- Existing modules follow two shapes. `modules/hives` keeps records in `domain/models.py`,
  Protocols in `contracts/ports.py` (importing `..domain.models`), and the service in
  `application/services.py`, re-exported by `__init__`. The work-impact slice follows the same
  shape: `modules/work/{contracts,domain,application}/impact.py`, `adapters/impact_*.py` and
  `bootstrap/impact.py`.
- Legacy composition of a module happens in a legacy file: `hive_services.py` imports
  `beadhive.modules.hives`, because neither `bootstrap` nor an adapter may import legacy (E8).
- Module unit tests live in `tests/unit/modules/<name>/` under `_unit_tests("modules/<name>",
  ...)` with `attest:unit` (`tests/BUILD:67`, `:138-142`). Each module has an import and
  side-effect independence sentinel (`test_hive_independence.py`).

**E8. Checker probe of the proposed edges.**

`_direction_allowed(_role(a), _role(b))` was evaluated for each edge below.

| Edge (`a -> b`) | Verdict |
|---|---|
| `modules.intake.application -> modules.tracker.contracts` | allowed |
| `modules.intake.contracts -> modules.tracker.contracts` | allowed |
| `modules.intake.application -> modules.intake.domain` | allowed |
| `adapters.intake -> modules.intake.contracts` | allowed |
| `adapters.intake -> beadhive_bd_cli` (library package) | allowed (external) |
| `modules.intake.application -> modules.tracker.application` (the ADR's `paging.iterate`, `snapshot_ids`) | **FORBIDDEN** |
| `modules.intake.domain -> modules.tracker.contracts` | **FORBIDDEN** |
| `modules.intake.application -> beadhive.triage` / `beadhive.cli` | **FORBIDDEN** (legacy) |
| `modules.intake.application -> adapters.intake` | **FORBIDDEN** |
| `adapters.intake -> beadhive.bd_cli` (legacy shim) | **FORBIDDEN** |
| `bootstrap.cli -> beadhive.triage` | **FORBIDDEN** (legacy) |
| `modules.intake.application -> modules.work.contracts` | **allowed** (passes today) |
| `modules.tracker.contracts -> modules.intake.contracts` | **allowed** (passes today; ADR gap G3) |

`_is_forbidden_runtime`: `typer` and `httpx` are flagged, and `githubkit` is **not** (ADR gap
G2). Two findings follow, and neither is in the ADR:

- **G5.** The ADR's direction block (`tracker-adapter-port-adr.md:240`, `modules/intake ...
  ---> beadhive.modules.tracker.contracts (+ application)`) is not admissible under today's
  checker. A module's application layer may not import another module's application layer.
- **G6.** Nothing stops the intake module importing its own consumers' **contracts**, for
  example `modules.work.contracts`. That would make the one-way rule "intake imports no work
  layer" a convention rather than a rule.

Legacy importers are unchecked by construction. Nothing therefore stops `cli.py` or `work.py`
importing `modules.intake.application` internals (**G7**).

### Current closure, measured

**E9. Static import closure** (AST, `collect_imports`, all edges including function-local lazy
imports).

| Module | Forward transitive closure | Reverse transitive closure | Direct production importers |
|---|---:|---:|---|
| `triage` | 147 of 456 | 22 | `contributor`, `mcp`, `report`, `work_intake` |
| `report` | 147 | 22 | `cli`, `escalate`, `triage` |
| `work_intake` | 147 | 17 | `work` |
| `escalate` | 147 | 3 | `cli` |
| `state` | 0 | **238** | 13 (`adopt`, `contributor`, `dispatch_status`, `escalate`, `hq`, `hub`, `localloop`, `mcp`, `plan`, `registry`, `report`, `triage`, `work_metrics`) |
| `adopt` | 1 (`state`) | 54 | `plan`, `plan_filing`, `work`, `work_logic` |
| reference: `modules.hives` | **7** | — | `hive_services` (+ integrations) |
| reference: `modules.work.application.impact` | **3** | — | `bootstrap.impact` |

- At runtime, `import beadhive.triage` loads **47** `beadhive.*` modules (561 in `sys.modules`),
  and `import beadhive.report` loads 52. `import beadhive.modules.hives` loads 10.
- Of the 641 test files, **194** reach `report`, `triage`, `work_intake` or `escalate` through
  their own imports' transitive closure, and **473** reach `state.py`.
- Direct test importers:
  - `report`: 3 (`test_report`, `test_triage`, `test_escalate`);
  - `triage`: 5 (adds `test_contributor`, `test_mcp_resource`, `test_mcp_strict_bd_reads` and
    `test_mcp_work_intake_dupes_resource`);
  - `state`: 10.
- All of these are in the `legacy-stateful-tests` target (`tests/BUILD:147`, `attest:stateful`).
  They fake bd at the argv level by monkeypatching `beadhive.bd._run` (for example
  `tests/test_triage.py:85`), so they are coupled to bd argv shapes rather than to a port.

**E10. Measured test cost.**

- `uv run pytest tests/test_triage.py tests/test_report.py tests/test_escalate.py
  tests/test_mcp_work_intake_dupes_resource.py` gave **61 passed in 12.73 s** (17.3 s wall,
  single process).
- The reference module suite `tests/unit/modules/hives` gave **11 passed in 0.58 s**.
- `just _pants dependents --transitive src/beadhive/work_intake.py` **failed**: `Pants cannot
  infer owners ... scripts/render_operation_catalog.py:lib: publish_wire_release`. This is an
  unrelated ownership defect on this tree, so no Pants dependents figure is reported. The
  implementation beads must record it once the defect is fixed (the ADR's measured-closure rule).

### Inputs the port must honour

**E11. The tracker port has no single-issue read.**

- The accepted `TrackerReader` (ADR §3 and the prototype's lines 405-412) has `describe`,
  `list_issues`, `list_comments` and `list_ids`. `list_issues` excludes change requests and
  includes closed issues.
- There is no `get(url)` and no URL classifier. An import by URL can therefore be served by
  paging `list_issues` and matching `IssueKey.url`, an O(issues) scan. That scan cannot tell
  three cases apart: "a pull request", "another repository" and "does not exist".
- The adapter alone knows the URL grammar (`IssueKey.url` is "the canonical web URL", prototype
  `IssueKey` docstring). Putting GitHub URL parsing in intake would recreate the ADR's rejected
  candidate D (GitHub shape leaking into intake).

**E12. Prior boundary decisions and patterns.**

- `capability-module-boundaries.md:223-242` placed `report` and `triage` in the planning slice's
  compatibility surface. It **rejected "a global planning/report module"**.
- The opt-in scheduled-import pattern named by the epic is
  `release_catalog_auto_import.py`. A host-daemon timer, off by default
  (`hq.release_catalog.auto_import`), runs the **same function the CLI verb runs**
  (`import_release`), wired from `host_daemon.py:1445`.
- `store_target.operational_store` never returns a public store (`store_target.py:287-292`).
  `registry.public_store_owner` recognises a public id by its fleet-unique prefix
  (`store_target.bead_scope`, `:334-341`).
- The epic notes that the live repository currently has 0 closed issues.

### Scoring the boundary

**E13. Scorecard** (`bh:modularize`; 0 poor, 1 mixed, 2 strong). The dimensions are cohesion,
coupling, data and side-effect ownership, port narrowness, replaceable implementations,
dependency direction and test-closure reduction, scored in that order.

| Candidate | Coh | Cpl | Own | Port | Repl | Dir | Clos | Total | Outcome |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| **A. `modules/intake` owns the whole intake queue (filing, queue, dispositions, external import, link) behind `IntakePort`, over the tracker port and its own `ReportStore` port** | 2 | 1 | 2 | 2 | 2 | 1 | 2 | **12** (14 once G5-G7 land) | **chosen** |
| B. `modules/external_intake` for import and link only; `report`/`triage` stay legacy and share the queue through `state.py` constants | 1 | 1 | 1 | 2 | 2 | 1 | 1 | 9 | rejected |
| C. Extend `report.py`/`triage.py` in place (no module) | 1 | 0 | 1 | 0 | 0 | 0 | 0 | 2 | hard stop (port 0, direction 0) |
| D. Fold intake into the planning module | 0 | 1 | 1 | 1 | 1 | 1 | 0 | 5 | rejected |
| E. Put import inside `modules/tracker` | 0 | 0 | 1 | 0 | 1 | 0 | 1 | 3 | hard stop |
| F. A package: library `packages/beadhive-intake`, or a plugin package | 2 | 1 | 2 | 2 | 2 | 0 | 2 | 11 | hard stop (direction 0) |
| G. Fold intake into `modules/work` | 1 | 0 | 1 | 1 | 1 | 0 | 0 | 4 | hard stop |

Evidence behind A:

- **Cohesion 2.** One capability: getting reports into the shared queue, binding them to their
  source, disposing of them, and linking them to work. The invariants are one queue label, one
  closed channel and one binding per URL (E1, E4).
- **Coupling 1.** Cross-hive filing still needs `registry`, `hub._fetch_cache` and
  `engine.push_state` (E1). These stay in the legacy composition root as port implementations,
  and so does label validation (`validate.bead_violations`). `state.py`'s 238-module reverse
  closure is untouched, because it remains as a facade.
- **Ownership 2.** The module owns the intake vocabulary, the binding rule, the link rule and
  discovery checkpoints. Every effect goes through `ReportStore`, `CheckpointStore` or the tracker
  port.
- **Port 2.** About ten behaviour-oriented operations with value-type requests and results. No
  bd argv, labels or store paths cross it.
- **Replaceable 2.** On the store side, `BdReportStore` sits over `beadhive_bd_cli`, with an
  in-memory `MemoryReportStore`. On the tracker side, `MemoryTracker` already exists in the ADR's
  conformance kit.
- **Direction 1.** Every forbidden edge in E8 is caught today except G5 (which must be avoided),
  G6 and G7.
- **Closure 2.** The expected forward closure is about 15 modules, against 147 today (see
  Recommendation §7).

Why the alternatives lose:

- **B** splits one queue and its vocabulary between two owners. Import's duplicate surfacing
  would have to call legacy `triage.find_dupes`, which E8 forbids, or duplicate it. The
  `report`/`triage` cycle (E3) would survive.
- **D** contradicts E12's rejection of a global planning/report module. Planning consumes
  intake through `intake:promoted`; it does not contain it.
- **E** is the ADR's rejected candidate E: the tracker is a leaf, and threads and outbox are not
  port business.
- **F** fails on direction. A library package may not import
  `beadhive.modules.tracker.contracts` (ADR §4, `check_package_imports.py`). A plugin package
  may not be imported statically by `work`/`merge`, yet the merge and release hooks must reach
  intake. Intake is core behaviour, not an optional plugin.
- **G** fails the epic's one-way rule (intake must not import the work layer), and every work
  test would gain tracker contracts.

## Verdict — **GO**

External intake can be an explicit module, `beadhive.modules.intake`, owning the one shared
queue. Two narrow ports make it possible:

- a consumer-facing `IntakePort`;
- an intake-owned bead-store port, `ReportStore`, beside the accepted tracker port.

The concrete enablers are all present:

- a free `external_ref` on internal reports, settable at creation;
- an existing closed `origin:github` value;
- a non-blocking, unused, directional bd edge type, `tracks`;
- a checker that already rejects every edge the module must never take, except three gaps that
  are small and named (G5 to G7).

Two adjustments to the accepted tracker work are required, and neither changes the verdict:

- intake must not import `tracker.application` (G5);
- the port needs an optional single-issue resolver (amendment A1) for distinct refusal reasons
  on import-by-URL.

## Recommendation

### 1. Module name, path and layout

The capability is **external intake**. Its package is `beadhive.modules.intake`, matching the
`modules/intake` already named in the ADR's direction block. One queue means one module, so
`bh report` and `bh escalate` move behind it too. Records sit in `domain`, because the domain may
import nothing outside itself (E8). `contracts` holds the Protocols and re-exports, which is the
`modules/hives` shape.

```text
src/beadhive/modules/intake/
  __init__.py            public API: IntakeService, the contracts, the vocabulary (bounded __all__)
  BUILD                  python_sources(tags=[category:code, attest:package,
                                              owner:modules/intake, role:capability])
  domain/
    models.py            records below (stdlib dataclasses / StrEnum only)
    errors.py            IntakeRefused(reason: RefusalReason, detail), IntakeStoreError
    vocabulary.py        INTAKE_DIM/ORIGIN_DIM, the value sets, DISPOSITION_STATE, label constants,
                         predicates and channel_of (moved from state.py), INTAKE_DIMENSIONS,
                         TRACKS_EDGE = "tracks"
    references.py        pure classification of the operator's *input* string: URL-shaped,
                         bead-id-shaped, public prefix (no tracker URL grammar)
    rules.py             upsert decision (create / update / unchanged / duplicate binding),
                         link rule table, dupes_touching
  contracts/
    ports.py             IntakePort (inbound); ReportStore, CheckpointStore, LabelPolicy (outbound)
    __init__.py          re-exports domain records and errors with the ports
  application/
    service.py           IntakeService(...) implements IntakePort
    discovery.py         paging loop, checkpoint-after-durable-page, full-id reconcile
    mapping.py           TrackerIssue -> SourceSnapshot / IssueBinding (the only tracker-typed code)
src/beadhive/adapters/intake/
  bead_store.py          BdReportStore: ReportStore over an injected beadhive_bd_cli.CliRoutes
  checkpoints.py         file-backed CheckpointStore in the hive's private .bh/ state
src/beadhive/testing/intake/
  memory_store.py        MemoryReportStore (the fake every consumer test uses)
  cases.py               ReportStore contract cases, run against both implementations
src/beadhive/intake_services.py   legacy composition root (the hive_services.py precedent)
tests/unit/modules/intake/        module tests + test_intake_independence.py
```

The domain records that `bh-6wj9y` and the consumers build on are listed below. Names are
binding for this design; fields may grow.

| Record | Fields (summary) |
|---|---|
| `IssueBinding` | `report` (bead id), `tracker` (descriptor's tracker value), `project` (opaque locator), `url` (the key), `identifier` (`#12`) |
| `SourceSnapshot` | `title`, `body`, `state` (open or closed), `closed_reason`, `labels`, `author` (`id`, `kind`; display never compared), `revision` (opaque, tracker-issued) |
| `ReportDraft` | `title`, `issue_type`, `description`, `origin`, `actor`, optional `binding` and `snapshot` |
| `ImportRequest` / `ImportOutcome` | `reference`, `actor`, `dry_run` / `kind` (`created`, `updated`, `unchanged`), `binding`, `dry_run` |
| `LinkRequest` / `LinkOutcome` | `report`, `work`, `actor`, `replace` / `kind` (`linked`, `already_linked`, `relinked`, `unlinked`) |
| `QueueQuery` / `QueueItem` / `QueueView` | `channel` / `id`, `title`, `issue_type`, `channel`, optional `binding` / `items`, `duplicates` |
| `DispositionRequest` / `DispositionOutcome` | `report`, `disposition` (accept, reject, promote, reroute to a superintendent seat), `issue_type`, `priority`, `reason`, `seat` |
| `DiscoveryRequest` / `DiscoveryOutcome` | `full_reconcile` / `created`, `updated`, `unchanged`, `source_missing`, `checkpoint_advanced` |
| `StoreScope` | `operational_prefix`, `public_prefixes` (fleet-wide), `hive` |

### 2. The named port for consumers: `IntakePort`

The port lives at `beadhive.modules.intake.contracts.IntakePort`, a `Protocol`. `IntakeService`
is its one concrete implementation. A `NullIntake` (`queue` empty, `reports_for_work` returning
`()`, every write raising `IntakeRefused(INTAKE_DISABLED)`) is bound when external intake is off.

```python
class IntakePort(Protocol):
    def file_report(self, draft: ReportDraft) -> str: ...                    # bh report, bh escalate
    def import_issue(self, request: ImportRequest) -> ImportOutcome: ...     # bh work import <url>
    def link(self, request: LinkRequest) -> LinkOutcome: ...                 # bh work link
    def unlink(self, report: str, *, actor: str) -> LinkOutcome: ...         # bh work link --remove
    def queue(self, query: QueueQuery) -> QueueView: ...                     # bh work intake, MCP
    def dispose(self, request: DispositionRequest) -> DispositionOutcome: ... # accept/reject/promote
    def binding_of(self, report: str) -> IssueBinding | None: ...
    def reports_for_work(self, work: str) -> tuple[IssueBinding, ...]: ...   # merge/release hooks
    def discover(self, request: DiscoveryRequest) -> DiscoveryOutcome: ...   # scheduled discovery
```

The consumers that use it:

| Consumer | Calls |
|---|---|
| CLI: `bh report`, `bh escalate`, `bh work intake`, `accept`, `reject`, `reroute`, `promote` (`work_intake.py`), new `bh work import`, `bh work link` | through `intake_services` |
| MCP resources `beadhive://work/intake` and `.../dupes` (`mcp.py:1161-1178`) | `queue` |
| Work layer: `bh work merge` / `finish` after a close, and later the release path | `reports_for_work` only. Wrap every call so that an intake failure is reported and never fails the merge. With intake off, `NullIntake` makes the call free |
| `bh-6wj9y`: milestone triggers, communication records, dimensions | `reports_for_work` and `binding_of`. Further operations (for example `draft_milestone`) are added to this port, not beside it |

Reroute to another hive stays an orchestration in `intake_services`: it runs
`target.file_report(...)` and then `source.dispose(reroute)`. A port instance is bound to one
operational store and never reaches into another.

### 3. The outbound bead-store port: `ReportStore`

This is intake's own narrow bead-store port, following the per-capability-Protocol precedent of
`beadhive_core` (`Issues`, `Leases`, `StateReads`). No generic bead-store contract exists in
`modules/` today. If a shared one appears, for example over `bd serve`, then `BdReportStore` is
re-implemented over it and the port stays the same.

```python
class ReportStore(Protocol):
    def scope(self) -> StoreScope: ...
    def get(self, bead: str) -> BeadView | None: ...
    def find_by_external_ref(self, url: str) -> tuple[BeadView, ...]: ...    # every status
    def create(self, draft: ReportDraft) -> str: ...      # one bd create, external_ref included
    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None: ...
    def record_snapshot(self, bead: str, snapshot: SourceSnapshot, *, actor: str) -> None: ...
    def update_fields(self, bead: str, *, issue_type: str, priority: str, actor: str) -> None: ...
    def close(self, bead: str, *, reason: str, actor: str) -> None: ...
    def assign(self, bead: str, seat: str, *, actor: str) -> None: ...
    def list_open_labeled(self, label: str) -> tuple[BeadView, ...]: ...
    def duplicate_pairs(self, *, threshold: float) -> tuple[DuplicatePair, ...]: ...
    def tracked_work(self, report: str) -> str | None: ...
    def track(self, report: str, work: str, *, actor: str) -> None: ...      # bd dep add -t tracks
    def untrack(self, report: str, work: str, *, actor: str) -> None: ...
    def trackers_of(self, work: str) -> tuple[str, ...]: ...                # dep list --direction up
```

`BdReportStore` needs three new named routes in `beadhive-bd-cli`:

- `create_report(..., external_ref=)`;
- `issue_list(external_ref=, all_=True)`;
- `dependency_list(..., type_="tracks")`, which already exists.

The composition root builds the store over `store_target.routes(store_target.operational_store(
entry))`. It never builds one over a public store, so "nothing but the pull writes the public
store" holds by construction. A test asserts that the intake composition never resolves
`SCOPE_PUBLIC`. `LabelPolicy.violations(labels)` wraps `validate.bead_violations` in the root.
`CheckpointStore` holds `get(binding_key)` and `put(binding_key, token)` with opaque
`Checkpoint.token` values; its contents are local, non-secret and not configuration (ADR §6).

### 4. Import: behaviour, idempotence key, refusals

The verb is **`bh work import <issue-url> [--hive H] [--dry-run] [--json] [--as SEAT]`**. It is
deliberately not `promote` (D3), and it lands only in `intake:untriaged`. The verb name is a
proposal for the implementation molecule to ratify. The channel comes from the bound tracker, not
from the verb: `origin:github`, never the legacy `origin:import`.

Refusals come before any tracker request, in order. Each raises `IntakeRefused(reason)` with one
line naming the fix, exits 1, and leaves zero store writes (asserted against
`MemoryReportStore`).

| # | Input | Reason | Decided by |
|---|---|---|---|
| R0 | external intake off for the hive, or no tracker bound, or `issues.read` not enabled | `INTAKE_DISABLED` | composition root / `OperationNotEnabled` |
| R1 | bead-id-shaped with a prefix in `StoreScope.public_prefixes` (for example `bhpub-12a`) | `PUBLIC_STORE_ID`: "public-store ids are minted per host (bh-nkssm); import by the issue URL" | `domain.references` |
| R2 | any other non-URL (an internal `bh-…` id, a bare `#12`) | `NOT_A_URL` | `domain.references` |
| R3 | URL that the adapter's grammar does not recognise (another host, `/discussions/…`, a repository page, an API URL) | `UNKNOWN_URL` | tracker `IssueResolver.resolve` (A1) |
| R4 | issue URL of another project (another owner or repository) | `OTHER_PROJECT` | `IssueResolver.resolve` (pure, no request) |
| R5 | change-request URL (`/pull/N`), or an `/issues/N` that the tracker answers as a pull request | `CHANGE_REQUEST` | `resolve` (pure), then `IssueResolver.get` (one read) |
| R6 | recognised and in project, but missing or hidden from the token | `NOT_VISIBLE` | `get` (`NotVisible`) |
| R7 | the issue was transferred, so the canonical URL returned by `get` is in another project | `OTHER_PROJECT` | service compares `IssueKey.url` against the descriptor |
| R8 | more than one internal bead already carries this URL | `DUPLICATE_BINDING`: names the ids, writes nothing, and refuses until the operator resolves it | `domain.rules` |

The **idempotence key** is the canonical `IssueKey.url` that the adapter returns, never the
string the operator typed. Fragments (`#issuecomment-…`), queries, trailing slashes and
case differences in owner or repository all resolve to the one URL before lookup. Lookup is
`ReportStore.find_by_external_ref(url)` across **every status**, closed included, and **only in
the internal (operational) store**. The public projection's row for the same URL is never read
and never linked (D1, two-store ADR §5).

The upsert:

- **None found: create.**
  1. Run one `bd create --type <intake.external.default_type, bug> --external-ref <url>` with
     the hive triplet labels, `--actor <importer>` and the title and body. The binding therefore
     exists atomically with the bead.
  2. Run `set-state origin=<tracker>` and `set-state intake=untriaged`.
  3. `record_snapshot`: tracker state, `closed_reason`, labels, author `(id, kind)` and the
     opaque revision go to bead metadata under one namespaced key (`bh.intake.source`).

  The reporter is the issue author in the snapshot. The service actor stays on `--actor`, kept
  separate as design §2 requires, with no reporter label (`state.py:32-33`).
- **One found, same revision: `unchanged`.** No write. This is what makes the inclusive `since`
  repeats harmless.
- **One found, newer revision: `updated`.** Only `record_snapshot` runs. The title, description,
  `intake` value, status and links are **never** touched. A routine re-import never resets
  triage (design §2), and it never overwrites triage edits. Field ownership beyond this is
  `bh-6wj9y`'s.
- **One found but missing its `origin` or `intake` stamp** (an interrupted earlier create): stamp
  only the missing dimension. Never overwrite an existing disposition.

**Closed issues are in scope.** A closed issue imports exactly like an open one: an open report
in `intake:untriaged`, with `state: closed` in the snapshot. The bead's own status is the
internal triage lifecycle, not the tracker's. A later reopen or close upstream is a snapshot
update only. Waking triage on a state change or a reply waits for `bh-elkv`'s `needs-info` state
(D3).

**Concurrency.** The store cannot enforce uniqueness (E4). `IntakeService` therefore serialises
`import_issue` and `discover` per hive through an injected lock seam (an in-process lock in
tests, the host's per-hive lock in the root). After every create it re-reads by URL; a second
row raises `DUPLICATE_BINDING` without auto-closing anything. That covers a cross-host race on a
replicated internal store.

**Dry run** resolves and classifies (R0 to R8) and reports `would create` or `would update`.
It performs zero store writes, and only read requests reach the tracker.

**Tracker amendment A1** goes to the tracker implementation molecule. It is additive and
optional, and the API version stays 1:

```python
class IssueResolver(Protocol):
    def resolve(self, url: str) -> IssueKey: ...   # pure; raises UrlNotRecognized, OtherProject,
                                                   # ChangeRequestUrl (new TrackerError subclasses)
    def get(self, key: IssueKey) -> TrackerIssue: ...  # one read; NotVisible, ChangeRequestUrl
```

`MemoryTracker` implements both trivially. Until A1 lands, `import_issue` falls back to paging
`list_issues` and matching `IssueKey.url`. In that fallback R3 to R6 collapse into one
`NOT_FOUND_IN_PROJECT` reason. The fallback is correct but O(issues), so A1 should land with or
before the import bead.

### 5. Link: many reports to one work bead

The verb is **`bh work link <report> --to <work> [--replace]`**, with `bh work link <report>
--remove` to undo it. The edge is **`tracks`, from report to work** (`bd dep add <report> <work>
-t tracks`).

Why `tracks`:

- It is directional, so "report R tracks work W" reads as the subscription that design §1 asks
  for.
- It is non-blocking in bd (`AffectsReadyWork` false), so a report never gates work and work
  never gates a report.
- It is unused by bh (E6), so the edge type alone identifies a report binding.
- bd renders it as `TRACKED BY` on the work.
- The edge survives closing the report. A duplicate report closed early therefore still receives
  milestones (design §1, "even if a duplicate report has already been closed").

Rejected edge types:

- `blocks`, `conditional-blocks` and `waits-for` all affect readiness.
- `parent-child` affects readiness and is single-parent. It would also collide with adopt's
  report-child-of-epic edge (`adopt.py:16-20`).
- `duplicates` is created only by `bd duplicate`, which closes the source, and it means report
  to report.
- `relates-to` and `related` are undirected and loose, and `relates-to` already carries
  disposition evidence (E6).
- `caused-by` and `discovered-from` point work to report and imply that the work originated from
  the report. That is false for pre-existing canonical work.

Link rules (`domain.rules`). Each refusal is an `IntakeRefused` with zero writes:

| Case | Result |
|---|---|
| `report` is not a bound report (no `external_ref` binding or no `origin`) | `NOT_A_REPORT`. Internal `origin:report` beads may link too once 6wj9y needs it; v1 links bound reports only |
| `work` has a public-store prefix | `PUBLIC_STORE_ID` (no edge crosses stores) |
| `work` is not in this operational store (another hive, unknown id) | `CROSS_STORE_LINK` |
| `work == report` | `SELF_LINK` |
| `work` is itself `intake:untriaged` | `TARGET_UNTRIAGED`: triage the canonical bead first. An *accepted* report may act as its own one-to-one work (design §1) and may be a target |
| `work` is a report that itself tracks another work | `CHAINED_REPORT`: link to the canonical work directly (depth 1) |
| `report` already tracks `work` | `already_linked`, no write |
| `report` tracks a different work | refused (`ALREADY_LINKED_ELSEWHERE`) unless `--replace`, which runs `untrack` then `track` (`relinked`) |
| several reports, one work | allowed. This is the point |

Linking changes no dimension. Disposition stays `accept`/`reject`, and later `bh-elkv`'s
`dedupe`. The external `external_ref` stays on its report and is never copied onto the work
(design §1).

**Consumer change forced by the edge:** add `"tracks"` to `_NON_BLOCKING_DEPENDENCIES` in
`operator_contract.py:44` and `operator_work_items.py:43`. A better fix is to invert both into
bd's own blocking set. Without it, operator views count every linked report as blocked (E6).

### 6. Scheduled discovery calls the module, never the adapter

`IntakePort.discover` is the one ingestion operation that both triggers call (design §2):

- an operator verb, `bh work discover [--full]`;
- an opt-in host-daemon timer that follows the `release_catalog_auto_import` pattern (E12).

The timer lives in the composition root and calls
`intake_services.discover(hive)`, which calls `IntakeService.discover`. Neither trigger holds a
tracker adapter. Only `intake_services` binds one, through the plugin wiring of `bh-mobxo`.

The discovery steps:

1. Page `list_issues(IssueQuery(since=<checkpoint>, include_closed=True))`, using intake's own
   ten-line pager. It does **not** call `tracker.application.iterate` (G5).
2. Run each `TrackerIssue` through the same upsert as `import_issue`, minus R1 and R2. Discovery
   never needs A1, because `list_issues` already excludes change requests and other projects.
3. Run `CheckpointStore.put` only after the page and every earlier one are durably ingested,
   using the tracker's own checkpoint, never the local clock (ADR §3).
4. Run a full reconcile every `full_reconcile_every` ticks, or on `--full`:
   - `list_ids` builds a snapshot, assembled by intake's own `snapshot_ids` loop.
   - Only a `complete` snapshot may mark an issue `source_missing` in its snapshot.
   - No bead is ever deleted or closed by discovery.

Tests drive `tick()` directly with an injected clock and never sleep. Discovery never touches the
public store or `bd github sync`: the projection remains a separate operator verb, and two
GitHub readers is an accepted ADR cost (§5).

**Configuration** is a proposal for the implementation molecule and `bh-mobxo` to ratify. Every
key is per hive, and every value below is the default:

| Key | Default |
|---|---|
| `intake.external.enabled` | `false`; off means `NullIntake` for import, link and discover |
| `intake.external.tracker` | none; names the bound tracker plugin binding |
| `intake.external.default_type` | `bug` |
| `intake.external.discovery.enabled` | `false` |
| `intake.external.discovery.interval_seconds` | none (required when enabled) |
| `intake.external.discovery.full_reconcile_every` | `24` (ticks) |

No key holds a credential. Tracker reads also need the tracker's own `read.issues.enabled`
(ADR §6).

**Outbound safety.** Import, link and discover perform **no tracker write**. They hold no
`WriteGrant` and call no `CommentWriter`/`LabelWriter`. No `bh-*` id or internal content leaves
the host. Every write belongs to `bh-6wj9y`'s supervised outbox (D2) behind its own gate, with
the `bh-o2mjz` sanitizer seam. Issue bodies are untrusted input: they are stored as data and
never interpreted.

### 7. Dependency direction, enforcement and expected closure

```text
cli.py / work.py / mcp.py / host daemon --> beadhive.intake_services   (legacy composition root)
beadhive.intake_services --> beadhive.modules.intake (public API), beadhive.adapters.intake,
                             store_target / registry / validate / hub / engine (legacy),
                             the bound tracker adapter (lazily, via the plugin kernel)
beadhive.adapters.intake --> beadhive.modules.intake.contracts, beadhive_bd_cli
modules.intake.application --> modules.intake.{domain,contracts}, modules.tracker.contracts
modules.intake.contracts   --> modules.intake.domain, modules.tracker.contracts
modules.intake.domain      --> standard library only
merge / release hooks, bh-6wj9y code --> modules.intake.contracts (IntakePort) only
forbidden: intake -> any legacy module, adapters, integrations, bootstrap, CLI, typer/httpx/
           githubkit, tracker.application, work/plan contracts; tracker -> intake (G3)
```

**Enforced today by `check_import_boundaries.py`** (E8):

- intake layers importing legacy (`triage`, `cli`, `bd_cli`, `work`);
- intake importing adapters;
- the domain importing tracker contracts;
- `typer`, `httpx` and `fastmcp` in the domain or application layer.

**Close in the module's first bead**, each with a negative-control test:

- **G5.** Resolve the ADR's "(+ application)". Either intake owns its pager and snapshot loop
  (recommended: about 20 lines, no new rule), or the tracker molecule moves `iterate`/
  `snapshot_ids` into `tracker.contracts`. Do **not** relax `_direction_allowed`.
- **G6.** A consumer-exclusion rule: owner `intake` may not import `modules.work.*` or
  `modules.planning.*`. This is a two-entry table beside the ADR's G3 leaf rule.
- **G7.** A module-local AST test (`test_intake_independence.py`): no legacy file except
  `intake_services.py` may import `beadhive.modules.intake.domain` or `.application`. Others use
  the package's `__all__` or `contracts`. The test also carries the hives-style sentinel: no
  subprocess, socket, file read or `typer` import when the module is imported.
- **G1 and G2** from the ADR, which protect intake's contracts and application from `httpx` and
  `githubkit`.

**Test closure.**

| | Current (measured, E9 and E10) | Expected (structural, to be measured with Pants once E10's defect is fixed) |
|---|---|---|
| Forward closure of the intake logic | 147 of 456 production modules (runtime: 47 loaded) | about 15: intake (about 10) + `tracker.contracts` (3) + `testing.intake` and `testing.tracker` kits |
| Where the tests live | `legacy-stateful-tests` (`attest:stateful`), faking `bd._run` argv | `tests/unit/modules/intake/` under `_unit_tests("modules/intake", "modules/intake", "capability")`, `attest:unit`, eligible for `pants:proven` |
| Cost of the current four files | 61 tests, 12.73 s | the `modules/hives` reference: 11 tests, 0.58 s |
| Test files that reach intake code statically | 194 (473 reach `state.py`) | a change inside `modules/intake/domain` or `application` reaches only module tests, `ReportStore` contract cases and boundary tests of the facades |
| A tracker adapter change | n/a | never runs intake tests (consumer tests use `MemoryTracker`) |

`state.py`'s 238-module reverse closure is unchanged, because the facade stays. Only callers that
migrate to the port shed it.

### 8. What moves, what stays, which consumers change

| Today | Moves to | Stays as |
|---|---|---|
| `state.py` intake and origin vocabulary (`STATE_DIMENSIONS["intake"]`, `["origin"]`, `INTAKE_*`, `ORIGIN_*`, `ORIGIN_DIM`, `DISPOSITION_STATE`, `is_untriaged_intake`, `is_promoted`, `disposition_state`, `origin_of`, `is_report_origin`, `is_escalation_origin`, `origin_from_source_system`, `channel_of`) | `modules/intake/domain/vocabulary.py` | `state.py` re-exports them and builds `STATE_DIMENSIONS` from `INTAKE_DIMENSIONS` plus its own entries. `registry.closed_dimensions` is unchanged. `bh-6wj9y` registers new dimensions in `INTAKE_DIMENSIONS`, which reach `state.STATE_DIMENSIONS` through the facade |
| `state.py` `outbound`/`publish` (contributor) and `escalation`/`dispatch` (dispatch loop) | nothing | stay in `state.py`; they are not intake |
| `report.file_report` policy (type gate, label set, create-then-stamp order) | `IntakeService.file_report` + `BdReportStore.create` | `report.py` keeps hive resolution, clone-on-demand (`hub._fetch_cache`) and push (`engine`), then calls the port through `intake_services` |
| `report.entry_dupes`, `triage.find_dupes`, `dupes_touching`, `_dupe_note` | `ReportStore.duplicate_pairs` + `domain.rules` | `triage.py` facade functions for `contributor.py` until it migrates |
| `triage.list_intake`, `intake_payload`, `_channel_of`, `CHANNELS` | `IntakeService.queue` | facade |
| `triage.accept`, `reject`, `promote`, `reroute --super`, `_require_untriaged`, `_clear_intake` | `IntakeService.dispose` | facade. `reroute --to <hive>` becomes the `intake_services` orchestration (§2), which **retires `cycle-edge-036`** (E3) |
| `triage.print_intake` (typer rendering) | nothing (presentation) | `work_intake.py` renders a `QueueView` |
| `work_intake.py` | nothing | thin CLI over `intake_services`; gains `import`, `link` and `discover` |
| `escalate.py` | nothing | HQ resolution and role/tool stamping stay; it files through the port |
| `adopt.py`, `plan.py` adopt | nothing | consumers of `is_promoted`/`channel_of` through the facade. The `bh-elkv` rename touches only `vocabulary.py` and these consumers |
| `report_target.py`, `github_projection.py`, `tracker_sync.py`, `triage_store.py` (validation, unrelated despite the name) | nothing | out of the boundary. The projection stays on the bd path (ADR §5) |

**Consumers that change:**

- `work.py` / `work_intake.py`: new verbs, plus routing through `intake_services`.
- `cli.py` (`report_cmd`).
- `escalate.py`.
- `mcp.py:1161-1178` (queue and dupes through `queue`).
- `contributor.py:448` (later; facade until then).
- `operator_contract.py:44` and `operator_work_items.py:43` (`tracks`, §5).
- The `bh work merge` / `finish` path (a non-fatal `reports_for_work` seam for `bh-6wj9y`).
- `beadhive-bd-cli` `routes.py` (three routes, §3).
- `tests/BUILD` (the new unit target).
- `docs/design/import-boundary-exceptions.toml` (retire `cycle-edge-036` once the `report`→
  `triage` edge is gone).

These consumers keep working through facades: `hub.py:1207`, `hq.py:766`, `registry.py:353`,
`localloop`, `dispatch_status` and `work_metrics`.

### 9. Suggested bead order for the implementation molecule (replan input)

1. Create `modules/intake` with the vocabulary move behind the `state.py` facade, plus
   `MemoryReportStore`, the contract cases and gaps G5 to G7. No behaviour change. The existing
   61 intake tests pass unchanged.
2. Add `BdReportStore` and the three bd-cli routes. Migrate `file_report`, `queue` and `dispose`
   behind the port, and retire `cycle-edge-036`.
3. Add `bh work import` (§4), which depends on A1 or ships the fallback, and on the epic's "pull
   includes closed issues" proof.
4. Add `bh work link` (§5) and the `operator_contract` `tracks` fix.
5. Add `discover` with the opt-in timer (§6).
6. Hand `reports_for_work` to `bh-6wj9y`'s milestone work.

Every bead records its measured `pants dependents` closure once E10's ownership defect is fixed.
