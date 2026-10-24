# ADR: The bead tracker adapter port and the GitHub adapter transport

- **Status:** Accepted (verdict **GO**), effective when `bh-x6f9l` merges
- **Date:** 2026-10-24
- **Decision owner:** Beadhive maintainers
- **Related work:** `bh-x6f9l` (this decision), epic `bh-ire9a` (the spike molecule), spikes
  `bh-a1svm`, `bh-mqvdq`, `bh-gj1hb`; sibling design molecules `bh-mobxo` (plugin category,
  prerequisite 2) and `bh-wjvq9` (external-tracker intake, prerequisite 3); placeholder for the
  whole loop `bh-33d5a`; defect `bh-cm4er`
- **Evidence (all merged, all verdict GO):**
  - [`docs/spikes/bh-a1svm-github-transport.md`](../spikes/bh-a1svm-github-transport.md)
    (transport)
  - [`docs/spikes/bh-mqvdq-github-identity.md`](../spikes/bh-mqvdq-github-identity.md)
    (credentials, actor, write probing, own comments)
  - [`docs/spikes/bh-gj1hb-tracker-port.md`](../spikes/bh-gj1hb-tracker-port.md)
    (port contract, boundary, projection)
- **Amends:** nothing. It builds on
  [`package-class-library-vs-plugin-adr.md`](package-class-library-vs-plugin-adr.md),
  [`plugin-kernel-v1-adr.md`](plugin-kernel-v1-adr.md),
  [`two-store-scope-adr.md`](two-store-scope-adr.md) (section 3) and
  [`github-issue-projection.md`](github-issue-projection.md).

## Context

Operator direction of 2026-10-24: GitHub becomes a data source that Beadhive operates against
through one named, tracker-neutral **bead tracker adapter port**, with a GitHub adapter module
behind it. Today there is no such port. The only GitHub read is the issue projection
(`bd github sync --pull-only`, `src/beadhive/github_projection.py`); every other GitHub touch
is an ad hoc `gh` subprocess. The public GitHub issue to internal work loop (`bh-33d5a`) needs
more than the projection offers: closed issues, comments with author identity, edits and
deletions, a posted comment, and Beadhive-owned label changes, all behind a configuration that
is off by default and an operator-approved gate on every write.

Fixed constraints (from the operator and the epic): configurable and off by default; every
GitHub write its own operator-approved gate; credentials local and ambient (never stored by
`bh`); adopt a permissive-licensed library over bespoke infrastructure and flag copyleft; host
tools (the `gh` binary) stay optional; tests offline and never timing-dependent; the lens is
`bh:modularize` (narrow behaviour-oriented port, one-way dependency rule). Three spikes ran
research-only (no product code, no GitHub request beyond read-only `GET`s) and each returned
GO. This ADR reads them together, settles where they disagree, and records the decision the
implementation molecule builds on.

## Decision

**Overall verdict: GO.** Adopt the tracker-neutral port `beadhive.modules.tracker.contracts`
with the GitHub adapter as a separate plugin package, using githubkit as the adapter's
transport behind an injected, scrubbed, provenance-carrying credential source. The three
spikes agree on every load-bearing point. Each independently found its question answerable with
offline evidence: a licence-clean library (one metadata override), a read-only tri-state
capability probe that never claims proof, and a 22-case conformance suite that one GitHub
adapter and one in-memory fake both pass (51 of 51 on four interpreters, 13 of 13 mutations
killed). No spike found a blocker, and the one that would have made `bh-mqvdq` a NO-GO (a read
that proves write permission) is simply not depended on. The residual costs are small and
named below: one licence override, one retry seam to own, four checker gaps (G1 to G4), and two
readers of GitHub until the projection is revisited. None changes the verdict.

### 1. Transport (from `bh-a1svm`)

- **githubkit 0.16.1 (MIT)**, confined to the GitHub adapter package and imported **lazily**
  inside it, never at `bh` start-up (about 250 ms of import plus about 130 ms of first-call
  model load, measured). It is declared in the adapter package's own `pyproject.toml`, not the
  root's, and exposed as an extra (`beadhive[github]`), so the root wheel installs without it
  (the `beadhive-pants` precedent, `bh-mxjoy`). It adds about 33 MB and moves no existing pin.
- **The `hishel` licence override lands in the same change as the dependency.** `just
  license-check` fails as shipped on the transitive `hishel` (`non-standard`: it declares only
  the generic BSD trove classifier). The wheel's `LICENSE` is the three-clause BSD text, so
  the `osv-scanner.toml` override shape in `bh-a1svm` E3 applies, with its mandatory
  `effectiveUntil` and re-verify-at-expiry discipline.
- **The adapter owns the retry decision and the sleep seam.** githubkit's retry decision is an
  injectable pure function, but its sleep is a module attribute and its primary-rate-limit path
  reads the real clock and sleeps 60 s on a zero delay. The adapter supplies its own decision
  function that clamps the wait, enforces `RetryBudget` (section 3), and routes sleep through an
  injected seam. It does not rely on githubkit defaults. `http_cache` is passed explicitly and
  **off** (githubkit's own default is on); it is a per-hive performance opt-in for the cheap
  full-id reconcile pass, never a correctness dependency.
- **`gh` stays optional.** It remains a credential source of last resort (`gh auth token`) and
  may later be offered as an opt-in alternative implementation behind the same port. Hard
  requiring the binary would contradict "host tools are optional adapters". The first
  implementation molecule does not build it.
- **Fallback trigger.** If the implementation review finds the weight or the retry seam
  unacceptable, switch to the plain-`httpx` transport already prototyped in
  `docs/spikes/artifacts/bh-a1svm/transport_httpx.py`. The port makes that a drop-in. Carry
  over its two recorded defects as tests: an absolute `next` URL loses its query string under
  `httpx.URL(url, params=None)`, and a hand-written cache that drops `Link` on a `304` stops
  paginating.
- **Wire facts the adapter must honour.** `state=all` is load-bearing (the default hides closed
  issues); pull requests appear in the issues list and in the repository-wide comment list and
  are dropped by the adapter; `Link: rel="next"` is followed verbatim (it carries an `after=`
  cursor); `since` is inclusive; deletions are invisible to `since`, so a periodic full id-set
  reconcile is required whichever transport is used.

### 2. Identity and credentials (from `bh-mqvdq`)

- **Credentials arrive through an injected `TokenSource`**, supplied by the composition root.
  The adapter never imports `beadhive.run` (a plugin package cannot: it may import root only
  through `beadhive.kernel.*.contracts`, `beadhive.modules.*.contracts` and `beadhive.testing`).
  The source returns a `ResolvedToken(value, source)` whose value is `repr=False` and whose
  `str()` prints only the provenance (`<token from env:GITHUB_TOKEN>`).
- **Resolution order is bh's existing chain, unchanged:** `GITHUB_TOKEN`, then `GH_TOKEN`, then
  a fresh `gh auth token` (only when `gh` is installed and authenticated; blank counts as
  unset). `gh` itself prefers `GH_TOKEN`, so any path that shells out to `gh` passes the
  *resolved* token explicitly, the actor probe uses that same token, and the adapter never calls
  `config_services.gh_login` or `registry.has_push_access` to learn its identity. If both
  variables are set and differ, one state-only advisory warning is shown.
- **No token is stored, by construction.** No config key holds a token; configuration carries
  at most a source *name* and the repo identity. The token lives in memory for one operation
  and never enters a config file, ledger, outbox, log field or `--json` envelope. Provenance
  (`env:GH_TOKEN`) may. A single `scrub(text, secrets)` renders all upstream text: exact-value
  replace (`***`, the existing `github_projection._scrub` rule) plus a token-shape regex
  (`github_pat_`, `gh[pousr]_`) as defence in depth, longest secret first. A planted-token test
  asserts absence from `repr`, `str`, logs, written files and error bodies that echo it, and
  carries a mutation check so it is not vacuous.
- **The actor is keyed `(id, type)`, never the login.** `GET /user` for user-kind tokens; for a
  later GitHub App installation token (where `/user` answers 403) the public
  `GET /users/<slug>[bot]` gives `(id, Bot)`. Response headers decide the credential kind, not
  the token prefix. `login` is carried for display only and is never compared.
- **Write capability is a GET-only tri-state, never a proof.** `NO` is definitive (`401`, `404`,
  archived or disabled, issues off, no credential, classic scopes lacking `repo` or
  `public_repo`, role below triage for labels). `YES` means only "not refuted". Fine-grained and
  installation tokens are irreducibly `UNKNOWN`. The probe is REST `GET` (deliberately not
  GraphQL, which is a `POST`) so that "the probe sends only `GET`" is a transport-level test.
  `NO` makes the write gate refuse before asking the operator; `UNKNOWN` lets the operator
  approve and the write itself becomes the probe, with a refusal classified rather than retried.
  `NO` is cached for the process lifetime only; `YES` and `UNKNOWN` are never cached as fact.
- **The first refused write gets an exact, scrubbed one-line diagnostic**, from the `bh-mqvdq`
  table: `401` bad credentials naming the provenance; `403` + `X-GitHub-SSO` authorize the
  token; `403` "not accessible by personal access token" or "by integration" naming the
  `X-Accepted-GitHub-Permissions` value; `403` with `Retry-After` or `X-RateLimit-Remaining: 0`
  is a rate limit, not a permission refusal; `403` archived or locked; `404` invisible to the
  token; `410` issues disabled; `422` payload rejected **after** authorization, so the write
  was permitted. Only `401` and `404` were observed live; the rest are documented forms to
  re-verify against recorded real responses when the first gated write is exercised.
- **Own comments are recognised by layered evidence**, outbox id first, then the configured
  App id (`performed_via_github_app.id` with a known `Bot` author), then a known actor
  `(id, type)` **and** a footer marker naming this hive. A known actor without the marker is not
  own (a human on a shared token). The marker is a loop-avoidance and attribution aid, not a
  security boundary. Where this logic lives is settled in the inconsistencies section below.
- **GitHub App installation support is deferred** (it needs JWT minting, a new secret kind) but
  stays a drop-in `TokenSource` plus actor-discovery variant behind the same port.

### 3. The port contract (from `bh-gj1hb`)

The port is **`beadhive.modules.tracker.contracts`**, API version 1, standard library only, with
no GitHub vocabulary: GitHub-shaped values travel only as opaque strings (`IssueKey.identifier`
`#12`, `Checkpoint.token`, `PageCursor.token`). A contract-shape test asserts both.
Signatures are as prototyped in
[`docs/spikes/artifacts/bh-gj1hb/tracker_contracts.py`](../spikes/artifacts/bh-gj1hb/tracker_contracts.py)
(prototype only; never imported by `src/`).

| Protocol | Required | Operations |
|---|---|---|
| `TrackerAdapter` = `TrackerReader` + `TrackerIdentity` | yes | the union below; the one Protocol the plugin wiring binds |
| `TrackerReader` | yes | `describe()`, `list_issues(IssueQuery)`, `list_comments(CommentQuery)`, `list_ids(IdQuery)` |
| `TrackerIdentity` | yes | `whoami() -> Actor or None`, `probe_capabilities() -> CapabilityReport` (read-only requests only) |
| `CommentWriter` | optional | `post_comment(issue, body, *, marker, grant) -> WriteReceipt` |
| `LabelWriter` | optional | `change_labels(issue, *, add, remove, ownership, grant) -> WriteReceipt` |

The small-interface split with optional writers follows bd's own `IssueTracker` prior art (its
optional `BatchPushTracker` and friends, and the `RateLimitedError` interface that keeps the
engine provider-free). It does not follow bd's whole-issue create and update operations or its
`FieldMapper`, which contradict "change owned labels only" and pull bead vocabulary into the
port (candidate F, rejected below).

- **Data types.** `ProjectRef(tracker, locator)`; `IssueKey(identifier, url, internal_id?,
  global_id?)` with equality on `(identifier, url)` because the repository-wide comment list
  names the parent issue only by URL, which is also the join key for the intake design;
  `CommentKey`; `Actor(ActorKey(id, kind), display)` with kind human, bot or unknown and display
  never compared; `IntegrationRef`; `OwnMarker(hive, install, outbox_id)`; `TrackerIssue` (state
  open or closed, labels, author, `Revision(updated_at)`, `closed_at`, `closed_reason` passed
  through verbatim); `TrackerComment` (author, body with the marker removed, revision, `via`,
  decoded `marker`). Queries are `IssueQuery`, `CommentQuery` and `IdQuery`; results are
  `Page(items, next_cursor, checkpoint, budget)`, `IdPage` and `IdSnapshot(ids, complete)`.
  `Checkpoint` and `PageCursor` are opaque.
- **Read semantics.** `include_closed` defaults true. Change requests (pull requests) are
  excluded from issues **and** comments. `since` is inclusive: boundary items may repeat and
  never skip, so consumers upsert on `(key, revision)`. The checkpoint is the tracker's own
  high-water mark, never the local clock, and is safe to persist after durably ingesting its
  page and every earlier one. An adapter that cannot order by revision (the per-issue comment
  endpoint returns creation order) returns `checkpoint=None` until the final page.
- **Deletions.** `list_ids` enumerates a full id set (issue URLs or comment ids). The
  application helper `snapshot_ids` returns `complete=False` on any port error, and only a
  complete snapshot may infer a deletion. An incremental pull is not authoritative under
  concurrent edits, so consumers schedule a periodic full pull and id snapshot, as
  [`external-issue-intake.md`](external-issue-intake.md) section 2 already requires.
- **Errors** (messages already scrubbed): `TrackerError` (carries `retryable`),
  `AuthenticationFailed`, `PermissionDenied(operation, needed)`, `NotVisible` (missing or
  hidden), `TargetStateRefused` (archived, locked, disabled), `PayloadRejected` (authorised,
  content refused, so the write **was** permitted), `RateLimited(retry_after, kind:
  quota|throttle)`, `TransientFailure`, `WriteOutcomeUnknown` (never retried), and the local
  refusals `OperationNotEnabled` and `LabelNotOwned`.
- **Rate-limit semantics.** The adapter retries reads, and writes refused by a rate limit
  (proof that nothing was written), within a configurable
  `RetryBudget(max_retries, max_single_wait, max_total_wait)` on an injected sleep. It raises
  `RateLimited` rather than waiting past the budget and reports remaining budget on
  `Page.budget` when the tracker says. A write answered `5xx` or cut off in transit raises
  `WriteOutcomeUnknown` and is never retried: the comment may exist exactly once and is found
  again by its decoded marker, which is the outbox's reconcile path.
- **Capability flags.** `CapabilityReport(actor, credential_source, states)` holds one
  `CapabilityState(operation, supported, enabled, probe: Tri, reason)` per `issues.read`,
  `comments.read`, `comment.create` and `labels.change`; `usable` means supported, enabled and
  not `NO`. Every write is disabled by default, and every write call carries its own single-use
  `WriteGrant(grant_id, operation, target)` issued by the operator-approved gate. A write is
  refused **before any request** when the operation is not enabled, the grant names another
  operation or issue, a grant is reused, or a label lies outside `LabelOwnership(prefixes)`;
  the cases assert the server saw zero write requests. Human labels are never touched.
- **Not in the port:** threads, revision history, own-message classification, the outbox and
  the report dimensions (prerequisite 3); the marker's encoding (adapter-owned); credentials
  (the injected `TokenSource`); webhooks; the plugin manifest, enablement and config wiring
  (prerequisite 2); multi-tracker support.
- **A second implementation is plausible.** The in-memory `MemoryTracker` passes the same 22
  cases with no HTTP, no clock and no GitHub notion; it is the substitute every consumer test
  uses. The Jira Cloud and Linear mapping in `bh-gj1hb` E19 fits the contract unchanged, but is
  from documentation and was **not exercised**.

### 4. Module and package home, dependency direction, enforcement

- **Port module:** `src/beadhive/modules/tracker/` with `contracts/` (`records.py`, `errors.py`,
  `ports.py`; stdlib only; public `__all__`) and `application/` (`paging.py`: `iterate`,
  `snapshot_ids`; stdlib plus its own contracts). It has no domain layer and no state, and it
  imports nothing from any other module. The conformance kit lives in
  `src/beadhive/testing/tracker/` (`cases.py` with the 22 cases, `memory.py` with
  `MemoryTracker`, `world.py`), following the `beadhive.testing.impact` precedent.
- **Adapter home:** a plugin package, `packages/beadhive-tracker-github` (import
  `beadhive_tracker_github`), copied from `packages/_template` and shaped like
  `beadhive-pants`: its own `pyproject.toml` (githubkit, plus the `hishel` override in the same
  change), `tests/`, `justfile` and BUILD files.
- **Adapter wiring is separate from the adapter.** Inside the package, `adapter/` (the port
  implementation, the githubkit transport, the mapping, the marker codec) is wiring-free. It is
  constructed with explicit arguments (transport or client, owner and repo, the resolved
  `enabled` set, a `RetryBudget`, `sleep`, the credential provenance), never reads config and
  never imports the plugin kernel. `plugin.json`, the capability id, the `plugins.<id>` config
  namespace, enablement, doctor and status presentation and the `bind_application_port` call
  belong to prerequisite 2 and live **beside** `adapter/`, never inside it.
- **Allowed direction:**

  ```text
  bootstrap (composition root) --lazy, after manifest selection--> beadhive_tracker_github
  beadhive_tracker_github ---> beadhive.modules.tracker.contracts, beadhive.testing (tests),
                               githubkit, httpx
  modules/intake (and any consumer) ---> beadhive.modules.tracker.contracts (+ application)
  modules/tracker ---> standard library only
  forbidden: tracker -> any other module, intake, work, plan, CLI, adapters, integrations;
             adapter package -> root internals or the plugin kernel; root -> the package
             statically; any consumer -> githubkit / httpx
  ```

- **Enforcement.** `scripts/check_package_imports.py` and `scripts/check_import_boundaries.py`
  already catch most of it: the application layer to `httpx` or to another module's application
  or to the CLI; the plugin package to a root internal; root importing the package statically;
  and a library package (no `plugin.json`) importing the contracts at all. Four gaps pass today
  and must be closed in the implementation molecule's first bead, each with a negative-control
  test in the style of `probe_boundary_rules.py`:

  | Gap | Passes today | Extension |
  |---|---|---|
  | G1 | A `contracts` module importing `httpx` | Apply `_is_forbidden_runtime` to the `contracts` kind too (one set literal near `check_import_boundaries.py:448`) |
  | G2 | Any layer importing `githubkit` | Add `githubkit`, `gidgethub` and `github` (PyGithub's import name) to `_FORBIDDEN_EXTERNAL_PREFIXES` |
  | G3 | Tracker contracts importing a consumer module's contracts | A leaf-module rule: owners in `_LEAF_MODULES = {"tracker"}` may import no other module's `contracts` or `application` |
  | G4 | The adapter importing the plugin kernel's contracts, coupling it to prerequisite 2's wiring | A package-local test in `beadhive-tracker-github` asserting `adapter/` imports no `beadhive.kernel.*` name (kept local, not a wider global rule) |

- **Sequencing constraint.** A package without `plugin.json` is a library package and cannot
  import the contracts. The first adapter bead must either land after prerequisite 2's manifest
  bead or carry a minimal manifest that prerequisite 2 then owns. The dispatcher orders this.
- **Scoring (`bh:modularize`, 0 to 2 per dimension, seven dimensions).** The chosen boundary
  scored 14 of 14 (dependency direction 1 until G1 to G4 land). Alternatives are under
  "Alternatives rejected".

### 5. The projection: keep `bd github sync` as is

- **Keep.** `bd github sync --pull-only --prefer-github` materialises and publishes the public
  store, a job the port does not do. The port's consumers do not need it: the intake design
  keys the internal report on the **issue URL**, never on a public-store id (minted fresh per
  host, `bh-nkssm`), so intake joins to the projection by URL without reading it.
- **Do not wrap it** (candidate C, a hard stop): bd's only output is store rows and a summary
  line, with no comments, no author ids, no deletions and scoped labels consumed, and a "read"
  would carry a store write as a side effect.
- **Do not replace it now.** Replacement would mean bh writing beads into the public store from
  port records and re-implementing bd's mapper for no current consumer, contradicting the
  ADR-named implementation of "public: GitHub-authoritative, pull-only" without a decision.
- **Cost accepted:** two GitHub readers (the issue list is fetched twice per cycle when both
  run, with two checkpoints). Both use bh's single credential chain, so they read as one
  identity; the mitigation is to share the `TokenSource` with the projection.
- **Revisit as a separate decision** if the public store must carry comments, deletions or
  author identity, or if the bd pull keeps producing defects of the `bh-cm4er` kind.
- **Consequence for `bh-cm4er`: it stays on the bd path.** The uncommitted-rows defect is in
  the projection's pull leg (bd's engine issues no commit after `--pull-only`, and bh's report
  does not say so). Its fix (an opt-in commit after pull, or reporting `uncommitted rows: N`)
  belongs in `github_projection` and `tracker_sync`, or upstream in bd. The port neither fixes
  nor obsoletes it, and this molecule does not block it; it proceeds as the issue loop's first
  feature, as already drafted.
- **Related risk, flagged and not filed.** bd's incremental pull uses the **local wall clock**
  as `last_sync` (`time.Now()` rounded up one second, taken at the end of the sync, then used
  verbatim as the next `since`). An issue edited on GitHub after the fetch but before the sync
  ends carries an `updated_at` older than the stored checkpoint, so the next incremental pull
  can skip it, and host clock skew widens the window. This is inferred from code reading
  (`internal/tracker/engine.go`) and **not reproduced**. This ADR files nothing; the planner
  should file it beside `bh-cm4er` for the projection, with a regression test that drives the
  engine's clock. The port is not affected: its checkpoint is the tracker's own high-water mark.

### 6. Configuration surface

Principles (fixed by the operator and the spikes, not proposals):

- The adapter is **off unless configured**. Reads and writes are **separately enabled**. Every
  write operation is disabled by default.
- **Enabling a write is necessary, never sufficient.** Every write is its own operator-approved
  gate, and that gate issues a **single-use** `WriteGrant` scoped to one operation and one
  target. No configuration value, environment variable or prior approval can stand in for a
  grant, and a grant cannot be reused or widened.
- **Credentials are ambient and never stored.** No key holds a token. The schema fails closed
  on unknown members, including anything token-, password- or command-shaped (the existing
  plugin-config rule in `docs/PLUGIN-AUTHORING.md`). At most a source *name* is configurable.
- Every default is configurable, every optional feature is off, and hard limits exist only for
  correctness. The adapter's namespace is owned by prerequisite 2 (`plugins.<plugin_id>`, per
  the plugin kernel ADR); the adapter itself reads no config (section 4).

**The key names below are PROPOSALS** for prerequisite 2 to ratify. The namespace segment, the
exact spelling and the grouping are not decided here; the semantics and the defaults are.

| Proposed key (under `plugins.<tracker-github id>`) | Default | Meaning |
|---|---|---|
| `repo` | none (adapter inert) | `owner/name`; identity, not a secret |
| `base_url` | the public GitHub API | GHES prefix support |
| `credential.sources` | the existing chain: `env`, then `gh` | Source **names** only; never a value |
| `read.issues.enabled` | `false` | Enable `issues.read` |
| `read.comments.enabled` | `false` | Enable `comments.read` |
| `read.include_closed` | `true` | Closed issues are in scope when reading is on |
| `read.page_size` | tracker default | Page size for listings |
| `http_cache.enabled` | `false` | Conditional requests for the cheap full-id pass |
| `retry.max_retries` | 3 | Part of `RetryBudget` |
| `retry.max_single_wait` | 60 s | Part of `RetryBudget` |
| `retry.max_total_wait` | 120 s | Part of `RetryBudget` |
| `write.comment.enabled` | `false` | Enable `comment.create` (a necessary condition only) |
| `write.labels.enabled` | `false` | Enable `labels.change` (a necessary condition only) |
| `write.labels.owned_prefixes` | empty (no label writes possible) | The prefixes `LabelOwnership` allows |

Not configuration, by design: tokens, grants (runtime artifacts issued by each approved gate),
the install UUID and the outbox (local non-secret state), and the full-id reconcile cadence
(a consumer concern, prerequisite 3). Read-only first: the first implementation molecule ships
the read path; the write keys exist in the schema from day one (so the default is pinned and
tested) but the write operations land later, each behind its own gate.

## Inconsistencies found between the spikes, and how this ADR resolves them

The three spikes agree on verdict and substance. Reading them side by side shows four
differences; none is a conflict of intent, but an implementer would trip on each.

1. **Where `TokenSource` lives.** `bh-mqvdq` (section 5, item 1) puts a `TokenSource` contract
   "in the adapter's `contracts` module"; `bh-gj1hb` keeps credentials out of the port and
   constructs the adapter with explicit arguments, and a plugin package cannot import
   `beadhive.run`. **Resolution:** the tracker contracts stay credential-free (the port is
   neutral; its `CapabilityReport` carries only a provenance string). `TokenSource` is a small
   structural `Protocol`, and `ResolvedToken` a stdlib-only value type, owned by the adapter
   package; the composition root passes a conforming callable, so root needs no import of the
   package. Sharing `scrub()` and `ResolvedToken` with the root projection (which `bh-mqvdq`
   asks, retiring the private `_scrub`) needs a stdlib-only home both sides may import under the
   package-import rules; picking it is an implementation-molecule design bead (open question
   O1). Until then the projection keeps its `_scrub`.
2. **Marker grammar.** `bh-mqvdq`'s marker is `<!-- bh:v1 hive=... install=... -->`; the
   `bh-gj1hb` prototype's is `... outbox=<id> -->` and the port's `OwnMarker` carries
   `outbox_id`. **Resolution:** the `bh-gj1hb` form wins, because the outbox id in the marker is
   what lets a `WriteOutcomeUnknown` comment be found again. The encoding stays adapter-owned
   and versioned (`bh:v1`); the port carries only the decoded `OwnMarker`. The marker still
   counts only as the last non-empty line, so a quoted copy is ignored.
3. **Who classifies "own".** `bh-mqvdq` places `classify_comment` (outbox, then App, then actor
   plus marker) with the adapter; `bh-gj1hb` lists own-message classification under "not in the
   port". **Resolution:** the port transports the evidence (`author`, `via`, decoded `marker`,
   and `whoami()`); the classification rules and the known-actor set (current actor plus those
   recorded in the outbox ledger) belong to the intake consumer (prerequisite 3), which owns the
   outbox. The adapter's job is to decode the marker, never to decide.
4. **Neutral record names.** `bh-a1svm` calls its neutral records `IssueRec` and `CommentRec`
   (and the protocol `GitHubTransport`); `bh-gj1hb`'s `TrackerIssue`, `TrackerComment` and
   `TrackerReader` supersede them. `GitHubTransport` survives only as the adapter's internal
   seam, if one is wanted at all; it is not a public contract.

## Consequences

### Positive

- Intake and any later feature depend on one neutral, tested port, not on `bd github sync`,
  `gh` or the REST shape. A second tracker is a new adapter package, not a consumer change.
- githubkit and its 33 MB stay out of the root wheel and out of every consumer test closure.
  Consumer tests run against `MemoryTracker`; adapter tests run in the package's own sandbox
  over an offline mock transport.
- The one-way rule is enforced by tooling, not convention, once G1 to G4 land.
- Read-only operation is possible from day one with no write code present, and a write cannot
  happen without a fresh, scoped, single-use operator grant.
- The checkpoint is the tracker's own high-water mark, so the port is immune to the
  wall-clock skip that affects bd's `last_sync`.

### Negative or accepted costs

- One licence override (`hishel`) with an expiry that someone must re-verify.
- An adapter-owned retry layer and sleep seam instead of githubkit's defaults.
- Two GitHub readers until the projection is revisited; duplicate issue fetches per cycle.
- Four small checker extensions (G1 to G4) must land before the dependency rule is fully real.
- The adapter cannot ship as a usable plugin before prerequisite 2's manifest exists.
- `UNKNOWN` write capability means the first write for fine-grained and App tokens is also the
  probe; the operator may approve a write that is then refused (with an exact diagnostic).

**Test-closure expectation (expected, not measured).** An adapter change runs only the package's
own tests under the `packages` attest key; a contract change runs the conformance cases for
both implementations plus consumers' boundary tests; a consumer change never builds the GitHub
package. The implementation molecule records `pants dependents` per path, per the
measured-closure rule in `docs/MODULES.md`.

## Alternatives rejected

Boundary candidates, scored on the seven `bh:modularize` dimensions (`bh-gj1hb` E18):

| Candidate | Total | Why not |
|---|---|---|
| **A: neutral port + plugin package** (chosen) | 14 | n/a |
| B: same port, adapter in root `integrations/github` | 12 | Puts githubkit in the root wheel or a root extra; an `integration` may import any module's application layer, so "the GitHub module never imports intake" is not enforced; tests live in the root suite |
| C: wrap bd (`bd github sync`, read the public store back) | 3 | Hard stop: the lossy mapping drops comments, author ids and deletions, a read carries a store write, tests need the bd binary |
| D: no port; intake consumes a GitHub-shaped `GitHubTransport` | 9 | Leaks `number` and `Link` into intake; no second tracker without changing consumers |
| E: one wide port that also owns threads, revisions and the outbox | 5 | Hard stop: consumers would depend on thread-model internals that belong to prerequisite 3 |
| F: mirror bd's `IssueTracker` (whole-issue create and update, `FieldMapper`) | 9 | Contradicts "owned labels only" and the pull-only public store; drags bead vocabulary into the port. Its small-interface split is adopted, its operations are not |

Adapter-location variants rejected with B: a `modules/tracker/adapters/github/` subpackage is
classified `legacy` by the checker (an unchecked importer) and puts a provider inside a neutral
module; a library package (no `plugin.json`) cannot import the contracts.

Transport candidates (`bh-a1svm` E9, scored on the transport boundary only):

| Candidate | Total | Why not |
|---|---|---|
| **githubkit 0.16.1** (chosen) | 13 | n/a |
| Plain `httpx` | 14 | Scores higher on structure and costs no dependency, but `Link` parsing, retry policy, rate-limit arithmetic and an ETag cache are ours to write and keep correct (two bugs found in the prototype), with no typed models, no GHES handling, and growth with every write endpoint. Kept as the documented fallback |
| gidgethub 6.0.0 | 11 | Async-only; its `httpx` helper needs the `httpx2` distribution (a second HTTP client) or our own `_request`; untyped dicts; needs its own licence override (`uritemplate`) |
| PyGithub 2.10.0 | n/a | **Copyleft (LGPL)**, outside `license_allow`; drags in `requests` and `pynacl`, a second HTTP stack. Cannot honestly be overridden |
| `gh api` | 9 | A subprocess per call, untyped JSON, no `MockTransport` (needs a subprocess fake), host state; hard-requiring the binary contradicts "host tools are optional". Stays a credential source and an optional later alternative |

Other rejections: GraphQL `viewerPermission` as the capability probe (a `POST`, so the "GET
only" invariant becomes uncheckable, and it collapses "no" and "could not tell"); keying the
actor on `login` (mutable); a stored or config-held token (violates the fixed constraint);
treating the bd `last_sync` clock as the port's checkpoint (the skip risk in section 5).

## Open questions and unverified items carried from the spikes

Open design questions:

- **O1.** The home of the shared `scrub()` and `ResolvedToken` (inconsistency 1): a stdlib-only
  module both root and the plugin package may import. Decide in the implementation molecule's
  design bead, in line with prerequisite 2's package wiring.
- **O2.** Final config key names, grouping and namespace (section 6 is a proposal).
- **O3.** Issue identity across a transfer or a Jira-style key change is a question for the
  intake design's URL key (D1), not a port change.

Unverified, as stated in the spikes (none blocks GO):

- From `bh-a1svm`: GitHub's "a `304` does not count against the primary rate limit" (documented
  rule, not tested); that githubkit's `Issue` models validate every live payload (a handful of
  public rows were parsed); behaviour against authenticated, SSO-restricted or GHES hosts;
  Python 3.15. Python 3.14 passed but is outside the repo's declared classifiers (3.11 to
  3.13).
- From `bh-mqvdq`: fine-grained PAT and App installation behaviour (documented, not observed;
  fixtures encode the documented forms); every refusal diagnostic except `401` and `404`
  (re-verify on the first gated write); whether a fine-grained token's `permissions` object is
  capped by the token's grant.
- From `bh-gj1hb`: the Jira and Linear mapping rows (documentation, not exercised); whether a
  GitHub issue's numeric `id` survives a transfer; GitHub's real ordering guarantees under
  concurrent edits beyond its documented parameters; the bd `last_sync` window (code reading
  only); every test-closure figure (expected, not measured).

## Next steps

This ADR does **not** file the implementation molecule and does not touch any other bead.

1. **File the implementation molecule** through `/bh:replan` on epic `bh-ire9a`, run by a
   human-interactive planner. The close reason of `bh-x6f9l` carries the verdict (GO).
2. **Suggested bead sequence** (from `bh-gj1hb` section 7), for the planner to shape:
   1. Create `modules/tracker` (contracts and application) plus `beadhive.testing.tracker`
      (cases, `MemoryTracker`, world), with checker extensions G1 to G3 and their negative
      controls.
   2. Create `packages/beadhive-tracker-github`: port the prototype mapping onto githubkit with
      the `hishel` override and a `MockTransport` stand-in grown from `fake_github_rest.py`;
      the adapter-owned retry layer; the `TokenSource` seam and `scrub()`; the identity probe
      and refusal diagnostics; the marker codec; the G4 package-local test and the G2 prefix
      entry. **Ordering constraint:** this bead lands after prerequisite 2's plugin manifest
      bead, or carries a minimal manifest that prerequisite 2 then owns.
   3. Read path first; each write operation (`comment.create`, then `labels.change`) as its own
      later bead behind its own operator-approved gate, re-verifying the documented refusal
      diagnostics against recorded real responses.
   4. `bh-cm4er` and the `last_sync` risk proceed independently on the bd path, not as
      children of this molecule.
3. **Sequencing with prerequisite 2.** Prerequisite 2 (`bh-mobxo`) owns `plugin.json`, the
   capability id, the `plugins.<id>` namespace, enablement, doctor and status, and the
   `bind_application_port` call, and ratifies the config keys in section 6. Its dispatcher
   should order the adapter package bead after its manifest bead.
4. **The sibling design molecules may proceed.** Their kickoff waited on this decision. The
   operator can now approve the kickoff gates for the plugin-category design molecule
   (`bh-mobxo`, swarm `bh-zmftp`) and the external-intake design molecule (`bh-wjvq9`, swarm
   `bh-r8xhz`); both consume the port contract recorded above.
5. **Record in the implementation beads** the `pants dependents` measurement for each test
   closure path (section "Consequences"), and the `hishel` override's `effectiveUntil`
   re-verification date.
