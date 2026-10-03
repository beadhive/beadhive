# Dolt-first HQ configuration, seeded from the current fleet

Date: 2026-10-01  
Status: operator-authorized replan; implementation and deployment remain pending  
Parent: Fleet membership (`bh-yzhcg`)

The operator approved replanning Fleet Management around shared Dolt as the central
configuration authority, using the configuration available to the Beadhive hive as seed
data. The goal is to swap the remote configuration source and eventually deprecate the
`beadhive-hq` repository once it is a historical snapshot. This decision expands the
membership-only scope of `bh-cem7l`; that bead's merged document explicitly excluded
moving fleet configuration and HQ's own beads. It does not claim the SQL adapter or remote
deployment has already passed qualification.

## Evidence and source selection

Read-only inspection on 2026-10-01 found these sources. Paths describe this inspected
installation; implementation resolves configured paths rather than hardcoding them.

| Source | Role | SHA-256 at inspection |
| --- | --- | --- |
| `/home/bees/.beadhive/hq/fleet.yaml` | Current fleet working configuration, 27 managed hives | `e03d0a6b8df03f212aaee6c7c53c1313cf94e6e5a7b55cf9f6491d5223161e15` |
| `/home/bees/.beadhive/hq/workspace.toml` | Workspace/provider definitions | `97ebdd94689ec37c18a58a1188cfe43b8a6f06f5cc68f9ce1176349c4f9c08e4` |
| `/home/bees/.beadhive/config.yaml` | Host layer, endpoint and credential-reference inputs; not wholesale fleet seed | `9614e984f7b670c96dc1271ccd4e3349e84c5584eb102e6322124be9d5c1f23d` |
| `/home/bees/.beadhive/hq/hives/github/beadhive/beadhive.yaml` | Observed hive manifest | `bc626c3044ef70a52a0b9daccc900749ebc6f6b67759ff8e0683fc32865e379e` |
| `.beads/metadata.json` in this hive | Existing Beads binding: database `bh`, server mode | `406a6d9fddba3a5040678fa7965d12d692b7622f687a1f883794140a42f28ac3` |
| `.beads/config.yaml` in this hive | Hive configuration and replication inputs; classify before import | `d4a8e232a9900a6bc873cd391c0fd296b614c23c1ce28ff11c068690e024ce00` |

HQ has two host manifests, 27 hive manifests, and `allowed_signers`. Its Git HEAD was
`5a2015cd94c93df7b917f5847f366c52786f1105`, with `fleet.yaml` modified in the working tree.
The effective configuration loads successfully. Fleet YAML contains `work.identity.name`
and `work.identity.email`, which the current ownership policy classifies as host-local.
Neither effective-load success nor a manifest's presence establishes trusted admission or
database usability. The inspected Dolt connection succeeded at `127.0.0.1:3308`; historical
cleanup work mentions 3310. Neither loopback address is an enrollment endpoint for another
frame.

The migration source is the current local working configuration, including its uncommitted
changes, rather than only the repository HEAD or GitHub's last publication. Build a
content-addressed, access-controlled snapshot at migration time, with working-file hashes,
Git provenance, per-field ownership, and a redacted diff. Abort if the source changes
between plan and apply. Capture all manifest hashes in the implementation's migration
manifest; the table above is an evidence record, not permission to use stale content later.
Do not publish raw configuration or credential material into this source repository.

The current effective workspace provider source is two external files, not the lower-priority
HQ workspace. Seed central desired provider definitions from all selected effective sources,
preserving canonical group paths, filters, transport and public non-secret auth metadata.
Keep actual host roots/runtime paths/credentials and external override files host-local.
Reconcile the six effective groups with the seven-block HQ fallback explicitly before apply;
compare semantic parity for the source host and fresh central-only enrollment. Do not reuse
HQ's current first-file-only scaffold helper or silently select stale fallback definitions.
See [the seed inventory](dolt-hq-current-seed-inventory.md) for the manifest and dispositions.

## Authority and storage boundary

Use the shared Dolt service as the central storage substrate. Keep a distinct, versioned
HQ configuration schema/database, HQ-origin Beads data, and each hive's existing Beads
database logically separate. The membership database may become the HQ configuration
database through an explicit migration; do not add application tables to a Beads-managed
schema without a supported ownership contract. Co-location does not make HQ an aggregate
copy of every hive or change database identity. Preserve this hive's database `bh`, prefix,
issue IDs, history, and remotes. Use existing supported Beads operations for attachment or
hydration; no direct writes into Beads issue tables.

Version shared fleet configuration, workspace definitions, hive registrations and desired
non-secret database/remote bindings, trusted public-key policy, admission and attachments.
Preserve schemas, extension fields and canonical provider/org/repo identities. Observed
manifests and signed registrations retain their provenance and freshness; importing them
does not promote an untrusted candidate to an admitted frame. Existing legacy executor
compatibility remains explicit and does not derive new authority from file modification
times or sender timestamps.

Store heartbeats/reports outside config commits, as in the membership ADR. Persist trusted
observer receipts, sequence floors, incarnation/epoch grants and signer retirement under
operator-controlled authority. A committed config snapshot and the live authority facts
used together must be consistent. Signature, replay, freshness and restore-denial checks
remain mandatory. Runtime SQL users do not gain operator config/admission privileges.
Pinned-server grants, TLS, ignored-table and backup behavior are implementation proofs,
not guarantees inferred from MySQL compatibility.

Central fleet config replaces the remote config source; local host overrides and runtime
paths remain host-local. Apply the existing ownership policy and override rules. In
particular, preserve the misplaced `work.identity` values in the correct host layer for
the source host, reconcile conflicting values explicitly, and exclude them from the fleet
seed. Credentials stay in the broker/local credential store; import only non-secret
references. Reject unknown ownership and secret-bearing entries with precise diagnostics
rather than silently dropping or promoting them. Endpoint/CA/broker bootstrap metadata
must be available before loading fleet config, avoiding a circular config dependency.

## Read and write behavior

All fleet consumers and writers use a backend-neutral configuration store: config
load/edit/provenance/schema, hive add/init/rm, workspace materialization, host provisioning,
registration and HQ status/sync. Add a backend-neutral way to attach a host to central HQ;
the old clone/init behavior remains supported for explicit Git mode. Central reads return
a committed revision; writes validate and publish atomically with an expected revision so
concurrent updates cannot overwrite one another. Caches include backend and revision in
their identity and expose revision/age/provenance.

New enrollment with a central endpoint selects Dolt explicitly. Existing installations do
not change backend merely by upgrading. Keep Git compatibility for fleets still selecting
Git; this fleet's cutover does not wait on GitHub becoming the preferred authority. The
hosted-Git investigation remains a qualification requirement for its own supported path,
not proof that central SQL is supported. Selecting Dolt cannot silently fall back to the
stale HQ checkout on error. Outages surface unavailable state; authoritative mutations,
new admission and dispatch require valid current authority. Any bounded read cache has an
explicit freshness and offline policy.

## Seed, switch and rollback

1. Inventory and snapshot the real current configuration. Distinguish intended registration
   from stale observations; classify fleet, host, secret-reference and derived fields.
2. Implement and qualify the central backend and all fleet config readers/writers.
3. Use the existing HQ migration bead (`bh-4shu3`) for idempotent seed and verified cutover:
   import a configuration revision with provenance, compare canonical resolved config and
   hive identities, and report every difference. Import neither private keys nor old beats
   as fresh authority. Protect a nonempty destination from unintended replacement.
4. Freeze source writes for the final reconciliation, compare expected source/destination
   revisions, switch the explicit backend pointer, read back, and resume one authority's
   writes. Failure retains the original usable config or the verified new authority and
   reports the precise phase; retry never duplicates objects or discards concurrent writes.
5. Prove existing executors retain host identity, key and hive lease ownership. Prove a
   fresh host with only non-secret enrollment and broker access can load config, attach or
   hydrate its Beads database, and register without an HQ GitHub token or HQ checkout.
   Product source checkout remains governed by its own provider/access requirements.
6. Exercise backup/restore including authority and credential/grant recovery. After restore,
   reject replayed observations and require fresh signed evidence before new eligibility.

Rollback after central edits cannot be just flipping a pointer to an old Git snapshot.
Freeze writes, export the latest verified authoritative revision into a supported Git-mode
layout, verify parity and identity, then explicitly change mode. Preserve sequence floors
and incarnation/signing authority, or remain fenced pending safe recovery. Keep the existing
Git/Dolt round-trip verification requirement. A configured shared-server engine is not by
itself a remote central HQ; implementation must qualify the chosen network endpoint and
custody rather than assuming local fixtures establish remote support.

## Deprecating beadhive-hq

After successful cutover, the old repository becomes a labeled historical snapshot, with
the last authoritative revision and new source documented. Stop routine config writes and
automatic config sync to it; do not create dual authority. A deliberate export is a snapshot
or rollback artifact and cannot be consumed as current config in Dolt mode.

Deprecation checks cover every config reader/writer, host enrollment, existing executors,
HQ-origin `hq-` beads, intake/escalations, backup/restore, signing policy, workspace/hive
catalog and credentials/transport. If HQ-origin beads still replicate through the repository,
retain that channel until a verified independent durable path exists; config cutover alone
does not authorize losing those beads. Record the remaining dependency and block repository
retirement. Do not conflate a GitHub source repository with the HQ configuration repository.

Retirement requires no active consumers or writers, proven restore and rollback, and a
named retained snapshot. Actual archive/delete, credential revocation and production
cutover remain explicit deployment actions; this planning turn performs none of them.

## Delivery and reconciliation

Amend the existing membership port, SQL binding, migration, eligibility, end-to-end proof
and documentation beads rather than duplicating their implementation. Add a nested follow-on
for seed ownership/provenance, central fleet config access, fresh-host bootstrap, and
repository deprecation readiness. Wire implementation dependencies to the actual beads.
The SQL binding must not depend on new config access that itself consumes the binding.
Keep existing submitted/in-progress assignments and review requirements intact.
The merged membership ADR remains historical evidence, not a completed config migration.
Existing HQ sync/topology/hydration backlog is reconciliation input, not automatically closed
or accepted; backend-aware shared collaborators should avoid competing config sync surfaces.

### Filed work and integration order

The compiler filed `bh-f964m` — Dolt-first HQ config cutover seeded from the current fleet —
as a nested follow-on under `bh-yzhcg`. Kickoff was approved by the operator on 2026-10-01.
It reuses the existing
open migration (`bh-4shu3`) and end-to-end proof (`bh-nnhsc`) by moving them into the
follow-on; their requirements and original parent provenance remain recorded. This keeps
the complete config/import/bootstrap/recovery implementation on one assembled branch,
avoiding a child container that must finish before its own upstream migration can run.

| Work | Bead | Prerequisites |
| --- | --- | --- |
| Current seed ownership and provenance | `bh-wd7wr` | follow-on kickoff |
| Central fleet config readers/writers | `bh-9ej9n` | seed inventory and SQL binding `bh-v0k3i` |
| Seed and verified authority switch | `bh-4shu3` | config store and existing SQL binding |
| Fresh-host bootstrap and executor continuity | `bh-b5urp` | config store and verified migration |
| Membership and central-cutover end-to-end proof | `bh-nnhsc` | bootstrap plus original admission/eligibility/port prerequisites |
| Historical repo/deprecation readiness | `bh-zg0lp` | bootstrap and end-to-end proof |

Dispatch refinement: the nested epic tracks the existing SQL binding; that prerequisite
blocks the config implementation leaf and migration, rather than the read-only inventory.
This permits inventory to start immediately while preserving implementation ordering.
Managed assign/claim refreshes the nested container from its integration base before later
children fork, incorporating the integrated parent prerequisites through the lifecycle.
Do not refresh containers by manually merging parent branches. Parent documentation
(`bh-yka1n`) depends on the assembled follow-on, not an unintegrated child change.
Existing membership assignments remain intact. These dependencies specify readiness,
not a claim that production has been switched or that implementation review has passed.
