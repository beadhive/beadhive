# Spike `bh-gj1hb` — the bead tracker adapter port: contract, boundary, and the bd projection

**Bead:** `bh-gj1hb` · **Seat:** `dev/port` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-x6f9l` (adopt the bead tracker adapter port and the GitHub adapter
transport), under epic `bh-ire9a`. Consumed by the sibling design molecules for the tracker
plugin category (prerequisite 2) and the external-intake module (prerequisite 3).
**Builds on:** [bh-a1svm](bh-a1svm-github-transport.md) (transport: githubkit, GO) and
[bh-mqvdq](bh-mqvdq-github-identity.md) (credentials, actor, tri-state write probe, GO).

## Question

Can intake features depend on **one named, tracker-neutral port**, with GitHub as a separately
packaged adapter behind it and a one-way dependency rule that tooling enforces? The port must
cover:

- listing issues, closed ones included, with a checkpoint;
- listing comments with author identity, edits and deletions, plus a full id-set reconcile
  (GitHub's `since` filter cannot report a deletion);
- posting a comment on an existing issue, and changing labels Beadhive owns;
- declared read and write capability flags (tri-state, from bh-mqvdq);
- error and rate-limit semantics, and the acting identity.

No GitHub-specific field may appear in the contract. Two consequences are part of the question:
whether the existing issue projection (`bd github sync` into the public store) is **kept,
wrapped or replaced**, and therefore whether **bh-cm4er**'s fix stays on the bd path.

Explicitly **not** asked:

- the transport (bh-a1svm) or credential resolution (bh-mqvdq);
- the conversation and thread **model**, the outbox and report dimensions (prerequisite 3; this
  port only transports raw comments);
- the plugin category, manifest, enablement and config wiring (prerequisite 2);
- config key names (the ADR in bh-x6f9l);
- multi-tracker support.

This spike sent **no request to GitHub at all**, read or write.

## Method

- **Read the inputs**: both prerequisite spikes and their prototypes
  (`artifacts/bh-a1svm/sync_port.py`, `fake_github.py`; `artifacts/bh-mqvdq/identity_probe.py`).
- **Read the repository's module conventions**: `docs/MODULES.md`,
  `docs/design/package-class-library-vs-plugin-adr.md`, `docs/CONFORMANCE-TESTING.md`, the
  existing ports (`modules/{hives,state,agents}/contracts/ports.py`), `beadhive.testing`
  (`conformance.py`, `impact/`), `packages/beadhive-pants` (manifest, `pyproject.toml`) and both
  checkers, `scripts/check_import_boundaries.py` and `scripts/check_package_imports.py`.
- **Read the projection path**: `src/beadhive/github_projection.py`, `tracker_sync.py`,
  `public_publish.py`, `docs/design/github-issue-projection.md`,
  `docs/design/two-store-scope-adr.md` section 3, `docs/design/external-issue-intake.md`.
- **Read bd's prior art** in a beads checkout
  (`/home/bees/.herdr/worktrees/beads/worktree-quiet-field-e815`, main of 2026-09-24) and checked
  each cited passage at the installed bd's commit, `f45b249ce` (`bd version 1.3.0`):
  `internal/tracker/{tracker,errors,engine}.go`, `internal/tracker/conformance/`,
  `internal/github/{tracker,client}.go`, `cmd/bd/github.go`.
- **Prototype** the contract under `docs/spikes/artifacts/bh-gj1hb/` (never imported by `src/`):
  a standard-library contract module, a runner-neutral conformance suite with 22 named cases, an
  in-memory fake, and a GitHub adapter over an offline REST stand-in that also models the write
  endpoints. One suite runs against both implementations. Run in throwaway venvs (CPython 3.11
  to 3.14, `httpx` 0.28.1, `pytest` 9.1.1); `conftest.py` makes any socket connection or real
  `time.sleep` fail the test.
- **Negative controls**: 13 single-line mutations of the implementations; each must make a case
  fail (`mutation_controls.py`).
- **Boundary probes**: ran the two real checkers on a `git archive` copy of the tree with the
  proposed module, a stand-in consumer and a stand-in plugin package added, then once per
  forbidden edge (`probe_boundary_rules.py`). The checkout itself was not modified.
- **Second-tracker sketch** for Jira Cloud and Linear from their public API documentation as I
  know it. **Not exercised** (no network), and flagged as such.

Reproduce: see [`artifacts/bh-gj1hb/README.md`](artifacts/bh-gj1hb/README.md), which also holds
the full recorded output.

## Evidence

### Inputs carried forward

- **E1. Transport (bh-a1svm).** githubkit 0.16.1 (MIT) inside the adapter only, lazily
  imported; the `hishel` license override lands with it; retry decisions are injectable but the
  sleep is a module attribute; GitHub's `since` is inclusive and cannot report deletions;
  `Link: rel="next"` URLs must be followed verbatim (they carry an `after=` cursor);
  `githubkit` is not yet in `_FORBIDDEN_EXTERNAL_PREFIXES`.
- **E2. Identity (bh-mqvdq).** The adapter receives an injected `TokenSource` with provenance; the
  actor is keyed `(id, type)`, never the login; write capability is a GET-only tri-state
  (`NO` definitive, `YES` only "not refuted", fine-grained and App tokens `UNKNOWN`); own comments
  are recognised by outbox id, then App id, then known actor plus a footer marker; the first
  refused write gets an exact diagnostic.

### Repository conventions the port must fit

- **E3. Ports are `Protocol`s in `modules/<name>/contracts/`** and import only their own domain
  (for example `modules/state/contracts/ports.py:9-11`). `beadhive.testing` already provides a
  runner-neutral `PortCase` / `assert_port_conforms` kit (`src/beadhive/testing/conformance.py:52`,
  `:130`) and a precedent for module-specific fixtures (`beadhive.testing.impact`, which the
  `beadhive-pants` package's `test_impact_conformance.py` consumes).
- **E4. Plugin packages may import root only through contracts.**
  `scripts/check_package_imports.py:77-87` admits `beadhive.kernel.*.contracts`,
  `beadhive.modules.*.contracts` and `beadhive.testing`; anything else fails (`:240`); root may
  resolve a plugin package only lazily (`:299`). A package **without** `plugin.json` is a library
  package and may not import `beadhive` at all (`:250`; ADR
  `package-class-library-vs-plugin-adr.md` section 1).
- **E5. The optional-dependency precedent.** `beadhive-pants` is a workspace member exposed as
  the extra `beadhive[pants]` (`pyproject.toml:83-91`) precisely so the root wheel installs
  without it (bh-mxjoy); core tolerates its absence. githubkit adds about 33 MB (bh-a1svm E2), so
  an off-by-default adapter should not make it a root dependency.
- **E6. What `check_import_boundaries.py` classifies.** Only `modules/<x>/{domain, contracts,
  application}`, `kernel/*`, top-level `adapters/*` and `integrations/*`, and `bootstrap` get a
  role (`:200-231`). Anything else, including a `modules/<x>/adapters/` subpackage, is
  `legacy`: an unchecked importer. The forbidden-runtime list applies to `domain` and
  `application` only (`:448`), and a `contracts` module may import any other module's public
  contracts (`:250-253`). An `integration` may import any module's public `application`
  (`:270-274`).

### bd's prior art and the projection

- **E7. bd's `IssueTracker` has no comment entity.** The interface (`internal/tracker/tracker.go:
  12-58`) is fetch, create and update of whole issues, plus a `FieldMapper` of eight conversions
  between tracker and bead status, priority and type (`:80-106`). Optional capabilities are small
  separate interfaces (`BatchPushTracker`, `BatchPushDryRunner`, `PullStatsProvider`, `:60-76`),
  which is the shape adopted here. In `internal/tracker/` and `internal/github/` the word
  "comment" occurs once, in a test stub (`uow_store_engine_test.go:130`, `CommentUseCase()`
  returning `nil`). Rate limiting is an error *interface*, `RateLimitedError` with
  `RateLimitRetryAfter()` (`errors.go:9-17`), so the engine never imports a provider: also
  adopted.
- **E8. bd's conformance suite tests the sync engine and its store, not the adapter.** Its cases
  are `pull_persists_normalized_fields_and_last_sync`, `push_persists_config_and_metadata`,
  `dry_run_does_not_mutate`, `refusal_is_explicit_and_does_not_mutate`,
  `external_ref_resolution_prefers_issue_plane` and `api_only_completes_without_store`
  (`internal/tracker/conformance/run.go:87-226`). None states what a fetch must return.
- **E9. How bd pulls GitHub.** State defaults to `all` (`internal/github/tracker.go:96-99`); pull
  requests are filtered client-side (`client.go:244`, `:293`); the incremental request is
  `?since=...&direction=asc` with no `sort`, i.e. creation order (`client.go:278`). The
  checkpoint is the **local wall clock**: `time.Now()` rounded up one second, taken at the *end*
  of the sync and stored as local metadata (`internal/tracker/engine.go:236-248`), then used
  verbatim as the next `since` (`:339-349`). Inference from the code, not reproduced: an issue
  edited on GitHub after the fetch but before the sync ends carries an `updated_at` older than
  the stored checkpoint, so the next incremental pull skips it, and host clock skew widens that
  window. `cmd/bd/github.go:411-470` runs `engine.Sync` and prints the summary; no commit is
  issued there.
- **E10. What the projection is for.** `github_projection.project` runs exactly
  `bd github sync --pull-only --prefer-github` against the **public** store (`:241-246`), parses
  bd's summary line by regex (`_PULLED`, `:45`) and then verifies that every row's `external_ref`
  is a unique issue URL of this repository (`:267`). `tracker_sync` then publishes that store to
  its Git Dolt replica (`tracker_sync.py:1-16`, `public_publish.py`). Its documented mapper limits
  (`github-issue-projection.md:86-100`): comments **not imported**; deletions and transfers **not
  represented**; scoped `type::` / `priority::` / `status::` labels **consumed** and not kept;
  one assignee login; no author identity. The two-store ADR names this path as the
  implementation of "public: GitHub-authoritative, pull-only" (section 3, `:98-103`).
- **E11. What consumers need from it.** Operator decision D1 (prerequisite 3's design input) keys
  the internal report bead on the **issue URL**, never on a public-store id, because the public
  store is minted fresh per host (bh-nkssm); and "nothing writes into the public store except the
  pull" is a fixed constraint there. So intake can join to the projection by URL without reading
  it, and the port must expose the canonical URL. bh-cm4er itself is a bd-path defect: after
  `--pull-only` the imported rows sit uncommitted in the Dolt working set, and bh's report does
  not say so. The planning draft for the issue loop already lists "pull commits what it imports
  and reports uncommitted rows (absorbs bh-cm4er)" as its first feature.

### The prototype

- **E12. The contract has no GitHub vocabulary and needs only the standard library.**
  `tracker_contracts.py` imports only `collections.abc`, `dataclasses`, `datetime`, `enum`,
  `typing`; `test_contract_shape.py` asserts that, and that no dataclass field or Protocol
  parameter matches a GitHub term (`number`, `node_id`, `html_url`, `login`, `pull_request`,
  `repo`, `owner`, `scopes`, `installation`, `link`, `etag`, and so on). GitHub-shaped values
  travel only as opaque strings: `IssueKey.identifier` (`#12`), `Checkpoint.token`,
  `PageCursor.token` (the verbatim `next` URL).
- **E13. One suite, two implementations: 51 passed on CPython 3.11, 3.12, 3.13 and 3.14.** That
  is 22 cases times two implementations (`memory`, `github`), plus four contract-shape tests and
  three GitHub-only tests (fine-grained token probes `UNKNOWN`; an archived repository probes a
  definitive `NO`; a quoted marker is not ownership). Every case's subject is fresh; every
  timestamp is explicit; rate-limit waits go to an injected `sleep` that records.

  | Area | Cases |
  |---|---|
  | issues | `closed-included`, `change-requests-excluded`, `pagination-complete`, `checkpoint-incremental`, `checkpoint-mid-pull-resume`, `id-snapshot` |
  | comments | `identity`, `edit-is-new-revision`, `deletion-needs-id-snapshot`, `issue-scope`, `checkpoint-safe-at-every-page` |
  | reconcile | `ids.failed-snapshot-is-incomplete` |
  | identity | `port.shape`, `capability.default-read-only` |
  | writes | `refused-when-not-enabled`, `grant-matches-and-single-use`, `comment-round-trip`, `ambiguous-not-retried`, `labels.only-owned` |
  | rate limits | `within-budget`, `beyond-budget`, `wait-cap` |

- **E14. Thirteen mutations, thirteen killed.** Examples: sending `state=open` fails
  `closed-included`; keeping pull requests fails `change-requests-excluded`; GitHub's default
  ordering (`created`, `desc`) fails `checkpoint-incremental` and `checkpoint-mid-pull-resume`;
  retrying a write that got a `502` fails `ambiguous-not-retried`; an exclusive `since` fails
  `checkpoint-incremental` (ties at one-second resolution are real); a snapshot that counts a
  failed listing as complete fails `failed-snapshot-is-incomplete`. Two of my first cases were
  too weak, and the controls found both: the single-wait cap was masked by the total-wait cap,
  and nothing exercised an unordered listing's mid-pull checkpoint. Both cases were tightened
  before the recorded run.
- **E15. Three GitHub facts the contract had to absorb**, each now encoded as a case, not a
  GitHub field:
  - GitHub's *repository-wide* comment list names the parent issue only by URL (the comment's
    `html_url` up to `#`). So `IssueKey` equality is `(identifier, url)`, and the internal and
    global ids are optional. This is also D1's join key.
  - The repository-wide comment list **includes pull-request conversation** (`/pull/<n>` URLs).
    Excluding change requests therefore applies to comments too, not only to issues.
  - The *per-issue* comment endpoint accepts only `since` and returns creation order, so an
    edited old comment comes first. A per-page checkpoint there would skip later comments
    (mutation killed by `checkpoint-safe-at-every-page`). The contract therefore lets an adapter
    return `checkpoint=None` until the final page; the GitHub prototype carries the running
    high-water mark inside its opaque cursor. For sortable listings it asks for
    `sort=updated&direction=asc`, which makes every page's checkpoint safe to persist.
- **E16. Writes are refused before any request** when the operation is not enabled (writes
  default off), when the grant names another operation or issue, when a grant is reused, or when
  a label lies outside the owned prefixes. The cases assert the server saw **zero** write
  requests. A write answered `5xx` or cut off in transit raises `WriteOutcomeUnknown` and is
  never retried; the comment exists exactly once and is found again by its decoded marker, which
  is the outbox's reconcile path. A rate-limit answer proves nothing was written, so that write
  *is* retried within the budget.

### Boundary rules today

- **E17. The two checkers already enforce most of the rule; four gaps remain.** On the archived
  tree with the proposed layout added, the baseline passes both checkers. These forbidden edges
  are already caught: tracker application to `httpx`, tracker application to intake application,
  tracker application to `beadhive.cli`, intake application to `httpx`, the plugin adapter to a
  root internal (`beadhive.github_projection`), and root importing the plugin package
  statically. A package with no `plugin.json` cannot import the contracts at all. These pass but
  should not:
  - **G1.** A `contracts` module importing `httpx` (the forbidden-runtime check skips
    contracts).
  - **G2.** Any layer importing `githubkit` (not in `_FORBIDDEN_EXTERNAL_PREFIXES`).
  - **G3.** Tracker contracts importing a consumer module's contracts (`work`; equally `intake`).
  - **G4.** The adapter importing the plugin kernel's contracts, which would couple it to
    prerequisite 2's wiring.

### Scoring the boundary (bh:modularize, 0 poor, 1 mixed, 2 strong)

- **E18.** Candidates:
  - **A (chosen):** a tracker-neutral port in `src/beadhive/modules/tracker/contracts/`, with
    the GitHub adapter as a plugin package `packages/beadhive-tracker-github`;
  - **B:** the same port, with the adapter in root at `src/beadhive/integrations/github/` (the
    Herdr precedent);
  - **C:** wrap bd: implement the port by running `bd github sync` and reading the public store
    back;
  - **D:** no neutral port; intake consumes bh-a1svm's GitHub-shaped `GitHubTransport`;
  - **E:** one wide port that also owns the conversation model (threads, revisions, outbox);
  - **F:** mirror bd's `IssueTracker` in Python (whole-issue create/update plus a `FieldMapper`).

  | Dimension | A | B | C | D | E | F |
  |---|---|---|---|---|---|---|
  | Cohesion | 2 | 2 | 1 | 2 | 0 | 1 |
  | Coupling | 2 | 2 | 0 | 1 | 1 | 1 |
  | Data and side-effect ownership | 2 | 2 | 0 | 2 | 1 | 2 |
  | Port narrowness | 2 | 2 | **0** | 1 | **0** | 1 |
  | Replaceable implementations | 2 | 2 | 1 | 0 | 1 | 2 |
  | Dependency direction | 2 (1 until G1 to G4) | 1 | 1 | 2 | 1 | 1 |
  | Test-closure reduction | 2 | 1 | 0 | 1 | 1 | 1 |
  | **Total** | **14** | **12** | **3** | **9** | **5** | **9** |

  Reasons for the marks that decide it:

  - **C is a hard stop** (narrowness 0). The port returns raw records, but bd's only output is
    rows written into a Dolt store and a summary line (E10). Reading back gives bd's lossy
    mapping: no comments, no author ids, no deletions, scoped labels consumed. A "read" would
    also carry a store write as a side effect, and the tests would need the bd binary or
    recorded bd output.
  - **E is a hard stop** (narrowness 0, cohesion 0). Consumers would depend on thread-model
    internals, and the model belongs to prerequisite 3.
  - **D** leaks GitHub fields (`number`, `Link`) into intake, so no second tracker can be
    substituted without changing consumers (replaceable 0).
  - **F**'s whole-issue `CreateIssue`/`UpdateIssue` contradicts both "change owned labels only"
    and the pull-only public store, and its `FieldMapper` drags bead vocabulary into the port.
    It proves replaceability (bd ships GitHub, GitLab, Linear and Jira behind it), which is why
    the small-interface split is adopted from it, but not its operations.
  - **B versus A.** Both have the same port. B puts githubkit in the root wheel or behind a root
    extra. Its tests live in the root suite, and an `integration` may import any module's
    application layer (E6), so "the GitHub module never imports intake" would not be enforced.
    A gets the stricter rule from `check_package_imports.py` today (E4, E17), keeps githubkit
    out of root (E5), and keeps its tests in the package's own Pants sandbox.
  - **Adapter-location variants rejected with B:** a `modules/tracker/adapters/github/`
    subpackage is classified `legacy`, so it is an unchecked importer (E6), and it would put a
    provider inside a neutral module. A *library* package (no `plugin.json`) cannot import the
    contracts (E4, E17).

### A second implementation is plausible

- **E19. The in-memory fake passes the same 22 cases** (E13) with no HTTP, no clock and no
  GitHub notion; it is the substitute every consumer test would use. The mapping onto two other
  trackers, from their public API documentation, is **not exercised** here:

  | Port operation | GitHub (prototype) | Jira Cloud REST v3 (sketch) | Linear GraphQL (sketch) |
  |---|---|---|---|
  | `list_issues` | `GET /repos/{o}/{r}/issues?state=all&sort=updated&direction=asc&since=` | `GET /rest/api/3/search/jql`, JQL `project = K AND updated >= "<t>" ORDER BY updated ASC`, `nextPageToken` | `issues(filter: {team, updatedAt: {gte}}, includeArchived: true, after:)`, `pageInfo.endCursor` |
  | closed | `state=closed`, `state_reason` | status category Done; status name as `closed_reason` | state type `completed` or `canceled` |
  | `IssueKey` | `#12`, `html_url`, `id`, `node_id` | `PROJ-12`, `/browse/PROJ-12`, numeric id | `ENG-12`, issue URL, UUID |
  | `list_comments` | repository-wide list, or per issue | per issue (`/issue/{key}/comment`); project-wide = issues updated since, then their comments | `comments(filter: {issue, updatedAt: {gte}})` |
  | author kind | `user.type` `User` / `Bot`; `performed_via_github_app` | `author.accountType` `atlassian` / `app` / `customer` | `user`, `botActor`, `externalUser` |
  | deletions | full id set | full id set | full id set |
  | own marker | trailing HTML comment | comment entity property (invisible, structured) | adapter-chosen (unknown) |
  | `post_comment` | `POST .../issues/{n}/comments` | `POST /issue/{key}/comment`, body in ADF | `commentCreate` |
  | `change_labels` | `POST .../labels`, `DELETE .../labels/{name}` | `PUT /issue/{key}` with `update.labels` add/remove | `issueAddLabel` / `issueRemoveLabel` by label id |
  | `probe_capabilities` | `GET /repos/{o}/{r}` and scope header: often `UNKNOWN` | `GET /rest/api/3/mypermissions?permissions=ADD_COMMENTS,EDIT_ISSUES`: definitive yes or no | viewer and team membership: mostly `UNKNOWN` |
  | rate limit | `403`/`429` with `Retry-After` or `X-RateLimit-Remaining: 0` | `429` with `Retry-After` | complexity budget; a `RATELIMITED` error code |

  Every row fits the contract unchanged. Three points to settle when such an adapter is
  designed, all already representable: JQL dates have minute resolution (more boundary repeats,
  still no skips); Linear may not sort by update time ascending, in which case the adapter
  withholds the checkpoint until the final page (E15); Jira issue keys change when an issue moves
  between projects. That last one is an issue-identity question for prerequisite 3's D1 (URL
  key), not a port change.

## Verdict — **GO**

The port is buildable as specified, with no GitHub field in it. Four small Protocols and a set
of frozen, standard-library records cover every required capability. One conformance suite
holds a GitHub adapter and an in-memory fake to the same behaviour (51/51 on four interpreters,
13/13 mutations killed). The boundary scores 14/14 with no zero on narrowness or direction.
Most of the one-way rule is already enforced by `check_package_imports.py`; the four gaps
(G1 to G4) are small checker extensions. **The projection is kept as it is**: not wrapped, not
replaced now. So **bh-cm4er's fix stays on the bd path.**

## Recommendation

### 1. The port: "bead tracker adapter port", `beadhive.modules.tracker.contracts`

Signatures as prototyped in
[`tracker_contracts.py`](artifacts/bh-gj1hb/tracker_contracts.py). API version 1.

| Protocol | Required? | Operations |
|---|---|---|
| `TrackerAdapter` = `TrackerReader` + `TrackerIdentity` | yes | the union below; the one Protocol prerequisite 2 binds |
| `TrackerReader` | yes | `describe() -> TrackerDescriptor`; `list_issues(IssueQuery) -> Page[TrackerIssue]`; `list_comments(CommentQuery) -> Page[TrackerComment]`; `list_ids(IdQuery) -> IdPage` |
| `TrackerIdentity` | yes | `whoami() -> Actor or None`; `probe_capabilities() -> CapabilityReport` (read-only requests only) |
| `CommentWriter` | optional | `post_comment(issue, body, *, marker, grant) -> WriteReceipt` |
| `LabelWriter` | optional | `change_labels(issue, *, add, remove, ownership, grant) -> WriteReceipt` |

- **Records:** `ProjectRef(tracker, locator)`; `IssueKey(identifier, url, internal_id?,
  global_id?)` with equality on `(identifier, url)`; `CommentKey(id, global_id?)`;
  `Actor(ActorKey(id, kind), display)`, where kind is human, bot or unknown and display is never
  compared; `IntegrationRef`; `OwnMarker(hive, install, outbox_id)`; `TrackerIssue`, with state
  open or closed, labels, author, `Revision(updated_at)`, `closed_at` and `closed_reason`;
  `TrackerComment`, with author, body (marker removed), revision, `via`, `marker` and an
  `edited` property.
- **Reading:** `IssueQuery(since, cursor, include_closed=True, page_size)`; `CommentQuery(since,
  cursor, issue=None, page_size)`; `IdQuery(subject, issue, cursor)`;
  `Page(items, next_cursor, checkpoint, budget)`; `IdPage`; `IdSnapshot(ids, complete)`.
  `Checkpoint` and `PageCursor` are opaque tokens.
- **Checkpoint semantics:**
  - `since` is inclusive: boundary items may repeat, never skip, so consumers upsert on
    `(key, revision)`;
  - the checkpoint is the tracker's own high-water mark, never the local clock (contrast E9);
  - it is safe to persist after durably ingesting its page and every earlier one;
  - an adapter that cannot order by revision returns `None` until the final page.
- **Deletions:** `list_ids` enumerates a full id set (issue URLs or comment ids). The
  application helper `snapshot_ids` returns `complete=False` on any port error, and only a
  complete snapshot may be used to infer a deletion. An incremental pull is not authoritative
  under concurrent edits (an offset page can shift); consumers schedule a periodic full pull and
  id snapshot, as `external-issue-intake.md` section 2 already requires.
- **Capability flags:** `CapabilityReport(actor, credential_source, states)`, one
  `CapabilityState(operation, supported, enabled, probe: Tri, reason)` per
  `issues.read`, `comments.read`, `comment.create` and `labels.change`, where `usable` means
  supported, enabled and not `NO`. These flags are the whole config surface the port knows about:
  the adapter is off unless configured; reads and writes are separate operations; every write is
  disabled by default; and every write call carries its own single-use `WriteGrant(grant_id,
  operation, target)` issued by the operator-approved gate. Key names and storage are for the ADR.
- **Owned labels:** `LabelOwnership(prefixes)`. An add or remove outside it raises
  `LabelNotOwned` before any request, and human labels are never touched.
- **Errors** (messages already scrubbed): `TrackerError` (with `retryable`),
  `AuthenticationFailed`, `PermissionDenied(operation, needed)`, `NotVisible` (missing or
  hidden), `TargetStateRefused` (archived, locked, disabled), `PayloadRejected` (authorised,
  content refused: the write *is* permitted), `RateLimited(retry_after, kind: quota|throttle)`,
  `TransientFailure`, `WriteOutcomeUnknown` (never retried), `OperationNotEnabled` and
  `LabelNotOwned` (refused locally).
- **Rate limits:** the adapter retries reads, and writes refused by a rate limit, within a
  configurable `RetryBudget(max_retries, max_single_wait, max_total_wait)` on an injected
  sleep. It surfaces `RateLimited` instead of ever waiting past the budget, and reports the
  remaining budget on `Page.budget` when the tracker says. This adopts bh-a1svm's
  clamp-the-default advice.
- **Not in the port:** threads, revision history, own-message *classification*, the outbox, the
  report dimensions (prerequisite 3); the marker's encoding (adapter-owned); credentials (an
  injected `TokenSource`, bh-mqvdq); webhooks.

### 2. Module layout

```text
src/beadhive/modules/tracker/
  contracts/   records.py, errors.py, ports.py  (stdlib only; public __all__)
  application/ paging.py (iterate, snapshot_ids)  (stdlib + own contracts)
  BUILD
src/beadhive/testing/tracker/   cases.py (the 22 cases), memory.py (MemoryTracker), world.py
```

The module has no domain layer and no state: it is a transport contract plus two helpers. It
imports nothing from any other module.

### 3. Where the GitHub adapter lives, separate from plugin wiring

- **Home:** a plugin package, `packages/beadhive-tracker-github` (import
  `beadhive_tracker_github`), copied from `packages/_template` and shaped like `beadhive-pants`:
  its own `pyproject.toml` declaring `githubkit` (and the `hishel` override in the same change,
  bh-a1svm), its own `tests/`, `justfile` and BUILD files, and exposure as an extra
  (`beadhive[github]`), so the root wheel installs without it.
- **Separation from prerequisite 2:** inside the package, `adapter/` (the port implementation,
  the githubkit transport, the mapping, the marker codec) is wiring-free. It is constructed with
  explicit arguments: client or transport, owner and repo, an already-resolved `enabled` set, a
  `RetryBudget`, `sleep`, the credential provenance. It never reads config and never imports the
  plugin kernel. `plugin.json`, the capability id, the `plugins.<id>` config namespace, enablement,
  doctor and status presentation, and the `bind_application_port` call belong to prerequisite 2
  and live beside `adapter/`, never inside it. A package-local architecture test enforces this
  (G4).
- **Sequencing constraint:** a package without `plugin.json` is a library package and cannot
  import the contracts (E17). The first adapter bead must either land after prerequisite 2's
  manifest bead or carry a minimal manifest that prerequisite 2 then owns. Prerequisite 2's
  dispatcher should order this explicitly.

### 4. Allowed dependency direction and how to enforce it

```text
bootstrap (composition root) --lazy, after manifest selection--> beadhive_tracker_github
beadhive_tracker_github ---> beadhive.modules.tracker.contracts, beadhive.testing (tests),
                             githubkit, httpx
modules/intake (and any consumer) ---> beadhive.modules.tracker.contracts (+ application)
modules/tracker ---> standard library only
forbidden: tracker -> any other module, intake, work, plan, CLI, adapters, integrations;
           adapter package -> root internals or the plugin kernel; root -> the package statically;
           any consumer -> githubkit / httpx
```

Already enforced (E17): plugin package to root internals; root to the package statically; the
application layer to `httpx` and to another module's application; the application layer to the
CLI. Extend in the implementation molecule's first bead:

- **G1.** In `check_import_boundaries.py`, apply `_is_forbidden_runtime` to the `contracts` kind
  too (one set literal at `:448`).
- **G2.** Add `githubkit`, `gidgethub` and `github` (PyGithub's import name) to
  `_FORBIDDEN_EXTERNAL_PREFIXES`.
- **G3.** Add a leaf-module rule: owners in a small `_LEAF_MODULES = {"tracker"}` set may import
  no other module's `contracts` or `application`. The probe confirms today's rule admits
  `tracker -> work.contracts`.
- **G4.** A package-local test in `beadhive-tracker-github` asserts that `adapter/` imports no
  `beadhive.kernel.*` name. Keep it local rather than widening the global checker.

Each extension gets a negative-control test, in the same style as `probe_boundary_rules.py`.

### 5. Expected test closure

- **Today:** no port exists. The only GitHub read is the projection, tested by
  `tests/test_github_projection.py` replaying recorded bd output. Any new ad hoc GitHub call from
  root would land in the root native partition (`unit`, `stateful` attest keys).
- **Adapter change** (`packages/beadhive-tracker-github/**`): the package's own tests (the shared
  cases over an offline githubkit `MockTransport` stand-in, mapping and probe tests) under the
  `packages` attest key in the package's Pants sandbox. No root test runs.
- **Contract change** (`modules/tracker/contracts/**`): the conformance cases for every
  implementation (the memory fake and the GitHub package) plus consumers' boundary tests, found by
  Pants dependency inference once BUILD targets exist.
- **Consumer change** (intake): its own tests against `MemoryTracker`. The GitHub package is
  never built and githubkit is never installed for them.
- **Projection:** unchanged. These are expected, not measured: the module does not exist yet.
  The implementation molecule should record `pants dependents` for each path above, per the
  measured-closure rule in `docs/MODULES.md`.

### 6. The projection decision: keep `bd github sync` as is, and its consequence for bh-cm4er

- **Keep.** It materialises and publishes the public store, a job the port does not do (E10).
  The port's consumers do not need it, because D1 joins on the issue URL (E11).
- **Do not wrap.** That is candidate C, a hard stop (E18).
- **Do not replace now.** Replacement would mean bh writing beads into the public store from port
  records, re-implementing bd's mapper for no current consumer, and it would contradict the
  ADR-named implementation without a decision.
- **What it costs.** Two GitHub readers: the issue list is fetched twice per cycle when both
  run, and there are two checkpoints. Both use bh's one credential chain (bd receives the
  resolved token in its child env), so they read as one identity. Mitigate by sharing the
  `TokenSource` and the promoted `scrub()` with the projection, as bh-mqvdq recommends.
- **Revisit it as a separate decision** if the public store must carry comments, deletions or
  author identity, or if the bd pull keeps producing defects of the bh-cm4er kind. The port
  already supplies everything bd's `FetchIssues` reads.
- **bh-cm4er stays on the bd path, unblocked by this molecule.** The uncommitted-rows defect is
  in the projection's pull leg (bd's engine issues no commit, E9). Its fix, an opt-in commit
  after pull or reporting `uncommitted rows: N`, belongs in `github_projection` and
  `tracker_sync`, or upstream in bd. The port neither fixes nor obsoletes it, so it should
  proceed as the issue loop's first feature, as already drafted (E11).
- **A related bd-path risk** (E9, inferred from code, not reproduced): bd's wall-clock
  `last_sync` can skip an issue edited during a sync on the next incremental pull. It
  should be filed beside bh-cm4er for the projection, with a regression test that drives the
  engine's clock. I have not filed it.

### 7. For the ADR (bh-x6f9l) and the implementation molecule

1. ADR content: section 1 (port, records, errors, semantics), section 3 (package and
   separation), section 4 (direction and G1 to G4), section 6 (projection kept).
2. First bead: create `modules/tracker` (contracts and application) plus
   `beadhive.testing.tracker` (cases, `MemoryTracker`, world), with G1 to G3 and their negative
   controls.
3. Then the GitHub adapter package, after prerequisite 2's manifest (section 3): port the
   prototype mapping onto githubkit with the `hishel` override; run the shared cases over a
   `MockTransport` stand-in grown from `fake_github_rest.py`; add the G4 test.
4. bh-cm4er and the `last_sync` risk proceed independently on the bd path.

**Uncertain or unverified:**

- the Jira and Linear rows (documentation, not exercised);
- whether a GitHub issue's numeric `id` survives a transfer (the contract keys on the URL, so
  the answer affects intake, not the port);
- GitHub's real ordering guarantees under concurrent edits, beyond its documented parameters;
- the bd `last_sync` window (code reading only);
- every test closure in section 5 (expected, not measured).
