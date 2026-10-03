# HQ configuration authority migration

`bh hq migrate` changes the selected **configuration** authority. It does not move a
Beads database, re-enroll a frame, reset a hive lease, or retire the Git HQ. A
successful selector readback reports `CONFIG_READY`; Beads store readiness and
runtime admission are separate checks. All commands below are operator steps for
a reviewed deployment. The local implementation fixtures do not apply them to a
production HOST or Dolt server.

## Before first publication

Provision a new dedicated configuration database with the reviewed
[`ops/hq-config`](../../ops/hq-config/README.md) resources. Its three versioned
tables are initially empty. A reader has only table reads; the trusted publisher
has table writes plus branch-wide Dolt staging and commit capability. Keep those
credentials apart from frame, observer, and Beads principals. Native MySQL TLS
advertisement, a trusted CA, server-name verification, a noninteractive broker,
the exact backend/generation, and server-local operator access are prerequisites.

Prepare the HOST SQL binding with `hq.sql.enabled: false`. Merely preparing the
endpoint, CA, or reader reference never selects SQL. Preserve the existing HOST
identity, signing key, Beads engine binding, attachments, clone roots, remotes,
actors, branches, and hive leases. The source Git HQ must carry the canonical
`beadyard.json`; a legacy instance requires the separately reviewed explicit
identity-adoption operation before seed. The dry-run reports missing identity and
HOST-owned `work.identity` relocation as named conflicts. No load-time UUID is
generated and HOST identity values are never copied into central fleet truth.

Run `bh hq migrate --to dolt-server` to obtain the value-free plan. It captures
the exact Git HEAD/status, ordered selected external workspace files, raw source
hashes, typed semantic projection, canonical identity, destination schema HEAD,
backend/generation, and conflicts. Resolve every conflict and approve that exact
plan. Suspend Git configuration writers through the cutover, then run
`bh hq migrate --to dolt-server --confirm --intent <private-journal>` with the
same reviewed source and empty destination. The journal must be on a durable
operator-owned 0700 directory; its files are 0600. It records the original
schema parent and publication UUID before Dolt commit. Keep it on uncertainty:
retry the same intent and original source; never create a new UUID or refresh
the parent to mask an unknown commit. The initializer validates the exact fresh
v3 schema, performs empty-singleton arbitration, commits only the three config
tables, and verifies immutable AS OF readback. The guarded HOST selector change
follows exact readback and is restored to its original bytes if post-switch
verification fails while this process still owns its write.

An actual source change, new workspace file, different selected tier/order,
different HOST ownership disposition, dirty SQL staging, nonempty destination,
foreign identity, or changed schema HEAD requires a new reviewed plan. An
already-completed original seed is recovered by the recorded UUID and original
parent, even if later valid SQL publications have advanced HEAD.

## A fresh host

A fresh host needs HOST-local non-secret SQL bootstrap/trust metadata, its
canonical `hq.beadyard_id` pin, and broker access. It can read the seeded fleet
document, selected workspace documents, hive catalog, and prospective
shared-server database name without an HQ Git checkout or HQ GitHub credential.
Source repository access still follows the normal provider rules. `bh host
provision --dry-run` probes the existing selected Beads engine: a usable store,
published origin that can be bootstrapped, unpublished origin, and unavailable
store/origin are different outcomes. Apply uses the existing `bd bootstrap` and
sync ports, never reinitializes an existing store, and does not infer readiness
from a directory alone. A CONFIG_READY host without bound signed runtime remains
`AUTHORITY_NOT_READY` for lease/admission operations; it cannot fall back to raw
Git HQ lease refs.

## Latest-authority return to Git

Freeze the SQL configuration publisher and drain in-flight writes. The freeze
assertion is an operator-signed, bounded artifact naming backend, generation,
original SQL HEAD, principal, denied DML/commit capabilities, and a clean stage.
Qualify effective permissions with separate account sessions; a syntactically
valid artifact alone does not prove the server's current grants. Capture the
latest SQL and current signed Git revisions, then preview with
`bh hq migrate --to git`.

Run the confirmed command with the original `--expected-sql-revision` and
`--expected-git-revision`, plus `--operator-key`, `--suspension-artifact`,
`--suspension-signature`, `--intent`, and `--mirror-journal`. It exports the
**latest** verified SQL documents to an operator-signed Git config revision,
including the same `beadyard.json`. A pending intent binds the original heads,
raw ordered-document digest, HOST proposal, signer assertion, and publication
UUID *before* the signed push. If the push result or receipt write is lost,
retry with the same original arguments and intent. Recovery accepts only the
signed direct child with that exact UUID and documents; it cannot republish
under a new identity.

The mirror installer stages the selected latest fleet and workspace files and
reconciles the original bytes of every path under a private journal. It checks
the signed export and original SQL head before installing and can resume after
a partial local install. Only after normal Git config and workspace readback
match the export may the guarded HOST selector choose Git. An ordinary
`bh config set hq.sql.enabled false` is refused while SQL is selected.

Keep SQL history, the signed Git export, original private journals, writer
suspension evidence, and the old HQ recovery source until backup/restore,
runtime admission, Beads durability, and repository retirement have their
own qualified receipts. Git and SQL histories are distinct; this protocol
does not assert a cross-database atomic commit. A rollback cannot authorize
work from stale observations: current protected policy and a fresh signed
heartbeat still govern frame eligibility.
