# beadhive-tracker-github

The GitHub tracker adapter **plugin** (`tracker-github`, capability `tracker.adapter` v1).
Design: [tracker-adapter-plugin-adr.md](../../docs/design/tracker-adapter-plugin-adr.md) and
[tracker-adapter-port-adr.md](../../docs/design/tracker-adapter-port-adr.md).

> **Read path plus `comment.create` and `labels.change`.** The adapter lists issues (closed
> included) and comments, takes full id snapshots for deletion reconciliation, resolves issue URLs
> (`IssueResolver`), reports the actor and a GET-only tri-state capability probe. Its two landed
> writers are `comment.create` (`CommentWriter.post_comment`, W8 `bh-03s4w`) and `labels.change`
> (`LabelWriter.change_labels`, W9 `bh-t082i`). A writer method exists only when its write flag is
> on, and even then every call needs a fresh, single-use, operator-approved `WriteGrant`. **No live
> GitHub write has been made yet**: the first real write is an operator-approved step recorded on
> the writer's bead, outside the tests.

## Shape

| Path | Holds |
|---|---|
| `src/beadhive_tracker_github/plugin.json` | The manifest. Byte-identical to `src/beadhive/kernel/plugins/manifests/tracker-github.json` (drift-tested) |
| `src/beadhive_tracker_github/factory.py` | The one call root's lazy row makes: `build(settings, token_source, *, project, sleep)` |
| `src/beadhive_tracker_github/adapter/` | The wiring-free port implementation. The **only** place `githubkit` may be imported, and only inside functions |

Inside `adapter/`: `tracker` (the port implementation), `compose` (the writers of the enabled
write operations), `comments` (the `comment.create` writer), `labels` (the `labels.change`
writer), `grants` (the one in-process single-use grant ledger every writer spends from),
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
* The connect/read timeout is finite (30 s; githubkit's default is none).
* **`comment.create`** posts the body plus the `bh:v1` marker line. Refused before any request
  (and before the credential is resolved) when the operation is off, the grant is missing, names
  another operation or issue, or was already presented; the grant is spent by the first call that
  passes those checks, whatever happens next. A rate-limit refusal is retried within the budget;
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
  every case passes, the `comment.create` cases and `labels.only-owned` included (no expected
  failure is left).
* **Refusal diagnostics** are replayed from `tests/recorded/comment_create_refusals.json`, one
  exchange per row of the `bh-mqvdq` table, each labelled with its evidence: only the `401` and
  `404` status and message were observed live (on read-only GETs, `bh-mqvdq` Evidence 10); every
  other row is the documented form. **Live re-verification is pending** the first
  operator-approved write.
* **Offline**: `tests/conftest.py` disables the real transport and outbound sockets for every
  test; recorded exchanges (`tests/recorded/`) are synthetic, never captured with a credential.

## Licence

githubkit is MIT. The transitive `hishel` declares only the generic BSD trove classifier, so the
root `osv-scanner.toml` carries a time-boxed override for it (see the entry for the date it was
re-verified and when it lapses).
