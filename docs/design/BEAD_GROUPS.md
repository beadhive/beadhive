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
