# Current HQ seed inventory and ownership

Date: 2026-10-01  
Decision bead: `bh-wd7wr` — Inventory current HQ seed and reconcile fleet versus host ownership  
Parent: `bh-f964m` — Dolt-first HQ config cutover seeded from the current fleet  
Status: inventory complete; snapshot capture and authority switch require implementation

This record supplements [the cutover ADR](dolt-first-hq-config-cutover-adr.md).
Inspection used the configured HQ working directory and the actual host and hive files;
it performed no source, identity, admission, database, or remote writes. The committed
[evidence index](dolt-hq-seed-evidence.md) contains hashes and ownership paths only,
not raw config, public-key entries, credential references, or private repository names.
Source IDs in that index are numbered within lexically sorted source categories. An
access-controlled migration manifest must retain their exact path/identity mapping.

## Verified baseline and discrepancies

The HQ HEAD remains `5a2015cd94c93df7b917f5847f366c52786f1105`. Only `fleet.yaml`
is dirty. All six previously recorded source hashes match the cutover ADR. Fleet has
27 unique canonical managed IDs, two host manifests and 27 hive observations; both
manifest models validate. Effective config loads and validates with the current canonical
`BeadhiveConfig` model. These checks prove readable input, not trust or readiness.

Catalog membership and observations differ despite equal counts. `local/factory/hq`
is registered without an observation; one observation is outside the current catalog.
Keep HQ's registration, its `hq-` Beads identity and independent durable state. Quarantine
the orphan observation as historical evidence; it cannot create a registration, attachment,
lease or admission. The exact orphan ID belongs in the restricted reconciliation report,
not this public summary. Missing HQ schema observation is a missing measurement, not a
reason to remove HQ or invent a schema version. No duplicate managed IDs were observed.

`work.identity.name` and `work.identity.email` appear in both HEAD and the dirty fleet
working copy with unchanged values. Neither exists in the host config. During migration,
move them into the source host's config before excluding them from fleet import; compare
resolved identity before and after, including environment/invocation layers. Other hosts
retain their own identity. If a host now supplies a different explicit value, retain both
inputs as a conflict and require a named reconciliation choice before apply. Do not infer
that all dirty changes are identity changes, or discard the working diff.

The HQ workspace is an input to inventory but is not the effective provider source here.
`gitworkspace.config_paths(config.load())` resolves two external host workspace files.
Both have separate hashes in the evidence index. Precedence is explicit host path, then
external workspace files, then HQ workspace files; the first nonempty layer wins without
union. Use the current effective provider projection from both selected files as the initial
central desired workspace seed. Preserve provider/group `path`, transport type, filters and
public non-secret authentication metadata; exclude actual host filesystem roots, runtime
locations and credentials. Keep source-host external files and override precedence locally.
Compare/archive the seven-block HQ fallback against the six effective groups, recording an
explicit resolution for every divergent definition before apply. Do not import two central
truths or choose the stale lower-precedence fallback for fresh enrollment. Require semantic
provider parity both for the source host and for a fresh host reading only central definitions.
Capture exact selected file paths, hashes, source order and reconciled projections in the
restricted manifest. `hq._workspace_toml()` currently copies only the first selected file,
while effective workspace resolution uses two; migration must consume all selected sources
rather than reuse that scaffold helper and lose the second file's groups.
Provider group path is distinct from provider transport type: `contrib` can use GitHub.
Never rewrite canonical `provider/org/repo` IDs by guessing their transport.

## Content-addressed migration manifest contract

`bh-4shu3` captures a new snapshot at migration time. This inventory is not that snapshot.
Use a restricted directory outside the product repository with directory mode 0700 and
files mode 0600 (or equivalent broker/object-store ACLs); encrypt backups and do not attach
raw artifacts to issues, reviews, logs or telemetry. Never resolve credential references
while capturing. If source bytes contain inline credentials, refuse and classify the entry;
do not put those bytes in the migration artifact. Restricted host-only provenance may
record permitted non-secret references, never credential-file or private-key contents.

The manifest format is `beadhive.hq-seed-manifest.v1` and requires:

| Field | Required contents |
| --- | --- |
| `source` | Configured source kind, normalized paths, HQ Git HEAD, symbolic branch, porcelain dirty/staged/untracked inventory, capture time and schema/policy code revision |
| `files[]` | Exact relative/source path, category, byte length, SHA-256, source file mode, existence/type, Git blob at HEAD/index when present and working-copy provenance |
| `canonical_hives[]` | Exact provider/group, org, repo, canonical triplet, prefix and kind; source row/path, registration/observation disposition and non-secret binding identity |
| `fields[]` | Every scalar/list leaf and opaque extension: source path, ownership, contract/schema owner, disposition, target layer, value-free provenance and explicit conflict resolution |
| `workspaces[]` | All HQ and effective workspace source paths/hashes, resolver precedence, selected layer and canonical provider-group projection |
| `authority` | Desired registration/public signer source versus observation; explicit legacy compatibility decision, no inferred admission or rejuvenated lease |
| `checks` | Validation codes/paths, uniqueness/path-identity checks, conflicts, source stability, before/after canonical config/workspace/identity/database parity |
| `destination` | Explicit backend/schema identity, expected committed revision, empty/nonempty policy, import revision and restricted snapshot location |

Canonical JSON means UTF-8, sorted object keys, compact separators, arrays ordered by the
contract's stable source path or canonical ID, no NaN/Infinity and no implicit timestamps.
Define the manifest ID as `sha256:` plus the SHA-256 of canonical manifest bytes excluding
its own `manifest_id`; capture time is part of those bytes. Raw source files are independently
addressed by byte hash, preserving comments and dirty content. Keep canonical semantic
projections separately to compare resolved values without pretending YAML bytes equal SQL.
Hash the canonical sorted hive-ID array; the evidence index records this initial digest.
Never hash secret values for publication or put them into semantic projections.

Capture hashes and Git state before reading, read/validate/classify the selected files,
then rehash and recapture Git state after. Any changed, newly missing, additional or replaced
source, changed source selection, unsupported version, unresolved field or duplicate/path
identity is a refusal with a redacted code/path. Freeze writers for final capture/apply;
recompare manifest ID and expected destination revision immediately before transaction.
A lost race aborts; recapture/review a fresh snapshot, never reuse the earlier evidence.
Idempotency keys bind destination schema and manifest ID, not just a timestamp or HEAD.
Expected-revision publication and post-switch readback prevent overwriting concurrent SQL edits.

## Per-field ownership and refusal decisions

The evidence index enumerates all 287 observed fleet leaves and each host config leaf,
without values. Core ownership derives from
`modules/config/application/partition.py` using longest-prefix classification. Registry
item descendants inherit `managed_repos`' fleet container ownership; numerical list indices
are storage locations, not schema keys. Validate each per-hive contextual override with its
own contract as well. Do not classify Beads configuration with the Beadhive partition table.

| Source/fields | Ownership and disposition |
| --- | --- |
| Fleet `schema_version`, delimiter, providers, orgs, exclude, dimensions, passthrough | Fleet; preserve current schema, shape and source provenance |
| Fleet `managed_repos.*` | Fleet catalog and contextual hive policy; preserve canonical IDs, prefix/kind, upstream/contribution intent and supported extensions |
| Fleet `work.*` except host-specific prefixes | Fleet scheduling/routing/signing/validation policy; preserve supported aliases through canonical validation |
| Fleet `work.identity.*` | Source-host ownership; staged local relocation, excluded from central projection |
| Fleet `worktrees.*` except `worktrees.path` | Fleet behavior; retain host `worktrees.ephemeral=false` escape hatch only when allowed by existing policy |
| Host `dolt.backend`, `hq.remote`, `work.validation_slots`, `host.daemon.enabled` | Host bootstrap/runtime settings; retain locally, do not copy host endpoint/remote semantics into central fleet policy |
| Host `host.daemon.auth.credential_file` | Host-local non-secret credential reference; preserve locally, never read/upload file content |
| Workspace provider/name/path/include/exclude/filter fields | Fleet desired seed from all currently selected effective sources, reconciled with HQ fallback; preserve group path/filters/non-secret transport metadata and local external overrides |
| Host-manifest `host_id`, frame identity, role/state, public identity mechanism | Fleet-visible inventory; operator authority must approve lifecycle/admission separately; preserve legacy spelling compatibility |
| Host-manifest capacity, harnesses, OS/arch, remote-only hive placement | Host-reported or host-specific intent; preserve provenance/ownership without promoting to global settings or trusted fresh capacity |
| Hive-manifest provider/org/repo | Observation identity joins a catalog item only after validated exact path/ID match |
| Hive-manifest schema/version/mode, observed time/host/bd version | Historical observation; preserve source/freshness, do not turn into trusted admission, live health or database availability |
| `allowed_signers` entries | Public operator-controlled trust policy; import independently verified principals/keys/namespaces and retirement state, no private-key reads |
| Hive `.beads/config.yaml`: sync.remote, external_projects | Hive-owned supported replication/project references; preserve using Beads contracts, classify credentials separately |
| Hive Beads export.auto/path/git-add and backup.enabled | Existing hive/host operational behavior; preserve locally unless an explicit supported migration changes it |
| Hive Beads dolt.shared-server | Host engine attachment choice; not proof of remotely reachable central HQ |
| Hive `.beads/metadata.json`: database/backend/dolt_mode/dolt_database | Beads-owned attachment metadata; preserve database `bh`, prefix/history/IDs; classify logical binding versus local engine paths |

The currently inspected host has no competing identity leaves. Typed fleet config and all
host/hive manifests validate; a legacy host role emits a deprecation warning but resolves
through existing compatibility policy. These observations do not bless future invalid
entries. Unknown ownership, unsupported extension versions, malformed canonical ID,
path/ID disagreement, duplicate ID/prefix, inline secret or identity conflict blocks apply.
Preserve unresolved bytes as restricted source evidence where safe; an explicit decision
must either supply a supported owner/contract, retain inactive data, or remove it in a
separate authorized change. No default owner, last-write winner or silent omission.

## Consumers, writers and bootstrap dependencies

These are the current implementation seams to migrate or prove compatible. Shared-Dolt
config must replace authority behind the existing config module ports, not duplicate its
resolver, model, secret policy or caches. Searches covered direct config/HQ/workspace/signer
paths and their immediate collaborators; implementation must rerun the inventory at its
code revision and close indirect callers and filesystem/tool consumers as well.

| Surface | Read/write responsibility and cutover obligation |
| --- | --- |
| `config.py`, `config_store.py`, `config_edit.py`, config module YAML adapter/resolver | Fleet load/save/dotted edit, provenance, cache, reconcile and transactional writes; backend/revision cache identity and expected-revision atomicity |
| `config_policy.py`, `config_split_migration.py`, CLI/config validation | Missing-fleet/remote detection, split/scaffold/migration and editor scope; Dolt availability must not depend on a checkout existing |
| `registry.py`, hive add/init/rm/onboard | Managed registry and fleet save; retain canonical HQ special case and supported hive bindings |
| `hq.py` init/clone/scaffold/push/status/query/intake | Current Git config layout/publication plus HQ-origin Beads; separate central attach/config status from Git compatibility and bead durability |
| `gitworkspace.py`, `host_answers.py`, `host_provision.py` | Provider selection and workspace materialization, link for external binary, provision ordering and database hydrate; capture all selected files despite HQ scaffold first-file loss, preserve local precedence and central fresh-host parity |
| `hosts.py`, `host_cli.py`, `host_retire.py`, `host.py` | Host manifest read/write/delete, enrollment/retirement and local stable ID/private signing reference; backend publication with unchanged host-local identity |
| `hive_schema.py`, hive ready/doctor | Schema observations and freshness; historical observation store remains separate from desired config and trusted authority |
| `git_identity.py`, `identity.py`, `worktree_git.py`, `work_logic.py` | Public signer enrollment/lookup and commit verification; versioned policy materialization for Git tooling still required even when authority is SQL |
| `guard.py`, `host_lease.py`, host lifecycle/localloop | Primary/adopt/renew/release/takeover through HQ refs/caches; preserve membership port authorization, replay floors and compatibility rather than importing old refs as fresh leases |
| `hive_sync.py`, `sync_remote.py`, engine/Beads gateway | Source/bead publication and hydration remain distinct from central config; avoid double publication and preserve per-hive remotes |
| daemon HQ probe/factory/contract/network/auth/telemetry/OpenAPI | HQ availability and bootstrap/config status, host-local daemon auth and paths; status must expose backend/revision/age without raw source data |
| state stream polling, operator API, frame bridge/upstream/runtime, Herdr services | Derived fleet/control-plane views and readiness; prove revision propagation and consistent authoritative versus derived status |
| `hq_restore.py`, `hq.py` backup, `publish_export.py` | Preserve HQ-origin beads, non-secret config/public policy and authoritative replay state with credential/grant recovery; stop assuming config must be restored into Git layout |
| scheduled HQ sync, operator scripts, git-workspace/SSH signing tools | Outside-process readers/writers must consume explicit materialized revision or selected backend; keep Git compatibility isolated and block retirement until enumerated |

A fresh host must have central backend selector, network endpoint/schema identity, TLS/CA
trust, broker address/authentication and secret references before fetching central config.
It also needs local stable identity/private-key custody, supported Beads executable/engine
attachment, desired hive database/prefix/remote catalog, selected workspace projection,
public trust policy and bootstrap cache rules. Product repository authentication remains
separate. No localhost/legacy port, current shared-engine selection or HQ remote URL is a
usable remote enrollment endpoint by inference. Runtime SQL identity cannot write operator
config/admission; qualify endpoint/grants before deploying to the central shared server.

## Existing-work reconciliation and retirement gate

`bh-cem7l`'s membership-only ADR remains accepted historical intent. This authorized cutover
expands configuration authority without mixing application tables into Beads-managed schemas.
`bh-dd7z0` supplies the membership port, `bh-v0k3i` the SQL binding; the config implementation
`bh-9ej9n` consumes both the binding and this inventory. `bh-4shu3` owns actual snapshot/import,
parity and switch; `bh-b5urp` owns fresh-host/executor proof. Inventory does not close these.

HQ sync is reconciled with the existing hive-sync unification design: central config reads
and writes need one backend-aware collaborator, while source and Beads replication retain
their supported transports. `bh-pc2a.30` SSH-only HQ transport remains relevant to retained
Git mode. `bh-b1scz` S3 remotes catalogs desired per-hive durability, without adopting S3
compatibility. `bh-ukit`'s local shared-engine decision and `bh-h506s` endpoint cleanup
remain separate from remote central authority. `bh-t2eww`'s central daemon topology spike
is not an adopted event bus. Hydration remains through supported Beads operations, not
application SQL writes or an invented schema copied from observations. Preserve HQ-origin
beads and `local/factory/hq` until its independent durable channel is proven.

After config cutover, Git HQ can be labeled a historical snapshot only after stopping its
config writers and showing no active config consumers. Actual repo retirement additionally
requires intake/escalation/HQ bead replication, workspace materialization, signing tools,
lease compatibility, backup/restore and external jobs to have independent supported paths.
`bh-zg0lp` owns that readiness proof; a stale config repo is not proof those channels are gone.

## Validation evidence

Read-only inspection compared hashes with the initial ADR, checked HQ HEAD/dirty paths,
loaded/validated effective config, validated both host manifests and all 27 hive records,
checked unique canonical IDs and catalog/observation set difference, classified fleet/host
leaf ownership and resolved selected workspace sources. Restricted source data were not
copied or modified. The documented manifest protocol is an implementation requirement;
this bead adds no migration executable or tests that pretend a production switch occurred.

The initial JSON evidence packaging selected a broad managed gate. Its terminal submit
result was red: `tests/test_validation_concurrency_proof.py::test_forced_submit_owner_death_reaps_child_checkout_record_and_locks`
timed out waiting ten seconds for active validation count to reach zero after owner death.
The stateful run recorded 8,899 passed, 12 skipped and one failed; this documentation change
does not establish the failure's cause. Keep that failure as a delivery qualification input,
not a green full-suite claim. The final evidence is documentation in fenced JSON; lifecycle
validation chooses its required route from the final scope without an override.
