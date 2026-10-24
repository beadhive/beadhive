# Spike `bh-a1svm` — how should the GitHub adapter talk to GitHub: `gh`, a Python SDK, or plain HTTP?

**Bead:** `bh-a1svm` · **Seat:** `dev/transport` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-x6f9l` (adopt the bead tracker adapter port and the GitHub adapter
transport) and the port-contract spike `bh-gj1hb`, both under epic `bh-ire9a`.

## Question

Which single transport should the GitHub adapter module use to read GitHub issues (including
closed ones) and issue comments, with pagination and a `since` checkpoint — and does the leading
candidate, **githubkit**, survive this repository's constraints?

- **License gate:** `just license-check` (the `license_allow` list in the `justfile`).
- **Python matrix:** every interpreter this repo supports, with 3.14 reported explicitly.
- **Testability:** retry and HTTP-cache behaviour must be disable-able or injectable, so a test
  asserts retry decisions on a mocked clock and never sleeps.
- **Dependency weight:** what it adds next to the existing `httpx` / `pydantic` pins.
- **Dependency direction:** the transport must live inside the GitHub adapter, never in the
  domain/application layers.

Explicitly **not** asked: the port contract (`bh-gj1hb`), credential and identity resolution, the
config surface, or GitHub *writes*. This spike performed no GitHub write of any kind.

## Method

1. **Read the repo's constraints.** `justfile` (`license_allow`, `license-check`, `sbom`),
   `osv-scanner.toml` (override mechanism), `pyproject.toml` (`requires-python`, classifiers),
   `uv.lock` (existing pins), `scripts/check_import_boundaries.py`, `scripts/check_package_imports.py`,
   `src/beadhive/github_projection.py` (`TokenSource`, `ambient_token`), and `hq_release_catalog.py`
   (the existing injected-`httpx.Client` + `httpx.MockTransport` pattern).
2. **Cite the candidates** from the PyPI JSON API (`https://pypi.org/pypi/<name>/json`) and, for the
   `gh` CLI, the GitHub API's `license` and `releases/latest` endpoints.
3. **Run the real license gate.** `just license-check` on the untouched tree, then on a scratch
   copy of the tree (`git archive HEAD` into the session scratchpad) after `uv add githubkit` —
   once per candidate (`githubkit`, `gidgethub`, `PyGithub`). The product `pyproject.toml` and
   `uv.lock` were never touched.
4. **Build throwaway virtualenvs** under the session scratchpad, one per interpreter
   (`uv venv --python 3.11 | 3.12 | 3.13 | 3.14`), twice: (a) latest-resolvable `githubkit`, and
   (b) `githubkit` installed against the project's own `uv.lock` pins, exported with
   `uv export --frozen --no-dev --no-hashes` and passed as `-c constraints.txt`.
5. **Write a prototype** under `docs/spikes/artifacts/bh-a1svm/` — two interchangeable
   transports (`transport_githubkit.py`, `transport_httpx.py`) behind one neutral
   `GitHubTransport` protocol (`sync_port.py`), driven by an in-memory GitHub stand-in
   (`fake_github.py`) over `httpx.MockTransport`, with 27 offline tests. Nothing under `src/`
   imports it.
6. **Negative control.** Flip `state="all"` to `state="open"` in both transports and confirm the
   closed-issue tests fail (they do, for both), then restore.
7. **Read-only live corroboration** (optional; a few dozen unauthenticated `GET`s at most, of the
   public `octocat/Hello-World` and `cli/cli` repositories; my first live script over-paginated
   before I stopped it, which cost rate-limit budget but wrote nothing) to check the fixture's
   assumptions about the real wire format. No credential was used and nothing was written.

Reproduce:

```sh
uv venv --python 3.12 /tmp/gk && uv pip install --python /tmp/gk/bin/python githubkit pytest
/tmp/gk/bin/python -m pytest -c /dev/null --rootdir=. -p no:cacheprovider docs/spikes/artifacts/bh-a1svm
```

## Evidence

### E1. Candidates: license and latest release

All values read from PyPI JSON on 2026-10-24 (`gh` from the GitHub API).

| Candidate | Latest release | License | Python | Copyleft? |
|---|---|---|---|---|
| githubkit | 0.16.1 (2026-08-14) | MIT (`License-Expression`) | `>=3.10,<4.0`; **no Python classifiers at all** | no |
| gidgethub | 6.0.0 (2026-09-25) | `Apache-2.0 AND MIT` | `>=3.10`; classifiers 3.10 to 3.15 | no |
| plain httpx | 0.28.1 (project pin, already installed) | BSD-3-Clause | n/a | no |
| PyGithub | 2.10.0 (2026-08-20) | **LGPL** (trove classifier only; no SPDX expression) | `>=3.10`; 3.10 to 3.15 | **yes** |
| `gh` CLI | v2.103.2 (2026-10-23; this host runs 2.97.0) | MIT | host binary | no |

**PyGithub is copyleft (LGPL).** Consequence: it is outside `license_allow`
(`MIT,Apache-2.0,BSD-3-Clause,BSD-2-Clause,ISC,PSF-2.0,Unlicense,MPL-2.0,HPND`). The real gate
fails on it (E3). Allow-listing LGPL would be a policy change, not an override, because the
license is genuinely LGPL; it also drags in `requests` and `pynacl`, a second HTTP stack beside
`httpx`. Rejected, as the bead already stated.

### E2. What each candidate would add to the dependency set

Observed from `uv add` into the scratch copy of the tree, compared by `name version` pairs
against the real `uv.lock`:

| Candidate | New distributions | Existing pins moved |
|---|---|---|
| githubkit | `githubkit` 0.16.1, `githubkit-schemas` 26.10.21, `githubkit-schemas-2026-03-10` 26.10.21, `hishel` 1.4.0, `anysqlite` 0.0.5, `msgpack` 1.2.3 | **none** (`httpx` 0.28.1, `pydantic` 2.13.4, `anyio`, `typing-extensions`, `certifi`, `h11`, `idna` already present) |
| gidgethub | `gidgethub` 6.0.0, `uritemplate` 4.2.0 | none |
| PyGithub | `pygithub` 2.10.0, `pynacl` 1.6.2 | none |

Weight for githubkit: the generated schema package `githubkit-schemas-2026-03-10` is the bulk
(wheel 3.6 MB; about 30 MB installed under `githubkit_schemas/`); `msgpack` is a compiled wheel
(about 0.46 MB). Total new install is roughly 33 MB, versus the 0.3 MB `httpx` package. Measured on
this host (single runs, indicative, not gating): `import githubkit` adds about 230-270 ms on top
of an already-imported `httpx` (about 100-135 ms; 356 modules loaded in total after
`import githubkit`, `httpx` and `pydantic` included), and the first typed call costs about 130 ms
more while the models load. This is why
the import must stay lazy (see Recommendation).

`githubkit` requires `pydantic>=1.9,<3`, `httpx<1`, `anyio<5`, `typing-extensions<5`, all
satisfied by the project's existing pins; `githubkit-schemas` is **CalVer with only a lower
bound** (`>=26.5.7`), so the lockfile, not the range, is what keeps the schema package stable.

### E3. The license gate, run for real

`just license-check` runs `osv-scanner scan source -L bom.json --licenses=<license_allow>` over a
CycloneDX SBOM exported from `uv.lock`; license data comes from deps.dev at scan time.

| Tree | Result |
|---|---|
| Untouched (80 packages) | pass (exit 0) |
| `+ githubkit` (86 packages) | **FAIL**: `hishel@1.4.0: non-standard` |
| `+ githubkit` and a `hishel` override in `osv-scanner.toml` | pass (exit 0) |
| `+ gidgethub` | **FAIL**: `uritemplate@4.2.0: non-standard` |
| `+ PyGithub` | **FAIL**: `pygithub@2.10.0: non-standard` |

So **githubkit itself is MIT and passes, but one transitive dependency does not pass as shipped**.
`hishel` declares only the generic trove classifier `License :: OSI Approved :: BSD License` and
no SPDX expression, which deps.dev buckets as `non-standard` — the same class the file already
dispositions for `colorama`, `pyperclip` and `shellingham`. Its wheel ships
`hishel-1.4.0.dist-info/licenses/LICENSE`, which I read: the full three-clause BSD text including
the "Neither the name of the copyright holder ..." endorsement clause. That supports exactly the
override the scratch run used (the same shape as the existing entries, with the file's mandatory
`effectiveUntil`):

```toml
[[PackageOverrides]]
name = "hishel"
ecosystem = "PyPI"
license.override = ["BSD-3-Clause"]
effectiveUntil = 2027-02-01
reason = "BSD-3-Clause. Verified: hishel-1.4.0.dist-info/licenses/LICENSE is the three-clause text incl. endorsement clause; metadata carries only the generic BSD classifier."
```

The other new githubkit distributions resolve cleanly without an override: `githubkit` and
`githubkit-schemas` declare `License-Expression: MIT`, `anysqlite` BSD-3-Clause, `msgpack`
Apache-2.0 (from each wheel's `METADATA`). gidgethub needs its own override for `uritemplate`
(PyPI: `BSD 3-Clause OR Apache-2.0`), so the "needs an override" cost is not unique to githubkit.
PyGithub's failure cannot honestly be overridden (it is LGPL).

### E4. Python matrix

`pyproject.toml`: `requires-python = ">=3.11"`; classifiers list **3.11, 3.12, 3.13**; `.python-version`
is 3.12. The only workflows are `release.yml` and `promote-stable.yml`; neither defines an
interpreter matrix. githubkit declares `requires-python >=3.10,<4.0` and **declares no
interpreter classifiers at all** (not 3.14 specifically), so "does not declare 3.14" is accurate
but unremarkable.

Observed: install plus the full prototype suite on CPython 3.11.15, 3.12.13, 3.13.5 and **3.14.6**,
in both environments (latest-resolvable, and project `uv.lock` pins). `uv pip check` was clean on
the latest-resolvable 3.11 and 3.14 environments:

```text
py3.11 (project pins): 27 passed in 0.86s
py3.12 (project pins): 27 passed in 0.80s
py3.13 (project pins): 27 passed in 0.82s
py3.14 (project pins): 27 passed in 0.72s
```

On 3.14 (latest-resolvable environment) I observed no install error, no test failure and no
warning under `-W default` in the prototype's own code path. That is evidence of working, not a
support promise; 3.14 is also outside the repo's declared classifiers today. (Pins used in the
project-pins environment: `httpx 0.28.1`, `pydantic 2.13.4`, `anyio 4.14.1`,
`typing-extensions 4.15.0`.) The same suite in an environment with **no** githubkit (`httpx`
only) yields `11 passed, 9 skipped`, i.e. the githubkit tests skip cleanly and the plain-httpx
candidate stands alone.

### E5. Prototype: issues (including closed), comments, Link pagination, `since` checkpoint

`docs/spikes/artifacts/bh-a1svm/` (all offline; the fake rejects any non-`GET`):

- `fake_github.py` — `httpx.MockTransport` handler reproducing GitHub's behaviour: `state` defaults
  to `open`; `since` is an inclusive `updated_at >= since`; `per_page`/`page` with a `Link:
  rel="next"` header; the issues endpoint also lists pull requests; deleted comments vanish; weak
  `ETag` plus `If-None-Match` to `304`; scripted failures; a `FakeClock` whose `sleep` records and
  never blocks.
- `sync_port.py` — the neutral `GitHubTransport` protocol (`list_issues`, `list_comments`), a
  `Mirror`, an incremental `pull` that advances a checkpoint to the max `updated_at` seen, and
  `reconcile_comment_ids` (the full-id-set pass).
- `transport_githubkit.py` / `transport_httpx.py` — the two implementations.

Verbose run (Python 3.12, project pins):

```text
test_githubkit_controls.py::test_githubkit_auto_retry_false_disables_retry PASSED
test_githubkit_controls.py::test_githubkit_custom_decision_function_is_injected_and_pure PASSED
test_githubkit_controls.py::test_githubkit_default_decision_functions_are_pure_and_importable PASSED
test_githubkit_controls.py::test_githubkit_primary_limit_reset_in_the_past_sleeps_sixty_by_default PASSED
test_githubkit_controls.py::test_githubkit_async_retry_sleeps_through_anyio_only PASSED
test_githubkit_controls.py::test_githubkit_http_cache_off_means_plain_httpx_client_and_no_conditional_headers PASSED
test_githubkit_controls.py::test_githubkit_http_cache_on_revalidates_with_etag PASSED
test_githubkit_controls.py::test_githubkit_base_url_supports_ghes PASSED
test_transport_spike.py::test_fixture_fidelity_default_state_hides_closed PASSED
test_transport_spike.py::test_lists_closed_issues_and_follows_link_header[httpx] PASSED
test_transport_spike.py::test_lists_closed_issues_and_follows_link_header[githubkit] PASSED
test_transport_spike.py::test_lists_comments_paginated[httpx] PASSED
test_transport_spike.py::test_lists_comments_paginated[githubkit] PASSED
test_transport_spike.py::test_since_checkpoint_is_incremental_and_idempotent[httpx] PASSED
test_transport_spike.py::test_since_checkpoint_is_incremental_and_idempotent[githubkit] PASSED
test_transport_spike.py::test_deletion_is_invisible_to_since_but_found_by_full_id_pass[httpx] PASSED
test_transport_spike.py::test_deletion_is_invisible_to_since_but_found_by_full_id_pass[githubkit] PASSED
test_transport_spike.py::test_server_error_retried_with_backoff_on_the_mock_clock[httpx] PASSED
test_transport_spike.py::test_server_error_retried_with_backoff_on_the_mock_clock[githubkit] PASSED
test_transport_spike.py::test_secondary_rate_limit_honours_retry_after[httpx] PASSED
test_transport_spike.py::test_secondary_rate_limit_honours_retry_after[githubkit] PASSED
test_transport_spike.py::test_client_error_is_not_retried[httpx] PASSED
test_transport_spike.py::test_client_error_is_not_retried[githubkit] PASSED
test_transport_spike.py::test_server_error_gives_up_after_the_retry_budget[httpx] PASSED
test_transport_spike.py::test_server_error_gives_up_after_the_retry_budget[githubkit] PASSED
test_transport_spike.py::test_httpx_primary_rate_limit_uses_the_injected_clock PASSED
test_transport_spike.py::test_httpx_etag_cache_is_hand_written_but_works PASSED
============================== 27 passed in 0.81s ==============================
```

What the passing tests establish, identically for both transports unless noted:

- All five issues come back, **closed ones and the PR included** (`state=all`); the fixture's
  default (`state` omitted) returns only the two open issues, so `state=all` is load-bearing. The
  adapter must drop items with a `pull_request` key itself.
- Three pages for five rows at `per_page=2`; pages 2 and 3 are reached by following the server's
  `rel="next"` URL, never by computing page numbers. **Live corroboration:** the real
  `Link: rel="next"` URL for `octocat/Hello-World` carries an `after=<cursor>` token and a
  `/repositories/<id>/issues` path, so verbatim `next` following is mandatory, not stylistic.
  githubkit's `rest.paginate` does this (`NEXT_LINK_PATTERN`).
- The checkpoint is the max `updated_at` seen. A second pull sends `since=<checkpoint>` to both
  endpoints, picks up a reopened issue, a new closed issue and a new comment, and the mirror has no
  duplicates. Because `since` is inclusive, the boundary item is fetched again; upserts are
  idempotent.
- **Deletion:** a deleted comment never appears in a `since` pull (the mirror keeps it);
  `reconcile_comment_ids` — a full pass with no `since` — finds it. The transport does not change
  this: it is a GitHub property and the adapter needs the full-id-set reconcile whichever
  transport is chosen. What the transport changes is the *cost* of that pass (below).
- Wire details observed for githubkit: it sends `Authorization: token <...>`, `Accept:
  application/vnd.github+json`, `X-GitHub-Api-Version: 2026-03-10`, and serializes `since` as
  `str(datetime)` (`2026-01-20 00:00:00+00:00`, i.e. a space, not `T`). **Live check:** GitHub
  answered `200` to that form and to `2020-01-01T00:00:00Z`, and `422` to garbage, so the form is
  accepted.
- Live corroboration of the typed path: unauthenticated `list_for_repo(state="all")` on
  `octocat/Hello-World` parsed 3 real issues into githubkit's `Issue` model (PRs present, closed
  state present) and `list_comments_for_repo` parsed real `IssueComment` rows; the project's
  fixtures are hand-built, so this is the only evidence that real payloads validate.

Two defects in **my own plain-httpx prototype** were caught only by the tests, and are the honest
cost of "hand-written" (githubkit, via hishel, did not have either):

1. `httpx.URL(url, params=None)` **silently drops the query string** of an absolute `next` URL;
   it broke the per-page ETag cache key.
2. A `304` has no body and (as modelled by the fake) no `Link`, so a hand-written cache that does
   not also store `Link` silently stops paginating after page 1 and loses issues.

### E6. Retry and HTTP cache: disable-able, injectable, testable without sleeping

Source read from `githubkit/core.py`, `retry.py`, `config.py`; behaviour exercised in
`test_githubkit_controls.py` and the `[githubkit]` rows above. The autouse fixture in `conftest.py`
makes `time.sleep` and `anyio.sleep` **raise**, so a passing test proves no real sleep happened.

**Retry.**

- `auto_retry: bool | RetryDecisionFunc` on the client. `False` disables it
  (`test_githubkit_auto_retry_false_disables_retry`: one request, a `RequestFailed`, zero sleeps).
- The decision is a **pure function** `(exc, retry_count) -> RetryOption(do_retry, retry_after)`.
  A custom one is injected and records its calls
  (`..._custom_decision_function_is_injected_and_pure`: decisions seen were `(RequestFailed, 503,
  0)` and `(..., 503, 1)`; sleeps `[0.5, 0.5]`). The stock `RETRY_DEFAULT` (`RETRY_RATE_LIMIT`
  once, `RETRY_SERVER_ERROR` three times, `(n+1)**2` seconds) can be unit-tested with **no client,
  transport or sleep** at all.
- The *sleep after a positive decision* is **not** injectable: `core.py` calls `time.sleep(...)`
  (sync) and `anyio.sleep(...)` (async) directly. The tests route it to `FakeClock.sleep` by
  replacing the single module attribute `githubkit.core.time` (`patch_sleep`), which affects
  nothing else, and the async path by patching `anyio.sleep`. With that, the default policy is
  asserted exactly: two 502s give sleeps `[1.0, 4.0]`; a `Retry-After: 7` 403 gives `[7.0]`;
  a 404 is never retried; four 500s give up after `[1.0, 4.0, 9.0]` and four requests. The plain
  httpx candidate takes `sleep` and `now` as constructor arguments and asserts the same numbers.
- **Observed defect, pinned by a test** (`..._reset_in_the_past_sleeps_sixty_by_default`):
  primary-rate-limit `retry_after` is computed against the **real** `datetime.now` (not
  injectable), and `core.py` evaluates `retry_after.total_seconds() if retry_after else 60`, so a
  `timedelta(0)` (reset already passed) is falsy and sleeps a full minute. The plain-httpx
  candidate asserts `x-ratelimit-reset` against an injected epoch clock instead.
  Mitigation for the adopter: wrap the default in an adapter-owned decision function that clamps
  the wait (and caps total wait), which the injection point supports.

**HTTP cache.**

- `http_cache: bool` (library default `True`) plus `cache_strategy`. With `False`,
  `_create_sync_client()` returns a plain `httpx.Client` — **not** hishel's `SyncCacheClient` —
  and two identical reads send no `If-None-Match`
  (`..._http_cache_off_means_plain_httpx_client_and_no_conditional_headers`).
- With `True`, a repeat read is conditional (`If-None-Match: W/"..."`), the 304 is answered from
  the cache, and the parsed rows are identical
  (`..._http_cache_on_revalidates_with_etag`). The cache works through `MockTransport`, so it is
  testable offline. The client always sends `Cache-Control: no-cache`, i.e. it revalidates rather
  than serving stale data.
- Live: GitHub serves **weak** ETags (`W/"..."`); the fake does too, and hishel handles them.
- Effect on the deletion reconcile: a conditional full-id pass can be answered with `304`s,
  which GitHub documents as not counting against the primary rate limit (documented rule, not
  re-verified here); with the cache off, the pass costs one request per 100 comments every cycle.
  That makes the cache a *performance opt-in*, not something correctness depends on.

**Transport injection.** `transport=` / `async_transport=` accept any `httpx` transport; all 27
tests use `httpx.MockTransport`. `base_url=` handled a GHES-style prefix correctly
(`/api/v3/repos/...`), so enterprise hosts are not a separate path.

### E7. The other candidates, assessed (not prototyped)

- **gidgethub 6.0.0** is sans-I/O and **async-only** (`GitHubAPI` declares abstract `_request` and
  `sleep` coroutines; `sleep` is genuinely injectable, which is its testing strength). It returns
  untyped dicts. Its `httpx` helper requires the **`httpx2`** distribution (a different, newer HTTP
  client, `httpx2` 2.16.0), not the project's `httpx` 0.28 — so using it either adds a second HTTP
  client or means writing and owning our own `_request`. This repo's existing call sites
  (`hq_release_catalog.py`'s `httpx.Client`, the `gh` subprocesses) are synchronous. Viable in
  spirit, but it buys less than plain httpx here.
- **`gh api`** is a host binary: a subprocess per call, untyped JSON, no `MockTransport`
  (tests need a recorded-subprocess fake), `--paginate` hides the `Link` mechanics but not the
  need to reconcile. It stays what it already is: a credential source (`gh auth token` in
  `ambient_token`) and an optional alternative.
- **Plain httpx** costs no dependency and is fully clock-injectable, but the prototype shows the
  price: `Link` parsing, a retry policy, rate-limit arithmetic and an ETag cache are ours to
  write and keep correct (two bugs above), and there are no typed models, no GHES handling, no
  future GraphQL or write-endpoint surface. Roughly 130 lines for two read endpoints; it grows
  with every endpoint the later write gates add.

### E8. Fit with the one-way dependency rule

- `scripts/check_import_boundaries.py` bans `httpx` (and `fastmcp`, `typer`, `uvicorn`, ...) from
  the `domain` and `application` layers via `_FORBIDDEN_EXTERNAL_PREFIXES`. `githubkit` is **not**
  in that list today, so the rule does not yet protect against it; the implementation molecule
  should add it. A transport that lives in the GitHub adapter module and exposes only
  `GitHubTransport`-shaped neutral records (`IssueRec`, `CommentRec` in the prototype) keeps
  githubkit's pydantic models from crossing the port — the prototype's two implementations are
  swappable behind `sync_port.py` with no change to `pull` or `reconcile_comment_ids`.
- `scripts/check_package_imports.py` governs `beadhive`-internal imports of plugin and library
  packages; a third-party dependency belongs to the package that declares it. If the GitHub adapter
  becomes a plugin package (sibling spike), `githubkit` is declared in *that* package's
  `pyproject.toml`, not the root's, and root stays free of it.
- Credentials stay ambient: `build_client(token, ...)` takes the string produced by the existing
  `TokenSource` / `ambient_token`; nothing here stores or logs a token.

### E9. Scoring against the seven `bh:modularize` dimensions (transport boundary only)

0 = poor, 1 = mixed, 2 = strong; judgement, not measurement.

| Dimension | githubkit | plain httpx | gidgethub | `gh api` |
|---|---|---|---|---|
| Cohesion | 2 | 2 | 2 | 1 |
| Coupling across the boundary | 1 (typed models must be mapped out) | 2 | 1 | 2 |
| Side-effect ownership | 2 | 2 | 1 (we write `_request`) | 1 (subprocess, host state) |
| Port narrowness | 2 | 2 | 2 | 2 |
| Replaceable implementations | 2 | 2 | 1 | 1 |
| Dependency direction | 2 | 2 | 2 | 2 |
| Test-closure reduction | 2 (MockTransport) | 2 | 2 | 0 (needs subprocess fakes) |
| **Total** | **13** | **14** | **11** | **9** |

No candidate scores 0 on port narrowness or dependency direction (the hard stops). Plain httpx
edges githubkit on pure structure; the difference is what each leaves *us* to build.

## Verdict — **GO** (githubkit), with two named conditions

**GO for githubkit 0.16.1** as the GitHub adapter's transport. No constraint failed outright:
MIT, no copyleft, all four interpreters in and beyond the repo's matrix install and pass,
retry is disable-able and its decision injectable, the cache is a boolean off-switch and works over
`MockTransport`, and nothing already pinned moves.

It is **conditional**, because the evidence also shows two things that are not free:

1. **`just license-check` does not pass as shipped.** It passes once `hishel` (a transitive
   dependency; BSD-3-Clause verified from the wheel's LICENSE file) has an `osv-scanner.toml`
   override, the repo's established mechanism for exactly this metadata gap. The adopting bead
   must land that override *in the same change* as the dependency.
2. **Retry timing is testable but not by injection alone**: the sleep is a module attribute you
   patch, and the primary-rate-limit path reads the real wall clock and can wait 60 s on a zero
   delay. The adopting bead must supply its own decision function and its own sleep seam rather
   than relying on the defaults.

If the operator would rather not carry a ~33 MB install and a hishel/msgpack/anysqlite cache stack
for two read endpoints, plain `httpx` is the credible, already-prototyped alternative — see
Recommendation.

## Recommendation

**One transport: githubkit, confined to the GitHub adapter module.** `gh` stays an *optional
fallback*, not a required one.

1. **Adopt githubkit 0.16.1** as a dependency of the GitHub adapter only, pinned by `uv.lock`
   (`githubkit-schemas` has no upper bound). Import it **lazily inside the adapter**, never at
   `bh` start-up (about a quarter second of import plus about 130 ms of first-call model load,
   measured above).
2. **Land the `hishel` license override in the same change** (E3 snippet), with the existing
   re-verify-at-expiry discipline. Re-run `just license-check` in the PR; it must go green.
3. **Keep the port neutral.** The adapter maps githubkit models to the port's own records
   (`IssueRec` / `CommentRec` here) so no githubkit type reaches the domain/application layers.
   Add `githubkit` to `_FORBIDDEN_EXTERNAL_PREFIXES` in `scripts/check_import_boundaries.py`.
4. **Make every default configurable and every optional feature off** (operator direction):
   `http_cache` **off** by default, opt-in per hive for the cheap reconcile pass; retry policy
   (max retries, max single wait, max total wait) configurable, defaulting to an
   adapter-owned wrapper that clamps githubkit's zero-delay-sleeps-60 case; `per_page` and
   `base_url` (GHES) configurable. Reads only; each write remains its own operator-approved gate.
5. **Tests**: the adapter takes an injected `httpx.BaseTransport` (the `hq_release_catalog.py`
   pattern) and a `sleep` seam; tests assert retry decisions on a `FakeClock` and make real sleep
   raise, exactly as `conftest.py` does here. Promote `fake_github.py`'s behaviours (default
   `state=open`, inclusive `since`, `Link` `next`, PRs in the issues list, vanishing deletions,
   weak ETag/304) into the real test fixture.
6. **Use `since` checkpoints for the incremental pull and a periodic full-id-set reconcile for
   deleted comments** (E5). The transport does not remove that need.
7. **`gh` stays optional.** It remains the credential source of last resort (`gh auth token`,
   already `ambient_token`) and may later be offered as an opt-in alternative implementation
   behind the same port for hosts that prefer it; the first implementation molecule need not
   build it. Hard-requiring the `gh` binary would contradict "host tools are optional adapters".
8. **Fallback trigger.** If the implementation molecule's review finds the dependency weight or
   the retry seam unacceptable, switch to the plain `httpx` implementation already in
   `docs/spikes/artifacts/bh-a1svm/transport_httpx.py` (the port makes this a drop-in); the two
   defects recorded in E5 are the tests to carry over. That is the NO-GO path for githubkit, and
   it is cheaper than it sounds precisely because of step 3.

**Not verified here:** GitHub's `304`-does-not-count claim (documented rule, not tested); that
the real `Issue` models validate every possible live payload (only a handful of public rows were
parsed); any behaviour against authenticated, SSO-restricted or GHES hosts; Python 3.15.
