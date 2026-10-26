# beadhive-tracker-github

The GitHub tracker adapter **plugin** (`tracker-github`, capability `tracker.adapter` v1).
Design: [tracker-adapter-plugin-adr.md](../../docs/design/tracker-adapter-plugin-adr.md) and
[tracker-adapter-port-adr.md](../../docs/design/tracker-adapter-port-adr.md).

> **Read path.** The adapter lists issues (closed included) and comments, takes full id
> snapshots for deletion reconciliation, resolves issue URLs (`IssueResolver`), reports the actor
> and a GET-only tri-state capability probe. It has **no writer**: `comment.create` and
> `labels.change` are later beads, each behind its own operator-approved gate, so the bound object
> has no write method even when a write flag is on.

## Shape

| Path | Holds |
|---|---|
| `src/beadhive_tracker_github/plugin.json` | The manifest. Byte-identical to `src/beadhive/kernel/plugins/manifests/tracker-github.json` (drift-tested) |
| `src/beadhive_tracker_github/factory.py` | The one call root's lazy row makes: `build(settings, token_source, *, project, sleep)` |
| `src/beadhive_tracker_github/adapter/` | The wiring-free port implementation. The **only** place `githubkit` may be imported, and only inside functions |

Inside `adapter/`: `tracker` (the port implementation), `transport` (githubkit: one credential per
operation, GET only), `retry` (the adapter-owned retry decision on an injected sleep and clock),
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

## Install and enablement

Install with the `beadhive[github]` extra. **Installation never implies enablement and enablement
never installs.** The plugin is off unless `plugins.tracker-github.enabled` is `true` on the host;
a store binding (`managed_repos[].stores.public.adapter`) never enables it; a hive with no binding
is unaffected. githubkit is declared here, not in the root distribution, so `import beadhive` never
needs it and never imports it.

## Rules enforced by this package's tests

* **P1** githubkit is imported only lazily and only under `adapter/`.
* **G4** the package imports no `beadhive.kernel.*` and, from `beadhive_plugins`, only
  `beadhive_plugins.credentials`.
* **Conformance**: the shared `beadhive.testing.tracker` suite runs over an offline
  `httpx.MockTransport` stand-in (`tests/tracker_github_fake.py`), on github.com and a GHES prefix;
  the five writer cases are strict expected failures until the write beads land.
* **Offline**: `tests/conftest.py` disables the real transport and outbound sockets for every
  test; recorded exchanges (`tests/recorded/`) are synthetic, never captured with a credential.

## Licence

githubkit is MIT. The transitive `hishel` declares only the generic BSD trove classifier, so the
root `osv-scanner.toml` carries a time-boxed override for it (see the entry for the date it was
re-verified and when it lapses).
