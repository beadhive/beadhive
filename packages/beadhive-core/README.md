# beadhive-core

The API-first Beadhive command handlers described in
[the parallel-replacement ADR](../../docs/design/beads-api-first-parallel-replacement-adr.md).
They depend on `beadhive-beads-client` (the pinned Beads v1.3 SDK and `BeadsSession`) and never
import the root `beadhive` application, `bd` argv helpers, Dolt, Pants, or stateful test fixtures.

## Operation routing (bh-l5sxi.1)

`beadhive_core.RoutingTable` (`beadhive_core/routing.py`) turns the bh-97fo0.3 operation matrix
(`beadhive_beads_client.load_operation_matrix`) into one named route per operation — never a
generic storage-provider abstraction, and never an automatic HTTP-to-CLI fallback. Every one of
the matrix's 64 operations is exactly one of four kinds, taken verbatim from its classification:

| Kind | Type | Selector | Carries |
|---|---|---|---|
| `api-ready` | `ApiRoute` | `select_api(name, capabilities)` | `capability`, `session_method`, `evidence` |
| `cli-compatibility` | `CompatibilityRoute` | `select_cli(name)` | `reason` |
| `administrative` | `CompatibilityRoute` | `select_administrative(name)` | `reason` |
| `denied` | `DeniedRoute` | unroutable — every selector raises `OperationDenied` | `reason` |

`select_api` raises `beadhive_beads_client.CapabilityMissing` when the caller's negotiated
capabilities don't cover the route's required capability, and `RouteMismatch` when the named
operation isn't actually api-ready — both **before** a caller can reach the session method, so an
unsupported capability fails before any mutation is attempted. `RoutingTable.call_api(session,
name, *args, **kwargs)` is the one dispatch point: it selects the route against
`session.context.capabilities` and calls the exact `BeadsSession` method the matrix names — never
a method chosen generically at runtime. `select_cli` / `select_administrative` only name the
route and its reason; the compatibility shell still owns the actual `bd` invocation (`work_review.py`'s
`CliGateOperations` / `CliStateOperations` and `beadhive_bd_cli.coordination`, all in
[`beadhive-bd-cli`](../beadhive-bd-cli/README.md) since bh-o3xuf, are the existing examples).
Every selector accepts an optional `RoutingObserver` so the chosen route can be attributed in
telemetry.

`COORDINATION_OPERATIONS` names every lease, heartbeat, reclaim, merge-slot and gate operation
that must stay `cli-compatibility`: their exclusivity and staleness guarantees come from the real
service or the real `bd` binary (`packages/beadhive-bd-cli/tests/test_coordination_int.py`, `tests/test_merge_slot.py`),
never from an in-memory stand-in, and a policy test fails the day the installed matrix moves one
of them to `api-ready` without a deliberate, evidenced bead.

`beadhive_core.review`'s `GATE_ROUTES` / `STATE_ROUTES` are now resolved through this table
(`select_cli` at import time) instead of as bare literals, so the same "the matrix is the only
source of truth" guarantee covers the ports `ReviewCommands` already depends on.

## Review: approve and bounce

`beadhive_core.ReviewCommands` owns the review policy only: which open gates count as review
gates, the warden-only `security:*` and releaser-only `release-hold:` resolutions, the human-gate
and self-review guards, the allowed review transitions, the operator-facing result lines, and the
observer notifications. Durable issue behavior goes to Beads over HTTP:

| Effect | Route |
|---|---|
| Read the bead (status, assignee, labels, revision) | `issues.get` |
| Approve: drop stale `review:*` labels | `issues.update`, guarded by revision |
| Bounce: record the feedback as a comment | `issues.addComment` |

Beads v1.3 has no gate lookup/create/resolve route and no state-dimension route, so those are two
narrow ports the compatibility shell implements over its named `bd` CLI routes:

- `GateOperations` — `work.gate.lookup`, `work.gate.resolve`
- `StateOperations` — `work.state.update` (`bd set-state` writes the transition event that rework
  metrics read, plus the `review:<value>` label)

Gate semantics are never fabricated from generic issue operations: no generic close stands in for a
gate resolution, and nothing here creates a gate. Every read happens before the first write; writes
stop at the first failure, and an ambiguous HTTP write is reported, never replayed.

The installed `beadhive` shell selects these handlers at one seam, `src/beadhive/work_review.py`,
which resolves the hive's one supervised Beads service (`bh host beads`, see
[`docs/BEADS-SERVICE.md`](../../docs/BEADS-SERVICE.md)). Neither the core nor the shell ever
starts `bd serve`: with no verified service running, approve and bounce fail closed and name
`bh host beads start --hive <hive>`. Session factories signal that with the client's
`ServiceError` or the core's `SessionUnavailable` (embedded-Dolt hives cannot be served).

## Ready selection and claim-next (bh-l5sxi.2)

`beadhive_core.queue` keeps Beadhive's queue policy narrow, on the same principle as `review.py`:
Beads owns readiness and claim atomicity, and this module never reproduces its state machine.

| Effect | Route |
|---|---|
| List the ready front, priority-ordered | `ready.list` (`QueueCommands.list_ready`) |
| Atomically take the next ready issue | `issues.claimNext` (`QueueCommands.claim_next`) |
| Release a wrongly-claimed issue back | `issues.release` (only for the seat-mismatch case below) |

`QueueCommands.claim_next` replaces the CLI-compatibility "list ready, optimistically claim the
first eligible candidate, re-read to see who actually won, retry the next one on a lost race" loop
(`beadhive.work_next`'s `eligible` / `claim_won` / `decline`, still the CLI path) with ONE atomic
HTTP transaction — the ready predicate, the compare-and-set, and the hydration of the won row all
commit together, so there is nothing left to retry. `eligible` / `decline` / `by_parent` are the
pure policy left over: which of Beads' rows count as a candidate for one actor, which closed-set
reason a decline carries, and a rows-only direct parent-child filter (`by_parent` is NOT a
recursive molecule-membership traversal — Beads' `parent` field is the direct edge, one level).

The installed `beadhive` shell selects this route at `src/beadhive/work_queue.py`, used by
`bh work next`'s claim orchestration (and, through it, `bh work loop`'s dispatch-claim step,
since the local loop shells out to `bh work next --json`). The seam selects BEFORE execution,
never as a retry after an API failure: an `--epic`-scoped claim (no recursive-molecule filter over
HTTP) and an undeclared actor (whose seat prefix depends on the claimed bead's type, unknowable
before an atomic claim commits) stay on the named CLI-compatibility route unconditionally; a
declared actor with no capable Beads service falls back the same way `work_review.py` does
(`SessionUnavailable` / `CapabilityMissing`, both before any write). A seat-mismatched atomic claim
(a declared `dev/<name>` actually won an epic) is released back in the same session rather than
left claimed, and reported as a refusal — see `work_queue`'s module docstring.

### Retained CLI-compatibility surface (named for bh-sy36q.6)

The CLI-compatibility pick/claim/re-verify loop in `beadhive.work_next` / `beadhive.work_dispatch`
(`eligible`, `claim_won`, `decline`, `_try_claim`, the `bd ready` + `bd update --claim` calls in
`impl_next_`) is retained, not superseded: it is the explicit route for an undeclared actor, an
`--epic`-scoped claim, and any hive with no capable Beads service — including `bh work loop`'s
local no-server tier, which must keep working.

## `bh work ready` and `bh work schedule` (bh-mu5yb.1)

Checked against the pinned Beads v1.3 client (`packages/beadhive-beads-client`, generated from
Beads commit `f45b249ce6b40ba62aecc03949e6371e8f7c79d8`) and the `bd` binary installed in this
hive's dev environment, confirmed to be the same build: `bd --version` reports
`1.3.0 (f45b249ce)`. All claims below (recursive-`parent`, sort defaults, byte-shape parity) were
additionally verified empirically against a real disposable `bd serve` 1.3.0, not just read off
the spec — see `packages/beadhive-core/tests/test_core_queue_policy.py`'s `to_bd_json` fixture and
this bead's own verification notes for the exact probes.

### The bh-l5sxi.2 gap list, re-verified rather than assumed

bh-l5sxi.2 left both commands entirely on the CLI path and named four reasons. Re-checking each
against the pinned client (not just its own prior notes) before adopting or keeping any of them:

- **No `--mol` / `--mol-type` filter in `list_ready_work`.** STILL TRUE. Neither
  `GET /v0/beads/ready` nor `GET /v0/beads/ready:count` nor `GET /v0/beads/issues` publishes a
  `mol` or `mol_type` parameter anywhere in the pinned v1.3 OpenAPI spec — `bd`'s own `--mol`
  filters to a `type: molecule` / `mol_type: swarm|patrol|work` issue's membership (`bd swarm
  create`'s output, a DIFFERENT construct than Beadhive's epic-parented "molecule"), and that
  membership computation has no HTTP surface at all yet.
- **No recursive epic membership over HTTP.** FALSE AS STATED — corrected, not just re-confirmed.
  `GET /v0/beads/ready`, `GET /v0/beads/ready:count`, `GET /v0/beads/issues`, and
  `POST /v0/beads/ready:claimNext` all document their `parent` QUERY PARAMETER identically:
  "Restrict to recursive descendants of this issue." A real disposable-hive probe confirms it: an
  epic → child-epic → grandchild-leaf tree, queried with `GET /v0/beads/ready?parent=<top-epic>`,
  returns the grandchild leaf (and excludes the intermediate child epic only because `ready`
  excludes the `epic` type by default, not because the walk stopped short). This is genuinely
  recursive, and it existed in the SAME v1.3 spec bh-l5sxi.2 already had. What is one level only,
  and always was, is a DIFFERENT thing with the same name: the ROW field `IssueWithCounts.parent`
  each item reports back (what `queue.by_parent` filters on) — the query parameter that selects
  which rows come back, and the field each returned row carries, are not the same contract. This
  bead corrects the false claim in `beadhive_core.queue`'s and `beadhive.work_queue`'s module
  docstrings and adds `direct_children` (the row-level narrowing a caller applies AFTER a
  recursive fetch, mirroring `beadhive.bd._has_parent_edge`'s dual top-level-field-or-dependency-
  edge check) alongside the existing one-level-only `by_parent`.

  This bead does NOT extend the correction to `work.claim-next`'s already-shipped `--epic`
  CLI-compatibility carve-out (`beadhive.work_queue.claim_next` / `work_dispatch.impl_next_`):
  whether `--epic` scoping should move onto the recursive `parent` filter is a live design
  question or bh-l5sxi.2's own successor to weigh (the CLI path's `bd children --include-infra
  --all` also widens infra/ephemeral inclusion in ways not yet checked against the HTTP
  equivalent), not something to change as a side effect of fixing a docstring. Flagged, not fixed.
- **`get_dependency_tree` for recursive membership.** Investigated as directed. It is real,
  present since the initial v1.3 pin, and does walk `parent-child` edges recursively — but it
  answers a DIFFERENT predicate than what `--mol`/molecule-scoped `ready` narrowing needs: it
  starts from ONE root and returns depth/parent-annotated nodes for a dependency-graph walk
  (`direction`, `max_depth`, an optional flat `status` filter), not a members-of-this-molecule set
  filtered to unblocked-and-open in one call. `GET /v0/beads/issues?parent=<epic>` (recursive,
  confirmed above) already gets the same "every descendant of this epic" answer more directly and
  is what this bead uses for `bh work schedule`'s children fetch — `get_dependency_tree` adds
  nothing over it for THIS use, so it is not adopted.
- **bd's own table rendering isn't reproducible from typed data.** STILL TRUE, and unrelated to
  the JSON questions above: no evidence anywhere claims a typed-row renderer can reproduce `bd`'s
  terminal tree/priority-symbol formatting byte-for-byte. Human (non-`--json`) output for both
  commands stays on the CLI-compatibility route unconditionally.
- **`--gated` release ordering composes over the CLI's own output.** Unchanged by this bead: it
  still re-derives `release_order.merge_sequence` over whatever ready rows it is handed, which is
  orthogonal to where those rows come from. Left as CLI-compatibility here (not re-plumbed to the
  API-sourced rows below) — a real, bounded follow-up, not attempted in this delivery.

### `bh work schedule`: routed through `work.issue.list`

`bh work schedule <epic>` has no narrowing flags of its own (`epic`, `--hive`, `--json` only) — the
routing question is entirely about how its ONE fetch (an epic's direct, non-closed children) is
made. `beadhive.work_dispatch.impl_schedule_payload` now tries
`beadhive.work_queue.open_children(main, entry, epic)` FIRST (pre-execution selection, mirroring
`claim_next`'s pattern exactly): it asks `beadhive_core.queue.QueueCommands.list_children` for
`GET /v0/beads/issues?parent=<epic>&sort=priority&limit=0` (recursive fetch — see above) and
narrows the result to the direct edge with `direct_children`, reproducing `beadhive.bd.children`'s
exact selection (dual top-level-`parent`-or-`parent-child`-dependency-edge check, so a closed
intermediate parent's row that only carries the edge as a dependency entry is still found). `sort`
is passed explicitly as `priority`: `GET /v0/beads/issues`'s own unset default is `created`,
NOT `bd list`'s flagless `priority` ordering — an easy silent-reorder bug this bead's real-hive
probe caught (see `to_bd_json`'s docstring and `test_list_children_narrows_a_recursive_fetch_to_the_direct_edge`).
`open_children` returns `None` (select CLI instead: `beadhive.bd.children`) exactly when the
service/capability is genuinely unavailable — the same fallback set `claim_next` already catches,
since this is a read with none of `claim_next`'s actor/seat carve-outs. `bh work schedule`'s MCP
resource (`beadhive://work/schedule/{epic}`) already threads an explicit `entry`/`main` through
`worktree.locate`, so it gets this rerouting automatically with no resource-specific change.

Byte-shape parity for the rows themselves (labels, dependencies, descriptions with quotes/
newlines, `parent`) was proven identical between `GET /v0/beads/issues` and `bd list --json` for
the same rows against a real disposable `bd serve` — see `to_bd_json` below; `schedule`'s own
payload was never byte-compared to raw `bd` bytes to begin with (`impl_schedule_payload` already
re-serializes/re-shapes into `{groups, singletons, coordinators, max_depth}`), so the row-shape
proof only needed to cover field content and order, not JSON formatting.

### `bh work ready`: wired for an unbounded `--json` read (bh-p76tk.1)

`beadhive_core.queue.QueueCommands.list_ready` (and `BeadsSession.list_ready`) are widened to
accept every one of `bh work ready`'s narrowing flags that `GET /v0/beads/ready` also accepts —
`assignee`, `unassigned`, `type`, `exclude_type`, `label`, `label_any`, `exclude_label`,
`priority`, `parent`, `has_metadata_key`, `metadata_field` — and `beadhive_core.to_bd_json` proves,
against a real disposable `bd serve`, that re-serializing the typed rows it returns is
BYTE-IDENTICAL to `bd ready --json`'s own stdout for representative rows, including ones that
previously looked like they might not survive round-tripping through Python's `json` module:

- **Key order.** `IssueWithCounts.to_dict()` emits fields in the same order the OpenAPI schema (and
  therefore bd's own Go struct) declares them — proven equal to `bd`'s own key order for rows
  carrying `dependencies`, `labels`, and an absent optional field (Unset is dropped from both
  sides identically, never emitted as `null`).
- **Escaping.** Go's `encoding/json` (every `bd` JSON writer) HTML-escapes `<`, `>`, `&`, U+2028,
  and U+2029 to `\uXXXX` by default but leaves non-ASCII as literal UTF-8; Python's `json.dumps`
  default is the exact mirror image (escapes non-ASCII via `ensure_ascii=True`, never touches the
  other five). Neither encoder's default reproduces the other's bytes alone — `to_bd_json` uses
  `ensure_ascii=False` AND an explicit escape pass for those five characters, proven byte-identical
  against real `bd` output for a title containing an emoji, `<tag>`, `&`, a quoted/newlined
  description, and a U+2028 line separator together.

bh-mu5yb.1 stopped short of wiring this into `beadhive.work_reads.ready`'s composition, for a
demonstrated, not hypothetical, hazard: `bh work ready` (unlike `bh work schedule` / `bh work
next`) never threads an explicit `entry` through `worktree.locate` — it resolves `cwd` via
`registry.hive_dir_for(cfg, hive)` and would need `registry.entry_for_dir(cfg, cwd)` to build a
session, exactly the AMBIENT-cwd resolution `beadhive.work_queue.claim_next` already uses for `bh
work next`. bh-p76tk.1 retrofits that isolation (`tests/test_work_reads.py`'s
`_isolate_ambient_hive_resolution`, autouse for the whole module — pointing `$BH_WORKTREES` at a
scratch `tmp_path` closes the SAME shadow-worktree-root branch `$GIT_WORKSPACE` alone does not,
proven directly against this hive: `registry.entry_for_dir({}, Path.cwd())` resolved to the REAL
`github/beadhive/beadhive` entry from inside this bead's own worktree with only `$GIT_WORKSPACE`
faked) and wires `beadhive.work_reads.ready`'s `--json` composition through
`beadhive.work_queue.open_ready` — but only for the ONE shape that can be reproduced byte-for-byte
without a second-guess: an UNBOUNDED read (`--limit 0`, explicit or auto-widened by
`widen_narrowed_ready_args` for any narrowing flag) whose narrowing flags all have a typed
`list_ready` equivalent. `ReadyPage` carries no total-count field, so it cannot reproduce bd's own
"Showing X of Y ready issues" truncation notice byte-for-byte — a CAPPED read (bd's own default, or
an explicit non-zero `--limit`) therefore stays CLI-compatibility unconditionally, a pre-execution
route choice rather than a fallback. `beadhive://work/ready`'s MCP resource is unchanged (out of
this bead's stated scope: `bh work ready` / `bh work schedule`'s CLI composition only).

| `bh work ready` flag | Route | Why |
|---|---|---|
| `-a/--assignee`, `-u/--unassigned`, `-t/--type`, `--exclude-type`, `-l/--label`, `--label-any`, `--exclude-label`, `-p/--priority`, `--parent`, `--has-metadata-key`, `--metadata-field` (with `--json` AND a resolved `--limit 0`) | `api-ready` (`work.ready.list`, pre-execution selection) | `QueueCommands.list_ready` accepts every one of these (see above); falls back to `cli-compatibility` only when the service/capability is genuinely unavailable, decided before any Beads read is attempted |
| any narrowing flag above, with `--json` but a CAPPED (non-zero, including bd's own default) `--limit` | `cli-compatibility` (unconditional) | `ReadyPage` has no total-count field to reproduce bd's truncation notice byte-for-byte — never attempted over the API at all, not a fallback |
| `--mol`, `--mol-type` | `cli-compatibility` (unconditional) | genuinely no HTTP equivalent — re-verified, see the gap list above |
| `--gated` | `cli-compatibility` (unconditional) | composes `release_order.merge_sequence` locally over whatever rows it is handed — orthogonal to their source, not re-plumbed here |
| `--sort`, `--label-pattern`, `--label-regex`, `--include-ephemeral`, `--include-deferred`, `--brief`, `--claim`, `--explain`, `--plain`, `--pretty`, `--max-rows`, every `bd` global flag | `cli-compatibility` (unconditional) | `bh` never specially interprets these today, narrowing or otherwise; `_api_ready_kwargs` treats any of them as "select CLI instead" |
| (no `--json`) human table rendering | `cli-compatibility` (unconditional) | bd's own terminal rendering has no typed-data equivalent |

No argv construction or test became dead code from this wiring: the CLI-compatibility forward
(`beadhive.bd.run`/`beadhive.bd.json` inside `forward_ready_plain` / `forward_ready_ordered` /
`emit_start_gated_ready`) is the fallback IMPLEMENTATION itself for every capped, gated, `--mol`,
and human-mode read, and for a genuinely unavailable service on the unbounded shape too — never a
duplicate of logic the API route now owns outright (unlike bh-sy36q.1's assign/claim/resume/
abandon, where the OLD Python-side pick/claim/re-verify implementations were fully replaced and
so were deletable). `tests/test_work_reads.py`'s existing ~40 tests are unchanged and still cover
that fallback path exactly as before; the new `work.ready.list` route and its pre-execution
carve-outs (`--mol`, a capped limit) are covered by new tests alongside them.

**`work.claim-next --epic` carve-out, re-evaluated (bh-p76tk.1).** `beadhive.work_dispatch.
impl__molecule_members` (the epic-scoped candidate set `work_queue.claim_next`'s `--epic` guard
falls back to) calls `beadhive.bd.children(epic, main, ["--include-infra", "--all"])` — needing
`bd list --parent`'s rows including INFRA types (gate/event) and CLOSED beads, narrowed to the
direct parent edge. The now-confirmed-recursive `parent` query parameter does not, by itself,
close this gap: `BeadsSession.list_issues` (the method `QueueCommands.list_children` already routes
`bh work schedule` through) forwards only `limit`/`cursor`/`parent`/`sort` — even though the pinned
OpenAPI spec's `listIssues` operation itself also publishes `all`, `include_infra`,
`include_gates`, and `include_templates` query parameters, none of them are surfaced by the
GENERATED CLIENT WRAPPER yet. Moving `--epic`'s carve-out to the API would need widening
`BeadsSession.list_issues` (a `beadhive-beads-client` change with its own tests), a new/extended
`QueueCommands` method, rewiring `work_queue.claim_next`'s guard, and — because this is a WRITE
path (claiming work), not a read — a dedicated real-service test proving exact set parity with `bd
children --include-infra --all` (recursive membership, infra inclusion, closed inclusion
together), not just a mocked-transport policy test. That is a second, materially larger unit of
work than this bead's stated ready/schedule scope, so it is NOT acted on here — recorded as a
finding and recommended as its own follow-up bead. (Separately, and unrelated to routing:
`beadhive.bd.children`'s own CLI implementation already narrows whatever `bd list --parent`
returns down to the DIRECT parent edge locally, so `impl__molecule_members`'s candidate set today
is direct children of the epic plus the epic itself, not a multi-level recursive descendant set —
worth flagging on its own terms, but out of scope here too.)

| `bh work schedule` flag | Route | Why |
|---|---|---|
| `<epic>` (children fetch) | `api-ready` (`work.issue.list`, pre-execution selection) | see above; falls back to `cli-compatibility` (`beadhive.bd.children`) only when the service/capability is genuinely unavailable |
| `--hive` | local/administrative | resolves which hive directory `bh` targets — not a Beads operation |
| `--json` | local/administrative | output-format switch over the already-computed payload |

## Lifecycle: assign, claim, resume, abandon and submit state (bh-sy36q.1)

`beadhive_core.LifecycleCommands` (`beadhive_core/lifecycle.py`) owns the policy of `bh work
assign`, `claim`, `resume`, `abandon`, the bead-state half of `submit` (the claim-holder admission
read and the `review=pending` transition), and the claim / verify / provision / release steps
`bh work next`'s CLI-compatibility loop shares: the orchestrator-only, seat, not-other and open
guards; "a claim is believed only on a re-read" (`claim_won`); release-on-failed-provisioning
only while a re-read proves the actor still holds the claim; abandon's re-read (`claim_residue`,
bh-0mckw); and every operator line. The root shell composes it at one seam,
`src/beadhive/work_lifecycle.py`.

Every Beads operation takes one named route from the matrix:

| Operation | Route | Served by |
|---|---|---|
| `work.issue.get` | `api-ready` | `SessionIssues` over `BeadsSession` (guard reads, claim re-verification, abandon's re-read, submit's admission) |
| `work.issue.update` | `api-ready` | `SessionIssues.assign` — a guarded update (`expected_version` from the guard read), so a bead that moved after the guard is refused (`409`), never overwritten |
| `work.lease.acquire` / `work.lease.release` | `cli-compatibility` | `Leases` port → `beadhive_bd_cli.CliLeases` (`bd update --claim` / reopen+unassign): `issues.claim` does not grant the renewable lease |
| `work.state.get` / `work.state.update` | `cli-compatibility` | `StateReads` / `StateOperations` → `bd state` / `bd set-state` (review, dispatch dimensions) |
| `work.gate.lookup` / `work.gate.resolve` | `cli-compatibility` | `GateOperations` (resume's orphaned-review-gate GC) |

Route selection for the two api-ready rows is made **before the first Beads operation of a
command** (the `beadhive.work_queue` rule): when the hive's supervised service cannot be used — not
running, an embedded-Dolt hive Beads 1.3 cannot serve, a missing capability — the shell selects
`bd show` / `bd assign` for that command instead. These verbs are the start of every developer
loop and the recovery path for stalled work, so they do not fail closed on hives the API cannot
serve. Once the API route is selected, a failing call is reported, never replayed through `bd`.

Worktree, identity, claim-record and state-sync effects are root-supplied capabilities behind the
`Workspace` port. The shell realises worktree mechanics through `worktree.ensure` /
`worktree.remove`, which bind the selected `worktrees.manager` (`beadhive-worktrees`, bh-055ot);
the core therefore does not depend on `beadhive-worktrees` directly. `beadhive-worktrees` declares
no bead-state port (only `WorktreeCreateObserver`, `BranchInspector`, `WorktreeInventory`), and
none of these verbs need one, so no BeadsSession adapter for such a port is supplied here.

Retained in the shell, named for bh-sy36q.6: the `--group` / `--collapse` batch claim and
`submit --group` (`work_group`), `start` (the epic seat claim), `--preview`, review-feedback
rendering (`bd comments`' markdown renderer is the operator contract and is not reproducible from
typed comment rows), and submit's gate creation, validation and Git handoff (`work.review.submit`
is `cli-compatibility`).

## Molecule filing (bh-sy36q.2)

`beadhive_core.planning` owns molecule filing: compiling a validated Beadhive spec into ONE Beads
`BatchApply` request and orchestrating the gate/kickoff/swarm conventions that request cannot
carry. `beadhive.plan_filing` is the one composition seam (`bh plan file`, and `plan_file`'s MCP
tool via `beadhive.plan.file_molecule`).

`compile_molecule` is a PURE function: given a spec, the identity/dimension labels, and (when
filing links an adopted report or the epic already exists) the adopted report ids / existing epic
id, it always returns the same `CompiledMolecule` — every issue create carries a stable
request-local key derived from its handle (`issue_key`), and the epic-parent edge plus every
declared dependency is a `dep_add` item in the SAME ordered list, so `--dry-run` preview and a real
filing share the exact lowering. `items` over the 100-entry cap raises `MoleculeTooLarge` before
anything is sent — refused outright, never chunked into multiple non-atomic requests.

| Operation | Route | Served by |
|---|---|---|
| `plan.batch-apply.atomic` | **api-ready** (bh-sy36q.2: reclassified from `cli-compatibility` — see `test_core_planning_real_service.py`) | `SessionMoleculeFiler` over `BeadsSession.batch_apply` |
| `plan.gate.create` / `plan.kickoff.update` | `cli-compatibility` (no v1.3 HTTP route for either) | `PlanningGates` port → `beadhive_bd_cli.CliPlanningGates` (`bd gate create` / `bd set-state kickoff=pending`) |

Route selection follows the same pre-execution rule as `work_queue` / `work_lifecycle`: when the
hive's supervised Beads service can be reached, `plan_filing._filer` opens it and selects
`SessionMoleculeFiler`; otherwise (no service running, an embedded-Dolt hive Beads 1.3 cannot
serve, a missing capability) it selects `CliMoleculeFiler` — a thin interpreter of the SAME
compiled item list, walking it one `bd create` / `bd dep add` at a time rather than reimplementing
the molecule contract a second way. Selection happens once, before the first Beads operation of
the command; a failure after that point is reported, never silently retried on the other route.

`PlanningCommands.file` composes both: compile once, submit once (`MoleculeFiler.apply`, resolving
every key to the id it was bound to), then `create_swarm` / `create_kickoff_gate` per root /
`set_kickoff_pending`, and `create_release_hold_gate` for every `release:breaking` bead when the
hive's `release.enforce_hold` is on. `CliPlanningGates` is the ONE authoritative implementation of
the kickoff-gate contract (moved here from `beadhive.plan`'s `_create_swarm` /
`_create_kickoff_gate` / `_set_kickoff_pending` / `_create_release_hold_gate`): `bh plan repair`
calls the SAME methods, so the gate description format the read-side convention checks
(`plan._names_kickoff_for`) cannot drift between the two callers.

**Narrowed out of this bead** (left on the pre-existing `bd`-read implementation, unchanged):
molecule verification (`plan.verify_epic` / `_verify_loaded`) and repair's convention-checking
reads (`plan.repair_epic`) stay in `beadhive.plan` — both are already-correct `cli-compatibility`
reads per the matrix (`plan.molecule.verify` / `plan.molecule.repair`), and reimplementing their
~275 lines of accumulated edge-case policy (nested-epic gating, satisfied-vs-genuine roots,
closed-dimension checks — each with its own historical-bug citation) as core policy would be a
large, high-regression-risk mechanical port for no routing benefit, since nothing about them moves
to `BeadsSession`. `show` / `status` / `adopt`'s frame-seeding also stay as-is (read-only rendering
and pure spec shaping, no Beads write). See the bead's NOTES for the full narrowing rationale.

An epic carrying native `source_system` provenance (an adopted report with a system-of-record,
not just an `external_ref`) is still born via `bd import` (`plan_filing.import_epic`):
`ApplyCreateItem` has no member for `source_system` — settable only at bead creation — so this is
the one piece of filing BatchApply genuinely cannot express. Every other molecule primitive (the
child issues, every parent-child and declared-dependency edge, and any adopted-report link) still
compiles into the ONE BatchApply request that follows, addressing the epic by the id `bd import`
returned rather than a request-local key.

## Molecule progress, swarm inspection, dispatch polling, and local-loop bead-state access (bh-sy36q.5)

`beadhive_core.dispatch` (`DispatchCommands`) serves the bh-97fo0.3 matrix's four named routes for
the `local` work-runtime tier's poll loop (`beadhive.localloop.LocalLoop`) and its `--epic`
molecule-scoping (`beadhive.work_dispatch.impl__molecule_members`) — nothing here is a generic
"read a bead" abstraction; each route is a distinct call site, attributed separately in routing
telemetry, even where two share one underlying capability:

| Operation | Capability / session method | Served by | Existing call site |
|---|---|---|---|
| `work.molecule.progress` | `issues.get` / `get_issue` | `DispatchCommands.molecule_progress` | `LocalLoop.load_molecule`'s epic-row read |
| `work.local-loop.state` | `issues.get` / `get_issue` | `DispatchCommands.local_loop_state` | `LocalLoop._default_routing`'s per-bead read |
| `work.swarm.inspect` | `issues.list` / `list_issues` | `DispatchCommands.swarm_members` / `.event_rows` | `LocalLoop.load_molecule`'s children + per-child event-stream reads; `work_dispatch.impl__molecule_members`'s `--epic` scoping |
| `work.dispatch.poll` | `ready.list` / `list_ready` | `DispatchCommands.poll_ready` | `LocalLoop.claimable_now`'s ready-set poll |

`BeadsSession.list_issues` is widened (bh-sy36q.5) with `status` / `all_` / `include_infra`
kwargs, reproducing `bd list`'s own `--all` / `--include-infra` override of its default
status/infra exclusions — the generated `list_issues.sync_detailed` call already accepted them
(`all_`, `include_infra`), only the wrapper method didn't expose them yet. Without this, a
molecule's full RESTARTABLE membership (closed children, so finished work reads as finished; infra
rows such as event beads, so the loop-breaker's `attempt_count` and the escalation latch see their
history) would be unreachable over the API at all.

`DispatchCommands.swarm_members` fetches `GET /v0/beads/issues?parent=<epic>&all=true
&include_infra=true&sort=priority&limit=0` and narrows to the direct parent edge with
`beadhive_core.queue.direct_children` — the same `list_children` pattern `bh work schedule` uses,
with the fuller default-exclusion override this cohort's callers need. **Measured against a real
disposable `bd serve` 1.3.0 (f45b249ce) while proving this bead** (`test_core_dispatch_real_service.py`):
unlike `GET /v0/beads/ready?parent=<epic>` (proven recursive-descendant in bh-mu5yb.1's own probe),
`GET /v0/beads/issues?parent=<epic>` returned ONLY the direct child in a three-level epic → child-epic
→ grandchild-leaf tree — the two operations' `parent` query parameters, despite identical OpenAPI
wording ("restrict to recursive descendants of this issue"), do not behave identically on this
server build. This does not change any Beadhive behavior (`direct_children`'s narrowing is a no-op
safety net either way, and `load_molecule`'s own children fetch never needed more than direct
membership), but it corrects an assumption a future caller of `list_issues(parent=...)` should not
inherit from `list_ready`'s own proof.

`DispatchCommands.event_rows` fetches the SAME shape for one bead's own dotted-id stream
(`parent=<bead>`, not narrowed to the direct edge — mirrors `beadhive.bd.child_rows` exactly: an
old event bead can retain bd's historical prefix match even without a `parent` edge, and narrowing
here would silently turn populated history into an empty one).

A gate bead is **not** reachable through either of `swarm_members` / `event_rows`: an ad-hoc gate
(`bd gate create --blocks <id>`) carries no `parent-child` edge to anything (confirmed against the
real service), so it is invisible to a `parent`-scoped `list_issues` fetch regardless of
`include_infra`. `LocalLoop` does not need it to be — gating is read through the ready predicate
(`poll_ready`) and the CLI-compatibility `bd gate check`, unaffected by this bead.

Route selection is the composition seam `beadhive.dispatch_state` (mirrors `work_queue` /
`work_lifecycle` exactly): each of the four fetches is tried independently, before execution,
falling back to its own named `bd` forward on a genuinely unavailable service or capability — one
route being unreachable does not force the others onto the CLI path too. Process scheduling and
role execution (spawning a seat, the CANCEL ladder, process-group reaping) are untouched: this
cohort owns only the bead-state reads those flows depend on. Polling is the whole mechanism; event
streaming is explicitly a later, out-of-scope optimization (design note, bh-sy36q.5).

**Narrowed out of this bead** (left on the pre-existing `bd`-read implementation, with reason,
recorded in the bead's NOTES): the hive-level dispatch picker
(`beadhive.dispatch_hive_run.kicked_off_ready_epics`, `bh host dispatch run`'s `bd ready --limit 0`
poll) — a distinct, deliberately-dumb supervisor-level picker that takes only a bare `hive_dir`,
not a registry `entry`; `bh work readiness` (`beadhive.work_reads.molecule_readiness_payload`) and
`bh plan swarm` (`beadhive.plan`'s `bd swarm list` / `bd swarm status` reader) — neither is named
by any `work.*` route in the bh-97fo0.3 matrix, and `bh plan swarm` reads a DIFFERENT Beads
construct (`bd swarm`, a `type:molecule`/`mol_type:swarm` tracking bead) than Beadhive's own
epic-parented molecule this cohort's four routes serve.

## CLI composition cutover (bh-sy36q.6)

The last cohort bead of bh-sy36q. It makes the composition decision explicit and singular, deletes
what the four cohorts (bh-sy36q.1 lifecycle, bh-sy36q.2 filing, bh-sy36q.5 dispatch reads,
bh-p76tk.1 ready/schedule) left superseded, and names the root surface that stays on purpose.

### One composition decision

`beadhive.beads_routing` is the one place the migrated cohorts open a Beads session.
`work_lifecycle`, `plan_filing`, `work_queue` and `dispatch_state` each keep their
`hive_session` / `session_factory` test seam, but every one is now a single call into
`beads_routing.hive_session(main, entry, <COHORT>_CAPABILITIES)`. The four copies of the
resolve-and-map-errors body, and of the "which errors mean select the `bd` route" tuple, are gone.

- **Default: beadhive-core.** A capable session opens and the command runs through its
  `beadhive_core` handler.
- **Pre-execution `bd` compatibility route (kept, see the decision below).** No capable session
  means `SessionUnavailable` / `ServiceError` / `IncompatibleService` before the first Beads
  operation, and the seam selects its named `bd` route for the whole command. It is never a retry.
- **Bounded rollback: `BH_BEADS_ROUTE=cli`.** Makes `hive_session` refuse before resolving
  anything, so every cutover command takes its `bd` route at once, with the service still
  running. Unset, empty or `api` selects the default. Any other value renders one `✗` line and
  exits 2 (`BeadsRouteInvalid`, a `typer.Exit` subclass). It is deliberately not one of the
  errors the seams map to the `bd` route, so a typo cannot silently reroute a command or silently
  fail to. The switch ships with the release that first carries this cutover and is removed in
  the release after it. The follow-up that deletes it has to touch only `selected_route` and its
  tests.

The audit found that the per-verb pre-execution selection already was the only second path. No
verb had a separate old Python implementation left to switch to: bh-sy36q.1 and bh-sy36q.2 had
already deleted theirs in place, and bh-sy36q.5 and bh-p76tk.1 had deleted nothing because their
`bd` forwards *are* the compatibility route. So the rollback forces that existing route. It does
not preserve dead code.

`approve` / `bounce` (`work_review`, bh-bwnys.1) are deliberately outside the switch. They have no
`bd` implementation to fall back to and still fail closed without a service.

### Decision: keep the pre-execution `bd` route for assign / claim / resume / abandon

bh-sy36q.1 left these verbs selecting the `bd` route, instead of failing closed, when the hive's
Beads service cannot be used. That route is **kept deliberately** as named compatibility surface,
not retired:

- Beads 1.3 cannot serve an embedded-Dolt hive at all. Retiring the route would break every
  developer loop on those hives, because `claim` is the first step of every loop.
- `abandon` is the stall-recovery path. It must work when the service is the thing that stalled.
- The same rule already governs `work.claim-next`, `schedule`, `ready --json`, `plan file` and
  the dispatch reads, so retiring it for four verbs would leave the cohorts inconsistent.
- It is not a retry. Once a command selects the API, a failing call is reported and never replayed
  through `bd`. That keeps the ADR's ambiguous-write rule intact.

The rollback switch is temporary. This automatic route is not, and stays after the switch is
removed.

### Superseded: `work.beads.route` makes the `bd` route opt-in (bh-m36pc)

The decision above is superseded by operator decision bh-fqsp2: the API is the only default route.
The automatic selection is kept, but only as a named per-hive opt-in, the `work.beads.route` key
(`beadhive.beads_routing.route`, per-hive > global > `api`):

- `api` (default): a seam that cannot open a capable session calls
  `beads_routing.allow_cli_route`, which renders one `✗` line naming
  `bh host beads start --hive <hive>` and the key, and raises `BeadsServiceRequired` (a
  `typer.Exit`, exit 1) from the original error. No `bd` route is selected. Every error branch a
  seam used to map onto its `bd` route goes through this gate, including `work_queue` /
  `dispatch_state`'s `RouteMismatch` / `OperationDenied` / `UnknownOperation` / `OSError` /
  `ValueError` branch. Pre-execution CLI routes that are not a fallback (`claim_next`'s bare
  actor, `--epic` scoping, capped `ready` reads) are unchanged.
- `api+cli-fallback`: exactly the automatic selection described above.
- `cli`: `hive_session` refuses before resolving anything, exactly like `BH_BEADS_ROUTE=cli`.

An unknown value renders one `✗` line and exits 2 (`BeadsRouteConfigInvalid`); the config
schema (`WorkConfig.beads.route`, a `Literal`) refuses it at `bh config set` and `validate`
time too. `BH_BEADS_ROUTE`, while it exists, still wins with its old meaning (`api` there is
`api+cli-fallback`). Embedded-Dolt hives, which Beads 1.3 cannot serve, must opt in explicitly.

### What was already cut over, and what this bead changed

| Area | Found | Changed here |
|---|---|---|
| assign / claim / resume / abandon, submit state | `work.py` delegates to `work_lifecycle` with no second implementation (bh-sy36q.1) | session opened via `beads_routing` |
| `plan file` | `plan_filing` + `beadhive_core.planning` (bh-sy36q.2) | same; `PlanError` and the one `MoleculeGraph` now come from here |
| ready / schedule / claim-next | `work_queue` (bh-l5sxi.2, bh-mu5yb.1, bh-p76tk.1) | same |
| local-loop and `--epic` reads | `dispatch_state` (bh-sy36q.5) | same |
| check / submit / review / merge / schedule payload | still went through `WorkLifecycleService`, a callback pass-through with one `CallbackBeadStore` port | layer deleted; `work.py`, `work_show.py` and `mcp.py` call the `impl_*` functions directly |
| plan validate / verify / approve / repair | still went through `PlanningService`, a callback pass-through | layer deleted; plain functions in `beadhive.plan` |
| `bh plan show`, `--dry-run` and `plan_file` preview ordering | root-side `MoleculeGraph` duplicate (flagged by bh-sy36q.2) | uses `beadhive_core.MoleculeGraph` through `plan_filing.molecule_graph`, so it is the same graph the compiler lowers. A malformed graph still raises `ValueError` |

### Deletion inventory

Source:

- `src/beadhive/work_services.py` (whole file)
- `src/beadhive/planning_services.py` (whole file)
- `src/beadhive/modules/planning/` (whole package: `PlanningService`, its four ports, eight
  request/result DTOs, `PlanningError`, and the duplicate `MoleculeGraph`)
- `src/beadhive/modules/work/application/services.py` (`WorkLifecycleService`)
- `src/beadhive/modules/work/contracts/ports.py` (`BeadStore`, `ExecutionPort`,
  `ValidationEvidenceStore`, `IdentityProvider`, `WorkNotifier`)
- `src/beadhive/modules/work/domain/models.py` (ten lifecycle request/result DTOs)
- `plan._opt`, dead since filing moved to the compiler
- four duplicated `hive_session` bodies and four error tuples in the cohort seams

`modules/work` keeps only impact resolution.

Tests:

- `tests/unit/modules/work/test_work_lifecycle_services.py`
- `tests/contracts/test_work_capability_adapters.py`
- `tests/unit/modules/planning/test_planning_services.py`
- `tests/unit/modules/planning/test_planning_independence.py`
- `tests/contracts/test_planning_capability_adapters.py`. Its one real assertion, the kickoff-gate
  `bd gate create` argv, moved to `tests/test_plan.py`.

FakeBd branches that nothing reaches, found with branch coverage over the six cohort test files:

- `tests/test_work_queue.py` FakeBd: the `update --status` and `set-state` branches
- `tests/test_plan.py` FakeBdApprove: the `create` branch

Registry: the `module.planning` test closure is declared `absent`. `module.work`'s port is now
`contracts/impact.py`. `tests/BUILD` and both Pants proven-test manifests drop the deleted files.

Every other FakeBd and harness was checked by reference and coverage, and each is still load-bearing:

- `test_work.py` FakeBd serves submit, merge, finish, resume and `bd` route tests.
- `test_plan.py` FakeBd is the only end-to-end proof of the CLI filing fallback.
- `test_work_next.py` FakeBd models the race for the CLI claim loop.
- `test_localloop.py` FakeBd drives the dispatch fallbacks.
- `test_plan_repair.py` FakeBdRepair covers repair.

The `integration` real-`bd` tests exercise the retained `bd` routes and topology, not
superseded code, so none were deleted.

### Intentionally retained CLI administration and compatibility surface

> **2026-09-28 (bh-o3xuf).** The `bd` argv routes behind items 1 and 3 — `CliIssues`,
> `CliLeases`, `CliStateReads`, `CliStateOperations`, `CliGateOperations`, `CliMoleculeFiler`,
> `CliPlanningGates`, the `show` / `children` / `child_rows` / `ready` reads, and every
> `COORDINATION_OPERATIONS` wrapper — moved to the
> [`beadhive-bd-cli`](../beadhive-bd-cli/README.md) library package, which root resolves lazily
> by name (`beadhive.bd_cli`). The route *selection* stays in root, as below. Still in root, over
> inline `bd` argv: submit's gate creation (`work.review.submit`), the `bd import` epic birth,
> and everything in items 4–7.

In the root package, over named `bd` routes:

1. **Pre-execution `bd` route when no capable Beads service is available** (kept, decision
   above). It covers:
   - assign, claim, resume, abandon and submit's admission read (`CliIssues`: `bd show` / `bd
     assign`)
   - `work.claim-next`'s pick/claim/re-verify loop (`work_next` / `work_dispatch`: `eligible`,
     `claim_won`, `decline`, `_try_claim`)
   - schedule's children fetch (`bd.children`)
   - unbounded `ready --json` (`bd ready` forward)
   - `plan file` (`CliMoleculeFiler`)
   - the local loop's molecule, swarm, event and poll reads
2. **The bounded rollback**, `BH_BEADS_ROUTE=cli`, for one release.
3. **Matrix `cli-compatibility` operations with no v1.3 HTTP route**, always on `bd`:
   - `work.lease.acquire` / `release`
   - `work.state.get` / `update`
   - `work.gate.lookup` / `resolve`
   - `plan.gate.create` / `plan.kickoff.update` (`CliPlanningGates`: swarm, kickoff gates,
     `kickoff=pending`, release-hold gates)
   - `work.review.submit` (submit's gate creation, validation and Git handoff)
   - every `COORDINATION_OPERATIONS` lease, heartbeat, reclaim and merge-slot operation
   - the `bd import` epic birth for native `source_system` provenance
4. **Inherited from bh-sy36q.1:**
   - batch `claim --group` / `--collapse` and `submit --group` (`work_group`)
   - `start` (epic seat claim)
   - `--preview`
   - review-feedback rendering. Decided here that it stays shell presentation: `bd comments`'
     markdown renderer is the operator contract, and typed comment rows cannot reproduce it.
5. **`bh work ready` shapes with no byte-identical API equivalent:**
   - capped reads
   - `--mol` / `--mol-type`
   - `--gated`
   - every other forwarded `bd ready` flag
   - human table output
6. **`bh work next --epic` and undeclared-actor claims.** `--epic` stays a listed CLI route;
   moving it is bh-sy36q.9.
7. **Verbs no cohort migrated, which stay shell-owned per the ADR:**
   - `check`, `submit` (beyond its state half), `review`, `merge` / `finish` / `land`,
     `refine` / `show`
   - `brief` / `issue` / `list` read forwards
   - the intake verbs and `readiness`
   - `loop`'s process orchestration
   - `bh host dispatch run`'s hive-wide picker
   - `bh plan verify` / `approve` / `repair` / `show` / `status` / `check` / `adopt` / swarm
     reads
8. **Administration:**
   - the `bh bd` passthrough
   - `bh host beads` service lifecycle
   - backup, migration, repair and federation
   - hive administration

### Measured (bh-sy36q.6)

Measured on this bead's tree against its fork point (container `0cec83c6`, main `829425d9`) on
a 32-core host. None of these are estimates.

| Measure | Before | After |
|---|---|---|
| `src/beadhive` Python lines | 145,793 | 144,924 (+245 / −1,114) |
| `tests/` Python lines | 184,091 | 184,042 (+381 / −430) |
| `packages/*/src` Python lines | 38,613 | 38,613 (unchanged) |
| Root selected suite (`not integration and not pants_profile`) | 9,514 | 9,541 (−13 deleted, one of them relocated; +40 new) |
| Root `integration` (real `bd` harness) | 74 | 74 |
| Root selected tests that spawn a real `bd` / `dolt` process | 188 | 188 |
| Packages suite (`packages/*/tests`) | 279 (16 opt-in `real_service`) | 279 (16 opt-in `real_service`) |
| Packages suite tests that spawn a real `bd` / `dolt` process | 0 | 0 |
| Packages suite wall time (`-n auto`, hermetic, MockTransport / in-memory) | — | 6.3 s (pytest 4.75 s; serial 5.8 s) |
| Full gate `bh work check bh-sy36q.6` (`just check-native`) | — | in bh-sy36q.6's NOTES ¹ |

¹ The gate validates this exact tree, so its wall time cannot be written into this file without
changing the tree the verdict is keyed on. It is recorded on the bead instead.

The "spawn a real process" rows were measured, not inferred from markers. `bd` and
`dolt` shims that log `$PYTEST_CURRENT_TEST` went first on `PATH` inside the hermetic fence, and
the full root selection ran under it: 515 spawns from 188 distinct tests, and zero from the
packages suite. The count is a lower bound: a test that execs `bd` by absolute path, or drops
`PATH`, is not seen.

The "before" spawn count is the measured after-count adjusted by the tests that changed:

- **Added:** 40 tests. Measured: none of them appears in the spawn log.
  `test_beads_routing.py` drives the real `bh work claim` / `assign` composition over a
  MockTransport session and `test_work`'s FakeBd.
- **Deleted:** 13 tests. They cannot spawn by construction: two are independence tests that
  monkeypatch `subprocess` to fail, and the rest call pure services over fake ports.

The cutover removed no stateful tests because nothing stateful was redundant. Every real-`bd`
test left in the root suite exercises a retained `bd` route or real topology (worktrees,
onboarding, doctor, hub), not superseded bead-access code.

## Tests

- `test_core_routing_policy.py` — every matrix row resolves to exactly one typed route; capability
  gating, denial, and route-kind mismatches; `call_api` dispatch against a generated-client
  transport fixture. No Beads state emulator.
- `test_core_routing_real_service.py` — opt-in (`BEADS_ROUTING_SCRATCH=1 ... -m real_service`)
  proof that the routed `work.merge-slot.acquire` CLI path is still exclusive under contention,
  against a disposable scratch hive this test creates itself (never a managed hive).
- `test_core_review_policy.py` — policy over generated-client transport fixtures and fake ports.
- `test_core_package_boundary.py` — import independence and operation-matrix route classification.
- `test_core_review_real_service.py` — opt-in (`-m real_service`) proof of the HTTP writes and
  their audit actor against a scratch hive's service started with `bh host beads start`; see its
  module docstring.
- `test_core_queue_policy.py` — ready-row shaping, eligibility/decline/parent policy, and
  claim-next dispatch (including the pre-execution `CapabilityMissing` refusal) over generated-
  client transport fixtures. No Beads state emulator.
- `test_core_queue_real_service.py` — opt-in (`BEADS_QUEUE_SCRATCH=1 ... -m real_service`) proof
  that `work.claim-next` is exclusive under real contention (several real OS threads racing one
  ready bead), against a disposable OWNED-mode (`bd init --server`) scratch hive this test creates
  and tears down itself — embedded-Dolt scratch hives cannot serve `bd serve` at all.
- `test_core_queue_policy.py` (bh-mu5yb.1 additions) — `direct_children`'s dual top-level-field-or-
  dependency-edge check and its exclusion of a recursive grandchild; `to_bd_json`'s byte-parity
  against a frozen sample captured from a real `bd serve` (emoji, HTML-special characters, a U+2028
  line separator, quoted/newlined description, together); `list_ready`'s widened narrowing kwargs
  reaching the wire under the right parameter names, and staying absent when unset; `list_children`
  narrowing a recursive `GET /v0/beads/issues?parent=` fetch to the direct edge.
- `packages/beadhive-beads-client/tests/test_session.py` (bh-mu5yb.1 additions) — `list_ready` /
  `list_issues`'s widened kwargs land on the wire under `GET /v0/beads/ready` / `GET
  /v0/beads/issues`'s own parameter names; an unrecognized `sort` value is refused before any
  request is sent.
- `tests/test_work_queue.py` (bh-mu5yb.1 additions, `beadhive` package) — `open_children` routes
  through the API and narrows to the direct edge; falls back to `None` when the service is
  unavailable; `schedule_payload` prefers the API route and never shells out to `bd` when it
  succeeds (mock-transport, pre-execution selection). `test_next_releases_and_refuses_a_seat_mismatch_against_a_real_service`
  — opt-in (`BEADS_QUEUE_SCRATCH=1 ... -m real_service`) real-service proof of the seat-mismatch
  auto-release path bh-l5sxi.2 left only policy/mock-tested (see
  `test_next_releases_and_refuses_a_seat_mismatched_atomic_claim` for the mock counterpart): a
  declared `dev/` actor that atomically wins an EPIC via a genuine `bd serve` 1.3.0 is released
  back in the same session, reported as refused, and the epic is verified `open`/unassigned in the
  real store afterward — against a disposable OWNED-mode scratch hive this test creates and tears
  down itself, reaping BOTH the Dolt SQL server AND the `bd db-proxy-child` TCP proxy `bd init
  --server` also spawns (the second process bh-l5sxi.2's own contention test left running; see
  `_reap_owned_scratch_hive`'s docstring).
- `tests/test_work_reads.py` (bh-p76tk.1 additions, `beadhive` package) — `_isolate_ambient_hive_
  resolution` (autouse) and its guard test proving `registry.current_hive`/`entry_for_dir` report
  "no hive here" from this process's own real cwd once isolated; `_api_ready_kwargs` mapping every
  narrowing flag and rejecting `--mol`/`--mol-type`/any unrecognized flag; `ready_via_api` routing
  an unbounded (`--limit 0`, explicit or auto-widened) `--json` read through the API (mock
  transport, byte-identical `to_bd_json` output) and falling back to CLI-compatibility when the
  service is unavailable, when `--mol` is present (never even opening a session), or when the
  resolved limit is capped (never attempted over the API at all).
- `test_core_lifecycle_policy.py` (bh-sy36q.1) — assign / claim / resume / abandon / submit-state
  policy over a generated-client transport fixture (the api-ready issue route) and fake ports for
  the lease, state, gate and workspace capabilities; route classification of every operation the
  cohort touches. No Beads state emulator.
- `test_core_lifecycle_real_service.py` — opt-in (`BEADS_LIFECYCLE_SCRATCH=1 ... -m real_service`)
  proof, against a disposable OWNED-mode scratch hive it creates and reaps, that assign's guarded
  update is attributed to the orchestrator and refuses a stale read after a competing real claim,
  and that the CLI lease is verified by the HTTP re-read (claim, a refused steal, abandon, and a
  foreign-actor release refused by bd's anti-steal fence and reported, not ✓).
- `tests/test_work_lifecycle_shell.py` (`beadhive` package) — the shell's `bd` compatibility routes
  and their argv, pre-execution route selection, and the `bh work assign` / `claim` / `abandon` /
  `resume` command contract over a transport-fixture session (claim output is byte-identical on
  both routes).
- `test_core_planning_policy.py` (bh-sy36q.2) — the pure compiler (keyed creates then edges, label
  lowering, an existing-epic-id molecule with no epic create, the 100-item cap refusal, preview
  and apply sharing one lowering); `PlanningCommands.file` over a generated-client transport
  fixture (the api-ready BatchApply route) and a fake `PlanningGates`, including the refused/
  indeterminate-write mapping to `MoleculeFilingFailed`. No Beads state emulator.
- `test_core_planning_real_service.py` — opt-in (`BEADS_PLANNING_SCRATCH=1 ... -m real_service`)
  proof, against a disposable OWNED-mode scratch hive it creates and reaps, that one molecule
  (epic + two issues + their parent-child and declared-dependency edges) lands atomically in ONE
  request with its keys resolved to real ids, and that a request refused partway (an id collision)
  writes NOTHING — no orphaned epic or issue to reconcile.
- `packages/beadhive-beads-client/tests/test_session.py` (bh-sy36q.2 addition) —
  `BeadsSession.batch_apply` posts `issues:batchApply` verbatim and returns the generated
  `ApplyBatchResponse` (key-to-id map + per-item results) untouched.
- `tests/test_plan.py` (`beadhive` package) — the CLI-compatibility fallback exercised end to end
  against a real git hive (identity-triplet resolution, complexity/dimension labels, declared
  deps and epic-parent edges as separate `dep add` calls, release-hold gates, and adopted-report
  linking including the native-`source_system` `bd import` birth); one test proves the shell
  selects the api-ready route instead when a session opens, submitting the whole molecule in one
  `BatchApply` request while gates/kickoff still go through the same `bd` route either way.
- `test_core_dispatch_policy.py` (bh-sy36q.5) — every route name traced to the installed matrix;
  `molecule_progress` / `local_loop_state` each re-derived per call (never cached) and attributed
  as distinct named routes even though both call `get_issue`; `swarm_members` narrowing a
  recursive fetch to the direct edge and keeping closed/infra rows; `event_rows` NOT narrowing to
  the direct edge; `poll_ready` re-derived per call and honoring `parent` scoping; capability
  gating refuses before any request reaches the transport. Generated-client transport fixtures
  only — no FakeBd, no Beads state emulator.
- `test_core_dispatch_real_service.py` — opt-in (`BEADS_DISPATCH_SCRATCH=1 ... -m real_service`)
  proof, against a disposable OWNED-mode scratch hive it creates and reaps: a read taken after an
  external `bd` write (standing in for a restart) sees the new state immediately; a closed child
  and a closed infra event row are visible only once `all_`/`include_infra` are asked for; a
  dependent bead moves from absent to present in `poll_ready`'s result the instant its blocker
  closes; two independently-opened sessions polling concurrently observe the identical
  transition. This is also where the `list_issues(parent=...)` non-recursion finding (see the
  section above) was measured.
- `packages/beadhive-beads-client/tests/test_session.py` (bh-sy36q.5 addition) —
  `list_issues`'s widened `status` / `all_` / `include_infra` kwargs land on the wire under
  `GET /v0/beads/issues`'s own parameter names, and stay at their bd-compatible defaults
  (`all=false`, `include_infra=false`) when omitted.
- `tests/test_dispatch_state.py` (new, `beadhive` package) — the composition seam itself:
  `open_molecule_progress` / `open_local_loop_state` / `open_swarm_members` (narrowing a recursive
  fetch to the direct edge, mock-transport) / `open_event_rows` (NOT narrowed) / `open_poll_ready`
  (forwarding `parent`) each route through the API when a session opens and fall back to `None`
  when it does not — the same pre-execution-selection discipline `tests/test_work_queue.py` proves
  for `open_children` / `open_ready`.
- `tests/test_localloop.py` (`beadhive` package) — the EXISTING `FakeBd`-backed fixtures keep
  passing unmodified (the new route attempt reliably fails to open — no real Beads service in the
  isolated test environment — and falls through to the same CLI-compatibility forward those tests
  already exercised); new additions prove `load_molecule` / `claimable_now` / `_default_routing`
  each try their named `dispatch_state.open_*` route BEFORE reaching `bd` (mocked to answer, `bd`
  never invoked) and that one routed fetch answering does not force the other two onto the CLI
  path, nor the reverse. `tests/test_localloop_int.py` (unmodified) re-proves restart-is-a-no-op
  against a REAL `bd` process with the routed reads wired in underneath it.
- `tests/test_work_next.py` (`beadhive` package) — one addition proving `_molecule_members`
  (the `--epic`-scoping read `bh work next --epic` and the loop share) selects
  `dispatch_state.open_swarm_members` before `bd children`, and that a successful routed answer
  means `bd` is never reached for it at all.
- `tests/test_beads_routing.py` (bh-sy36q.6, `beadhive` package): the one composition decision.
  It covers:
  - the `BH_BEADS_ROUTE` switch: default, `cli`, and a mistyped value refused with exit 2 and
    one diagnostic;
  - every cohort seam opening its session through `beads_routing` with its own capability set;
  - rollback selecting each seam's `bd` route before the service is even resolved;
  - a mistyped value failing every seam instead of rerouting it;
  - `bh work claim` byte-identical under rollback;
  - `bh work assign` writing over HTTP by default and through `bd` under rollback;
  - claim refusal keeping exit 1 and its `✗` diagnostic;
  - the migrated verbs' command names and options as an explicit table, alongside
    `tests/test_cli_projection.py`'s hash-pinned Click inventory.
