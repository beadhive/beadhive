# Spike `bh-mqvdq` — Can the GitHub adapter resolve a local credential, know who it is, judge whether it may write, and recognize its own comments, without ever holding a token?

**Bead:** `bh-mqvdq` · **Seat:** `dev/identity` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-x6f9l` (adopt the bead tracker adapter port and the GitHub adapter transport); also `bh-gj1hb` (port contract). Parent epic `bh-ire9a`.

## Question

Four sub-questions, one GO/NO-GO:

1. **Credentials.** In what order does the adapter find a credential, and can that be done with no token written to config, logs or output?
2. **Actor.** How does the adapter learn which GitHub principal it acts as, per credential kind (classic PAT, fine-grained PAT, `gh auth login`, and later a GitHub App installation)?
3. **Write capability.** Is there a read-only probe that decides whether a write is possible, and what does the adapter report when it is not?
4. **Own comments.** How does the adapter recognize comments it posted, robustly, when a human may use the same token?

Not asked: transport choice (`bh-a1svm`), the port's method set (`bh-gj1hb`), config keys (that molecule's config spike), or any write. The adapter is off by default and every GitHub write is its own operator-approved gate; this spike only decides what the adapter can know *before* that gate and what it says *after* a refusal.

## Method

- **Read the prior art** and cite file:line (Evidence 1-4): `src/beadhive/run.py`, `github_projection.py`, `config_services.py`, `registry.py`, `deps.py`, `git.py`, `dolt_remote.py`, `scripts/check_package_imports.py`, and `tests/test_child_env.py`, `tests/test_github_projection.py`.
- **Live read-only probes** with the operator's ambient `gh` login (a classic OAuth token minted by `gh auth login`): only `GET` requests through `gh api` (no `-X`, `-f`, `-F`, no `graphql`). Output was filtered to an allowlist of response headers and non-secret JSON fields; the token value was never printed or recorded. One probe deliberately used a fake all-zero `ghp_` token to capture the real `401` shape. Reproduced by `artifacts/bh-mqvdq/README.md` (script and recorded output).
- **Offline fixtures.** A prototype (`artifacts/bh-mqvdq/identity_probe.py`, never imported by `src/`) and 35 tests (`test_identity_probe.py`) drive `httpx.MockTransport`: no network, no sleeps, no clocks. Run in a throwaway venv outside the project (`uv venv` + `uv pip install httpx pytest`), output captured in `artifacts/bh-mqvdq/README.md`. The fixtures encode GitHub's *documented* behaviour for token kinds I could not mint here (fine-grained PAT, App installation); they are labelled **documented, not observed** wherever that applies.
- **Mutation check** of the scrub test: with the pattern scrub removed, 2 of 35 tests fail, so the tests do guard the property.
- **No writes.** No `POST`/`PATCH`/`PUT`/`DELETE` was issued. GraphQL (`viewerPermission`) is a `POST` transport even for a query, so it was *not* called live (see Evidence 6 and the recommendation to use REST `GET` instead).

## Evidence

### Credentials: where they come from and how they stay out of sight

1. **bh's existing chain is `GITHUB_TOKEN`, then `GH_TOKEN` (mirrored), then a fresh `gh auth token`; nothing is persisted.** `src/beadhive/run.py:127-170` (`_fill_github_token`): operator-set `GITHUB_TOKEN` wins (`:142`), then each `gh.auth.env_vars` entry is mirrored (`:143-147`), then only if `deps.satisfied(gh)` it runs `gh auth token` with a 15 s bound (`:106`, `:159`). The value lives "in one dict and one child process" (docstring `:131-135`); every failure branch logs a *state* (`github_token_underivable`, `:150-156`, `:162-168`) and never stdout/stderr, "on the success path it IS the token" (`:162`). Derivation is opt-in per call (`child_env(github_token=False)` default, `:174-`).
2. **The projection already injects the credential as a callable.** `github_projection.py:51` `TokenSource = Callable[[Mapping[str, str]], "str | None"]`; `ambient_token` (`:124-134`) delegates to the one launcher; `project(..., token_source=...)` (`:218`, `:230`) lets tests inject. The returned type is a bare `str`, so *provenance* (which source supplied it) is lost, which matters for the refused-write diagnostic below.
3. **Two readers disagree on order, and I confirmed it live.** `deps.py:321` declares `env_vars=("GH_TOKEN", "GITHUB_TOKEN")` (gh's order); bh's own derivation checks `GITHUB_TOKEN` first (Evidence 1). With `GH_TOKEN`=fake and `GITHUB_TOKEN`=real, `gh api user` fails with `401 Bad credentials`; with `GITHUB_TOKEN`=fake and `GH_TOKEN`=real it succeeds. So **`gh` prefers `GH_TOKEN`**. If both are set to different tokens, a bh-resolved token and a `gh`-resolved token are different identities. `config_services.gh_login` (`:118-137`, runs `gh api user`) and `registry.has_push_access` (`:500-521`, runs `gh repo view`) are two further `gh`-side readers that never see bh's resolution.
4. **Plugins cannot import the resolver.** A plugin package may import root only through `beadhive.kernel.*.contracts` / `beadhive.modules.*.contracts` / `beadhive.testing` (`scripts/check_package_imports.py:5-10`), so `beadhive.run` is out of reach: credentials must arrive through an **injected port** from the composition root.
5. **Scrubbing already exists in two forms; none is a general mechanism.** (a) `github_projection._scrub` (`:149-153`): exact-value replace with `_REDACTED = "***"` (`:48`), applied to bd stdout and to the error tail (`:156-162`, `:249`, `:252`), tested at `tests/test_github_projection.py:248` ("credential scrubbed", asserts the token is absent and `***` present). (b) `dolt_remote.redact_url` (`dolt_remote.py:259-`): masks userinfo and query values: "the one rendering of a remote URL for output, errors and logs". Neither catches a token bh did not itself resolve (for example one echoed in an upstream error body). The hygiene *tests* that bar leaks are the planted-token tests in `tests/test_child_env.py:263-` (token reaches the child env "and nowhere else", asserted against captured stdout/stderr) and `tests/test_credentials.py`.

### Actor and permission: what the API actually returns (live, read-only)

6. **Existing permission probe is GraphQL and fails closed.** `registry.py:497-521`: `gh repo view <o>/<r> --json viewerPermission`; write is `ADMIN|WRITE|MAINTAIN` (`:497`); any error, absent `gh`, non-github provider or unparseable JSON returns `False`. Fail-closed is right for "may I push the registry"; it is too coarse for the adapter (it collapses "no" and "could not tell").
7. **`GET /user` yields the stable key.** Response has `id` (numeric, immutable) and `type` (`User`). Observed for the ambient login: `{"id": <int>, "type": "User"}`. `login` is mutable (renames) and must not be a key.
8. **Classic/OAuth tokens expose their scopes on every response.** Observed on `/user`, `/repos/...`, `/rate_limit` and a `404`: `X-OAuth-Scopes: admin:public_key, admin:ssh_signing_key, gist, read:org, repo`; `X-Accepted-OAuth-Scopes: repo` on repo endpoints (empty on `/user`). This is the only read-only evidence of a *token-level* grant, and it exists only for classic PATs and OAuth tokens (`gh auth login` mints one, `gho_`). No `X-Accepted-GitHub-Permissions` header appeared, consistent with that header being specific to fine-grained/App tokens (**documented, not observed**).
9. **`GET /repos/{o}/{r}` carries the viewer's role.** Observed `permissions` objects: own repo `{"admin":true,"maintain":true,"push":true,"triage":true,"pull":true}`; a foreign public repo `{"admin":false,"maintain":false,"push":false,"triage":false,"pull":true}`. An **unauthenticated** call to the same foreign repo returned `200` with **no `permissions` key** and no `X-OAuth-Scopes` header (rate limit 60/h): absence of `permissions` therefore means "no credential was sent". `GET /repos/{o}/{r}/collaborators/{me}/permission` also answers for oneself (`"permission":"admin"`), but adds nothing over `permissions` and needs the login.
10. **Bad credential shape (real).** A fake `ghp_000...` token gives `HTTP 401` with body `{"message":"Bad credentials"}`. A nonexistent repo gives `404 Not Found` (indistinguishable from a private repo the token cannot see; this is GitHub's documented policy of hiding what you may not see).
11. **A `[bot]` actor is a normal public user.** `GET /users/github-actions%5Bbot%5D` returned `{"id": 41898282, "type": "Bot"}` (login `github-actions[bot]`) with no special credential. Hence an adapter configured with an App slug can compute its own `(id, type)` without calling `/user`.

### Comments: what identifies the author

12. **A comment carries `user.id`, `user.type` and `performed_via_github_app`.** Observed on public issue comments: human comments have `performed_via_github_app: null`, `user.type: "User"`; App comments have `user.type: "Bot"`, `user.login` ending `[bot]`, and `performed_via_github_app` = `{id, slug, ...}` (observed `bedevere-app` id 388350, `miss-islington-app` id 409059, `github-actions` id 15368, `kubernetes-prow` id 992787, each with a stable numeric bot `user.id`). Also present: `author_association`, which describes the *repo role* of the author and cannot identify a specific author.
13. **Comment ids are returned by the (future, gated) write.** A `POST` returns the created comment including its `id`, so an outbox can be filled at the moment of the operator-approved write, with no extra call.

### Prototype and offline tests (run, output captured)

14. `artifacts/bh-mqvdq/identity_probe.py` + `test_identity_probe.py`: **35 passed in 0.54 s** (`artifacts/bh-mqvdq/README.md`). Coverage: resolution order incl. blank-as-unset and lazy `gh` (1 test); planted-token bar across `repr`/`str`/f-string, an upstream error body echoing two tokens, a captured log record and a JSON ledger file, including a structural "no token-shaped text anywhere" assertion (1 test + 1 scrub unit test); prefix-vs-header kind detection; actor equality ignoring a rename; `/user` for a user, `401`, and an installation token's `403 Resource not accessible by integration`; App bot actor via `/users/<slug>[bot]`; tri-state capability for classic scopes, `public_repo`-vs-private, empty scopes, role-vs-operation, fine-grained `UNKNOWN`, archived/disabled/issues-off/`401`/`404`/no-credential `NO`, unexpected `502` `UNKNOWN`; ten refused-write diagnostics; six own-comment tests (marker, shared-token human, foreign actor/hive, quoted marker, outbox after human edit, App, login rename). Every capability test also asserts that only `GET` was sent.

## Verdict — **GO**

All four sub-questions have a workable, offline-testable answer with no token ever stored:

- Credentials resolve through an **injected `TokenSource` port** (the env chain already exists and is already hygiene-tested); the adapter never sees `beadhive.run`.
- The actor is discoverable by `GET /user` (user-kind tokens) or the public `<slug>[bot]` user (App), keyed by `(id, type)`.
- A **write cannot be proven read-only** (see below) but a **tri-state** read-only signal is both honest and useful: `NO` is definitive for archived, no-scope, below-triage and invisible repos; `YES` is only "not refuted"; fine-grained and installation tokens are irreducibly `UNKNOWN`. That is acceptable precisely because writes are off by default and each is an operator-approved gate whose first refusal produces an exact diagnostic.
- Own comments are recognized by **outbox ids first, then (known actor `(id,type)` AND footer marker)**, with `performed_via_github_app` sufficient for a dedicated App.

The one hard blocker that would have made this NO-GO (needing a *read* that proves write permission) was looked for and does not exist; the design simply does not depend on it.

## Recommendation

For the implementation molecule and for the `bh-x6f9l` decision:

### 1. Credential resolution (port, order, scrubbing)

- **Port.** The adapter module takes a `TokenSource` callable injected by the composition root, never importing `beadhive.run` (Evidence 4). Extend the shape to return a small `ResolvedToken(value, source)` where `value` is `repr=False` and `str()` prints `<token from env:GITHUB_TOKEN>`; the existing `str | None` loses the provenance that the diagnostic needs (prototype: `ResolvedToken`, `resolve_token`).
- **Order** (unchanged from bh, Evidence 1): `GITHUB_TOKEN`, then `GH_TOKEN`, then `gh auth token` (only when `gh` is installed and authenticated; blank counts as unset). Blank-as-unset and "never override the operator" are already pinned by `tests/test_child_env.py`.
- **Resolve once, use one identity.** Because `gh` itself prefers `GH_TOKEN` (Evidence 3), any adapter path that shells out to `gh` must pass the *resolved* token explicitly as `GH_TOKEN` in that child's env, and the actor probe must run with *that same* token. Never call `config_services.gh_login` / `registry.has_push_access` to learn the adapter's actor or capability. If both vars are set and differ, surface a one-line warning (state only) that they disagree.
- **No storage, by construction.** No config key holds a token; the adapter config carries at most a *source name* (`env`, `gh`) and the repo identity, never a value. The token is held in memory for the life of one operation. Nothing about it enters the adapter's ledger, outbox or `--json` envelope; the provenance string (`env:GH_TOKEN`) may.
- **Scrubbing, named.** Promote one function, `scrub(text, secrets)`, as *the* rendering for upstream text (the prototype's `scrub`): exact-value replace (the existing `github_projection._scrub` rule, `***`) **plus** a token-shape regex (`github_pat_…`, `gh[pousr]_…`) as defence in depth, longest secret first. Every exception message, log field and diagnostic that includes upstream bytes goes through it; diagnostics are otherwise built from *states*, as `run._fill_github_token` already does. This is the GitHub twin of `dolt_remote.redact_url`.
- **How a test asserts it** (prototype `test_token_never_appears_in_repr_str_logs_errors_or_serialised_output`): plant a shaped fake token, run resolution + probe + a refused write whose upstream body *echoes* it (and a second unknown token), then assert it is absent from `repr`/`str`/f-string, from `caplog`, from every file written to `tmp_path`, and that `TOKEN_PATTERN.search(...)` finds nothing at all in that haystack. Pair it with the existing `tests/test_github_projection.py:248` style assertion for the exact-value path. A mutation check (remove the pattern scrub, expect failure) proves the test is not vacuous.

### 2. Actor discovery, per credential kind

| Credential | How recognized | Actor source | Notes |
|---|---|---|---|
| Classic PAT (`ghp_`, or legacy 40-hex) | `X-OAuth-Scopes` header present | `GET /user` -> `(id, "User")` | scopes readable on any response |
| `gh auth login` OAuth (`gho_`) | same header; prefix `gho_` | `GET /user` | observed live; scopes include `repo` |
| Fine-grained PAT (`github_pat_`) | **no** `X-OAuth-Scopes`; no `X-Accepted-GitHub-Permissions` needed | `GET /user` works (documented) | per-repo grants unreadable (below) |
| App installation (`ghs_`), later | no scope header; **`/user` -> 403 "Resource not accessible by integration"** | public `GET /users/<slug>%5Bbot%5D` -> `(id, "Bot")`; installation endpoints (`GET /installation/repositories`) to list reachable repos; `GET /app` needs the App **JWT**, not the installation token | actor type is `Bot`; `performed_via_github_app.id` is available on its comments |

The prefix only *chooses a probe*; **response headers decide** (`kind_from_headers`). Key actors on `(id, type)`, never `login`; carry `login` for display only (test `test_actor_equality_ignores_a_renamed_login`). Treat the installation-token case as a later adapter module behind the same port: the port needs no change, only a different `discover_actor`.

### 3. The write-capability probe (be explicit about what it cannot do)

**Write permission cannot be PROVEN read-only.** No GET reveals a fine-grained token's `Issues: write` grant, an org policy that forbids the token, SSO authorization state for a given org, an interaction limit, or a locked thread. The only certain test is to perform the write, which the adapter must never do as a probe. So the adapter reports a **tri-state per operation class** (`comment`, `label`), computed from one `GET /repos/{o}/{r}` (REST `GET`, deliberately not GraphQL `viewerPermission`, so the invariant "the probe sends only GET" is checkable by a transport-level test, as the prototype does):

- **NO (definitive; the necessary condition is provably absent):** `401`; `404` (token cannot see the repo; private repos are hidden as 404); `archived`/`disabled`; `has_issues: false`; no `permissions` key (no credential); classic/OAuth token whose scopes lack `repo` (or `public_repo` for a public repo); for `label`/edit/assign, role below triage (`push|triage|admin` all false).
- **YES (means "not refuted", never "proven"):** classic/OAuth token with sufficient scope **and** an adequate role. Note that `comment` needs only an authenticated reader on a public repo with issues enabled; `label` needs triage. Document this as "likely".
- **UNKNOWN:** fine-grained and installation tokens (no scope header; `permissions` there reflects the identity's *role*, not the token's per-repo grant; whether it is capped by the token is **documented, not observed**), plus any unexpected status (`502`). The best read-only signal is the repo role and, for apps, `GET /installation/repositories` (reachability, not permission level).

**What the adapter reports when write is unavailable.** Reads proceed regardless. For writes: the capability result (state, one-line reason, what to change) is shown by the diagnostic surface (for example `bh hive ready -v`); writes are already disabled by config by default, so `NO`/`UNKNOWN` never *adds* a restriction, it only improves the message. `NO` makes the write gate refuse before asking the operator (cheap, actionable). `UNKNOWN` lets the operator approve; **the write is then the probe**, and a refusal is classified (below) rather than retried. Never cache `YES`/`UNKNOWN` as a fact across runs; cache a `NO` only for the process lifetime.

**The first refused write, exact diagnostics** (prototype `diagnose_refused_write`, one scrubbed line each, `source` is the token provenance, never the value):

| Response | Diagnostic and meaning |
|---|---|
| `401` | "bad credentials: the token from `<source>` is invalid, revoked or expired" |
| `403` + `X-GitHub-SSO` | "authorize this token for the organization (SAML SSO) and retry" |
| `403` "Resource not accessible by personal access token" / "...by integration" (+ `X-Accepted-GitHub-Permissions: issues=write` when present) | "token lacks the permission (endpoint needs `issues=write`)": fine-grained/App grant missing. Name the header value, it is the exact permission to add. |
| `403` + `Retry-After` / `X-RateLimit-Remaining: 0` | "rate limited (not a permission refusal)": back off; do **not** conclude writes are unavailable (the production retry must use an injected clock, never `sleep` in tests) |
| `403` "archived" / "locked" | repository archived (read-only) / conversation locked: state problems, not credential problems |
| `404` on a write | "invisible to this token (GitHub reports 'forbidden' as 'not found'); check repo access and scope" |
| `410` | issues are disabled on this repository |
| `422` | "payload rejected after authorization; NOT a capability failure": validation (body missing or too long, label already exists, ...); authorization had already passed, so record the write as *permitted* |

Treat these as the documented forms; only `401` and `404` were observed live here (Evidence 10), the rest are fixtures of documented behaviour and should be re-verified against recorded real responses when the first gated write is exercised in the implementation molecule.

### 4. Recognizing own comments (robust to a shared token)

Candidate signals and their tradeoffs:

| Signal | Strength | Weakness |
|---|---|---|
| `user.id` + `user.type` equals the resolved actor | **necessary, not sufficient** | A human using the same token (or the same account in the web UI) has the identical key. Mutable `login` must not be used. |
| `performed_via_github_app.id` + `user.type == "Bot"` | **sufficient for a dedicated GitHub App** (a human cannot post as the bot) | Null for PAT/OAuth (observed: null on human comments), so useless for the PAT/`gh` case. A user-to-server App token posts as the *human* with the app id set, hence also require `type == "Bot"`. |
| Footer marker `<!-- bh:v1 hive=<owner/repo> install=<uuid> -->` as the **last non-empty line** | Survives across machines/clones and re-reads; invisible in rendered Markdown | Forgeable by anyone who copies it; stripped if a human edits the comment; need to ignore it when quoted (`> <!-- ... -->` is not the last line). Not a security boundary (a human on the same token could equally post anything), a loop-avoidance/attribution aid. |
| Outbox of posted comment ids (from the `POST` response, written when the gated write succeeds) | **Strongest for "this exact comment"**; survives human edits and a token swap | Local to one install; lost on a fresh clone; useless for comments posted by another install of the same hive. |
| `author_association` | none | Describes the repo role, not the author. |
| Comparing body text/hash | weak | Breaks on any edit; no better than the marker. |

**Recommendation (layered, in this order; prototype `classify_comment`):**

1. comment id in the outbox -> **own** (`outbox`);
2. for a configured App: `performed_via_github_app.id == app_id` and the author is a known `Bot` actor -> **own** (`app`);
3. author `(id, type)` is in the **known-actor set** **and** the last non-empty line is a marker naming *this hive* -> **own** (`marker`);
4. author in the known-actor set but **no marker** -> **not own** (`same-actor-no-marker`: a human on the shared token; treat as external input);
5. any other author -> **not own**.

Concretely: (a) every comment the adapter posts is passed through `stamp()` to append the marker (`hive` = the registry's `owner/repo` identity, `install` = a random UUID generated once into local, non-secret state); (b) the known-actor set is the *current* actor plus previously used actors recorded in the outbox ledger, so a rotated token or a different PAT account does not orphan old comments; (c) the outbox is written by the same operator-approved write gate that already exists per write, since the `POST` response carries the id (Evidence 13); (d) because the user-id match is only necessary, a human typing under the same token is never silently classified as the bot; the cost is that a human who copies the marker *and* uses the shared token is misread as own, which is accepted (equal privilege, no escalation, and ignored comments are merely not re-processed). Prefer the REST `user.id` comparison over GraphQL `viewerDidAuthor` to keep the transport REST-`GET` only.

### 5. What the implementation molecule should contain

1. A `TokenSource` contract in the adapter's `contracts` module returning a provenance-carrying, `repr`-safe token; the composition root supplies the existing env/`gh auth token` chain.
2. `scrub()` plus the planted-token test described above, shared with the bd-projection path (retire the private `_scrub`).
3. `discover_actor` and `probe_capability` as pure functions over an injected HTTP client (REST `GET` only), returning `Actor(id,type)` and a tri-state `Capability`; a `diagnose_refused_write` mapping; all tested with `httpx.MockTransport`, no sleeps.
4. The marker, outbox and `classify_comment` rules for intake and the future write-back, with the loop-avoidance meaning documented.
5. An advisory warning when `GITHUB_TOKEN` and `GH_TOKEN` are both set and differ.
6. Defer: GitHub App installation support (needs JWT minting, which is a new secret kind: a private key), but keep it a drop-in `discover_actor` / `TokenSource` variant.

**One line:** GO. Resolve the token through an injected, repr-safe `TokenSource`; key the actor on `(id, type)`; report write capability as a GET-only tri-state (never claim proof) plus an exact first-refusal diagnostic; and recognize own comments by outbox id, then known-actor plus footer marker, with `performed_via_github_app` as the later App shortcut.
