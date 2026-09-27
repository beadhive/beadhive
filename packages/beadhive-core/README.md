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
`CliGateOperations` / `CliStateOperations` and `coordination.py` are the existing examples).
Every selector accepts an optional `RoutingObserver` so the chosen route can be attributed in
telemetry.

`COORDINATION_OPERATIONS` names every lease, heartbeat, reclaim, merge-slot and gate operation
that must stay `cli-compatibility`: their exclusivity and staleness guarantees come from the real
service or the real `bd` binary (`tests/test_coordination_int.py`, `tests/test_merge_slot.py`),
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

### `bh work ready`: the reusable API surface exists; the CLI composition is not switched over

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

This is real, tested, reusable infrastructure — but this bead does NOT wire it into
`beadhive.work_reads.ready`'s composition. The reason is a demonstrated, not hypothetical, hazard:
`bh work ready` (unlike `bh work schedule` / `bh work next`) never threads an explicit `entry`
through `worktree.locate` — it resolves `cwd` via `registry.hive_dir_for(cfg, hive)` and would need
`registry.entry_for_dir(cfg, cwd)` to build a session, exactly the AMBIENT-cwd resolution
`beadhive.work_queue.claim_next` already uses for `bh work next`. That resolution does not consult
`cfg` at all in its "shadow worktree path" branch — it synthesizes a hive triplet purely from
`cwd`'s OWN path segments, live in production and unmockable by faking `config.load`. Run from
inside a real bead worktree of THIS hive (`bh-mu5yb.1`'s own dev environment, mid-bead, right now),
`registry.entry_for_dir({}, Path.cwd())` resolves to the REAL `github/beadhive/beadhive` entry —
and this hive has a genuinely running, `bh host beads status`-verified `bd serve` at the time this
was checked. `beadhive.work_next` / `beadhive.work_queue`'s existing test suite avoids this
because its `nexthive` fixture isolates `$GIT_WORKSPACE` (and `$WS_WORKTREES`, `$HOME`) to a
throwaway `tmp_path`, so `entry_for_dir` resolves against an isolated, inert path instead —
`tests/test_work_reads.py`'s ~30 existing `ready`-related tests carry no such isolation today (they
never needed it: the old code never called `entry_for_dir` or opened a session at all). Retrofitting
that isolation safely across an existing, unrelated-in-most-cases test file is a properly-scoped
follow-up, not a same-bead addition bolted on to avoid a real, currently-live cross-test hazard on
shared/dev machines. Until that follow-up lands, EVERY flag of `bh work ready` — narrowing,
`--gated`, `--mol`/`--mol-type`, and plain — resolves to the CLI-compatibility route, selected
unconditionally and explicitly (not by catching an API failure): `beadhive.work_reads.ready` and
its helpers are unchanged by this bead. `beadhive://work/ready`'s MCP resource is unchanged for the
identical reason.

| `bh work ready` flag | Route | Why |
|---|---|---|
| `-a/--assignee`, `-u/--unassigned`, `-t/--type`, `--exclude-type`, `-l/--label`, `--label-any`, `--exclude-label`, `-p/--priority`, `--parent`, `--has-metadata-key`, `--metadata-field` | `cli-compatibility` (unconditional) | `QueueCommands.list_ready` accepts every one of these today (see above) — not wired into the CLI composition seam pending the `entry`-threading follow-up above |
| `--mol`, `--mol-type` | `cli-compatibility` (unconditional) | genuinely no HTTP equivalent — re-verified, see the gap list above |
| `--gated` | `cli-compatibility` (unconditional) | composes `release_order.merge_sequence` locally; not re-plumbed to API-sourced rows this bead |
| `-n/--limit`, `--sort`, `--label-pattern`, `--label-regex`, `--include-ephemeral`, `--include-deferred`, `--brief`, `--claim`, `--explain`, `--plain`, `--pretty`, `--max-rows`, every `bd` global flag | `cli-compatibility` (unconditional) | forwarded verbatim; `bh` never specially interprets these today, narrowing or otherwise |
| (no `--json`) human table rendering | `cli-compatibility` (unconditional) | bd's own terminal rendering has no typed-data equivalent |

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
| `work.lease.acquire` / `work.lease.release` | `cli-compatibility` | `Leases` port → shell `CliLeases` (`bd update --claim` / reopen+unassign): `issues.claim` does not grant the renewable lease |
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
