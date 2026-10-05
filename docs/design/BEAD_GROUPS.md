# Bead groups: a north star for project issue collections

**Status:** Long-term conceptual proposal, 2026-10-04. This captures the desired direction
for Beadhive; it is not an implemented contract or an accepted upstream Beads design.

**Reader:** Beadhive maintainers and potential upstream collaborators. After reading, they
should be able to distinguish project identity, bead ownership, storage, access, and sync,
and assess an implementation against the proposed boundaries.

Related context: [current hive model](../HIVES.md), [current project design](../DESIGN.md),
and [current aggregate hub](../HUB.md).

## 1. North star

### A hive identifies the source project

A **hive** (or rig) identifies one source project with a canonical Git location. This
location is the project's **source origin**: the nexus from which code is cloned or forked,
and to which proposed source changes are submitted for integration into the main branch.

This is a logical origin, not necessarily the remote named `origin` in every checkout.
A contributor's checkout often calls their fork `origin` and the canonical project
`upstream`. The hive must identify the canonical project independently of those local names.

A source origin may be public or private. The default bead-storage mode publishes project
beads through `refs/dolt/data` on that origin, inheriting the repository's access policy.
This is a convenient default, not a requirement that source and issues share permissions.

An alternative mode detaches bead storage from source hosting entirely. The authoritative
bead endpoint might be a Beads team server, DoltHub, a private Git remote, or another
supported service. In that mode no bead data need be published on the source origin's
Dolt ref. A database service endpoint and a replication destination are distinct bindings;
choosing a service does not by itself configure durable backup or cross-machine replication.

### A hive has multiple associated bead groups

A **bead group** is an independently governed collection of issues associated with a hive.
The core group contains issues authored directly by people or agents. Core issues need
never exist in an external issue tracker. Other groups may contain private planning,
upstream issues, or mirrors of named external trackers.

The association means the work concerns this project. It does not imply that the project
maintainers own every group or can read, edit, or publish all its contents. A planning group
may also concern several hives; whether associations are many-to-many is an open design
choice. Group identity should not depend on its current list of project associations.

### Dimensions that must remain independent

| Dimension | Question answered | Examples |
|---|---|---|
| Hive identity | Which source project does the work concern? | Canonical project Git location |
| Group identity | Which durable issue collection is this? | Core, personal planning, named tracker mirror |
| Authority | Who controls canonical changes to this group? | Project maintainers, a private team, an upstream project |
| Actor capabilities | What may this actor do here? | Read, claim, edit, create, submit proposals, publish |
| Audience | Who is allowed to access the group's data? | Public, organization, team, individual |
| Provenance | Where did a record originate? | Native bead, GitHub issue, Linear issue, upstream bead |
| Storage binding | Where does authoritative bead state live? | Source-origin Dolt ref, detached server/database |
| Replication binding | Where may bead state and history be copied? | Authorized replicas, private backup remote |
| Tracker binding | Which external records and fields are exchanged? | Pull-only GitHub mirror, bidirectional Linear connector |
| Presentation | How does a working view name and display the group? | Prefix, qualified alias, labels |

Public/private visibility and native/external provenance are orthogonal. A GitHub mirror
can be private. A native core group can be public. Contributor mode is an actor's relationship
to a particular group, not a property that makes all groups in a checkout read-only.

### Illustrative groups for one hive

| Group | Authority | Publication policy | Relationship to a tracker |
|---|---|---|---|
| Core | Project maintainers | Source origin or explicitly detached endpoint | None required |
| Private planning | Individual or private team | Private destinations only | None required |
| GitHub mirror | Defined by its connector and source policy | Explicit destinations | Pull-only or bidirectional |
| Linear mirror | Defined by its connector and source policy | Explicit destinations | Pull-only or bidirectional |
| Upstream replica | Upstream project | Upstream-authorized replication | Native Beads source |

The names and prefixes in examples are proposals, not current Beadhive configuration keys.
For bidirectional tracker groups, authority may be divided by field or operation. The
connector must define conflict handling, deletion, and reconciliation rather than assume
that whichever side synced last owns the whole record.

### Storage and working views

The desired experience is to work with core beads and external groups alongside one another,
potentially in the same Dolt database. Each group should have its own permitted replication
destinations and tracker bindings. Sending a mirrored group to its tracker must not send
unrelated core issues there.

The managed operator interface is `bh bd` and higher-level `bh` verbs wrapping the same
guards. Project guidance should strongly discourage raw `bd` for managed workflows.
Routine tracker push should update already-linked records only; creating a new external
issue is a separate, explicit enrollment operation. Tracker group membership can be defined
by a validated external binding, independently of the bead's readable prefix.

There are two different meanings of "alongside":

- **Authoritative co-location:** Several groups share a writable database while retaining
  their own ownership and publication rules.
- **Working aggregation:** A local view combines replicas or projections from independently
  authoritative stores, and routes writes back to the proper authority.

The long-term model should allow both. A practical implementation may first use separate
stores for independent permission boundaries and aggregate them for the operator. Whether
authoritative co-location is worthwhile depends on the available isolation and replication
mechanisms; a shared database alone does not establish group-specific access control.

A replication remote copies bead state and history. A tracker connector translates selected
records and fields into another issue model. They must be configured separately even when
the interface presents both as sync destinations.

Publication covers comments, dependencies, attachments, metadata, and history as well as
the current issue rows. Cross-group edges need an explicit disclosure policy. A private
dependency's title or description must not appear in a public issue merely because both
are visible in the author's local working view. Retraction cannot undo data already copied
to a public destination.

### Contribution without owning upstream beads

An operator working in a personal fork may read upstream's beads without permission to
create or change canonical upstream issues. They should still be able to author their own
planning and work notes in a group they control, and work against upstream issues.

One possible mechanism is an overlay: local activity and proposed changes reference an
upstream bead while upstream's canonical record remains owned by upstream. An overlay is a
proposed feature, not a claim about how Beads currently routes claims or updates.

Direct issue proposals should also be possible without requiring an external tracker:

1. Author a report in a group the contributor can write.
2. Submit the report to the upstream group's contribution endpoint.
3. Upstream accepts, rejects, or requests changes under its own policy.
4. Acceptance creates or adopts an upstream-owned bead with explicit lineage to the report.

If upstream grants direct issue-creation rights, the actor may create there directly.
Submission rights should not require general database publication rights. The precise
acceptance and identity-transfer semantics remain open.

### Identity beyond readable prefixes

There is no globally coordinated prefix allocator. Unrelated groups can legitimately choose
the same prefix, and federation must not merge their issues on that basis. Prefixes should
be readable aliases secondary to durable identity.

A candidate identity model is an immutable pair `(group_id, bead_id)`, with a globally unique
group identifier and a bead identifier unique within that group. The exact allocation
scheme and serialization are open. For example:

| Form | Illustrative spelling | Purpose |
|---|---|---|
| Canonical | `(group UUID, bead UUID)` | Durable references and replication |
| Qualified alias | `beadhive/core:bh-abc` | Human-readable disambiguation in a view |
| Local alias | `bh-abc` | Convenient lookup when unambiguous |

The qualified spelling is also an alias: project names and group names can change. Graph
edges should carry canonical identities. An ambiguous local alias requires qualification;
it must never silently select a different issue. Alias scope, historical aliases, and
allocation authority should be explicit.

A replica preserves the same group identity. An independent group fork receives a new
identity with recorded lineage. A tracker mirror records its external binding; that binding
does not automatically make a tracker record and a bead the same globally canonical object.
Source-host migration, prefix changes, and remote changes should not rewrite canonical IDs.

### Decisions still open

This proposal captures direction rather than settling every mechanism. In particular:

- Whether a group can be associated with several hives and how the default group is selected.
- Whether group ownership is represented by a service, keys, an organization, or a combination.
- How accepted proposals preserve identity and how group transfers differ from copies.
- Whether co-located authoritative groups warrant native replication support or should remain
  separate stores behind a unified interface.
- Which cross-group facts may be disclosed and how unavailable dependencies affect readiness.
- How tracker field authority, divergence, deletions, and offline edits are reconciled.

The invariant is that project association, readable naming, and storage location must not
silently determine ownership, permissions, or publication.

## 2. What Beads v1.3.0 can support today

### Research baseline and intended upstream usage

This assessment was researched on 2026-10-04 against the [v1.3.0 source tag][release], the
official documentation site, and command help from the installed `bd version 1.3.0
(f45b249ce)`. Source inspection establishes implementation behavior; this research did not
exercise live tracker accounts, a team-server deployment, or remote publication. Those
combinations need deployment-specific validation before a workaround is shipped.

The official [routing guide][routing-doc] recommends a separate private planning repository,
normally `~/.beads-planning`, with its own prefix. `bd init --contributor` configures creation
routing and additional repositories for aggregation. Explicit `--repo` selects a creation
target; contributor role is routing context, not a group permission system. The
[migration guide][migration-doc] gives personal experiment, phase, and persona variants.
Some branch/PR language in the guides remains historical; source behavior takes precedence
when assessing Dolt publication or command support.

`allowed_prefixes` is an admission list. It accepts explicitly supplied IDs from additional
prefixes; it does not establish their ownership, select their allocation policy, or choose
their destinations. It is not exclusively a hydration feature: the changelog describes
multi-prefix support, while `bd repo sync` itself skips prefix validation. Sources:
[explicit-ID admission][prefix-validation], [hydration][repo-source], and
[tagged changelog][changelog].

### Capability assessment

"Native" means a v1.3.0 mechanism already exists. "Wrapper" means Beadhive can approximate
the behavior while retaining separate stores or additional policy. "Missing" means the
stated guarantee cannot be obtained from unmodified v1.3.0 mechanisms alone.

| Desired behavior | Assessment | Available mechanism and limit |
|---|---|---|
| Core beads stored on the source origin | Native | Configure a Git-protocol Dolt remote; publication uses `refs/dolt/data` |
| Bead hosting detached from source hosting | Native | Explicit Dolt remote or external database endpoint; configure replication separately |
| Several groups associated with one hive | Wrapper | Extend Beadhive's registry to associate one project with several stores and group records |
| Private planning beside public work | Wrapper | Separate stores for independently accessible bead data; one private store can also publish selected public tracker projections |
| Native core beads never sent to a tracker | Native in a separate store; wrapper in a mixed store | Do not configure a tracker for core, or always apply an explicit connector scope |
| Core and mirror rows in one Dolt database | Native as ordinary rows | Labels, metadata, prefixes, and external references distinguish rows; no group isolation follows |
| Different Dolt remotes for groups in one authoritative database | Missing as selective replication | A remote selects a destination for a database branch, not a subset of issues |
| Different remotes with one store per group | Wrapper using native remotes | Invoke each group's configured store; every store can have its own remote bindings |
| Pull-only and bidirectional tracker exchange | Native for supported adapters | Direction and selection controls are adapter-specific; there is no universal group connector contract |
| Private core plus public tracker-linked beads with one prefix | Wrapper | Keep the authoritative database private; guarded update-only push publishes only selected linked records |
| Routine tracker push that never creates external issues | Wrapper approximation; missing native mode | Validate existing bindings and supply nonempty `--issues`; v1.3.0 push remains create-or-update internally |
| Several disjoint tracker groups in one database | Wrapper | Validate and scope each destination independently; each bead retains one native external reference |
| Distinct allocation prefixes per group in a mixed store | Wrapper | Supply explicit IDs and enforce registry policy; there is one default issue prefix |
| Read upstream beads without publishing local planning there | Wrapper using native routing | Keep the upstream replica and personal planning store separate; restrict upstream credentials |
| Direct upstream issue creation with granted rights | Native | Create against the authoritative workspace/service within its existing access policy |
| Submit a bead proposal without broad upstream write rights | Missing as a native protocol | A Beadhive service or manual maintainer-mediated transfer could supply it |
| Canonical issue identity independent of its prefix | Missing natively; wrapper approximation | Maintain an external identity/alias map, while Beads still keys records by the readable ID |
| Two unrelated identical issue IDs in one aggregate database | Missing without projection/remapping | The issue primary key is the ID string, not `(source, ID)` |
| Per-group authorization inside one database | Missing in the inspected Beads group model | Use separate access boundaries or a mediating service; no native bead-group ACL was found |
| Group overlays and a super-global graph | Wrapper approximation | Resolve identity and overlays above Beads; native dependencies do not implement this model |

The evidence and constraints behind these classifications follow. They are limits of
available Beads mechanisms, not claims that a custom storage engine or service could never
implement the desired behavior.

### Detached hosting and group-specific remotes

The [Dolt documentation][dolt-doc] describes Git, DoltHub, bucket, and filesystem remotes.
Git source origin and the configured Dolt remote can point to different repositories.
`bd dolt push --remote <name>` selects a named destination. `--no-adopt`, or
`BD_NO_REMOTE_ADOPT=1`, prevents push from adopting a source-origin-derived remote when none
is configured. These are useful controls for a detached deployment, not enforcement against
an actor who can reconfigure and publish the database.

External SQL-server storage and the experimental proxied `--team-server` integration are
also present. In team-server mode schema and identity are provisioned externally; the client
verifies them. `--team` is a setup wizard and is distinct from `--team-server`. This establishes
deployment support, not evidence that the external service supplies the proposed group ACLs.
Sources: [initialization][init-source] and [team-server initialization][team-init-source].

The [replication implementation][remote-source] calls Dolt push with a remote and branch.
There is no group/prefix row selector. Additional remotes receive that branch's replicated
tables and reachable history, subject to Dolt's existing ignored-table behavior. Removing
private rows from the current snapshot or filtering an export does not remove them from
reachable history. Separate Dolt branches also share ancestry unless deliberately built as
independent histories; branch naming is not group isolation.

Workaround: give each independently published group its own authoritative database, even
when several databases run on one SQL server. A publication projection in a separate store
could publish selected content from a mixed store, but would be a new Beadhive synchronization
system with its own identity, deletion, relation, and history semantics. It would not be a
native selectively replicated branch or a reason to use JSONL as the normal cross-machine
wire protocol.

### Aggregation is materialization, not a privacy boundary

`bd repo sync` materializes imported issues as ordinary rows, stamps `source_repo`, and
skips prefix validation. In v1.3.0 local-path hydration reads a passive JSONL export; a remote
URL path reads issues from a cached store. Local hydration therefore depends on an up-to-date
export even though authoritative cross-machine replication remains Dolt. The implementation
is rejected in proxied-server mode. Sources: [repo implementation][repo-source] and
[routing documentation][routing-doc].

Hydrated regular issues are not placed in a group-specific ignored table. Combined with
database-branch publication, this means importing private regular issues into a database
that is subsequently pushed publicly can publish them. This is an inference from the
hydration and push implementations, not a live public-push experiment.

`source_repo` preserves attribution; it is not a row ACL or an automatic publication filter.
Hydration alone also does not establish a general bidirectional write-back protocol for
edits to imported rows. A group-aware wrapper must deliberately target the authoritative
store for each mutation. Snapshot/remote paths should be checked for comment, dependency,
deletion, and update fidelity before being used as an operational mirror.

Workaround: use an aggregate that is explicitly non-authoritative and never publicly
published, or query the group stores without materializing them. If a private aggregate
combines several audiences, its own audience must be allowed to see every materialized
group. Beadhive's existing hub is a useful starting point, but currently its hive identity
and registry assumptions do not express several groups per project.

### Tracker mirrors can be scoped, with important limitations

The [GitHub adapter][github-source] supports pull-only, push-only, and bidirectional sync.
GitHub and Linear commands register `--issues` for an explicit comma-separated selection
and `--parent` for push of a subtree. Linear also has push type filters, exclusions, and a
`linear.push_prefix` allowlist. See [shared selection flags][selection-source] and
[Linear implementation][linear-source].

These controls can keep a native core out of a tracker, but a mixed-store wrapper must
always calculate and supply the intended selection. Empty selection must be a no-op:
the [sync engine][tracker-engine] treats an empty issue-ID filter as unrestricted. A forgotten
scope can therefore change the operation's meaning dramatically. Linear's push-prefix check
also uses starts-with matching; allowing `bh` includes `bh-private-*`.

Unscoped push is not implicitly limited to already-linked beads. For each eligible issue,
the engine creates an external issue when `external_ref` is absent or is not recognized by
the destination adapter, and successful creation replaces the stored reference. Each sync
invocation targets one configured tracker; running several unscoped invocations can therefore
publish the same native issues to several trackers or replace another tracker binding.
Linear has a conditional project-scoped guard against unlinked creation, but it is not a
universal linked-only policy. Sources: [push engine][tracker-engine] and
[Linear push hook][linear-source].

New GitHub and Linear pulls use the configured default issue prefix, not a first-class group
allocator. For a distinct mirror namespace, the simpler workaround is a dedicated tracker
store. An import/remapping or projection layer in a mixed database would be custom work.
The core issue model exposes one `external_ref` and one `source_system`; arbitrary metadata
is available, but native tracker behavior is not a general many-connector binding registry.
Sources: [GitHub pull allocation][github-source], [Linear pull allocation][linear-source],
and [issue fields][types-source].

Workaround: define tracker membership through validated bindings and guard operations through
`bh bd`. Distinct prefixes are optional. Several disjoint tracker groups can share the same
private database and prefix. A bead simultaneously mirrored to multiple trackers requires
additional binding machinery because the native external-reference field holds one value.
Dedicated connector stores remain an option when groups need different database audiences,
independent replication, or incompatible connector settings.

### Guarded tracker sync through `bh bd`

This is a proposed Beadhive contract, not existing passthrough behavior. The
[current passthrough](../PASSTHROUGH.md) already centralizes other managed-operation guards;
tracker publication should follow that pattern. Higher-level `bh` verbs must invoke the
same guard rather than provide an alternate unscoped path. Ordinary managed tracker sync
should mean **pull plus update**, with external creation reserved for explicit enrollment.

The guarded workflow is:

1. **Resolve the destination.** Identify the connector instance, host, repository or
   workspace/team/project, credentials context, group policy, and source store.
2. **Pull independently.** A broad pull-only operation discovers new external records and
   imports their bindings. Keep discovery separate from `--issues` selection so new remote
   issues are not hidden by a selection of existing local IDs.
3. **Select linked members.** Build the push set from existing bindings to that exact
   destination and permitted group. Caller-supplied IDs or subtree selection must satisfy
   the same checks; specifying an ID is not permission to enroll it.
4. **Validate before push.** Refuse missing, foreign, malformed, ambiguous, or mismatched
   bindings in a requested selection. Automatic selection excludes unlinked core beads.
   Refuse unresolved destination scope rather than guessing from a prefix or URL substring.
5. **Push the explicit set.** Supply a nonempty `--issues` list and the adapter's push-only
   direction. An empty computed set is a successful no-op without invoking native push.
   Unknown or incompatible options fail rather than reverting to unrestricted sync.
6. **Keep creation separate.** Enrollment explicitly selects a native bead and destination,
   publishes its permitted content, and records the resulting binding. Routine updates
   never silently enroll an unlinked bead or recreate a missing external issue.

The guard must cover equivalent command forms, including tracker `push` shortcuts and
bidirectional `sync`, not just a single flag spelling. A managed bidirectional request can
be decomposed into pull-only and guarded push-only stages. A subtree selection should be
expanded and checked before issuing an explicit ID list. Existing lease and publication
guards continue to apply where relevant.

Binding validation must be stricter than v1.3.0 adapter recognition. For example, the
[GitHub adapter][github-tracker] recognizes GitHub issue references and extracts their issue
number without establishing that the URL names the currently configured repository.
Repository-less shorthand references need a retained, verified destination binding.
Multiple GitHub targets must not reinterpret the same issue number under a different repo.

This allows **one private authoritative database, one shared prefix, private native core,
and selected public tracker projections**. The database is never published to the public
source-origin Dolt ref; its replication and backup destinations remain private. The fields
deliberately sent to public GitHub Issues are public even though their bead representation
and database history remain privately stored. Tracker selection is the publication policy
for that projection, not a row-level ACL inside the private database.

The v1.3.0 workaround approximates update-only semantics by selecting and validating linked
IDs. It does not change the native engine's create-or-update behavior. If a binding changes
between validation and execution, native push may still take its creation path. A practical
implementation needs coordination and binding revalidation; eliminating that race calls for
an adapter/engine update-only mode that rejects creation at the mutation boundary. Preview
output is useful review evidence, not an atomic guarantee.

Direct `bd` use is strongly discouraged because it bypasses these guards. This is the same
managed-client trust model as the existing passthrough, not a requirement to introduce a
publishing service before the convention is useful. If enforcement against clients holding
raw tracker write credentials is required, credentials must instead be held by a publisher
that independently enforces the contract.

### Prefixes, collision handling, and a canonical-identity shim

Prefix checks differ by operation:

- Initialization normalizes dots and trailing hyphens; a prefix-derived database name has
  a 64-byte limit and an ASCII identifier alphabet.
- Prefix rename accepts lowercase letters, digits, and hyphens after trimming trailing
  hyphens, with no explicit prefix-length cap in that validator.
- Doctor's YAML prefix check permits letters, digits, underscores, and hyphens, starting
  with a letter, and flags lengths above 20 bytes.
- Explicit-ID admission checks starts-with against default/allowed prefixes. It does not
  enforce exact namespace membership when prefixes overlap.

Sources: [init][init-source], [database names][database-name-source],
[rename][rename-prefix-source], [doctor][doctor-source], and [admission][prefix-validation].
For newly allocated Beadhive aliases, lowercase ASCII with single internal hyphens and a
20-character cap would be a proposed conservative convention. Existing external namespaces
should be preserved where possible. `rename-prefix --repair` consolidates namespaces and
must not be offered as generic repair for an intentionally multi-prefix collection.

The [issue schema][issue-schema] keys issues by `id VARCHAR(255)`. `source_repo` is a separate
field, not part of that primary key. The column bounds the full ID, including prefix,
separator, and any hierarchical suffix, rather than defining a 255-character prefix policy.
Short hashes reduce accidental collisions; they do not
provide an immutable identity layer or a guarantee that unrelated stores cannot produce the
same full ID. Native [rename][rename-source] changes the primary ID and references.

Beads does already provision a persistent `_project_id`/workspace identity, adopted when
joining existing storage. That can help identify a one-group-per-store wrapper, but it is
not a per-group key on issue rows or graph edges, and a storage clone does not automatically
mean a new independent group fork. Sources: [identity initialization][init-source] and
[team identity adoption][team-init-source].

Workaround: keep `(group_id, existing_bead_id)` in Beadhive's resolver and preserve source IDs
in separate stores. When IDs collide, query stores separately or assign unique projection
IDs in the local aggregate and retain a reversible source mapping. Imported dependencies
must be rewritten consistently. If source IDs are later renamed, an external alias/history
map needs to preserve continuity; storing an immutable UUID in metadata alone does not make
native lookups or foreign keys use it.

### Contribution and local-only storage

Contributor routing can direct new work to a separate private store while upstream is read
through its own replica. It does not introduce per-group permissions or a reviewed bead
submission queue. A local replica may be writable even when the actor cannot push upstream;
changes to that replica are not automatically an overlay or an upstream claim. Remote/service
credentials and a wrapper's operation policy must enforce the intended rights.

A maintainer can manually create or accept a proposed report in upstream's store. A wrapper
could automate that transfer through a maintainer-controlled service with explicit consent,
idempotency, attribution, and lineage. That is a new contribution mechanism, not merely
setting `beads.role contributor`. The [migration guide][migration-doc] explicitly marks its
issue-migration example as a future feature.

v1.3.0 also has unversioned/no-history and ephemeral storage classes. They route to the
clone-local ignored wisp plane rather than ordinary replicated issue history. They can
support local-only scratch behavior, but are not independently replicated private groups:
ephemeral work is purge-eligible, and unversioned work lacks normal Dolt replication/history.
Exports or a server actor can still expose local data. See [storage-class behavior][release]
and [ignored-table patterns][ignored-patterns]. Use a private authoritative store for durable
private planning with private replication.

### A credible v1.3.0 architecture

There are two useful starting arrangements:

- **One private store with tracker projections:** Core and disjoint tracker-linked groups
  share a database and prefix. `bh bd` guards public tracker updates; explicit enrollment
  is the only managed operation that creates a new external issue.
- **Several independently governed stores with one working view:** Groups that need distinct
  database access or replication boundaries keep separate authoritative storage. Beadhive
  associates them with the same logical hive and routes operations to the proper store.

Both need a registry and binding-aware guards. Group labels, metadata, and prefixes can aid
display and selection, but do not themselves grant authority.

This can approximate most operator-facing behavior without changing upstream's issue schema.
The major native gaps remain independent publication and ACLs for co-located authoritative
groups, prefix-independent graph identity, reliable group-aware mutation routing, and direct
reviewed contribution submission. Any cross-store readiness calculation or write-through
aggregate would be an explicit Beadhive feature with coverage and freshness semantics.

## 3. Changes needed to reach the north star

### Beadhive changes that can precede upstream work

1. **Group registry and bindings.** Give hives stable identity and a canonical source origin.
   Register associated groups with identity, authority, audience, storage bindings,
   replication destinations, connector instances, and operator capabilities. Retain the
   existing single-prefix hive as a backward-compatible default core group.
2. **Explicit operation routing.** Select a group for creation, resolve a record's authority
   for mutations, and require publication to name a permitted binding. The wrapper must
   distinguish source Git origin from local fork remotes and bead destinations. Ambiguous
   aliases, absent stores, and insufficient capabilities fail explicitly.
3. **Identity and alias resolver.** Persist group identity independently of URLs and prefixes.
   Initially resolve native IDs as `(group_id, legacy_id)` with alias history and projection
   mappings. Registering a replica preserves identity; registering an independent fork
   allocates a new identity and records lineage.
4. **Authorized aggregate and graph.** Build a view from accessible groups, retaining source
   revision and coverage. Route writes to authoritative stores. Resolve cross-store edges
   through canonical references, report unavailable/private dependencies honestly, and avoid
   presenting stale replicas as current authority.
5. **Connector policy.** Configure each named connector independently, with direction,
   scope, field ownership, external record mappings, conflict rules, and deletion behavior.
   Centralize update-only selection in `bh bd` and reuse it from higher-level verbs. Separate
   discovery pulls, linked-record updates, and explicit enrollment. Compile validated
   selections to supported v1.3.0 controls, make empty selections no-ops, and reject unsupported
   combinations rather than falling back to unscoped sync. Strongly discourage raw `bd`.
6. **Contribution service or workflow.** Represent proposals separately from canonical
   mutations. Preserve authorship, target authority, source revision, lineage, acceptance
   outcome, and replay protection. Begin with a reviewed maintainer-mediated transfer if
   no upstream submission endpoint exists.

These are proposed capabilities, not implementation tasks promised by this document. A
Beadhive-only implementation can mediate its own clients; it cannot enforce group policy
against unrestricted raw SQL, raw `bd`, or remote administrators with broader rights.

### Potential upstream or backend changes

| Area | Potential change | Guarantee it would establish |
|---|---|---|
| Group model | First-class immutable group ID and group membership on every issue and related record | Ownership remains explicit when groups share storage |
| Canonical identity | Immutable bead key separate from aliases; canonical graph endpoints | Prefix changes and namespace collisions do not change identity |
| Allocation and resolution | Group-aware allocation, qualified lookup, alias history, deterministic ambiguity errors | Native operations select the intended issue and authority |
| Access enforcement | Group-scoped authorization at the service boundary, including related data and history reads | Read/create/edit/claim/submit/publish rights can differ by group |
| Replication | Group-scoped replication/export protocol or independent histories per group | Destinations receive only authorized group data and history |
| Tracker connections | Multiple named connector bindings with stable external IDs and field-level reconciliation | Core and mirror groups sync independently without overwriting one binding |
| Update-only tracker push | Native mode that rejects absent/foreign bindings and never falls back to create, with destination validation at mutation time | Routine updates cannot accidentally enroll or rebind a bead through the native creation path |
| Contributions | Proposal ingestion and reviewed acceptance separate from general write access | Contributors can submit beads directly to upstream |
| Aggregation | Explicit replica/cache semantics and authoritative mutation routing | Imported rows are not mistaken for writable canonical records |
| Graph federation | Canonical cross-group references, resolution, disclosure policy, and partial-readiness semantics | A global graph works with disconnected or inaccessible authorities |

Native group-scoped replication cannot be achieved merely by adding a `group_id` column.
Every synchronized relation, journal, tombstone, configuration record, attachment, and
historical revision needs a defined boundary. Options include independent group databases,
separate independently rooted histories, or a new application-level replication protocol.
Keeping database-wide Dolt push while adding only current-row filtering would fail the
required publication guarantee.

Canonical identity needs a migration strategy for existing IDs and links. One approach is
to assign immutable bead keys, retain existing IDs as scoped aliases, backfill graph endpoints,
and keep legacy clients on a compatibility view until they can resolve qualified identities.
Copies, accepted proposals, transfers, and independent forks must have explicit identity rules.
Authenticating the authority behind a group ID is separate from allocating a unique UUID.

### Validation contracts for a future implementation

The following scenarios define useful acceptance evidence:

- Two unrelated groups contain the same legacy ID and remain distinct after aggregation,
  replication, alias changes, and graph traversal.
- A public destination receives no private rows, comments, relation payloads, attachments,
  or reachable historical content, including after a retry or deletion.
- A contributor can read upstream and write personal planning, but cannot change upstream
  ownership or claim state without the required capability.
- Tracker push touches only the declared group; an empty selection produces zero writes;
  scoped synchronization does not mutate unrelated relations through a repair pass.
- Unlinked core, foreign references, and wrong-repository references cannot enter routine
  managed tracker push, even through explicit IDs, shortcuts, or a subtree request.
- Multiple tracker groups share a prefix without exchanging or overwriting each other's
  references; a changed binding during execution is rejected by the update-only mutation
  path once that native capability is available.
- Broad pull discovers new tracker records while targeted update pushes create no external
  issues; only explicit enrollment creates a new record.
- Duplicate proposal delivery produces one acceptance result with preserved attribution.
- Missing or stale group replicas produce explicit coverage/readiness states rather than
  disappearing blockers or fabricated canonical state.
- Legacy single-group hives retain their IDs, default routing, and publication behavior.

The first viable step is the registry and guarded operation layer, using a private mixed
store for tracker projections and separate stores where database permissions or replication
must differ. Authoritative co-location with independent database permissions and history
publication should follow only once its backend guarantees are specified and verified.

## Research sources

Official documentation is useful for intended usage; tag-pinned source links establish the
version assessed here. Live documentation may change after this proposal.

- [Beads v1.3.0 release][release] and [tagged changelog][changelog].
- [Multi-repo routing][routing-doc], [migration workflows][migration-doc], and
  [Dolt storage and remotes][dolt-doc].
- [Initialization][init-source], [team-server initialization][team-init-source],
  [database-name checks][database-name-source], [prefix admission][prefix-validation],
  [prefix rename][rename-prefix-source], and [doctor prefix checks][doctor-source].
- [Repo hydration][repo-source], [database push][remote-source], [issue schema][issue-schema],
  [issue fields][types-source], [ID rename][rename-source], and [ignored local tables][ignored-schema].
- [Canonical ignored-table patterns][ignored-patterns].
- [GitHub integration][github-source], [Linear integration][linear-source],
  [tracker selection flags][selection-source], and [tracker sync engine][tracker-engine].
- [GitHub reference and destination handling][github-tracker].

[release]: https://github.com/gastownhall/beads/releases/tag/v1.3.0
[changelog]: https://github.com/gastownhall/beads/blob/v1.3.0/CHANGELOG.md
[routing-doc]: https://beads.gascity.com/multi-agent/routing
[migration-doc]: https://beads.gascity.com/multi-agent/multi-repo-migration
[dolt-doc]: https://beads.gascity.com/architecture/dolt
[init-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/init.go
[team-init-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/init_proxied_server.go
[database-name-source]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/storage/dolt/history.go
[prefix-validation]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/validation/bead.go
[rename-prefix-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/rename_prefix.go
[doctor-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/doctor/config_values.go
[repo-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/repo.go
[remote-source]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/storage/versioncontrolops/remotes.go
[issue-schema]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/storage/schema/migrations/0001_create_issues.up.sql
[types-source]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/types/types.go
[rename-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/rename.go
[ignored-schema]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/storage/schema/migrations/ignored/0001_create_local_state_tables.up.sql
[ignored-patterns]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/storage/schema/schema.go
[github-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/github.go
[github-tracker]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/github/tracker.go
[linear-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/linear.go
[selection-source]: https://github.com/gastownhall/beads/blob/v1.3.0/cmd/bd/sync_flags.go
[tracker-engine]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/tracker/engine.go
