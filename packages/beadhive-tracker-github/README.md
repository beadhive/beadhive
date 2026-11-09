# beadhive-tracker-github

The GitHub tracker adapter **plugin** (`tracker-github`, capability `tracker.adapter` v1).
Design: [tracker-adapter-plugin-adr.md](../../docs/design/tracker-adapter-plugin-adr.md) and
[tracker-adapter-port-adr.md](../../docs/design/tracker-adapter-port-adr.md).

> **Read path plus `comment.create`, `labels.change`, `labels.define`, `issue.close`,
> `issue.reopen`, `issue.type` and `issue.fields`.** The
> adapter lists issues (closed included), comments and repository label definitions (`labels.read`,
> `LabelReader`), takes full id snapshots for deletion reconciliation, resolves issue URLs
> (`IssueResolver`), reports the actor and a GET-only tri-state capability probe. Its landed
> writers are `comment.create` (`CommentWriter.post_comment`, W8 `bh-03s4w`), `labels.change`
> (`LabelWriter.change_labels`, W9 `bh-t082i`), `labels.define` (`LabelDefiner.define_labels` and
> `LabelPruner.delete_labels`, `bh-uv09p`), `issue.close` (`IssueCloser.close_issue`,
> `bh-pr5ll`), `issue.reopen` (`IssueReopener.reopen_issue`, `bh-ih484`), `issue.type`
> (`IssueTypeWriter.set_issue_type`) and `issue.fields`
> (`IssueFieldWriter.set_issue_fields`, both `bh-poqy0`). A writer method exists only when its
> write flag is on, and even then every call needs a fresh, single-use, operator-approved
> `WriteGrant`. **No live GitHub write has been made yet**: the first real write is an
> operator-approved step recorded on the writer's bead, outside the tests.

## Shape

| Path | Holds |
|---|---|
| `src/beadhive_tracker_github/plugin.json` | The manifest. Byte-identical to `src/beadhive/kernel/plugins/manifests/tracker-github.json` (drift-tested) |
| `src/beadhive_tracker_github/factory.py` | The one call root's lazy row makes: `build(settings, token_source, *, project, sleep)` |
| `src/beadhive_tracker_github/adapter/` | The wiring-free port implementation. The **only** place `githubkit` may be imported, and only inside functions |

Inside `adapter/`: `tracker` (the port implementation), `compose` (the writers of the enabled
write operations), `comments` (the `comment.create` writer), `labels` (the `labels.change`
writer), `label_definitions` (the `labels.read` reader and the `labels.define` writer), `closing` (the
`issue.close` writer), `reopening` (the `issue.reopen` writer), `issue_values` (the `issue.type`
and `issue.fields` writers), `grants` (the one in-process single-use grant ledger every writer spends from),
`transport` (githubkit: one credential per operation; writes only through `Session.send`, the one
write seam of an operation opened for one), `retry` (the adapter-owned retry decision on an
injected sleep and clock),
`diagnostics` (the exact scrubbed refusal wording), `identity` (the tri-state probe), `mapping`
(GitHub JSON to port records), `marker` (the `bh:v1` own-message codec), `urls` (the location and
the `IssueResolver` grammar) and `settings` (the folded `plugins.tracker-github` fragment).

## Behaviour worth knowing

* **Disabled reads are refused before any request**, before the credential is even resolved,
  like `MemoryTracker`; identity (`whoami`, `probe_capabilities`) is not gated by a read flag.
* **githubkit's own retry is off** (`auto_retry=False`), so its `time.sleep` and its real-clock
  rate-limit arithmetic are never reached; the adapter retries rate limits (reads and writes)
  and read server errors within `RetryBudget`, on the injected `sleep`, and computes reset waits
  on an injected clock. `http_cache` is passed explicitly and is off unless opted in.
* Requests pin `X-GitHub-Api-Version: 2022-11-28` and send `since` in GitHub's ISO 8601 `Z` form
  (githubkit renders a `datetime` with `str()`). A page cursor's `next` URL is followed verbatim
  only on the API origin, so a tampered cursor cannot send the credential elsewhere.
* The connect/read timeout is finite (30 s by default, configurable with `timeout_seconds`, a
  number in `(0, 300]`; githubkit's own default is none, and none is never allowed, nor is a
  value above the 300 s cap, which is refused rather than clamped).
* **`comment.create`** posts the body plus the `bh:v1` marker line. Refused before any request
  (and before the credential is resolved) when the operation is off, the grant is missing, names
  another operation or issue, or was already presented. A missing credential is refused next and
  keeps the grant; the grant is spent by the first call that passes those checks and resolves a
  credential, whatever happens next. A rate-limit refusal is retried within the budget;
  a `5xx`, a timeout or dropped connection after the send, an unconfirmed redirect (a write
  client never follows one) or an unreadable `201` raises `WriteOutcomeUnknown` and is **never
  retried** (the consumer reconciles by the marker's outbox id). Every other refusal carries the
  exact scrubbed one-line diagnostic.
* **`labels.change`** adds and removes labels inside `write.labels.owned_prefixes` only (`POST` /
  `DELETE .../issues/{n}/labels`), never rewriting the issue's whole label list. It is refused
  before any request unless it is enabled (flag on AND a non-empty prefix list, which config load
  enforces), the call's single-use `WriteGrant` names this operation and issue and is unspent, and
  every label is owned; a missing credential is refused before the grant is spent. It sends
  through the same write seam, so a redirect is never followed and an ambiguous outcome (`5xx`,
  timeout, dropped connection, redirect, unreadable answer) is `WriteOutcomeUnknown`, never
  retried. Adds go first, then removes; a `404` on a remove is told apart from an invisible issue
  by reading the issue back.
* **`issue.close`** closes one issue with `PATCH .../issues/{n}` carrying exactly `state: closed`
  and `state_reason` (`completed`, `not_planned` or `duplicate`, mapped from the port's
  `CloseReason`); the payload never carries `labels`, which would replace the issue's whole label
  set. It is refused before any request unless `write.issue_close.enabled` is on and the call's
  single-use `WriteGrant` names `issue.close` and this issue and is unspent; a missing credential
  is refused before the grant is spent. Closing an already closed issue succeeds. An ambiguous
  outcome, or an answer that is not a closed issue, is `WriteOutcomeUnknown`, never retried (the
  outbox reconciles by reading the issue's state back). The probe reports it `no` below triage.
  A `duplicate` close may name its canonical (`duplicate_of`, `bh-iamru`): the adapter reads the
  canonical once (`GET .../issues/{n}`) inside the write's session, before the grant is spent, and
  adds its numeric issue `id` as `duplicate_issue_id` to the same PATCH ("Update an issue";
  GraphQL's `closeIssue` `duplicateIssueId` is the same link). A canonical of another repository,
  or the issue itself, or with another reason, is refused before any request; one that is missing,
  a pull request, moved or unreadable is refused (`NotVisible`) with the grant kept. Issue
  dependencies and sub-issues are not written.
* **`issue.reopen`** (`bh-ih484`), the inverse of `issue.close` for a close triage later corrects,
  reopens one issue with `PATCH .../issues/{n}` carrying exactly `{"state": "open"}`; the payload
  never carries `labels`. Refusal order, the single-use grant (a close's grant never reopens), the
  credential check, the never-retried ambiguous outcome and the probe (`no` below triage) are those
  of `issue.close`; it is behind its own switch, `write.issue_reopen.enabled` (default off).
  Reopening an already open issue succeeds; an answer that is not an open issue is
  `WriteOutcomeUnknown` (the outbox reconciles by reading the state back).
* **`issue.type`** sets one issue's type with `PATCH .../issues/{n}` carrying exactly
  `{"type": <name>}` (never `labels`); a type the organization does not define is GitHub's `422`
  (`PayloadRejected`). **`issue.fields`** sets named field values: ONE `GET
  /orgs/{owner}/issue-fields` resolves each field's id and checks a select field's option (an
  unknown field or option is `PayloadRejected` with the grant kept), then `POST
  .../issues/{n}/issue-field-values` with `{"issue_field_values": [{"field_id", "value"}]}` adds
  only those values (never `PUT`, which replaces every field). Each is behind its own switch,
  `write.issue_type.enabled` / `write.issue_fields.enabled` (default off), which also gates the
  READ: the issue listing maps `type` and `issue_field_values` into `TrackerIssue.issue_type` /
  `fields` only while the switch is on (`None`, not read, otherwise). Refusal order, grant
  spending and the never-retried ambiguous outcome are those of `issue.close`; an answer that does
  not show the value is `WriteOutcomeUnknown`. The probe reports both `no` below the write role
  (GitHub silently drops a type or field from anyone without push) and `issue.fields` `no` for a
  classic token without `read:org`. A fine-grained token or a GitHub App needs the organization
  permission "Issue Fields" (read) for `GET /orgs/{owner}/issue-fields` and the repository
  permission "Issues" (write) for the `POST .../issue-field-values` and the type's `PATCH`; the
  probe cannot read those grants, so it reports `unknown` and the first approved write is the
  probe. Endpoints checked against docs.github.com
  (`/rest/issues/issue-field-values`, `/rest/orgs/issue-fields`, `/rest/issues/issues`), served on
  API version 2022-11-28.
* **`labels.read`** lists every repository label definition, owned or not (`GET .../labels`,
  `per_page=100`, following `Link: rel="next"`); like every read it is refused before any request
  while `read.labels.enabled` is off.
* **`labels.define`** converges repository label definitions inside `write.labels.owned_prefixes`
  (the ONLY ownership: the port's call carries none, and no prefix means the operation is not
  enabled) under one repository-scoped grant (`WriteGrant.target` is the `ProjectRef`). It is
  refused before any request on a malformed batch (a color must be six hex digits without `#`, a
  description at most 100 characters), the flag off (`write.label_definitions.enabled`), a
  mismatched or spent grant, a foreign name or a missing credential. It then reads the live
  definitions once and sends only the difference, in sorted name order: `POST .../labels` to
  create, `PATCH .../labels/{name}` to update (a case-variant of an owned label is renamed, never
  duplicated). Pruning is the separate optional `LabelPruner.delete_labels(names, *, grant)` (same
  operation, scope and refusal order; a name the repository does not hold is already gone):
  `DELETE .../labels/{name}`, whose `204 No Content` is a known success. Every `{name}` is
  percent-encoded. The grant is spent after the read, just before the first write.
* **`204 No Content`** answering a write is a known success with no body, never
  `WriteOutcomeUnknown`; an empty `2xx` is one too where the caller declares the endpoint
  documents `204`.

## Install and enablement

Install with the `beadhive[github]` extra. **Installation never implies enablement and enablement
never installs.** The plugin is off unless `plugins.tracker-github.enabled` is `true` on the host;
a store binding (`managed_repos[].stores.public.adapter`) never enables it; a hive with no binding
is unaffected. githubkit is declared here, not in the root distribution, so `import beadhive` never
needs it and never imports it.

## Status and doctor

The verbs live in root (`src/beadhive/tracker_github_plugin.py`, mounted lazily from the plugin CLI
runtime catalog; a plugin package may not import `beadhive.bootstrap`) and never import this package.

`bh plugin tracker-github status [--json]` and `doctor [--json] [--probe] [--hive provider/org/repo]`
are read-only and render the plugin state, the credential **source names** that are set or unset
(`env:GH_TOKEN`, `env:GITHUB_TOKEN`, `gh` on the path; never a value), and the capability
tri-state. Without `--probe` they are fully offline: no request, no credential read, no `gh` run,
identity shows `not probed`. `doctor --probe` is the only network path: it builds the one bound
store's adapter from the configured credential chain and runs its GET-only `whoami` and capability
probe, and is refused (with the binding rule's message) while the plugin is disabled. Neither verb
writes, spends a grant or enables anything; `doctor` exits 1 only on an error while the plugin is
enabled.

## Rules enforced by this package's tests

* **P1** githubkit is imported only lazily and only under `adapter/`.
* **G4** the package imports no `beadhive.kernel.*` and, from `beadhive_plugins`, only
  `beadhive_plugins.credentials`.
* **Conformance**: the shared `beadhive.testing.tracker` suite runs over an offline
  `httpx.MockTransport` stand-in (`tests/tracker_github_fake.py`), on github.com and a GHES prefix;
  every case passes, the `comment.create` cases, `labels.only-owned` and the seven
  `labels.*` definition cases included (no expected failure is left).
* **Refusal diagnostics** are replayed from `tests/recorded/comment_create_refusals.json`, one
  exchange per row of the `bh-mqvdq` table, each labelled with its evidence: only the `401` and
  `404` status and message were observed live (on read-only GETs, `bh-mqvdq` Evidence 10); every
  other row is the documented form. **Live re-verification is pending** the first
  operator-approved write. `tests/recorded/label_definitions_*.json` replay the paged listing, one
  create / update / `204` delete batch and the refusals GitHub's documentation is silent on (a
  duplicate name, a missing permission, a delete answered "not found").
* **Offline**: `tests/conftest.py` disables the real transport and outbound sockets for every
  test; recorded exchanges (`tests/recorded/`) are synthetic, never captured with a credential.

## Licence

githubkit is MIT. The transitive `hishel` declares only the generic BSD trove classifier, so the
root `osv-scanner.toml` carries a time-boxed override for it (see the entry for the date it was
re-verified and when it lapses).
