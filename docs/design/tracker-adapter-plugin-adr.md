# ADR: The `tracker.adapter` plugin category and the GitHub plugin binding

- **Status:** Accepted (verdict **GO**), effective when `bh-h7nw7` merges
- **Date:** 2026-10-25
- **Decision owner:** Beadhive maintainers
- **Related work:** `bh-h7nw7` (this decision), epic `bh-mobxo` (the plugin-category design
  molecule, prerequisite 2 of the tracker loop), spikes `bh-rktrh` and `bh-6bkun`; the port
  decision `bh-x6f9l` under epic `bh-ire9a`; sibling design molecule `bh-wjvq9` (external-tracker
  intake, the first consumer); placeholder for the whole loop `bh-33d5a`
- **Evidence (both merged, both verdict GO):**
  - [`docs/spikes/bh-rktrh-tracker-plugin-category.md`](../spikes/bh-rktrh-tracker-plugin-category.md)
    (category, manifest, credentials seam, `plugins.tracker-github.*` keys, status and doctor;
    32 offline tests against the real kernel)
  - [`docs/spikes/bh-6bkun-tracker-binding.md`](../spikes/bh-6bkun-tracker-binding.md)
    (the per-store binding, validation rules R1 to R13, the second-tracker list S1 to S8;
    58 offline tests against the real config models)
- **Builds on:** [`tracker-adapter-port-adr.md`](tracker-adapter-port-adr.md) (the port, the
  GitHub transport, open item O1, checker gaps G1 to G4),
  [`plugin-kernel-v1-adr.md`](plugin-kernel-v1-adr.md),
  [`package-class-library-vs-plugin-adr.md`](package-class-library-vs-plugin-adr.md) and
  [`two-store-scope-adr.md`](two-store-scope-adr.md).
- **Amends:** the port ADR, in that it resolves its open items O1 and O2 (and leaves O3 open);
  and the `bh-rktrh` spike, in that it removes the key `plugins.tracker-github.repo` (the
  `bh-6bkun` amendment, adopted below). Nothing else.

## Decision summary

Paste-ready for the close reason of `bh-h7nw7`.

- **Verdict: GO.** Adopt the tracker-adapter plugin category and the GitHub plugin binding.
- **Capability:** `tracker.adapter`, API major **1** (equal to the port's `PORT_API_VERSION`).
  The port is `TrackerAdapter` (reader plus identity) from `beadhive.modules.tracker.contracts`,
  bound by `bind_application_port` with no kernel change. `CommentWriter` and `LabelWriter` stay
  optional and are found with `isinstance` on the bound object.
- **Plugin id:** `tracker-github` (package `beadhive-tracker-github`, extra `beadhive[github]`,
  namespace `plugins.tracker-github`).
- **Binding:** `managed_repos[].stores.public.adapter`, one optional string holding a plugin id.
  It requires `source`, is valid on the public store only, and is **fleet-owned intent**. The
  `plugins.tracker-github.*` subtree is **host-owned authority**. A binding never enables a
  plugin, a read or a write. An absent key is today's behaviour byte for byte.
- **O1 resolved:** `ResolvedToken`, `TokenSource` and `scrub()` live in
  `beadhive_plugins.credentials` (stdlib only, in the existing library package
  `packages/beadhive-plugins`). Verified against the repository; see "Resolutions".
- **Repository amendment (bh-6bkun prevails):** `plugins.tracker-github.repo` is **removed**.
  Owner and repository are derived per store from the hive identity triplet
  (`provider`/`org`/`repo`). The effective-enablement formula is per store (below).
- **Must be in the implementation replan:** (a) the rollout order: the schema ships and rotates
  to every host before any config uses `adapter`; (b) the existing input-echo bug in
  `store_target.configured_stores` (verified): set `hide_input_in_errors` on the stores models.
- This ADR files no bead and does not run `/bh:replan`; the implementation molecule is filed
  separately (see "Next steps").

## Context

The port ADR settled the port, the GitHub transport and the credential chain, and deliberately
left the plugin wiring, the config key names, the hive-level selection and the home of the shared
credential helpers to this molecule (its prerequisite 2). Two research-only spikes ran against
the repository's real kernel and real config models, sent no request to GitHub, and changed
nothing under `src/`, `packages/`, `pyproject.toml` or `uv.lock`:

- `bh-rktrh` asked whether a tracker adapter can be a plugin category on the existing kernel with
  no kernel change and no new top-level config key. Verdict **GO**.
- `bh-6bkun` asked how a hive says "this store is served by the GitHub plugin" without breaking
  any existing config, adding a secret, or enabling anything by itself. Verdict **GO**.

The fixed constraints are unchanged: everything off by default and configurable; reads and writes
separately enabled; every write its own operator-approved single-use grant; credentials ambient
and never stored; host tools (`gh`) optional; tests offline and never timing-dependent. This ADR
reads the two spikes together, settles the places they disagree, and records one decision the
implementation molecule builds on.

## Decision

**Overall verdict: GO.** The category fits the plugin kernel as it is: `discover_plugins`,
`bind_application_port`, `parse_kernel_config` and the manifest v1 schema need no change (all
exercised unchanged by `bh-rktrh`). A store is bound by one optional string key; the schema is
already closed and additive, so a new optional key with no default changes nothing for a document
that omits it (shown by `bh-6bkun` over eleven existing configs and all 48 combinations of the
existing public-store axes, using the repository's own `validate_config`).

Three things are not free and are owned by the implementation molecule (none changes the
verdict): the config schema does not yet accept `plugins`, `plugin_kernel` or `adapter`; the
credential value type and `scrub()` need their shared home; and the catalog test and the
package checker need small extensions.

### 1. The category

| Element | Decision |
|---|---|
| Capability id | `tracker.adapter`, API major **1**. Names behaviour, not a product (kernel ADR section 2) |
| Port | `TrackerAdapter` from `beadhive.modules.tracker.contracts`; `CommentWriter` and `LabelWriter` optional, discovered with `isinstance` |
| Binding | `bind_application_port(TRACKER_ADAPTER_PORT, discovery, (ProviderBinding(ProviderKey(owner, TRACKER_ADAPTER), adapter),))` in root composition (`bootstrap/tracker.py`, modelled on `bootstrap/impact.py`) |
| Owner selection | the kernel's, unchanged: `plugin_kernel.capability_owners["tracker.adapter@1"]` |
| Plugin id | `tracker-github` |
| Shape | Pants's shape (package plugin, lazy composition-root row, discovery-selected owner, diagnostics as data) plus the legacy integrations' off-by-default fold, with a Herdr-style typed port. No new kernel concept |

**One capability, not one per operation or per product.** A separate `tracker.writer` capability
could let a different plugin own writes than reads, which breaks the identity the grant is bound
to; per-product ids break "capability ids name behaviour". Read and write are separated by
configuration and by the shape of the bound object, never by capability ids.

**Declaration and discovery.** The manifest is the concrete one in `bh-rktrh` section 2 (valid
under the real parser and the published wire schema): it provides `tracker.adapter@1`; names
the credential sources as environment variables only (`GH_TOKEN`, `GITHUB_TOKEN`; a command such
as `gh auth token` cannot be a credential source); declares `gh` as an optional executable; has
empty `lifecycle.subscriptions` (status is pulled, not pushed); and carries a
`configuration.schema_artifact` URN for the plugin-config fragment. Discovery is the kernel's
and imports nothing. `bootstrap/tracker.py` folds `plugins.tracker-github.enabled` (default
`false`) into the kernel map exactly as the legacy compatibility facade does
(`enabled[plugin.name] = legacy_enabled and policy.enabled.get(plugin.name, True)`, because
discovery otherwise enables a built-in manifest by default), so the fold is
`own_enabled AND kernel.enabled.get(id, True)` and `plugin_kernel.enabled.tracker-github: false`
still wins. Disabled means no owner, no port and no network. Only for the selected owner does
root lazily import the package factory. **Installation never implies enablement and enablement
never installs** (kernel ADR section 7).

**Install.** `beadhive[github] = ["beadhive-tracker-github"]` with a workspace source beside
`beadhive-pants`; the root wheel never needs githubkit.

**Factory.** The package exposes one thin `build(settings, token_source, *, project, sleep)`
returning a `TrackerAdapter`, where `project` is the port's `ProjectRef("github", "owner/name")`
derived by root (this is the `bh-6bkun` amendment to the `bh-rktrh` signature, which read a
`repo` setting). It receives the already validated, already folded settings, imports no
`beadhive.kernel.*`, and returns an object whose writer methods exist only for write operations
whose flags are on.

**Ownership is process-wide** (one enabled owner per `capability@major`). This is accepted for
now (D5, S5); two different tracker plugins in one process need a kernel change and are out of
scope.

### 2. The binding

```yaml
# Fleet layer (managed_repos is fleet-owned): intent only. Nothing here enables anything.
managed_repos:
  - provider: github
    org: beadhive
    repo: beadhive              # owner/repository for the adapter come from these keys
    stores:
      public:
        source: github-issues   # unchanged: selects the bd projection (and the tracker kind)
        adapter: tracker-github # NEW, optional: the plugin id serving this store. Absent = today

# Host layer (plugins.tracker-github is host-owned): the operator's switches.
plugins:
  tracker-github:
    enabled: true               # default false; a binding never sets this
    read: {issues: {enabled: true}}
```

| Question | Owner | Where | Default |
|---|---|---|---|
| Which tracker kind does this store mirror | the store (fleet) | `stores.public.source` (existing) | unset |
| Which plugin serves it | the store (fleet) | `stores.public.adapter` (**new**) | unset = unbound |
| Which owner and repository | the hive identity | `provider`/`org`/`repo` of the entry, **derived** | n/a |
| Is the plugin on | the plugin (host) | `plugins.tracker-github.enabled` | `false` |
| Operations, `base_url`, credential source names, retries, cache | the plugin (host) | `plugins.tracker-github.*` | all off |
| Credentials | nowhere in config | ambient, injected token source | never stored |
| Write approval | nowhere in config | a runtime single-use grant per write | none exists |
| Which plugin owns the capability when several are enabled | the kernel (host) | `plugin_kernel.capability_owners` | none |

Rules of the shape:

- **`adapter` is a plugin id and nothing else**: lowercase letters, digits and hyphens, at most
  64 characters. It cannot hold a URL, repository, token or option, and a value that fails the
  shape is rejected **without being repeated** in the message.
- **Intent versus authority.** `managed_repos` is a `FLEET` prefix and `plugins.tracker-github` is
  host-owned (C2), so a fleet push can declare "this store is served by `tracker-github`" but can
  never turn the plugin, a read or a write on. A host without the package, or with the plugin
  off, validates the same document and ignores the binding.
- **`adapter` requires `source`** (so a bound store also names the tracker it mirrors), is valid
  on the **public store only**, and leaves the bd projection untouched: `bh hive remote sync` and
  `github_projection` keep reading `source` only and never consult the adapter. The projection
  and the port remain two independent readers (port ADR section 5).
- **No per-hive override of plugin flags** and **no store-level `operations:` list**: the binding
  set is the per-hive selection (an unbound hive can neither read nor write), write enablement
  is a host-wide ceiling, and a list of writes in fleet config would read like a pre-approval.
  Requested operations are a runtime request from the consumer.

### 3. The `plugins.tracker-github.*` keys

No new top-level key: everything lives under the namespace the kernel ADR assigns to the plugin.
The names are **ratified** (this closes port-ADR open item O2) with the `bh-rktrh` units made
explicit, and **`repo` is removed** (the `bh-6bkun` amendment).

| Key (under `plugins.tracker-github`) | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | strict bool | **false** | The plugin's own gate. Off means no owner, no port, no network |
| `base_url` | https URL | unset (public API) | GHES prefix; no userinfo, query or fragment |
| `credential.sources` | list of `env`, `gh` | `["env", "gh"]` | Names and precedence only; empty means anonymous |
| `read.issues.enabled` | strict bool | **false** | Enables `issues.read` |
| `read.comments.enabled` | strict bool | **false** | Enables `comments.read` |
| `read.include_closed` | strict bool | true | Scope within a read, not a feature switch |
| `read.page_size` | int 1..100 | unset | Listing page size |
| `http_cache.enabled` | strict bool | **false** | Conditional requests for the id-reconcile pass |
| `retry.max_retries` | int >= 0 | 3 | Part of `RetryBudget` |
| `retry.max_single_wait_seconds` | number > 0 | 60 | Part of `RetryBudget` |
| `retry.max_total_wait_seconds` | number >= single | 120 | Part of `RetryBudget` |
| `write.comment.enabled` | strict bool | **false** | Enables `comment.create` (necessary, never sufficient) |
| `write.labels.enabled` | strict bool | **false** | Enables `labels.change` (necessary, never sufficient) |
| `write.labels.owned_prefixes` | list of non-empty strings | `[]` | Labels Beadhive owns; empty means no label write is possible |

All switches are off; the only default-true value (`read.include_closed`) sits inside a switch
that is itself off. The schema is a pydantic `_Section` (`extra="forbid"` at every level), so
`repo`, and any token-, password-, command-, grant-, approve-, auto- or bypass-shaped key, is
rejected. Every boolean is **strict**: a hand-edited `"false"` is invalid rather than truthy
(the existing lax sections do not guarantee this).

**Effective enablement (replaces the `bh-rktrh` formula; per store).** Evaluated once in the
composition root and handed to the adapter as the resolved `enabled` set:

```text
issues.read     = plugin.enabled AND kernel-enabled AND the store binds this adapter
                  AND the identity resolves AND read.issues.enabled
comments.read   = plugin.enabled AND kernel-enabled AND the store binds this adapter
                  AND the identity resolves AND read.comments.enabled
comment.create  = plugin.enabled AND kernel-enabled AND the store binds this adapter
                  AND the identity resolves AND write.comment.enabled
labels.change   = plugin.enabled AND kernel-enabled AND the store binds this adapter
                  AND the identity resolves AND write.labels.enabled
                  AND write.labels.owned_prefixes is non-empty
```

The former term "`repo` set" is gone; it is replaced by "the store binds this adapter AND the
identity resolves". Reads and writes are independent (turning a write on enables no read). The
status state `unconfigured` becomes "no store binds this adapter".

**The write gate keeps its five layers** from `bh-rktrh`: the bound object structurally lacks a
disabled write's method; the adapter refuses a disabled operation, a mismatched or reused grant
or an unowned label before any request; each write is its own consumer-side operator-approved
gate minting one single-use `WriteGrant`; no key, environment variable or flag can supply or
pre-approve a grant; and durable single use is the consumer's outbox. The wiring's own duty is
the first, the fourth and "one instance": **one adapter per (hive, store scope) per process**,
cached by the root, never re-created per call (the grant set is in memory).

**Ownership.** `plugins.tracker-github` is **host-owned** (so a fleet push cannot enable a write
on every host, and the credential is host-local), and `plugin_kernel` is host-owned. The binding
`stores.public.adapter` is correctly fleet-owned. This ownership is load-bearing and is the
reason C2 must land with the schema.

### 4. Validation rules and where they run

The rules are those of `bh-6bkun` section 2, adopted with the exact messages recorded there
(tests assert them); only the shape is summarised here.

| # | Rule | Tier |
|---|---|---|
| R1 | Unknown adapter id (closed static table) | T1 config load, error |
| R2 | Malformed adapter value (value not echoed) | T1, error |
| R3 | Adapter without `source` | T1, error |
| R4 | Adapter on the internal store | T1, error |
| R5 | Adapter serves another source (unreachable while `source` has one member) | T1, error |
| R6 | Plugin disabled (absent flag, `false`, non-boolean, or `plugin_kernel.enabled` false) | T3 error when a consumer asks; T2 **info** |
| R7 | Write requested without enablement (fails before any approval prompt) | T3, error |
| R8 | Read requested without enablement | T3, error |
| R9 | Adapter package absent (names `beadhive[github]`) | T3 error; T2 info while off, error once on |
| R10 | Another plugin owns the capability | T3, error |
| R11 | Two enabled providers, no owner selected | T3, error |
| R12 | Identity does not resolve (provider is not `github`, or no owner/repository) | T3 error; T2 **warning** |
| R13 | Store binds nothing | T3, not an error: the consumer's "tracker unconfigured" |

| Tier | Runs in | May do |
|---|---|---|
| **T1 config load** | `validate_config` (`bh config validate`, the heartbeat's `config_valid`) and any load of the stores block | Pure checks on the document: no discovery, environment, network or plugin import. R1 to R5 |
| **T2 `hive ready` / `doctor`** | one row beside `_plugin_checks`, plus the `doctor` section | Offline, read-only, no network, never fails while the plugin is off. R6, R9, R12 as information; a ready row with the bound repo and enabled operations |
| **T3 composition root** | `bootstrap/tracker.py`, called by a consumer | Fail closed with a typed error; lazy import only after selection. R6 to R13 |

Order at the root: not bound, then identity, then installed, then conflict or owner, then
disabled, then requested operations; the first failing rule is reported, so the operator never
sees a write complaint about a plugin that is off. R1 to R5 run at load because they depend on
nothing but the document; R6 to R12 depend on the host, so putting them at load would make one
fleet document valid on one host and invalid on another. The config layer may not consult
discovery, which is why the **known-adapter table** (`{adapter id: source it serves}`, one row:
`tracker-github: github-issues`) is a closed static table, drift-tested against the built-in
manifests. Accepted cost: a third-party adapter id is unknown at T1 until the table learns it
(a schema release, like a new `source`). No message contains a credential value; R1 echoes only
an id that already passed the R2 shape.

**Status and doctor.** `bh plugin tracker-github status [--json]` makes no network call;
`doctor [--probe]` is the only path that does (read-only `GET`s, refused unless the plugin is
enabled); the `hive ready`/`doctor` row is offline. All are rendered by one root function over
the neutral port, so they are identical for any future `tracker.adapter` plugin. A disabled or
uninstalled plugin never nags and never fails a gate. Output never contains a token, a header,
a raw upstream body or a grant secret; free text passes through `scrub`.

## Resolutions

### O1: where `TokenSource`, `ResolvedToken` and `scrub()` live

**Adopt `beadhive_plugins.credentials`** (a new stdlib-only module in the existing library
package `packages/beadhive-plugins`), per `bh-rktrh`. No concrete reason to differ was found; the
repository was checked and supports it:

- The package is a library package with `dependencies = []` and already holds the credential
  declaration types. Root vendors it unconditionally in the published wheel and imports it
  statically; a plugin package may import a library package through its public `__all__`.
  The other candidates fail: the adapter package cannot be imported by root (the checker
  forbids `src/beadhive` importing a plugin package other than lazily); a new
  `beadhive.kernel.credentials.contracts` adds a kernel package for about forty lines; a new
  `beadhive-credentials` distribution adds a lockfile entry and a release unit for three names.
- One class is required, not a structural Protocol alone: if root builds the token and the
  plugin receives it, the redaction (`repr=False`, `str()` prints only provenance) must be a
  property of a single class both sides import.
- **Implementation caveat found in the checker** (`scripts/check_package_imports.py`,
  `_public_surface_violation`): an import of `beadhive_plugins.credentials` is allowed only if
  that module defines a public `__all__` containing the imported names. The module must carry
  `__all__`. (`beadhive_plugins.worktree_slots` is the in-repo precedent for a submodule import.)
- **G4 is refined, not weakened:** a package-local test asserts `adapter/` imports no
  `beadhive.kernel.*` and, from `beadhive_plugins`, only `beadhive_plugins.credentials`.
- Consequences: the projection retires its private `_scrub` and `TokenSource` alias in favour of
  the shared ones; `run._fill_github_token` and `github_projection.ambient_token` become thin
  callers of one provenance-returning resolver (chain order unchanged); the source is
  zero-argument and called once per operation, so a rotated token is picked up and nothing is
  cached.

The manifest cannot order credential sources (the kernel stores them sorted, so `GH_TOKEN`
sorts first) and cannot express `gh auth token`; precedence therefore lives in
`credential.sources`, and a source name the manifest does not declare is a config error.

### Decisions D1 to D6 (from `bh-rktrh`)

| # | Decision | Resolution | Reason |
|---|---|---|---|
| D1 | Plugin id `tracker-github` (alternative `github`) | **Accept** | Matches the package and namespace; `github` would collide the day a GitHub pull-request plugin exists |
| D2 | O1: `beadhive_plugins.credentials` | **Accept**, with the `__all__` requirement above | Verified against the checker, the package's `dependencies = []` and the wheel vendoring |
| D3 | The `gh` leg is declared by the optional `gh` executable plus `credential.sources` names; no wire-schema change | **Accept** | A fifth credential kind would be a new wire release for one consumer; the drift test ties every accepted name to something the manifest declares |
| D4 | `plugins.tracker-github` is host-owned | **Accept**, and extend to `plugin_kernel` | A fleet push must not enable a write; load-bearing under `bh-6bkun` (binding is fleet intent, plugin is host authority) |
| D5 | Process-wide ownership accepted | **Accept** | Pre-existing property of every capability; multi-tracker per process is a separate kernel decision (S5) |
| D6 | Only `doctor --probe` touches the network | **Accept** | `status` and the readiness row stay offline and cannot fail a gate |

### Decisions B1 to B8 (from `bh-6bkun`)

| # | Decision | Resolution | Reason |
|---|---|---|---|
| B1 | Binding key `stores.public.adapter`, a plugin-id string, requires `source`, public store only | **Accept** | Additive; an object value is premature because the second-tracker needs are additive sibling keys |
| B2 | Repository derived from the hive identity triplet; `plugins.tracker-github.repo` removed | **Accept**; where the spikes conflict this prevails | One value per process cannot serve a multi-hive daemon; the projection already derives and cross-checks the slug; no second place to mis-type it |
| B3 | Binding is fleet intent, plugin is host authority, a binding never enables | **Accept** | Matches "off unless configured" and the kernel ADR's ownership split |
| B4 | Three validation tiers (R1 to R5 load; R6/R9/R12 informational at `hive ready`; R6 to R13 at the root) | **Accept** | Config load is pure and plugin-blind; host-dependent rules must not make one document valid on one host and invalid on another |
| B5 | Requested operations are a runtime request, not a store key | **Accept** | Avoids two places to flip a flag and a pre-approval-looking list |
| B6 | Known-adapter table is a closed static table, drift-tested against manifests | **Accept** | Same as `source` today; the cost (a third-party id needs a schema release) is accepted |
| B7 | No `schema_version` bump; rollout order | **Accept**, with the rollout order made an explicit replan item (below) | The key is optional with no default; the hazard is ordering, not versioning |
| B8 | Stores models hide input values in errors | **Accept**, and widen the requirement (below) | Verified as a real, existing bug |

### Where the two spikes conflict

`bh-6bkun` amends `bh-rktrh` and prevails wherever they differ. The differences, all resolved:

1. **`plugins.tracker-github.repo`**: removed (B2). Its note "Deriving it from the hive is
   `bh-6bkun`" is discharged by that spike.
2. **Effective enablement**: per store, with "the store binds this adapter AND the identity
   resolves" in place of "`repo` set" (section 3).
3. **Status state `unconfigured`**: "no store binds this adapter"; `status` additionally lists
   the bound stores (hive and derived slug) from the registry, offline.
4. **Factory signature**: gains `project: ProjectRef`; the `bh-rktrh` stub read `settings["repo"]`.
5. **Adapter lifetime**: one per (hive, store scope) per process, not one per hive.
6. **Operator lifecycle step 3**: "`bh config set plugins.tracker-github.repo ...`" becomes
   "declare `stores.public.adapter: tracker-github` on the hive entry (fleet layer)"; the host
   still sets `enabled` and the read flags.
7. **Hive-level selection and per-hive overrides**, deferred by `bh-rktrh`, are settled: the
   binding set is the per-hive selection and there are no per-hive overrides of plugin flags.

## Implementation replan items

These two must be explicit beads or acceptance criteria in the implementation molecule.

### (a) Rollout-order hazard

The schema must ship and **rotate to every host before any config uses `adapter`**. A host on a
binary that does not know the key fails closed, and not only at validation:
`store_target.configured_stores` validates the raw `stores` block with
`ManagedRepoStores.model_validate`, so one unknown member raises `StoreConfigInvalid` and that
host cannot resolve **any** store of the hive, including its internal store. The same applies to
the new top-level `plugins` and `plugin_kernel` keys (`BeadhiveConfig` is `extra="forbid"`):
`validate_config`, and the heartbeat's `config_valid()` built on it, would report an old host's
config invalid, and the daemon paths that call `BeadhiveConfig.model_validate` directly
(`host_daemon.py`, `frame_bridge_*`, `daemon_supervisor.py`) are exposed the same way. Required
sequence: (1) the config-schema bead ships; (2) it is rotated to every host (the frame-rotation
procedure; probe any new required `hive ready` check on all frames first); (3) only then does
any fleet config declare `adapter` or `plugins`; (4) until then the key is documented and
nothing in the repository declares it by default. No `schema_version` bump is needed.

### (b) Input echo in the stores models (verified)

`store_target.configured_stores` does
`raise StoreConfigInvalid(f"{_hive(entry)}: invalid stores block: {exc}")`
(`src/beadhive/store_target.py:185`), and `str(ValidationError)` repeats the offending input.
Verified for this ADR against the real code: validating a stores block whose
`public.source` is a pasted token prints
`input_value='ghp_...', input_type=str` verbatim in the raised message. `validate_config`
itself does not echo (it renders `err["msg"]` only), so the leak is the `store_target` path and
any other caller that stringifies a `ValidationError` from these models. No model in `src/` or
`packages/` sets `hide_input_in_errors` today.

**Recommendation:** set `ConfigDict(extra="forbid", hide_input_in_errors=True)` on the stores
models, and note a measured detail: the setting must be on the model that is *validated* (here
`ManagedRepoStores`, the root of `configured_stores`); setting it only on the nested
`StoreDescriptor` still echoes (checked with a minimal pydantic reproduction). Set it on both.
Acceptance for the bead: a planted-token test through `configured_stores` asserts the token is
absent from the raised message, with a negative control that removes the setting and asserts the
leak is detected. Whether to apply the setting at `BeadhiveConfig` level (it would also cover the
daemon call sites) changes error text for every key and is left to the config bead to evaluate.
A binding key is a name easily confused with a credential, which raises the odds of a paste, so
this lands **before** the key is documented to operators.

## Gap and item ownership

This ADR files nothing. Each gap maps to a named work item of the "Consolidated bead sequence"
below (names are stable labels for the planner, not bead ids).

| Gap | What | Owning work item |
|---|---|---|
| G1 | `contracts` importing `httpx` passes the checker | **W1** Tracker module and conformance kit |
| G2 | Any layer importing `githubkit` (root prefix half) | **W1**; the plugin-layer half is P1, owned by **W6** |
| G3 | Tracker contracts importing another module's contracts (leaf-module rule) | **W1** |
| G4 | Adapter importing the plugin kernel (refined: `beadhive_plugins.credentials` only) | **W6** Package skeleton |
| P1 | No external-dependency rule for packages: `githubkit` imported only lazily, only under `adapter/` | **W6** |
| P2 | Generalise `test_builtin_plugin_manifests.py` (third provider set, optional executable, non-null `schema_artifact`) | **W6** |
| P3 | Drift tests: packaged `plugin.json` equals `manifests/tracker-github.json`; capability and credential sources match; extends the known-adapter table test | **W6** (table test created in **W2**) |
| P4 | Config gaps C1 to C3 | **W2** Config schema |
| P5 | One shared `_BEADHIVE_VERSION`/`_KERNEL_VERSION` constant instead of a fourth copy | **W4** Category composition |
| C1 | `BeadhiveConfig` gains `plugins` and `plugin_kernel` | **W2** |
| C2 | Host ownership of `plugins.tracker-github` and `plugin_kernel` in `partition.py` | **W2** |
| C3 | `TrackerGithubConfig` (no `repo`) and its `_FRAGMENT_MODELS` entry; published fragment schema | **W2** |

## Consolidated bead sequence

For the planner to shape (merges the `bh-rktrh` section 7, `bh-6bkun` section 7 and port-ADR
next-step sequences; one set, no duplicates):

1. **W1 Tracker module and conformance kit.** `modules/tracker` (contracts and application),
   `beadhive.testing.tracker` (`MemoryTracker`, cases, world), checker extensions G1, G2 (root
   half), G3 with negative controls. Needed first: the category cannot be tested before
   `MemoryTracker` exists.
2. **W2 Config schema.** C1, C2, C3; `adapter` on the store descriptor; the known-adapter table
   with R1 to R5 in the stores validators; `hide_input_in_errors` on the stores models (item b);
   regenerate `config-v1.schema.json` and the wire artifact; the `bh-6bkun` corpus and
   48-combination matrix as the regression test against the real model. Independent of W1 and
   valuable alone (also closes the `plugins` gap for the five built-ins). **Ships and rotates to
   every host before any config uses the key (item a).**
3. **W3 Credential module (O1).** `beadhive_plugins.credentials` (with `__all__`), root's
   provenance-returning resolver, retire `_scrub` and the `TokenSource` alias. Independent.
4. **W4 Category composition.** `bootstrap/tracker.py` (R6 to R13, per-(hive, store) adapter
   cache), the generic status/doctor renderer, the four `DiagnosticCode` values, P5. Tested end
   to end with `MemoryTracker` through a **test-only manifest source**, so the category lands
   before the GitHub package and the port ADR's "manifest before adapter" constraint is met
   without a throw-away manifest. After W1, W2, W3.
5. **W5 Readiness.** The `hive ready` and `doctor` row (T2) and the status listing of bound
   stores. After W4.
6. **W6 Package skeleton.** `packages/beadhive-tracker-github` with `plugin.json`, the
   `BUILTIN_PLUGIN_IDS` entry, the manifest copy, `beadhive[github]`, the `catalog` entry,
   P1 to P3, G4, and the `hishel` licence override with its `effectiveUntil` date. After W4.
7. **W7 Adapter read path.** Port the prototype mapping onto githubkit; the adapter-owned retry
   layer and sleep seam; identity probe and refusal diagnostics; marker codec. After W6.
8. **W8 `comment.create` write** and **W9 `labels.change` write**, each its own bead behind its
   own operator-approved gate, re-verifying the documented refusal diagnostics against recorded
   real responses. After W7; W9 independent of W8.
9. **W10 Rotation, then first use.** Rotate the W2 schema to every host (frame rotation), verify,
   then the first fleet config declares `adapter`. A gate, not code; after W2 and before any
   fleet use (item a).

Independent of this molecule: `bh-cm4er` and the `last_sync` risk stay on the bd path (port ADR
section 5).

## Second-tracker stores-schema list (input for the multi-tracker work)

Listed, not designed, from `bh-6bkun` section 6. Everything above is additive, so none of these
changes the shape adopted here. The multi-tracker bead decides each.

| # | Gap | Evidence | To decide |
|---|---|---|---|
| S1 | `source` is a one-member `Literal` | `contracts.py` (`source` field) | Widen it or make it a validated open string; turn the known-adapter table into `source -> adapter ids`, still drift-tested |
| S2 | Identity is derived only for GitHub | `github_projection.py` slug derivation; the port's `ProjectRef(tracker, locator)` | Jira project key, Linear team, GitLab nested group and self-hosted hosts cannot come from `provider/org/repo`; add an optional per-store `locator` (and a host if needed) as an additive sibling key, absent and derived for GitHub; generalise R12 per source |
| S3 | The store pair is two fixed slots | `STORE_SCOPES`, `ManagedRepoStores`, `store_pair_problems`, `fleet_store_problems`, `store_target.SCOPES`, `tracker_sync.select_tracker_store` | A second tracker for one hive needs another remote-authoritative store (a named-store mapping, or `public` becoming a list) plus the resolution, uniqueness and sync-selection code that assumes one |
| S4 | Per-store non-identity options | plugin namespaces are process-wide | Where a store-specific setting lives: a per-store options key, or `plugins.<id>` keyed by store prefix |
| S5 | Capability ownership is process-wide | `bh-rktrh` E4, D5 | A kernel change (select by more than `capability@major`), not a schema change; the schema must not paper over it |
| S6 | Fleet uniqueness of the mirrored project | `fleet_store_problems` checks prefix, path, remote | "No two stores mirror the same `(adapter, locator)`" once a locator exists |
| S7 | Stores models echo input in errors | item (b) | Fixed once, for all stores keys, before more name-like keys are added (owned by W2) |
| S8 | `plugins`/`plugin_kernel` not in the schema | C1 | Prerequisite for any plugin-bound store; already W2 |

`authority: remote` and `sync: pull-only` do not change for a second tracker: they describe the
Dolt replica of the mirrored rows, not whether the adapter may write to the tracker.

## Consequences

### Positive

- A tracker adapter is a plugin on the existing kernel: no kernel edit, no new top-level key, no
  new capability concept; a second tracker is a new plugin package plus (later) a schema release.
- Off by default at every layer. A hive that never opts in behaves byte for byte as today
  (shown over the existing-config corpus and the 48-combination matrix).
- One host-owned plugin configuration serves any number of hives; each gets the repository of
  its own identity triplet, with no second place to mis-type it and a built-in agreement with
  the projection's existing cross-check.
- A fleet push can express intent but cannot enable the plugin, a read or a write.
- Status and doctor are identical for any future tracker plugin and cannot leak a secret.
- Fixing the input echo closes an existing leak for all stores keys.

### Negative or accepted costs

- The known-adapter table is closed: a third-party adapter id needs a schema release.
- Ownership is process-wide: one tracker plugin per process (S5).
- The schema must rotate to every host before use (item a); until W10 the key is unused.
- The plugin config model lives in root (`TrackerGithubConfig`) so that config validates without
  importing the optional package; a general "plugin supplies its own fragment" mechanism is not
  adopted now.
- `beadhive_plugins` gains a credentials module that root, the projection and the plugin package
  all depend on; changes to it widen the test closure of three consumers.
- The `unconfigured`/bound-store rendering needs the hive registry in the root status code.
- `provider` is the repo-group *path*, not necessarily the provider type; a hive whose group
  path is not `github` is refused (R12), as the projection already does.

## Risks

- **Prototype fidelity.** `bh-6bkun` subclassed the real models; a subclass shows the new key is
  additive, not that the real edit has no side effect on the published JSON schema. The schema
  drift tests in W2 must check this.
- **Documented, not observed.** Fine-grained and App token behaviour and every refusal
  diagnostic except `401` and `404` are from the port ADR and remain unverified until the first
  gated write.
- **Unrun surfaces.** `bh config validate`, the heartbeat, `hive ready`, wire-artifact
  regeneration, `bh config set plugins.tracker-github...` and `presentation.cli` parents under
  `plugin tracker-github` were read, not run. The daemon call-site exposure noted in item (a) is
  from a code search, not a run.
- **Forks.** A fork (`kind: fork`) derives from its own triplet, not `upstream`; whether it
  should read its upstream's issues is undecided and out of scope.
- **Composition is untested at scale.** Multi-hive, multi-process composition (the per-store
  adapter cache) is specified but untested.
- **Interpreters.** Both spikes ran on Python 3.12.13 only; the declared range is 3.11 to 3.13.

## What remains open

- **O3** (port ADR): issue identity across a transfer or a key change; a question for the intake
  design's URL key (`bh-wjvq9`), not a port or plugin change.
- **Hive-level overrides** of plugin flags, deliberately omitted; addable later without changing
  this shape.
- **Multi-tracker support** (S1 to S8), including selection by more than `capability@major`.
- **A fork reading its upstream's issues.**
- **Plugin-supplied config fragments** (a general mechanism) and third-party adapter ids.
- **GitHub App installation tokens**, deferred by the port ADR; a drop-in token source.
- Whether to set `hide_input_in_errors` at `BeadhiveConfig` level (item b).
- The conversation model, outbox and gate implementation (`bh-wjvq9`), which consume the port.

## Next steps

This ADR does **not** file the implementation molecule and touches no other bead.

1. The close reason of `bh-h7nw7` carries the verdict (GO); the "Decision summary" above is
   paste-ready.
2. The implementation molecule is filed by a separate, human-interactive `/bh:replan` run on
   epic `bh-mobxo`, using "Implementation replan items", "Gap and item ownership" and
   "Consolidated bead sequence" above as input.
3. The multi-tracker work takes "Second-tracker stores-schema list" as input.
4. Record in the implementation beads the `pants dependents` measurement for each test-closure
   path, and the `hishel` override's `effectiveUntil` re-verification date.
