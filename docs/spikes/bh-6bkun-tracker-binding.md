# Spike `bh-6bkun` — configuring the GitHub plugin as a hive's bead tracker adapter

**Bead:** `bh-6bkun` · **Seat:** `dev/6bkun` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-h7nw7` (adopt the tracker-adapter plugin category and the GitHub plugin
binding), under epic `bh-mobxo`. Consumed by the external-tracker intake module (`bh-wjvq9`) and
by the future multi-tracker bead (section "Recommendation", item 6).
**Builds on:** [`bh-rktrh`](bh-rktrh-tracker-plugin-category.md) (category `tracker.adapter@1`,
plugin id `tracker-github`, keys `plugins.tracker-github.*`, config gaps C1 to C3, decisions D1 to
D6), [`tracker-adapter-port-adr.md`](../design/tracker-adapter-port-adr.md),
[`plugin-kernel-v1-adr.md`](../design/plugin-kernel-v1-adr.md) and
[`CONFIGURATION.md`](../CONFIGURATION.md).

## Question

How does a hive say "this store is served by the GitHub tracker adapter plugin", such that:

1. an **existing** hive config, including today's `stores.public.source: github-issues`, validates
   **unchanged**, and a hive that never opts in behaves byte for byte as it does now;
2. owner and repository come from the **hive identity triplet**, not from a second place to
   mis-type them;
3. the binding adds **no secret**, enables **nothing** by itself, and keeps every write behind its
   separate enablement and per-write grant;
4. the failure modes (unknown adapter, plugin disabled, write capability requested without
   enablement) each have a **named rule, an exact message and a defined place to run**?

Also asked: relate the binding to the `plugins.tracker-github` keys (what lives on the store, what
on the plugin, which layer owns the repository), and list what a **second** tracker would need from
the stores schema, as input for the multi-tracker bead.

Explicitly **not** asked: the port, transport, credential chain or plugin category (settled by the
port ADR and `bh-rktrh`); the conversation model and the write gate's implementation (`bh-wjvq9`);
the design of multi-tracker support itself. This spike sent **no request to GitHub** and changed
nothing under `src/`, `packages/`, `pyproject.toml` or `uv.lock`.

## Method

- **Read the inputs** line by line and recorded `file:line`: the `bh-rktrh` spike (sections on
  keys, ownership gaps C1 to C3, states and diagnostics), the port ADR (sections 5 and 6), the
  plugin kernel ADR (section 6), `docs/CONFIGURATION.md` (the `stores` section), and the stores
  schema in `src/beadhive/modules/config/contracts.py`.
- **Read every consumer of the stores block**, because a new store key is only safe if each one
  tolerates it: `store_target.py`, `github_projection.py`, `tracker_sync.py`,
  `config_validate.py`, `heartbeat_report.py`, `hive_ready.py`, and the ownership table in
  `modules/config/application/partition.py`.
- **Built a prototype that subclasses the real config models** (`ManagedRepoStores`,
  `ManagedRepoEntry`, `BeadhiveConfig`) to add exactly one key, and ran the repository's **real
  `validate_config`** with that model swapped in for one call. The tests compare the shipped and the
  prototype results over a corpus of existing configs (including the `config.example.yaml`
  template, the `tests/fixture_config.yaml` fixture, and the `github-issues` example *parsed out of
  `docs/CONFIGURATION.md` itself*) and over all 48 combinations of the existing public-store axes.
- **Ran the real kernel** (`discover_plugins`, `bind_application_port`) through the `bh-rktrh`
  composition prototype for the composition-root rules.
- **Probed today's code** for the gaps: `validate_config`, `store_target.configured_stores` and
  `partition_of` on a document carrying the new keys. Three tests record what was found.
- 58 offline tests ([`artifacts/bh-6bkun/`](artifacts/bh-6bkun/README.md)); `conftest.py` makes a
  real socket, sleep or subprocess fail the run. Six mutation checks each fail the tests they
  should (see the artifacts' README).

## Evidence

### A. The stores schema today

- **E1. `source` is a closed one-member `Literal`.** `source: Literal["github-issues"] | None`
  (`modules/config/contracts.py:2806`), "Projection source of a `public` store; forbidden on
  `internal`" (rule at `:2920`). Nothing in the schema names a plugin.
- **E2. The stores block lives on the hive entry, which is fleet-owned.** `ManagedRepoEntry.stores`
  (`contracts.py:3029`) is a per-hive field of `managed_repos`; `managed_repos` is a `FLEET`
  prefix (`partition.py:64`), so `partition_of("managed_repos.0.stores.public.adapter")` is `fleet`
  (`test_ownership_of_the_binding_is_fleet_and_the_plugin_namespace_is_unclassified_today`). The
  plugin namespace is the opposite case: `partition_of("plugins.tracker-github.enabled")` is
  `None` (E15 of `bh-rktrh`, re-confirmed), while `hitch`, `orca`, `repowise`, `herdr` are `host`
  (`partition.py:12-52`).
- **E3. The store pair is a fixed two-slot shape.** `STORE_SCOPES = ("internal", "public")`
  (`contracts.py:2761`); `ManagedRepoStores` has exactly the fields `internal` and `public`
  (`:2875-2883`); `store_pair_problems` and `fleet_store_problems` iterate those two
  (`:2893`, `:3083`); `store_target.SCOPES` is `legacy | internal | public` (`store_target.py:53`).
  There is no place for a third store.
- **E4. Identity is already derived from the triplet, by the projection.** `resolve_projection`
  requires `provider == "github"` and a non-empty `org` and `repo` and builds the slug from them
  (`github_projection.py:116-122`), refuses a store whose bd config names a different repository
  (`:237`), and writes the repository to no config. The module docstring states the rule
  (`:10-12`): "derives owner/repository from the hive's own identity triplet ... never written to
  any config". `provider` is documented as the repo-group *path* (`CONFIGURATION.md:312`), but the
  projection already treats it as the provider type; the binding inherits that behaviour.
- **E5. The projection and the port are two independent readers.** Port ADR section 5
  (`tracker-adapter-port-adr.md:268-290`): `bd github sync` stays as is, is not wrapped and not
  replaced; "the port's consumers do not need it". So `source` (which selects the bd projection and
  is checked at `github_projection.py:106` and `tracker_sync.py:58`) and the adapter are two
  switches on two mechanisms. Overloading `source` with a plugin id would switch the projection
  off for exactly the stores that opt in.
- **E6. The schema is closed at every level.** `BeadhiveConfig` and every `_Section` are
  `extra="forbid"` (`contracts.py:113`, `:3146-3150`). `validate_config` reports an unknown key as
  an `error` and a wrong value as `` `dotted.path`: <pydantic msg> `` (`config_validate.py:181-205`).
  A store key unknown to a binary is therefore an error, and the real models reject both new keys
  today (`test_recorded_gap_real_models_reject_the_new_keys`):

  ```text
  unknown config key `managed_repos.0.stores.public.adapter` — not part of schema v1.
  unknown config key `plugins` — not part of schema v1.
  ```

- **E7. A pre-change host fails closed on the key, and not only at validation.**
  `store_target.configured_stores` validates the raw block with `ManagedRepoStores.model_validate`
  and raises `StoreConfigInvalid` (`store_target.py:177-185`). So a host that receives
  `adapter` before it runs a binary that knows the key cannot resolve **any** store of that hive,
  not just the public one (`test_recorded_finding_a_pre_change_host_cannot_resolve_any_store_of_a_bound_hive`).
- **E8. A bad store value is echoed today.** That same `StoreConfigInvalid` embeds
  `str(ValidationError)` (`store_target.py:185`), and pydantic repeats the input
  (`input_value='ghp_...'`). A token pasted into `source` is therefore printed in full. A binding
  key (a name, easily confused with a credential) raises the odds. Setting
  `ConfigDict(hide_input_in_errors=True)` on the stores model removes it
  (`test_recorded_finding_a_bad_store_value_is_echoed_by_the_real_stores_model`).

### B. Where each layer already validates

- **E9. Config load is pure and plugin-blind.** The composition module's own docstring: it
  "intentionally knows only the declarative manifest fields and canonical config models: importing
  this module must never import an optional integration or consult plugin discovery at runtime"
  (`plugin_fragments.py:3-5`). `validate_config` is called by `bh config validate`
  (`cli.py:4234`) and by the heartbeat's `config_valid()` (`heartbeat_report.py:25-28`), which is
  built on `validate_config`'s errors. A binding rule that needed discovery or the environment could
  not live there.
- **E10. `hive ready` already projects plugin state as offline rows.** `_plugin_checks`
  (`hive_ready.py:151-162`) and `_build_verify_checks` (`:165-181`) return `Check(label, required,
  state, detail)` (`:49-53`), appended at `:611-612`; a disabled plugin is `na`/`disabled`, never a
  failure. The build-verify diagnostics are `required` and turn `missing` on an error.
- **E11. The kernel ADR already assigns the pieces.** Core owns `plugin_kernel`; "a plugin owns
  exactly `plugins.<plugin_id>`" and "must not read or write another plugin's subtree or core's
  selection state"; "new top-level plugin-specific keys are forbidden"
  (`plugin-kernel-v1-adr.md:161-166`). A plugin namespace that listed hives or stores would make the
  plugin read the registry, which is outside its subtree.
- **E12. Ownership is process-wide.** One enabled provider per `capability@major`
  (`bh-rktrh` E4 and D5). Adapter *instances* are per store; the *owner* is per process.

### C. What `bh-rktrh` left open for this bead

- **E13.** Its key table has `repo` (`owner/name`) under `plugins.tracker-github`, with the note
  "Deriving it from the hive is `bh-6bkun`" (`bh-rktrh-tracker-plugin-category.md:442`), and its
  effective-enablement formula includes "`repo` set" (`:460-469`). A plugin namespace holds **one**
  value, so one process can serve one repository; the daemon and the MCP server serve many hives.
- **E14.** Hive-level selection and per-hive overrides were deferred here (`:534-535`).
- **E15.** Its status states include `unconfigured (enabled, repo unset)` (`:551`).

### D. Prototype results

All 58 offline tests pass on Python 3.12.13:

- **Existing configs.** Eleven documents: the template, the fixture (it lacks `schema_version`, so
  it is invalid today), a legacy hive, an internal-only hive, today's `github-issues` public
  store, the `CONFIGURATION.md` example, a public store with no `source`, a fork with host
  sections, and three more configs that are **invalid today**. For each, the real `validate_config`
  and the same function with the prototype model return **identical problem lists** (same level,
  message, order). For the seven that validate, `model_dump(exclude_none=True)` is also identical.
  For all `3 x 4 x 2 x 2 = 48` combinations of `authority`, `sync`, `source` and `dispatch` on a
  public store the results are identical too. The prototype adds exactly one field, `adapter`,
  which defaults to unset.
- **Rules fire** with the messages in the next section, at the tier that owns them, against the
  real kernel for the composition-root ones.
- **Layering.** One host-owned plugin config serves two hives, and each gets the repository of its
  own triplet (`test_each_hive_gets_its_own_repository_from_one_plugin_namespace`).

## Verdict — **GO**

A store can be bound to the GitHub plugin by **one optional string key, `stores.public.adapter`,
naming the plugin id**, with the repository derived from the hive identity triplet and everything
else on the plugin namespace. The enabler is that the schema is already closed and additive: a new
optional key with no default changes nothing for a document that omits it, which the prototype shows
over the full existing-config corpus and an exhaustive store-axis matrix, using the repository's own
`validate_config`.

Three things are **not** free, none of them blocks the verdict, and the implementation molecule
must own them: the schema must learn the key (and `plugins`/`plugin_kernel`, `bh-rktrh` C1) before
any fleet config carries it (E6, E7); the stores models should stop echoing input values (E8);
and `bh-rktrh` must be amended so the repository is no longer a plugin key (E13).

## Recommendation

### 1. The config shape

```yaml
# Fleet layer (managed_repos is fleet-owned): intent only. Nothing here enables anything.
managed_repos:
  - provider: github
    org: beadhive
    repo: beadhive              # owner/repository for the adapter come from these three keys
    prefix: bh
    stores:
      internal: {remote: beadhive-private}
      public:
        prefix: bhpub
        path: ~/.beadhive/stores/github/beadhive/beadhive/public
        remote: git-origin
        source: github-issues   # unchanged: selects the bd projection (and the tracker kind)
        adapter: tracker-github # NEW, optional: the plugin id that serves this store through
                                #   the tracker.adapter@1 port. Absent = today.

# Host layer (plugins.tracker-github is host-owned, bh-rktrh C2): the operator's switches.
plugins:
  tracker-github:
    enabled: true               # default false; a binding never sets this
    read: {issues: {enabled: true}}
    # write.comment.enabled / write.labels.enabled stay false until separately turned on
```

| Question | Owner | Where | Default |
|---|---|---|---|
| Which tracker kind does this store mirror | the store (fleet) | `stores.public.source` (existing) | unset |
| Which plugin serves it | the store (fleet) | `stores.public.adapter` (**new**, plugin id) | unset = unbound |
| Which owner and repository | the **hive identity** | `provider` / `org` / `repo` of the entry (existing), **derived** | n/a |
| Is the plugin on | the plugin (host) | `plugins.tracker-github.enabled` | `false` |
| Which operations, `base_url`, credential source names, retries, cache | the plugin (host) | `plugins.tracker-github.*` | all off |
| Credentials | nowhere in config | ambient, injected token-source port | never stored |
| Write approval | nowhere in config | a runtime single-use grant per write | none exists |
| Which plugin owns the capability when several are enabled | the kernel (host) | `plugin_kernel.capability_owners` | none |

Rules of the shape:

- **`adapter` is a plugin id, nothing else**: lowercase letters, digits and hyphens, at most 64
  characters. It cannot hold a URL, repository, token or option, and the validation message never
  repeats a value that fails the shape (a pasted secret is rejected without being printed).
- **The binding is intent, the plugin flag is authority.** Because `managed_repos` is fleet-owned
  and `plugins.tracker-github` host-owned, a fleet push can declare "this hive's public store is
  served by `tracker-github`" but can never turn the plugin, a read or a write on. A host without the
  package, or with the plugin off, validates the same document and ignores the binding.
- **`repo` leaves the plugin namespace** (amends `bh-rktrh` section 4, E13). The slug is
  `org/repo` of the hive entry whose store is bound, under the projection's existing rule that
  `provider` is `github` (E4). One host-owned plugin config then serves any number of hives, and
  there is no second place to mis-type the repository or to let it disagree with the projection
  (which already refuses a mismatch, `github_projection.py:237`). The effective-enablement
  formula becomes, per store: `plugin.enabled AND kernel-enabled AND the store binds this adapter
  AND the identity resolves AND <the operation's own flag>`. The `unconfigured` state becomes "no
  store binds this adapter".
- **No per-hive override of the plugin's flags.** Existing integrations have per-hive sections
  (`contracts.py:3015-3023`), but for the tracker the *binding set* is the per-hive selection: an
  unbound hive can neither read nor write. Write enablement stays a host-wide ceiling. A hive-level
  override can be added later without changing this shape.
- **Requested operations are a runtime request, not a store key** (rule 5 below). A store-level
  `operations:` list was rejected: it would duplicate the plugin's flags in a second layer (two
  places to flip) and a list of writes in fleet config reads like a pre-approval.
- **The projection is untouched.** `bh hive remote sync` and `github_projection` keep reading
  `source` only and never consult the adapter (E5). `adapter` without `source` is rejected so a bound
  store always also names the tracker it mirrors.

Alternatives rejected:

| Alternative | Why not |
|---|---|
| Widen `source` to also accept a plugin id (`source: tracker-github`) | `source` selects the bd projection and is checked by it (E5); overloading it turns the projection off for opted-in stores and cannot say "project but do not intake" or the reverse |
| Implicit binding: `source: github-issues` plus an enabled plugin means bound | Enabling a host plugin would silently bind every hive with a public store and start network use fleet-wide; violates "off unless configured" |
| List the bound hives under `plugins.tracker-github` | The plugin would read the hive registry (E11), the namespace is host-owned while the binding is a fleet property, and it duplicates identity |
| Keep `plugins.tracker-github.repo` as the only repository | One value per process (E13); cannot serve a multi-hive daemon |
| An object value (`adapter: {plugin: ..., ...}`) | Premature; the second-tracker needs below are additive sibling keys, so a scalar now costs nothing later |

### 2. The validation rules and exact messages

Messages are exactly what the tests assert. Config-load messages are shown as `validate_config`
renders them (pydantic prefixes "Value error," and the location); the store and hive name appear
only where the tier has them.

| # | Rule | Tier | Severity | Exact message |
|---|---|---|---|---|
| R1 | **Unknown adapter** | config load | error | `` `managed_repos.0.stores`: Value error, stores.public: unknown tracker adapter 'tracker-gitlab' — known adapters: tracker-github `` |
| R2 | Malformed adapter value | config load | error | `` `managed_repos.0.stores.public.adapter`: Value error, adapter must be a plugin id (lowercase letters, digits and hyphens, at most 64 characters) `` (value **not** echoed) |
| R3 | Adapter without a source | config load | error | see the block below |
| R4 | Adapter on the internal store | config load | error | `` `managed_repos.0.stores`: Value error, stores.internal: adapter is only valid on a public store `` |
| R5 | Adapter serves another source | config load | error | `stores.public: adapter 'tracker-github' serves source 'github-issues', not 'jira-issues'` (unreachable while `source` has one member; the rule a second source activates) |
| R6 | **Plugin disabled** | composition root; `hive ready` row | error at the root when a consumer asks; **info** in `hive ready` | `github/acme/widget: stores.public binds tracker adapter 'tracker-github' but the plugin is disabled — set plugins.tracker-github.enabled: true on this host (a binding never enables a plugin)` |
| R7 | **Write capability requested without enablement** | composition root | error | `github/acme/widget: write comment.create was requested for stores.public but is not enabled — set plugins.tracker-github.write.comment.enabled: true (enabling a write is necessary, never sufficient: each write still needs an operator-approved single-use grant)` |
| R8 | Read capability requested without enablement | composition root | error | `github/acme/widget: capability comments.read was requested for stores.public but is not enabled — set plugins.tracker-github.read.comments.enabled: true` |
| R9 | Adapter package absent | composition root; `hive ready` row | error at the root; **info** in `hive ready` while the plugin is off, **error** once it is on | see the block below |
| R10 | Another plugin owns the capability | composition root | error | `... binds tracker adapter 'tracker-github' but 'tracker-other' is the selected owner of tracker.adapter@1` |
| R11 | Two enabled providers, no owner selected | composition root | error | `github/acme/widget: two enabled plugins provide tracker.adapter@1 — select one with plugin_kernel.capability_owners` |
| R12 | Identity does not resolve | composition root; `hive ready` row | error at the root; **warning** in `hive ready` | `gitlab/acme/widget: cannot derive a GitHub repository — the hive provider is 'gitlab', not 'github'` or `github/acme/: the hive identity names no owner/repository` |
| R13 | Store binds nothing (today) | composition root | not an error: the consumer's "tracker unconfigured" | see the block below |

The three messages that contain backticks, verbatim:

```text
R3  `managed_repos.0.stores`: Value error, stores.public: adapter 'tracker-github' needs `source: github-issues` — a bound store must name the tracker it mirrors
R9  github/acme/widget: stores.public binds tracker adapter 'tracker-github' but no discovered plugin has that id — install `beadhive[github]`
R13 github/acme/widget: stores.public binds no tracker adapter — declare `stores.public.adapter` to opt in
```

Notes:

- **R6 is the same rule the three disabled forms trigger**: an absent flag, `false`, a non-boolean
  such as `"true"`, and `plugin_kernel.enabled.tracker-github: false` (the kernel still wins). Only
  an exact `True` enables, as in `bh-rktrh` E17.
- **R7 and R8 fire from the consumer's request**: the composition call takes `requested=` (the
  operations the caller is about to use) and compares it with the operations the plugin has enabled.
  A write that is not enabled therefore fails *before* any approval prompt, and an enabled write
  still needs its grant (`bh-rktrh` section 4, five layers). Reads and writes are independent: turning
  `write.comment` on enables no read (`test_enabling_a_write_is_independent_of_reads_and_adds_only_its_writer`).
- **Order at the root**: not bound, then identity, then installed, then conflict/owner, then
  disabled, then requested operations. The first failing rule is reported, so the operator fixes one
  thing at a time and never sees a write complaint about a plugin that is off.
- **No message contains a credential value.** Values appear only as plugin ids, operations, keys
  and the hive triplet (`test_no_message_repeats_a_credential_shaped_value`).

### 3. Where validation runs

| Tier | Runs in | Inputs | May do | Rules |
|---|---|---|---|---|
| **T1 config load** | `validate_config` (`bh config validate`, the heartbeat's `config_valid`), and any load of the stores block | the document only | pure checks; no discovery, environment, network or plugin import | R1 to R5 |
| **T2 `hive ready` / `doctor`** | one optional row beside `_plugin_checks` (`hive_ready.py:151`), and the `doctor` section | document plus offline environment (manifest catalog, the enabled flag, package present) | read-only, **no network**, never fails while the plugin is off | R6, R9, R12 as info, info/error, warning; ready row with the bound repo and enabled operations |
| **T3 composition root** | the root's `bootstrap/tracker.py` (`bh-rktrh` section 1), called by a consumer | T1/T2 inputs plus discovery and `bind_application_port` | fail closed with a typed error; lazily import the package only after selection | R6 to R13 |

Why not earlier or later: R1 to R5 depend on nothing but the document, so they must run at load
(a typo is caught on `bh config validate` and on a fleet push, not at the first intake). R6 to R12
depend on the host (is the plugin on, installed, the owner), so putting them at load would make one
fleet document valid on one host and invalid on another, and would make an off-by-default plugin
fail every other host's gate. They run at T2 as information and at T3 as refusals. `plugin_fragments`
may not consult discovery (E9), which is the reason the known-adapter table is a closed static table
in the config layer, exactly as `source` is a closed `Literal` today.

**The known-adapter table** `{adapter id: source it serves}` has one row, `tracker-github:
github-issues`. A drift test ties each row to a built-in manifest that provides `tracker.adapter`
and whose namespace is `plugins.<id>` (`test_the_known_adapter_table_matches_the_builtin_manifest_shape`;
`bh-rktrh` P3 extends it). Accepted cost: a third-party adapter plugin (kernel entry points are off
by default) is unknown at T1 until the table learns it, which is a schema release, like a new
`source`.

### 4. What a hive without the plugin keeps, and what migrates

- **Nothing migrates.** The key is optional with no default; no document is rewritten; no
  `schema_version` bump is needed (`SCHEMA_VERSION` is 1 and the stores block already lives in
  it). The existing
  corpus, including `stores.public.source: github-issues`, validates and dumps identically (Evidence
  D). The bd projection, `bh hive remote sync` and `store_target` behave as now.
- **Without the plugin installed or enabled**, a bound document still validates (R1 to R5 need no
  plugin), `hive ready` shows one `info` row, and no code imports the package. Installing the
  package (`beadhive[github]`) never enables it, and enabling never installs (kernel ADR section 7).
- **The one real hazard is rollout order** (E7): the key must not reach a host whose binary does not
  know it, because that host fails to resolve even the internal store of the bound hive. So
  (a) the schema bead ships and is rotated to every host first; (b) only then does any fleet config
  declare `adapter`; (c) until then the key is documented but unused. Nothing in the repository
  declares it by default.

### 5. Relation to the `plugins.tracker-github` keys (amendments to `bh-rktrh`)

| `bh-rktrh` item | Change |
|---|---|
| Key `repo` | **Removed.** `extra="forbid"` rejects it (`test_there_is_no_repo_key_on_the_plugin_namespace`) |
| All other keys (`enabled`, `base_url`, `credential.sources`, `read.*`, `write.*`, `retry.*`, `http_cache.*`) | Unchanged, still off by default, still host-owned |
| Effective enablement | Per store, as in section 1 |
| Factory `build(settings, token_source, *, sleep)` | Gains the bound project: `build(settings, token_source, *, project: ProjectRef, sleep)` with `ProjectRef("github", "owner/name")` from the port contract. The prototype injects it into the settings mapping only because the `bh-rktrh` stub reads `settings["repo"]` |
| One adapter per hive per process | One adapter per **(hive, store scope)** per process, cached by the root; the grant ledger stays the consumer's |
| `status` state `unconfigured` | "no store binds this adapter"; `status` additionally lists the bound stores (`hive` and the derived slug) from the registry, offline |
| C2 ownership | Unchanged and now load-bearing: `plugins.tracker-github` **must** be host-owned, otherwise a fleet push could enable a write on every host. The binding itself is correctly fleet-owned |
| D5 process-wide owner | Still accepted; one tracker plugin per process (see section 6, S5) |

### 6. The stores-schema change a second tracker would need (input for the multi-tracker bead)

Listed, not designed. Everything in 1 to 4 is additive, so none of these changes the shape above.

| # | Gap | Evidence | What the multi-tracker bead must decide |
|---|---|---|---|
| S1 | `source` is a one-member `Literal` | `contracts.py:2806` | Widen it (or turn it into a validated open string) and turn the known-adapter table into `source -> adapter ids`, still drift-tested against manifests |
| S2 | Identity is derived only for GitHub | `github_projection.py:116-122`; the port's `ProjectRef(tracker, locator)` already models an opaque project address | Jira project key, Linear team, GitLab nested group path and self-hosted hosts cannot come from `provider/org/repo`. Add an optional per-store `locator` (and, if needed, a host) as an **additive sibling key**; for GitHub it stays absent and derived. Generalise R12 to a per-source identity rule |
| S3 | The store pair is two fixed slots | `contracts.py:2761`, `:2875-2883`, `store_pair_problems`, `fleet_store_problems`, `store_target.SCOPES`, `tracker_sync.select_tracker_store` (resolves only `public`) | A second tracker for **one hive** needs a place for another remote-authoritative store (an extra named-store mapping, or `public` becoming a list), plus the resolution, uniqueness (prefix, path, remote) and sync-selection code that currently assumes one |
| S4 | Per-store non-identity options | plugin namespaces are process-wide (`plugins.<id>`) | Where a store-specific setting (a Jira board, a label scope) lives: a per-store options key, or `plugins.<id>` keyed by store prefix. Neither exists |
| S5 | Capability ownership is process-wide | `bh-rktrh` E4, D5 | Two different tracker plugins in one process need the kernel to select by more than `capability@major` (for example by binding). That is a kernel change, not a schema change, and the schema should not paper over it |
| S6 | Fleet uniqueness of the mirrored project | `fleet_store_problems` checks prefix, path, remote | Add "no two stores mirror the same `(adapter, locator)`" once a locator exists |
| S7 | Stores models echo input in errors | E8 | Fix once, for all stores keys, before more name-like keys are added |
| S8 | `plugins` / `plugin_kernel` are not in the schema | `bh-rktrh` C1 | Prerequisite for any plugin-bound store, and already scheduled |

`authority: remote` and `sync: pull-only` do **not** change for a second tracker: they describe the
Dolt replica of the mirrored rows, not whether the adapter may write to the tracker. Writes go through
the port behind a grant and never through store sync.

### 7. Suggested bead sequence (for the planner; this spike files nothing)

1. **Config schema** (the `bh-rktrh` config bead, extended): `plugins` and `plugin_kernel` in
   `BeadhiveConfig`; `TrackerGithubConfig` (no `repo`) and its fragment entry; `adapter` on
   `StoreDescriptor`; the known-adapter table with R1 to R5 in the stores validators;
   `hide_input_in_errors` on the stores models; host-ownership for `plugins.tracker-github` and
   `plugin_kernel`; regenerate the published config schema
   (`src/beadhive/schemas/contracts/v2.0.0/artifacts/config-v1.schema.json:3129` carries the `source`
   const) and the wire artifact. **Ships before any config uses the key.** The regression test is
   this spike's `test_binding.py` corpus and 48-combination matrix, run against the real model.
2. **Composition** (`bh-rktrh` bead 4): `resolve_store_adapter` with R6 to R13 and the per-store
   adapter cache.
3. **Readiness**: the `hive ready` / `doctor` row and the status listing of bound stores.
4. **Rotation, then use**: only after every host runs the new schema may the first fleet config
   declare `adapter` (section 4).

### 8. Decisions requested (`bh-h7nw7`)

- **B1.** The binding key is `stores.public.adapter`, a plugin-id string, requiring `source`, public
  stores only.
- **B2.** The repository is **derived from the hive identity triplet**; `plugins.tracker-github.repo`
  is removed.
- **B3.** The binding is fleet-owned intent; `plugins.tracker-github` is host-owned authority; a
  binding never enables a plugin.
- **B4.** Validation runs in three tiers (R1 to R5 at load; R6, R9, R12 informationally at
  `hive ready`; R6 to R13 as refusals at the composition root).
- **B5.** Requested operations are a runtime request from the consumer, not a store key.
- **B6.** The known-adapter table is a closed static table in the config layer, drift-tested against
  the manifests; a new adapter id is a schema release.
- **B7.** No `schema_version` bump; rollout order per section 4.
- **B8.** The stores models hide input values in errors (E8).

### 9. Risks and what was not verified

- The prototype **subclasses** the real models; the shipped edit is the schema bead's. A subclass
  proves the new key is additive and unobtrusive, not that the real edit has no side effect on the
  published JSON schema, which the schema drift tests will check.
- `bh config validate`, the heartbeat and `hive ready` were read, not run; `validate_config` and
  `store_target.configured_stores` were run directly. The `hive ready` row is a function returning
  data, not wired into `hive_ready.py`.
- The composition rules use the `bh-rktrh` stub adapter, not a GitHub one; no request was made.
- `provider` is the repo-group path, not necessarily the provider type (`CONFIGURATION.md:312`). A
  hive whose group path is not `github` is refused (R12), as the projection already does. A fork
  (`kind: fork`, `upstream`) derives from its own triplet, not `upstream`; whether a fork should read
  its upstream's issues is undecided and out of scope.
- Wire/schema artifact regeneration and the published `config-v1` change were read
  (`schema_artifacts.py`), not run.
- Multi-hive, multi-process composition (the per-store adapter cache) is specified but untested.
- Python 3.12.13 only; the project's declared range is 3.11 to 3.13.
