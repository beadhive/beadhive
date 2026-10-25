# Spike `bh-rktrh` — the `tracker.adapter` plugin category: contract, lifecycle, config and diagnostics

**Bead:** `bh-rktrh` · **Seat:** `dev/rktrh` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-h7nw7` (adopt the tracker-adapter plugin category and the GitHub plugin
binding), under epic `bh-mobxo`. Consumed by `bh-6bkun` (configuring the GitHub plugin as a hive's
bead tracker adapter) and, through it, the external-tracker intake module (`bh-wjvq9`).
**Builds on:** [`tracker-adapter-port-adr.md`](../design/tracker-adapter-port-adr.md) (verdict GO:
port, githubkit transport, adapter package, open item O1, checker gaps G1 to G4) and
[`plugin-kernel-v1-adr.md`](../design/plugin-kernel-v1-adr.md).

## Question

Can a bead tracker adapter be offered as a **plugin category** on the existing plugin kernel,
with no kernel change and no new top-level config key, so that:

1. the category is a kernel capability (`tracker.adapter`, API major 1) whose port is the
   Protocol from the port ADR, bound through `bind_application_port`;
2. the GitHub plugin receives credentials only through an **injected token-source port**, because
   a plugin package cannot import `beadhive.run`, and the manifest names credential *sources*
   without ever holding a value;
3. everything is **off by default**, with read and write enabled separately and every write still
   behind an operator-approved gate;
4. `status` and `doctor` report identity, credential presence, and per-operation capability,
   and **never print a secret**?

Explicitly **not** asked: the port contract, transport or credential chain (the three
spikes and the ADR settled them); which hive uses which adapter (`bh-6bkun`); the conversation
model, outbox and the gate's own implementation (`bh-wjvq9`); multi-tracker support; GitHub App
tokens. This spike sent **no request to GitHub**.

## Method

- **Read the inputs** line by line: the port ADR (sections 2, 3, 4, 6 and "Inconsistencies"), the
  plugin kernel ADR, `docs/PLUGIN-AUTHORING.md`, `docs/CONFIGURATION.md`, and the three prerequisite
  spikes' credential and contract sections.
- **Read every mechanism the category would touch**, and recorded `file:line`: kernel
  discovery and binding (now in `packages/beadhive-plugins`, with forwarding facades in
  `src/beadhive/kernel/plugins`), `BUILTIN_PLUGIN_IDS`, the legacy compatibility facade
  (`src/beadhive/plugins.py`), the two existing composition roots for package plugins
  (`bootstrap/impact.py`, `bootstrap/build_verify.py`), the typed-port precedents (Herdr
  `agent.session`, `worktree.manager`), the config layer (`modules/config`), the readiness and
  doctor surfaces, and `scripts/check_package_imports.py`.
- **Ran the real kernel** against a concrete manifest and stub adapters, offline
  ([`artifacts/bh-rktrh/`](artifacts/bh-rktrh/README.md)): 32 tests, plus four mutation checks.
  The tests import the repository's real `discover_plugins` and `bind_application_port`
  unchanged, so a claim "the kernel needs no change" is a test result, not an inference.
- **Probed today's config layer** by calling `validate_config`, `compose_plugin_config` and
  `partition_of` with a `plugins.tracker-github` document. Two tests record the gaps found.
- Nothing under `src/`, `packages/`, `pyproject.toml` or `uv.lock` was touched. The port prototype is
  [`bh-gj1hb/tracker_contracts.py`](artifacts/bh-gj1hb/tracker_contracts.py), reused as is.

## Evidence

### A. The kernel already has every mechanism the category needs

- **E1. A capability is a string plus a major, declared in data.** `capabilities.provides` carries
  `{id, api_version}`; ids match `^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$`
  (`kernel/plugins/discovery.py:49`). `CapabilityRef(capability_id, api_version)` names it
  (`packages/beadhive-plugins/src/beadhive_plugins/contracts.py:88`). `tracker.adapter` passes the
  pattern; the concrete manifest is valid under the real parser and the published wire schema
  (`test_manifest_is_valid_and_owns_the_capability`,
  `test_manifest_matches_the_published_wire_schema`).
- **E2. Typed binding is generic over the port.** `bind_application_port(request, discovery,
  providers)` selects the owner from discovery, requires exactly one binding for it, and checks
  `isinstance(binding.port, request.port_type)` (`beadhive_plugins/binding.py:18,47`). Every port
  in the ADR is `@runtime_checkable`. A `CapabilityKey(CapabilityRef("tracker.adapter", 1),
  TrackerAdapter)` therefore binds with no kernel edit
  (`test_enabled_plugin_with_no_operation_binds_a_reader_with_no_writer_methods`).
- **E3. Optional writers are discoverable without a second capability.** `TrackerAdapter` is the
  required union (reader plus identity); `CommentWriter` and `LabelWriter` are separate
  runtime-checkable Protocols (port ADR section 3). Because `isinstance` on a runtime-checkable
  Protocol tests for the methods' presence, an adapter object that simply *lacks* `post_comment`
  is provably not a `CommentWriter`. The prototype builds read-only, comment-only, labels-only and
  both variants and asserts exactly that (`test_each_write_flag_adds_exactly_its_own_writer`).
- **E4. Ownership is process-wide and fails closed.** One enabled owner per `capability@major`;
  two enabled providers without `plugin_kernel.capability_owners` produce `duplicate-capability`
  and no owner (`discovery.py:859-870`). The prototype reproduces both outcomes and shows a
  disabled second provider causes no conflict
  (`test_two_enabled_providers_conflict_until_an_owner_is_selected`,
  `test_a_disabled_second_provider_causes_no_conflict`).
- **E5. Discovery enables by default.** `policy.enabled.get(manifest.plugin_id, True)`
  (`discovery.py:817`): a manifest listed in `BUILTIN_PLUGIN_IDS` (`kernel/plugins/builtins.py:11`)
  is **on** unless `plugin_kernel.enabled.<id>` is `false`. Pants is selected this way. The five
  legacy integrations are off by default only because the compatibility facade folds each plugin's
  own predicate in: `enabled[plugin.name] = legacy_enabled and policy.enabled.get(plugin.name, True)`
  (`src/beadhive/plugins.py:182`), and the predicate is the top-level `enabled` flag, default false
  (`config.py:124-126` `layered_flag`; `docs/CONFIGURATION.md:263-276`). **An off-by-default tracker
  plugin therefore needs the same fold.** The prototype does it in a few lines
  (`compose_proto._own_enabled`) and three tests fail when it is removed.
- **E6. A package plugin's runtime binding is a lazy row, loaded only after selection.**
  `ImpactBackendProvider.load` is called only for the discovered owner; `ModuleNotFoundError` is
  mapped to "install `beadhive[pants]`" (`bootstrap/impact.py:56,60,119`; extra at
  `pyproject.toml:91`, workspace source at `:142`). The verify root does the same for
  `build.verify` (`bootstrap/build_verify.py`). This is the packaged precedent the port ADR
  names (`bh-mxjoy`), and it is the shape the tracker needs.
- **E7. The host and kernel versions are copied three times.** `_BEADHIVE_VERSION = "0.15.1"` and
  `_KERNEL_VERSION = "1.0.0"` appear in `bootstrap/impact.py:46`, `bootstrap/build_verify.py:20` and
  as literals in `plugins.py:167,192`. A tracker root would be a fourth copy; it should import one
  constant instead.
- **E8. Package manifests are copied, not generated.** `packages/beadhive-pants/.../plugin.json`
  and `kernel/plugins/manifests/pants.json` are byte-identical today, and
  `docs/PLUGIN-AUTHORING.md` tells authors to copy. I found **no test** that compares the two
  (`grep` over `tests/` and `packages/*/tests`), so drift is silent.

### B. The existing categories, compared

| Category | Capability and port | Binding | Off by default via | Status surface | Fits tracker? |
|---|---|---|---|---|---|
| Herdr | `agent.session@1`, `HerdrAgentSessionPort` (`integrations/herdr/agent_adapter.py:47,142`) | one-entry synthetic discovery, built in (`herdr/cli.py:86`) | no config flag: `enabled` is a live `server_up()` probe (`herdr_plugin.py:77`) | `bh plugin herdr ...` | Port-first typed binding: **reuse**. Synthetic discovery: no, a tracker is optional and installable |
| Worktree manager | `worktree.manager@1` slot (`beadhive_plugins/worktree_slots.py:28`) | selected by config key `worktrees.manager`, native default (`worktree.py:552-556`) | n/a (native always on) | none | Config-selected single slot: informs `bh-6bkun`, not used here |
| Pants | `build.impact@1`, `build.verify@1` | discovery owner, lazy loader row (`bootstrap/impact.py`) | kernel default **on**, inert without backend config | `PluginDiagnostic` data into `hive ready` (`hive_ready.py:165`) | **The template**: package plugin, lazy row, diagnostics as data |
| Orca, hitch, observaloop, repowise | none typed; `Plugin` nullable-callback facade | `registry()` plus `readiness` hook | own `enabled`, default false, folded in (`plugins.py:182`) | `readiness -> (state, detail)` lines | The facade is closed to new plugins (`PLUGIN-AUTHORING.md:241-242`); **reuse the fold, not the facade** |
| git-workspace | none | unconditional dep | n/a | readiness line | Not a plugin; irrelevant |

The tracker category is therefore Pants's shape (package, lazy row, discovery-selected owner,
diagnostics as data) with the legacy integrations' off-by-default fold, and a typed port in
the Herdr style. It adds no new kernel concept.

### C. Credentials

- **E9. The plugin cannot reach the resolver.** A plugin package may import `beadhive.kernel.*.contracts`,
  `beadhive.modules.*.contracts` and `beadhive.testing`, plus a library package's public
  `__all__` (`scripts/check_package_imports.py:77-84`, and the rule at `:222-247`). `beadhive.run`
  (`run.py:127` `_fill_github_token`) is out of reach. The token chain that already exists is
  `GITHUB_TOKEN`, then a mirrored `GH_TOKEN`, then a fresh `gh auth token` bounded at 15 s
  (`run.py:106,127-175`); `credentials.probe(gh)` already reports presence and provenance
  without a value (`credentials.py:56,173`).
- **E10. The manifest can name environment variables but not a command.** `security.credentials`
  admits exactly four source kinds: `environment-variable`, `file-reference`, `keyring`,
  `oauth-profile` (`discovery.py:52-57`); every nested object is closed, so `value`, `token`,
  `argv` and an unknown `kind` are all `invalid-manifest`
  (`test_manifest_fails_closed_on_values_and_commands`, four cases). `gh auth token` is a command and
  cannot be a credential source; the `gh` binary can only be declared as an optional executable,
  which discovery reports as a warning when absent (`discovery.py:673`) and does not block selection
  (`test_missing_optional_gh_warns_but_does_not_block_selection`).
- **E11. Declared sources are an unordered set.** The kernel stores them `sorted`
  (`discovery.py:448`), so `GH_TOKEN` sorts before `GITHUB_TOKEN`
  (`test_manifest_credential_sources_are_an_unordered_set`). bh's precedence is
  `GITHUB_TOKEN` first, so **precedence cannot live in the manifest**.
- **E12. The injected-callable pattern already exists, minus provenance.** The projection takes
  `TokenSource = Callable[[Mapping[str, str]], str | None]` (`github_projection.py:51`) and its
  private `_scrub` is exact-value replace only (`:149-153`). The ADR asks for a richer
  `ResolvedToken(value, source)` plus a shared `scrub()` and leaves their home open (O1).
- **E13. Redaction must be one definition.** If root builds the token object and the plugin
  receives it, then `repr=False` and the `<token from env:GH_TOKEN>` `str` have to be properties of
  one class both sides import. Two structurally-similar classes would let root hand over an object
  whose `repr` prints the value. This is the argument for a shared home rather than a structural
  Protocol alone.

### D. The config layer (this is where the real work is)

- **E14. The canonical namespace is not yet usable in `config.yaml`.** `BeadhiveConfig` is
  `extra="forbid"` (`modules/config/contracts.py:3138-3149`) and declares neither `plugins` nor
  `plugin_kernel`. `validate_config({"plugins": ..., "plugin_kernel": ...})` returns
  `unknown config key` **errors** for both
  (`test_recorded_gap_typed_config_rejects_plugins_and_plugin_kernel`). The runtime reads
  `plugin_kernel` straight from the raw map (`cli.py:150-158`, `plugins.py:_kernel_policy`), so
  behaviour works while validation does not. `heartbeat_report.config_valid()`
  (`heartbeat_report.py:25-28`) is built on `validate_config`'s errors (by reading, not run), so a
  host that sets `plugins.*` today would report an invalid config.
- **E15. The fragment registry is closed.** `compose_plugin_config` validates only plugin ids in
  `_FRAGMENT_MODELS` (`plugin_fragments.py:25-33`, `:322`); any other `plugins.<id>` is retained as
  opaque data with an `unknown_plugin_config` diagnostic (`:349`)
  (`test_recorded_gap_fragment_registry_is_closed_and_ownership_is_unclassified`). The five
  built-in models live in `modules/config/contracts.py` (`HitchConfig` at `:2661`, on
  `_Section`, `extra="forbid"`, `:110`). Each is published as a checked wire artifact
  (`docs/schemas/wire/v2.9.0/plugin-config-*-v1.schema.json`).
- **E16. Ownership is unclassified.** `partition_of("plugins.tracker-github.enabled")` and
  `partition_of("plugin_kernel.enabled.pants")` are both `None`, while `hitch`, `orca`, `repowise`,
  `herdr` and `observaloop` are **host**-owned prefixes (`modules/config/application/partition.py:12-52`).
  Host ownership is what keeps an integration, and especially a credential-using one, from being
  switched on by a fleet-wide config push.
- **E17. A flag read through `layered_flag` is `bool(value)`** (`config.py:124-126`), so a
  hand-edited `enabled: "false"` reads as true. The plugin must read the *validated* fragment
  value instead; the prototype treats only an exact `True` as on
  (`test_only_an_exact_true_enables_the_plugin`, three cases). Note the existing sections are
  **lax**: `HitchConfig.model_validate({"enabled": "true"})` returns `enabled=True`, although the
  plugin guide requires exact booleans (`PLUGIN-AUTHORING.md:42`). The new model must declare its
  booleans strict.
- **E18. The ADR's key table is semantically complete.** Mapping its 14 rows against E14 to E17
  leaves only naming (O2), units (`retry.*` are seconds elsewhere in the schema, for example
  `timeout_seconds`, `contracts.py:681`) and ownership to settle. No key needs to hold a secret,
  a command or an approval (`test_no_proposed_key_can_hold_a_secret_a_command_or_an_approval`).

### E. Diagnostics surfaces

- **E19. There are two existing readiness surfaces.** `bh hive ready` prints one `Check` per
  plugin (`hive_ready.py:151-162`, called at `:611`), fed for legacy plugins by the `readiness`
  callback and for Pants by `PluginDiagnostic` data (`:165-181`). `PluginDiagnostic(code, severity,
  detail, plugin_id, provenance, capability)` has a stable `render()` (`contracts.py:205-226`), but
  `DiagnosticCode` is a **closed** `StrEnum` (`:27-44`); Pants added its three codes there. Package
  plugins cannot contribute readiness through the legacy `Plugin.readiness` hook, which is frozen.
- **E20. A CLI for a package plugin is a root-mounted Typer app.** `projected_cli_mounts` imports the
  module named in `PLUGIN_CLI_RUNTIME_CATALOG` only for selected plugins and skips it when the
  package is missing (`plugins.py:239-264`, `plugin_runtime_catalog.py:41`); it uses
  `honor_legacy_enablement=False`, so the command exists while the plugin is **disabled**, which is
  exactly when an operator needs `status`. A package's own CLI cannot read config or the credential
  chain (E9), so the status renderer has to be root code. Rendered from the neutral port
  (`describe`, `whoami`, `probe_capabilities`), it is then identical for any future tracker plugin.
- **E21. The port already carries what status must show.** `CapabilityReport(actor,
  credential_source, states)` holds identity, a provenance string such as `env:GITHUB_TOKEN`, and per
  operation `supported`, `enabled`, the tri-state `probe`, a one-line scrubbed `reason`, and
  `usable` (port ADR section 3; prototype lines 258-295). Nothing GitHub-specific is needed.
- **E22. The single-use grant is per adapter instance.** The prototype adapter keeps
  `_used_grants` in memory (`bh-gj1hb/github_adapter.py:138,361-363`). Durable single-use is the
  consumer's outbox; the wiring's duty is not to re-create the adapter per call.

### F. Checker and catalog

- **E23. `check_package_imports.py` has no external-dependency rule.** It governs only `beadhive`
  and sibling-package edges (`:222-247`). That githubkit appears only lazily and only inside the
  adapter is not enforced for a package; it is a package-local test.
- **E24. The catalog test is shaped for today's plugins.** It asserts `set(manifests) ==
  set(catalog) | set(impact)` (`tests/contracts/test_builtin_plugin_manifests.py:85`), that each
  manifest lists exactly one executable equal to the catalog's (`:103`), and that a null
  `schema_artifact` is allowed only for impact providers (`:107`). A tracker manifest with an
  optional `gh` and its own config artifact needs a third provider set.

### G. Prototype results

32 tests pass offline on Python 3.12.13 against the real kernel. Four mutations of the prototype each
make the expected tests fail: dropping the `enabled` fold (3 tests); defaulting an absent
`enabled` to true (1); giving every stub both writer methods (the writer-absence tests and those
built on them); making `scrub` ignore the token shape (the negative-control test). See the artifacts'
README.

## Verdict — **GO**

The category fits the plugin kernel **as it is**: `discover_plugins`, `bind_application_port`,
`parse_kernel_config` and the manifest v1 schema need no change (E1 to E6, all exercised
unchanged). Everything the acceptance criteria require is expressible: a `tracker.adapter@1`
capability, a `security.credentials` entry that names sources only (E10), a
`plugins.tracker-github` namespace, an off-by-default fold (E5), separately enabled reads and
writes with the writer methods structurally absent until enabled (E3), and status and doctor
rendered from the neutral port (E20, E21).

The enabler is that Pants already proved the package-plugin shape and the legacy integrations proved
the off-by-default fold; neither needs the other's machinery.

Three things are **not** free and the implementation molecule must own them, none of them kernel
work: the config schema does not yet accept `plugins` or `plugin_kernel` (E14 to E16); the
credential value type and `scrub()` need a stdlib-only shared home (E13, O1); and the catalog test and
checker need small extensions (E24, E23). None changes the verdict.

## Recommendation

### 1. The category: `tracker.adapter@1`

| Element | Decision |
|---|---|
| Capability id | `tracker.adapter`, API major **1** (equals the port's `PORT_API_VERSION`). Names behaviour, not a product, per the kernel ADR section 2 |
| Port | `TrackerAdapter` (reader plus identity) from `beadhive.modules.tracker.contracts`. `CommentWriter` and `LabelWriter` stay optional and are found with `isinstance` on the bound object |
| Binding | `bind_application_port(TRACKER_ADAPTER_PORT, discovery, (ProviderBinding(ProviderKey(owner, TRACKER_ADAPTER), adapter),))` in root composition |
| Owner selection | the kernel's, unchanged: `plugin_kernel.capability_owners["tracker.adapter@1"]` |
| Plugin id | **`tracker-github`** (matches the package `beadhive-tracker-github`, namespace `plugins.tracker-github`). The id regex allows the hyphen. Alternative `github` is shorter but will collide the day a GitHub *PR* plugin exists |

**One capability, not one per operation or per product.** A separate `tracker.writer` capability
would let a different plugin own writes than reads, which breaks the identity the grant is bound to.
Per-product ids (`tracker.github`) break "capability ids name behaviour". Read and write are
separated by *configuration* and by the *shape of the bound object*, not by capability ids.

#### Kernel files extended, and what is new

| File | Change | Kind |
|---|---|---|
| `packages/beadhive-plugins/src/beadhive_plugins/contracts.py` | add four `DiagnosticCode` values: `tracker-package`, `tracker-config`, `tracker-credential`, `tracker-capability` (Pants did the same for `build-*`, `:42-44`) | extension of a closed enum |
| `packages/beadhive-plugins/src/beadhive_plugins/credentials.py` | **new**: `ResolvedToken`, `TokenSource`, `scrub` (section 3) | new module, stdlib only |
| `src/beadhive/kernel/plugins/builtins.py:11` | add `"tracker-github"` to `BUILTIN_PLUGIN_IDS` | data |
| `src/beadhive/kernel/plugins/manifests/tracker-github.json` | byte-identical copy of the packaged `plugin.json` (E8) | data |
| `src/beadhive/bootstrap/tracker.py` | **new** composition root: `TRACKER_ADAPTER`, `TRACKER_ADAPTER_PORT`, `TrackerAdapterProvider` rows, `collect_tracker_adapter(...)` | new, modelled on `bootstrap/impact.py` |
| `src/beadhive/plugin_runtime_catalog.py:41` | one `RuntimeCliEntry("tracker-github", "beadhive.tracker_plugin")` | data |
| `src/beadhive/hive_ready.py`, `src/beadhive/doctor.py` | one tracker row/section projecting `PluginDiagnostic` data | consumer |

`discovery.py`, `binding.py`, `parse_kernel_config` and the manifest schema are **not** modified.
`CapabilityKey` pairing lives in `bootstrap/tracker.py` (as `BUILD_IMPACT_PORT` does,
`bootstrap/impact.py:42`) because the tracker contracts module is stdlib only and must not import
`beadhive_plugins`.

### 2. Declaration, discovery, install, enablement

**Manifest** (concrete, valid; the tested copy is
[`artifacts/bh-rktrh/plugin.json`](artifacts/bh-rktrh/plugin.json)):

```json
{
  "capabilities": {"provides": [{"api_version": 1, "id": "tracker.adapter"}]},
  "compatibility": {
    "beadhive": {"maximum_exclusive": "1.0.0", "minimum_inclusive": "0.15.0"},
    "plugin_kernel": {"maximum_exclusive": "2.0.0", "minimum_inclusive": "1.0.0"}
  },
  "configuration": {"legacy_namespaces": [], "namespace": "plugins.tracker-github",
                    "schema_artifact": "urn:beadhive:wire-schema:plugin-config:tracker-github:1"},
  "lifecycle": {"subscriptions": []},
  "manifest_version": 1,
  "plugin_id": "tracker-github",
  "plugin_version": "1.0.0",
  "presentation": {"cli": [
    {"capability_id": "tracker.adapter", "command": "plugin tracker-github doctor"},
    {"capability_id": "tracker.adapter", "command": "plugin tracker-github status"}
  ]},
  "security": {
    "credentials": [{"id": "github-api", "required": false,
      "purpose": "authenticate GitHub REST requests; absent means anonymous, rate-limited reads",
      "sources": [{"kind": "environment-variable", "name": "GH_TOKEN"},
                  {"kind": "environment-variable", "name": "GITHUB_TOKEN"}]}],
    "executables": [{"name": "gh", "required": false,
                     "version": {"maximum_exclusive": "999.0.0", "minimum_inclusive": "0.0.0"}}],
    "permissions": [
      {"id": "network.github.read", "required": true,
       "purpose": "read issues and comments of the configured repository over HTTPS"},
      {"id": "network.github.write", "required": false,
       "purpose": "post a comment or change Beadhive-owned labels, only with a single-use operator-approved grant"}
    ]
  }
}
```

Notes on the choices:

- **`lifecycle.subscriptions` is empty.** The category subscribes to no kernel event; status is pulled,
  not pushed. This also keeps the plugin off the frozen `Plugin.readiness` hook (E19).
- **`credentials[0].required` is `false`.** The adapter works anonymously on a public repository
  (`whoami()` returns `None`); a missing credential is a capability `NO` for writes, not a startup
  failure.
- **`network.github.write` is declared `required: false`** so the permission list tells an operator
  the plugin *can* write, without implying it does.
- **No `legacy_namespaces`**: there is no top-level `tracker_github` key, and none is added.
- **`schema_artifact` is a plugin-config URN**, so a fragment must be published (section 4).

**Discovery and selection.** Unchanged: the static built-in list yields the manifest bytes;
nothing is imported. `bootstrap/tracker.py` folds `plugins.tracker-github.enabled` (default false)
into the kernel map exactly as `plugins.py:182` does, then calls `discover_plugins`, takes
`owner_of(TRACKER_ADAPTER)`, and only for that owner lazily imports the package factory. Disabled
means *no owner*, so `bind_application_port` would raise "no selected provider" and the consumer
treats the tracker as unconfigured. The fold is `own_enabled AND kernel.enabled.get(id, True)`,
so `plugin_kernel.enabled.tracker-github: false` still wins.

**Install.** `beadhive[github] = ["beadhive-tracker-github"]` in the root `pyproject.toml`, with the
workspace source beside `beadhive-pants` (`pyproject.toml:91,142`). The root wheel never needs
githubkit. A selected manifest whose package is absent yields a `tracker-package` diagnostic that
names `beadhive[github]` (the `_MISSING_PACKAGE` precedent). **Installation never implies
enablement**, and enablement never installs (kernel ADR section 7).

**Package layout.** `packages/beadhive-tracker-github/src/beadhive_tracker_github/` has `adapter/`
(wiring-free, the port ADR's rule), `plugin.json` and a thin `factory.py`. The factory is the one
call root's lazy row makes:

```python
def build(settings: Mapping[str, object], token_source: TokenSource, *, sleep: Callable[[float], None])
    -> TrackerAdapter: ...
```

It receives the **already validated, already folded** settings mapping, a token source and an
injected sleep, and returns a `TrackerAdapter`, with writer methods present only for write operations
whose flags are on (section 4). It imports no `beadhive.kernel.*`: root builds the `ProviderBinding`
(it already knows the plugin id and capability), so the plugin package needs neither `ProviderKey`
nor `CapabilityRef`.

**Conflict.** Two enabled tracker plugins fail closed with `duplicate-capability` until
`plugin_kernel.capability_owners["tracker.adapter@1"]` names one. Limitation worth stating for
`bh-h7nw7`: ownership is **process-wide**, so two hives that need different trackers in one
process (the daemon, the MCP server) cannot both be served by the kernel as it is. That is a
pre-existing property of every capability and is not widened here; supporting it needs a kernel
change (selection keyed by more than `capability@major`) and is out of scope.

### 3. Credentials: the injected token-source port

**The seam.**

```python
# beadhive_plugins/credentials.py  (stdlib only; prototype: artifacts/bh-rktrh/credentials_proto.py)
@dataclass(frozen=True)
class ResolvedToken:
    value: str = field(repr=False)
    source: str                       # provenance only: "env:GH_TOKEN", "gh-auth-token"
    def __str__(self) -> str: return f"<token from {self.source}>"

class TokenSource(Protocol):
    def __call__(self) -> ResolvedToken | None: ...      # zero arguments

def scrub(text: str, secrets: Sequence[str] = ()) -> str: ...  # exact value, longest first, then token-shape
```

- **Zero-argument.** The earlier prototype took `environ`; the plugin must not even see the
  environment. Root closes over it. The adapter calls the source **once per operation** and keeps
  the value only in locals, so a rotated token or a fresh `gh auth token` is picked up and nothing is
  cached.
- **Offline tests.** Tests inject a fake source; the `gh` leg is itself an injected callable in
  root's builder (`build_token_source(..., gh_auth_token=...)`), so no test spawns a process or
  waits (the prototype's conftest forbids both).

**O1 resolved here: the stdlib-only home is `packages/beadhive-plugins`.** Candidates:

| Home | Root imports it | Plugin package imports it | Verdict |
|---|---|---|---|
| The adapter package (the ADR's provisional answer) | **No**: root may not import a plugin package statically | yes | `ResolvedToken` would be defined twice; E13 |
| A new `beadhive.kernel.credentials.contracts` | yes | yes (kernel `*.contracts` allowlist) | Works, but adds a kernel package with a BUILD file for 40 lines, inside the distribution the plugin must already depend on |
| **`beadhive_plugins.credentials`** | yes (library package, statically) | yes (public `__all__`) | **Chosen.** The library already holds the credential *declaration* types (`CredentialRequirement`, `CredentialSource`, `contracts.py:126-139`); it is stdlib only and depends on nothing; a plugin package may import it |
| A new library package `beadhive-credentials` | yes | yes | Cleanest for G4, but a new distribution, lockfile entry and release unit for three names |

Consequences to carry into the implementation:

1. **G4 is refined, not weakened.** The package-local test asserts `adapter/` imports no
   `beadhive.kernel.*` and, from `beadhive_plugins`, only `beadhive_plugins.credentials`
   (an explicit allow-list of one module), never `beadhive_plugins.contracts` or `.binding`.
2. The root projection retires its private `_scrub` (`github_projection.py:149`) and `TokenSource`
   alias (`:51`) in favour of the shared ones, as the identity spike asked.
3. `run._fill_github_token` (`run.py:127`) and `github_projection.ambient_token` (`:124`) become
   thin callers of **one** provenance-returning resolver, so there is still exactly one derivation
   of the chain. The chain order is unchanged.

**What the manifest says, and what it cannot.** `security.credentials[github-api]` names the
*set* of environment variables the plugin may be handed (E10, E11). It cannot order them and cannot
express `gh auth token`. Therefore:

- **Precedence is config**: `credential.sources`, a list of logical names `env` and `gh`
  (default `["env", "gh"]`), in precedence order. `env` expands to the manifest's declared
  environment variables in bh's fixed order; `gh` means "call the injected `gh` leg".
- **The `gh` leg is declared by the optional `gh` executable**, not as a credential source. No wire
  schema change (adding a fifth credential kind would be a new wire release). Decision **D3**
  for the operator, below.
- **Drift test**: every name accepted by `credential.sources` maps to something the manifest
  declares (the env vars, or the `gh` executable); a source name the manifest does not declare is a
  config error (`build_token_source` raises on an unknown name:
  `test_token_source_follows_configured_names_and_treats_blank_as_unset`).

**Never stored.** No config key holds a value (`test_no_proposed_key_can_hold_a_secret_a_command_or_an_approval`
asserts no path segment matches token, secret, password, key, auth, header, command, grant,
approve, auto, bypass; the one allowed exception is `credential.sources`, which holds names). The
schema is `extra="forbid"` at every level, like every other section (`contracts.py:110`). The token
is held in a local for one operation; provenance (`env:GH_TOKEN`) may appear in output, a value never.

### 4. Configuration: keys, defaults, ownership, lifecycle

**No new top-level key.** Everything lives under `plugins.tracker-github`, which the kernel ADR
already assigns to the plugin (section 6). The semantics are the port ADR's; the names below are
**ratified** (O2) with units made explicit.

| Key (under `plugins.tracker-github`) | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | **false** | The plugin's own gate (E5). Off means no owner, no port, no network |
| `repo` | `owner/name` | unset (adapter inert) | Identity, not a secret. Deriving it from the hive is `bh-6bkun` |
| `base_url` | https URL | unset (public API) | GHES prefix. No userinfo, query or fragment |
| `credential.sources` | list of `env`, `gh` | `["env", "gh"]` | Names and precedence; empty list means anonymous only |
| `read.issues.enabled` | bool | **false** | Enables `issues.read` |
| `read.comments.enabled` | bool | **false** | Enables `comments.read` |
| `read.include_closed` | bool | true | Scope of a read once reading is on; not a feature switch |
| `read.page_size` | int 1..100 | unset | Listing page size |
| `http_cache.enabled` | bool | **false** | Conditional requests for the id-reconcile pass |
| `retry.max_retries` | int >= 0 | 3 | Part of `RetryBudget` |
| `retry.max_single_wait_seconds` | number > 0 | 60 | Part of `RetryBudget` |
| `retry.max_total_wait_seconds` | number >= single | 120 | Part of `RetryBudget` |
| `write.comment.enabled` | bool | **false** | Enables `comment.create` (necessary, never sufficient) |
| `write.labels.enabled` | bool | **false** | Enables `labels.change` (necessary, never sufficient) |
| `write.labels.owned_prefixes` | list of non-empty strings | `[]` | The labels Beadhive owns; empty means no label write is possible |

Every feature switch is off; the only default-true value (`read.include_closed`) is a scope inside
a switch that is itself off. A prototype test asserts all `*.enabled` defaults are false.

**Effective enablement** of an operation is a conjunction, evaluated once in the composition root
and handed to the adapter as the resolved `enabled` set:

```text
issues.read     = plugin.enabled AND kernel-enabled AND repo set AND read.issues.enabled
comments.read   = plugin.enabled AND kernel-enabled AND repo set AND read.comments.enabled
comment.create  = plugin.enabled AND kernel-enabled AND repo set AND write.comment.enabled
labels.change   = plugin.enabled AND kernel-enabled AND repo set AND write.labels.enabled
                  AND write.labels.owned_prefixes is non-empty
```

Reads and writes are independent: turning a write on enables no read, and vice versa
(`test_read_flags_enable_reads_without_enabling_writes`).

**Validation, fail closed.** The fragment is a pydantic `_Section` (`extra="forbid"`, every boolean
`StrictBool`, so the string `"false"` is invalid, which the lax existing sections do not guarantee,
E17): `repo` matches `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`;
`base_url` is `https` with no userinfo; `credential.sources` elements are unique members of `{env, gh}`;
`max_single_wait_seconds <= max_total_wait_seconds`. An invalid fragment is *retained* inactive and
a `tracker-config` error diagnostic is raised (the `compose_plugin_config` behaviour,
`plugin_fragments.py:325`), so a typo can never silently enable something.

**The write gate: five independent layers**, so no single misconfiguration can write:

1. **Plugin layer, structural.** With a write flag off, the bound object **has no such method**
   (E3); a consumer testing `isinstance(port, CommentWriter)` gets `False` and cannot even form the
   call. This is the "no write code present" property of the port ADR, verified by test.
2. **Adapter, nominal.** The adapter is built with the resolved `enabled` set and refuses a
   disabled operation, a mismatched or reused grant, or a label outside `LabelOwnership`, before any
   request (port ADR section 3).
3. **Consumer gate, per write.** Each write is its own operator-approved gate in the consumer
   (prerequisite 3's outbox). Approval mints one `WriteGrant(grant_id, operation, target)`. A
   capability `NO` makes the gate refuse *before* asking the operator; `UNKNOWN` lets the operator
   approve and the write itself is the probe.
4. **No bypass key exists.** There is no key, environment variable, flag or `--yes` that supplies,
   stores or pre-approves a grant; a prototype test fails the build if any key segment looks like
   `grant`, `approve`, `auto`, `yes` or `bypass`. A grant is a runtime value, never serialised into
   config.
5. **Durable single use is the consumer's.** The adapter's used-grant set is in memory (E22), so the
   wiring must create **one adapter per hive per process** and reuse it; the outbox ledger is
   the durable authority that a `grant_id` was spent.

The plugin layer's own duty is layers 1 and 4 and "one instance". Layers 2, 3 and 5 are asserted by
the port conformance suite and by prerequisite 3.

**Ownership and the schema gaps (E14 to E16), the config bead's job:**

- **C1.** `BeadhiveConfig` gains `plugins` (a mapping validated through fragment composition) and
  `plugin_kernel` (the kernel's already-typed `enabled`, `capability_owners`,
  `allow_external_entry_points`). This closes the gap for the five built-ins too and is worth doing
  first.
- **C2.** `partition.py` classifies **`plugins.tracker-github` as HOST-owned**, like `hitch`,
  `orca`, `repowise`, `herdr` and `observaloop`. The reason is specific: a fleet-wide push must
  not be able to turn on a GitHub write on every host, and the credential is host-local. Also
  classify `plugin_kernel` (recommended: host).
- **C3.** Add `TrackerGithubConfig(_Section)` beside `HitchConfig` and one entry in
  `_FRAGMENT_MODELS`; this publishes `plugin-config-tracker-github-v1.schema.json` in the next wire
  release (the manifest's `schema_artifact` URN must match, `plugin_fragments.py:308-317`). Keeping
  the model in root means config validates without importing the optional package; a general
  "plugin supplies its own fragment" mechanism is a larger change and is **not** recommended now.

**Lifecycle (operator view).** Each step is reversible and installs nothing silently:

1. Install: `uv tool install 'beadhive[github]'` (nothing runs, nothing is enabled).
2. `bh plugin tracker-github status` shows `disabled` and the exact key that would change that.
3. Configure: `bh config set plugins.tracker-github.repo octo/widgets`, then
   `...read.issues.enabled true`, then `...enabled true`. Order is free; nothing acts until
   `enabled` is true.
4. `bh plugin tracker-github doctor --probe` confirms identity and capability (read-only `GET`s).
5. Later and separately: `...write.comment.enabled true`. Nothing is written until a consumer asks
   and an operator approves a single-use grant.
6. Disable: `...enabled false`. The port unbinds on the next composition; nothing to wipe, because
   no credential was stored; the retained config subtree is kept inactive.

Hive-level selection ("this hive uses this adapter") and per-hive overrides are `bh-6bkun`'s; this
spike fixes only the plugin-owned namespace.

### 5. Status and doctor

**Commands (declared in `presentation.cli`, mounted from root).**

| Command | Network | Purpose |
|---|---|---|
| `bh plugin tracker-github status [--json]` | **none** | State, config as resolved, credential *presence*, per-operation enablement. Never runs `gh`, never opens a socket |
| `bh plugin tracker-github doctor [--probe] [--json]` | none without `--probe` | `status` plus diagnostics. `--probe` additionally calls `whoami()` and `probe_capabilities()` (read-only `GET`s) and may run `gh auth token` once; it refuses unless the plugin is enabled |
| `bh hive ready` / `bh doctor` row | **none** | One line from the same offline data, `na`/`disabled` while off (the legacy `_plugin_checks` convention, `hive_ready.py:151`) |

Both are rendered by one root function over the **neutral port**, so they are identical for any
future `tracker.adapter` plugin (E20). A disabled or uninstalled plugin never nags: it is `info`, not
an error.

**States:** `not-installed`, `disabled`, `unconfigured` (enabled, `repo` unset), `conflict`,
`invalid-config`, `ready`. `disabled` is rendered from the plugin's own flag, because the kernel's
`disabled-plugin` text says "disabled by plugin_kernel.enabled", which would be wrong when the
fold caused it.

**Recorded rendering of the default** (a test asserts these exact lines,
`test_status_text_is_stable_for_the_disabled_default`):

```text
tracker-github  disabled
  capability   tracker.adapter@1  (owner: none selected)
  repo         (unset)
  credential   env:GH_TOKEN unset, env:GITHUB_TOKEN unset; gh not found
  identity     not probed (bh plugin tracker-github doctor --probe)
  operations   issues.read     disabled  probe=not-probed
               comments.read   disabled  probe=not-probed
               comment.create  disabled  probe=not-probed  [operator-approved single-use grant per use]
               labels.change   disabled  probe=not-probed  [operator-approved single-use grant per use]
```

**Enabled and probed** (prototype output; the probe values here come from a stub, real ones come from
the adapter's `bh-mqvdq` rules, where a fine-grained or App token is `unknown`):

```text
tracker-github  ready
  capability   tracker.adapter@1  (owner: tracker-github)
  repo         octo/widgets
  credential   env:GH_TOKEN unset, env:GITHUB_TOKEN set; gh found
               resolved via env:GITHUB_TOKEN
  identity     id=583231 kind=human display=octocat
  operations   issues.read     enabled   probe=unknown  usable
               comments.read   enabled   probe=unknown  usable
               comment.create  disabled  probe=unknown  not usable  [operator-approved single-use grant per use]
               labels.change   disabled  probe=unknown  not usable  [operator-approved single-use grant per use]
```

**What is reported, and what is never reported.**

- **Identity**: the actor key `(id, kind)` and the display handle; anonymous when there is no
  credential. The key is the identity, the handle is display only (port ADR section 2).
- **Credential**: each declared source with `set`/`unset` (presence is a boolean), whether `gh` is on
  `PATH`, and, only after a probe, the *provenance* of the resolved token (`env:GITHUB_TOKEN`). A
  warning is shown when `GITHUB_TOKEN` and `GH_TOKEN` are both set and differ (state only).
- **Capability**: per operation `enabled`, tri-state `probe` (`yes`, `no`, `unknown`, `YES` meaning
  only "not refuted"), `usable`, and a one-line `reason`. Write rows always carry the gate note, and
  that note is a constant, not conditional on any config, so no output can suggest a bypass.
- **Never**: a token, any part of one, an `Authorization` header, a raw upstream body, or a grant
  secret. Free text (`reason`, `error`, upstream messages) always passes through `scrub`; every other
  field is built from enumerations, booleans, provenance strings and public ids.

**`--json`** carries `"schema": "tracker-adapter-status/1"` with `plugin`, `state`, `capability`,
`owner`, `repo`, `credential` (`legs[]` of `{source, present}`, `gh`, `resolved`), `identity`,
`operations[]` of `{operation, kind, enabled, probe, usable, reason, gate}`, and `error`. It is
`sort_keys` and deterministic; a wire schema artifact is the implementation's step, as for other
`--json` envelopes.

**Diagnostics** (`PluginDiagnostic`, new codes in section 1):

| Code | Severity | When |
|---|---|---|
| `disabled-plugin` (existing) | info | plugin off; shown as `disabled`, never an error |
| `tracker-package` | info if disabled, **error** if enabled | package not importable; names `beadhive[github]` |
| `duplicate-capability` (existing) | error | two enabled providers, no owner selected |
| `tracker-config` | error | fragment invalid (retained inactive); `repo` unset while enabled is a **warning** (inert) |
| `tracker-credential` | warning | enabled with reads on and no credential (anonymous, 60 requests an hour); **error** if a write is enabled and the credential is absent; warning on a `GITHUB_TOKEN`/`GH_TOKEN` disagreement |
| `tracker-capability` | warning | an enabled operation probes `NO`; info listing each enabled write ("each write still needs an operator-approved grant") |
| `optional-executable-missing` (existing) | warning | `gh` absent: shown as `gh not found`, downgraded to info in status text since it is optional |

`doctor` exits non-zero only on an **error** and only while the plugin is enabled, so an off-by-default
plugin can never fail a gate.

**Tests for "no secret ever printed"** (all offline, none timing-dependent): plant a shaped token
(`ghp_...`) and an unshaped one; run status and doctor, text and `--json`; also an adapter reason that
echoes the token and an error body that does. Assert absence from stdout, stderr, `repr`/`str`/f-string
of `ResolvedToken`, every file written to `tmp_path` and `caplog`. Pair with a **negative control**
that removes the exact-value scrub and asserts the leak *is* detected (the prototype does, and the
shape-only mutation is caught by it), so the assertion cannot pass vacuously. The prototype covers
the four output forms in `test_no_output_form_ever_contains_the_token` (four cases) and the control.

### 6. Checker gaps G1 to G4: which fall to the plugin layer

| Gap | Layer | Owner | Note from this spike |
|---|---|---|---|
| G1 `contracts` importing `httpx` | root checker (`check_import_boundaries.py`) | tracker module bead | Not a plugin concern |
| G2 any layer importing `githubkit` | root checker prefixes | tracker module bead | Root half only. The **plugin-layer half is new** (P1 below) |
| G3 tracker contracts importing another module's contracts | root checker leaf-module rule | tracker module bead | Not a plugin concern |
| **G4** adapter importing the plugin kernel | **plugin layer** | adapter package bead | Package-local test; refined by the O1 resolution to "no `beadhive.kernel.*`, and from `beadhive_plugins` only `credentials`". The recommended factory shape (root builds the `ProviderBinding`) makes the stronger form true: nothing in the package needs the kernel |

New gaps this spike found, all plugin-layer or config-layer:

- **P1.** `check_package_imports.py` has no external-dependency rule (E23). Add a package-local test
  that `githubkit` is imported only lazily and only under `adapter/`, never at module top level of
  `beadhive_tracker_github` (the 250 ms start-up cost from `bh-a1svm`).
- **P2.** Generalise `tests/contracts/test_builtin_plugin_manifests.py` (E24): a third provider set,
  a manifest may declare an optional executable that differs from the catalog's, and a non-null
  `schema_artifact` is the norm for a config-bearing provider.
- **P3.** A drift test that the packaged `plugin.json` equals `manifests/tracker-github.json` (E8),
  and that its `capabilities.provides` equals `TRACKER_ADAPTER` and its credential sources equal
  what the token-source builder accepts.
- **P4.** Config gaps C1 to C3 above.
- **P5.** Replace the fourth copy of `_BEADHIVE_VERSION`/`_KERNEL_VERSION` (E7) with one shared constant.

### 7. Suggested bead sequence (for the planner; this spike files nothing)

1. **Tracker module and conformance kit** (port ADR step 1), with G1 to G3. The category cannot be
   tested before `MemoryTracker` exists.
2. **Config schema bead** (C1 to C3 without the tracker fragment first): `plugins` and
   `plugin_kernel` become real; classify ownership. Independent of the tracker and valuable alone.
3. **Credential module** (O1): `beadhive_plugins.credentials`, root's provenance-returning resolver,
   retire `_scrub`. Independent of the package.
4. **Category composition**: `bootstrap/tracker.py`, the generic status/doctor renderer, `hive ready`
   and `doctor` rows, `DiagnosticCode` additions. Tested end to end with `MemoryTracker` bound through
   a **test-only manifest source**, as `tests/harness/sample-plugin-v1.json` does, so the category lands
   *before* the GitHub package exists and the port ADR's sequencing constraint is satisfied without
   a throw-away manifest.
5. **Package skeleton**: `packages/beadhive-tracker-github` with `plugin.json`, the `BUILTIN_PLUGIN_IDS`
   entry, the manifest copy, `beadhive[github]`, P1 to P3 and G4.
6. **Adapter read path**, then each write operation as its own bead (port ADR step 3).

### 8. Decisions requested (`bh-h7nw7`)

- **D1. Plugin id** `tracker-github` (alternative `github`).
- **D2. O1:** `beadhive_plugins.credentials` as the shared stdlib-only home.
- **D3. The `gh` leg** is declared by the optional `gh` executable and the `credential.sources`
  names, with **no** wire-schema change. Alternative: add a fifth credential kind
  (for example `credential-helper`, name only) in a new wire release; it would make the manifest
  complete but widens the schema for one consumer.
- **D4. Ownership:** `plugins.tracker-github` is HOST-owned, so a fleet push cannot enable a write.
- **D5. Process-wide ownership** is accepted for now; multi-tracker per process is a separate kernel
  decision.
- **D6. Probe network use:** `status` and the `hive ready`/`doctor` row never touch the network;
  only `doctor --probe` does.

### 9. Risks and what was not verified

- The **GitHub adapter's real probe results** are the port ADR's (documented, mostly unobserved);
  the status examples above use a stub, so the `probe=` values are illustrative.
- The heartbeat consequence of an unaccepted `plugins` key (E14) is from reading
  `heartbeat_report.config_valid`, not from running a heartbeat.
- `bh config set plugins.tracker-github...` was not run; `docs/CONFIGURATION.md:418` says an unknown
  section warns and the write proceeds, which matches `validate_config`'s error but was not exercised.
- Publishing the new fragment is a wire-release step I read (`schema_artifacts.py`) but did not run.
- Python 3.12.13 only for the prototype tests; the project's declared range is 3.11 to 3.13.
- Whether `presentation.cli` commands under `plugin tracker-github` need operation-catalog parents
  (`cli.py` `_CATALOG_PARENTS`) was not exercised; Pants's `plugin pants ...` commands suggest not.
- Multi-hive, multi-process composition (one adapter per hive per process) is specified but untested.
