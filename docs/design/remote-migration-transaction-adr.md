# Remote-migration transaction, corpus fingerprint, and retirement ADR

**Status:** accepted · **Date:** 2026-10-19 · **Decision bead:** `bh-ksmt0.1` ·
**Supersedes:** nothing · **Amends:** nothing
**Related:** epic `bh-ksmt0`; sub-epic `bh-ksmt0.11` (phase A); `bh-ksmt0.12` and `.5`–`.9`
(phase B); `bh-l3zvc` (the first consumer, `.1`–`.5`); `bh-ga5sz` (store/remote/authority
nomenclature); `bh-yxbq`, `bh-0wewj` (reconciled below);
[hq-dolt-remote-descriptor-adr.md](hq-dolt-remote-descriptor-adr.md) (declares the target remote;
refuses every data move this ADR owns),
[backup-retention-boundary-adr.md](backup-retention-boundary-adr.md) (backup roots),
[dolt-server-mode-adr.md](dolt-server-mode-adr.md),
[hq-dolt-remotes-runbook.md](hq-dolt-remotes-runbook.md)

Defines the transaction by which a hive's authoritative Dolt corpus moves between remote
transports, the ledger that makes it resumable, the fingerprint that proves nothing was lost,
and the rule that retiring the source is a separate, explicit, per-transport act. It also
splits the work into phase A (what the Beadhive instance cut-over needs) and phase B (the
automated verb, deferred).

## Context

A hive's Dolt remote is today the code repository's `refs/dolt/data`. The descriptor ADR lets
HQ *declare* another remote (S3-compatible, `file://`, ...) and refuses to move data. Moving
data is a one-way operation on the only copy of the bead history; the failures already
measured on this fleet are all of one family: a step that silently destroys state which is
registered *against* the database, not stored *in* it (the remote registration, the git
transport cache, the backup slot), with verification that cannot fail (a constant
`schema_version`, an exit code, a row count, a non-empty directory).

Fixed inputs for the first consumer (`bh-l3zvc`):

- The private internal store moves to
  `s3://beadhive-hives/<db>?endpoint=http://<garage-host>:3900&path-style=true&region=garage`.
  Dolt **server mode only**; bd embedded mode is being deprecated and is not designed for here.
- **Garage has no S3 object versioning.** An overwrite or delete on the remote is
  unrecoverable from the remote. Recovery rests on native Dolt backups that have been
  restore-tested, plus PBS snapshots of the Garage host.
- Credentials reach the shared sql-server's **start-up environment**: `dolt.server_credentials`
  (`bh-pphik`) when bh starts the server, `python -m beadhive.dolt_server_credentials render`
  (`bh-gaytj`) when systemd/launchd does. Credentials never appear in URLs, ledgers,
  fingerprints or logs (`dolt_remote.redact_url` on every rendered URL).
- The cut-over (`l3zvc.4`) is **supervised and operator-present** inside a scheduled freeze of
  about four hours (`l3zvc.1`–`.4`).
- Afterwards the old remote (the code origin's `refs/dolt/data`) is **not** retired by
  restoring anything. `l3zvc.5` replaces it with the audited public store through the guarded
  `bh hive remote sync --store <tracker> --force-with-lease`. The never-restore rule applies:
  the old SHA held the private corpus and must never be pushed back.
- Vocabulary follows `bh-ga5sz`: *store*, *remote* (kind `dolt` / `tracker` / `peer`), exactly
  one *authority* per store, everything else a *replica*; pull from the authority, push to the
  replicas. This ADR concerns `dolt`-kind remotes only; a tracker remote is never a migration
  source or target.
- Never `bd compact` / `bd flatten`. Bead history is the only lifecycle archive (CLAUDE.md);
  every phase below is built to preserve it, and the fingerprint compares it.
- Everything here is **configurable and off by default**; nothing runs unless an operator
  invokes the verb or enables the setting.

## Decision

### 1. Two separate command contracts

| Contract | Question it answers | Verb (existing / planned) | Moves |
|---|---|---|---|
| **Engine-mode migration** | Which Dolt *engine* holds the store on this host (embedded to server)? | `bh hive migrate-storage` (exists) | the store, same remote |
| **Remote relocation** | Which *remote transport* is the authority for the corpus? | a new verb under the `bh-ga5sz` `bh hive remote` group (name fixed by phase B; phase A ships primitives only) | the authority, same engine |

They share primitives (lock, ledger, backup plan, dry-run, per-hive isolation, fingerprint) and
nothing else. `migrate-storage` is **not** overloaded with relocation flags; a relocation verb
never changes `dolt_mode`; neither verb is a precondition of the other. Where a defect lives in
the engine-mode contract (see classification) it is fixed there, not folded into relocation.

### 2. The transaction

Every phase is idempotent, writes a ledger record before and after, and has a defined rollback.
"Source" is the current authority; "target" is the new one. The source is **never mutated** by
any phase before `cutover`, and never destroyed by any phase at all except `retire`.

| # | Phase | What happens | Ledger gate (must be true to leave) |
|---|---|---|---|
| P0 | `assess` | Read-only. Resolve hive, store, engine (must be server mode), source and target remotes (`dolt_remote.classify`), credentials presence, free space, `bd` / Dolt versions, pending schema delta, whether a local and remote merge base exist. Compute the **source fingerprint** (section 4) at the current HEAD, in both scopes. Dry-run stops here and prints the plan. | assessment recorded, no blocker |
| P1 | `quiesce` | Take the per-hive migration lock (reuse `hive_migration_lock`; stale-PID detection retained). Fence writers: set the store read-only for bh (writer fence), refuse concurrent `bd dolt push/pull`. Record the HEAD commit that will be migrated. | lock held, writers fenced, HEAD pinned |
| P2 | `snapshot` | Three artifacts under a unique, dated set in the `migrate` backup root (manifest per `bh-5009a`): JSONL export, **native** Dolt backup (over the connection, never a tar), and the **registry snapshot** (every Dolt-level remote and backup registration read below bd's abstraction, plus HQ descriptor and `.beads/config.yaml` `sync.remote`). Source fingerprint re-taken and must equal P0's. | each artifact verified non-empty and count-consistent |
| P3 | `attest-backup` | **Restore-test** the native backup into a scratch database with a unique name and unique path, compute its fingerprint, require equality with the source. Scratch is dropped afterwards. A backup that was not restored is *unverified* and blocks P4. | restored fingerprint == source fingerprint |
| P4 | `stage` | Register the target under a **temporary remote name** (`migrate-<txn>`) at a unique path (section 5). The canonical `origin` is untouched. Push the full history. | push exit 0 **and** target readable |
| P5 | `hydrate` | Full-history clone of the target into a clean, independent store (unique database name and path, never the live database, never shallow). | clone complete |
| P6 | `verify` | Compute the hydrated store's fingerprint; compare to source (strong tier, **committed** scope, section 4). Any mismatch is a stop, with a diff report. | fingerprints equal |
| P7 | `cutover` | The only phase that changes meaning: switch the canonical remote (replace `origin` by the engine remote API, never raw `dolt remote add`, which silently re-points an existing name) and update the HQ descriptor. Re-create transport caches rather than copy them. Source registration is kept under the name `retained-<txn>` for rollback. | registry reads back as intended |
| P8 | `prove` | Prove normal operation: a write, `bd dolt push`, a fresh clone/bootstrap of the new remote, `bd dolt pull` rc=0, fingerprint of the bootstrapped store equal. Unfence writers. | round trip green |
| P9 | `hold` | Source is retained untouched for a configured window. Backups from P2 are retained. This phase has no automatic exit. | operator or window |
| P10 | `retire` | **Separate command**, separate lock, separate confirmation, per-transport capability (section 6). Never run by the migration verb. | per section 6 |

### 3. Ledger

An append-only JSON-lines file per transaction at `$BH_HOME/migrations/<hive>/<txn-id>/ledger.jsonl`,
schema `bh.remote-migration-ledger/1`. Each record: `seq`, `txn`, `phase`, `state`, `ts`,
`actor`, `inputs` digest, `evidence` (paths and digests, never secrets), and `prev` (sha256 of
the previous record, so truncation and edits are detectable). Records are fsynced before the
phase's mutation and after its outcome. `bh` computes the current state by replaying, never by
trusting a mutable status field.

Per-phase states: `pending`, `running`, `done`, `failed`, `rolled-back`, `skipped`.
Transaction states, derived: `planned`, `quiesced`, `snapshotted`, `backup-attested`,
`staged`, `verified`, `cut-over`, `proven`, `held`, `retired`, `aborted`, `needs-operator`.

Invariants:

1. **Single writer.** One live lock per hive; a ledger record by a non-holder is a fault.
2. **Source immutability.** Before `cutover` no phase writes to the source remote or database;
   after `cutover` none deletes from it.
3. **No state without evidence.** A phase reaches `done` only with its gate evidence recorded.
4. **Verification can fail.** Every gate compares two independently derived values; a gate
   that compares a value to itself or to a constant is a defect.
5. **A running phase found on resume is `failed`**, not assumed complete; it is re-run from its
   start (all phases are idempotent) after the executor re-checks its preconditions.
6. **Resume never skips forward.** It continues at the first non-`done` phase, and re-verifies
   the P3 attestation if more than a configured age has elapsed or the pinned HEAD moved.
7. **No irreversible action is ever auto-retried** (`cutover` after a partial success,
   `retire`); these go to `needs-operator`.

### 4. Corpus fingerprint

Schema `bh.corpus-fingerprint/3` (`/1` hashed engine-rendered values and differed between server
and embedded reads; bh-m4ups): values are canonicalised (DATETIME to one UTC form, JSON
key-sorted) before hashing, the strong tier also carries Dolt-native
`dolt_hashof_db`/`dolt_hashof_table`, and `/1` digests are refused, never compared. `/3`
(bh-omxyj) adds the **scope** below; `/2` documents (existing backup manifests) are still read and
are re-computed in `/2` form when re-verified, never compared with a `/3` digest. A canonical
JSON document, key-sorted, UTF-8, reduced to a sha256, with every component also kept so a
mismatch reports *which* component differed.

| Component | Source | Why |
|---|---|---|
| `project_id` | `.beads/metadata.json` / store | refuses the PROJECT IDENTITY MISMATCH class |
| `prefix` | store config | identity, not a label |
| `schema_version` | **real** store probe (`dolt_health.probe_raw_schema_version`), never `bd dolt status --json` | the JSON-envelope constant is always 1 and cannot detect anything (`bh-ycmb`) |
| `main_head` | Dolt `main` HEAD commit hash | content-addressed; encodes entire history |
| `branches` | name to head for every branch | refuses a lost non-main branch |
| `issue_count` | store | cross-check, never sufficient alone |
| `issue_ids_digest` | sorted issue identities | identity set |
| `issue_rows_digest` | per-issue canonical row digest | content, not just identity |
| `tables` | for a configurable list (default: dependencies, labels, comments, events, config, metadata): row count + digest, names resolved against the **real schema** | gates, deps and notes live outside `issues` |
| `excluded` | explicit list | volatile caches, local-only files, transport caches are named exclusions, not silent ones |

Two tiers: the **strong** tier includes `main_head` and `branches` and is required whenever the
target is hydrated from the *same history* (relocation: P3, P6, P8). The **corpus** tier omits
the commit hashes and is the only tier meaningful when comparing a JSONL-derived or
re-initialised store (engine-mode migration, JSONL restore). A tier is always recorded next to
the digest; comparing across tiers is refused. A non-empty directory, an exit code, or a JSONL
row count alone is **never** verification.

#### Fingerprint scope (committed vs working)

`dolt_ignore` marks tables that live only in the Dolt **working set**: never committed, pushed or
cloned. bd ignores `events`, `bd_events_journal`, `bd_events_seq`, `bh_local_%`,
`ignored_schema_migrations`, `leases`, `local_metadata`, `repo_mtimes`, `wisp_%` and `wisps`. Ignored
tables are discovered from the store's own `dolt_ignore` patterns (a pattern matching a requested
table counts even when that table is absent, as it is in a clone), never from a hard-coded list. A
clone of a pushed store therefore can never equal its source on `events`, so the fingerprint has two
scopes, recorded next to the tier and refused across (`FingerprintScopeMismatch`):

| Scope | Reads | Digested tables | Native block | Used by |
|---|---|---|---|---|
| `committed` (default) | `AS OF` the `main` head | non-ignored only | `root` = `dolt_hashof_db('main')` plus per-table `dolt_hashof_table` of clean committed tables | **relocation**: clone-vs-source (P0 baseline, P6, P8) |
| `working` | the working set | non-ignored **and** the requested ignored tables (`components.local_only`) | `db` = `dolt_hashof_db()` plus per-table hashes | **native backups**: P2 snapshot, P3 restore attestation, `backup verify`/`restore` |

Local-only tables are always reported beside the digest (`local_only`: name, row count, a digest
wherever the rows were read) and never gate a relocation. `migration.fingerprint.local_digests`
(default off) adds their digests to the committed-scope report. Because a native backup carries
the working set, the `working` scope keeps comparing the requested ignored tables (default:
`events`), so drift there still fails a backup verify. Per phase: P0 records BOTH scopes of the
source (committed for the relocation baseline, working for the snapshot); P2 re-takes each at its
own scope and requires equality with P0's; P3 compares working-scope source and scratch restore;
P6 and P8 compare **committed**-scope fingerprints of the source and the hydrated/bootstrapped
store (the working-set `events` of the source is not on the remote, so it is reported, not
compared).

### 5. Official-guidance record

Recorded so that implementation does not rediscover it. Items marked (repo) are evidenced by a
bead, test or source comment in this repository; items marked (verify) are upstream guidance
as understood and **must be re-verified against the pinned Dolt/bd by the test that depends on
them** (phase A tests use `file://` and moto, never a live endpoint).

- **Native backup is the recovery artifact.** `dolt backup add|sync|restore` / `bd backup
  init|sync|restore` copy the whole database including history; the JSONL export is portable
  interchange and does not preserve Dolt history. A backup is trusted only after a restore
  into a scratch database matches the fingerprint (P3). (repo: `bh-kobw`, `bh-5009a`.)
- **A backup is not a remote** and must not share an address with one. Dolt refuses a backup
  at an address a remote already claims, and that remote can be invisible to `bd dolt remote
  list`. (repo: `bh-wov4i`.)
- **bd has one `default` backup slot per database.** Two writers (HQ backup, hive backup
  rotation) race for it. The attested backup uses a unique per-transaction destination and
  restores the previous registration afterwards. (repo: `bh-1sug`.)
- **Remote registration belongs to the database, not the directory.** `bd bootstrap` drops
  and recreates the database; that erases its remote and backup registrations, and the
  git-transport cache that lived beside it. The registry snapshot (P2) and the post-state
  check (P7, P8) exist for this reason. Transport caches are *derived*: rebuilt, never backed
  up or copied. (repo: `bh-xyt8c`, `bh-j42f0`, `bh-ypfnu`.)
- **Register remotes through bd's engine seam only.** `dolt remote add` with an existing name
  exits 0 and silently re-points it (repo: `engine.add_remote` comment, bd 1.3.0), so add is
  probe-then-add and replace is explicit.
- **Full-history clone.** Stage and hydrate use a complete clone. A shallow or depth-limited
  clone cannot satisfy the strong fingerprint and is refused as a hydrate source. (verify)
- **Unique paths.** Every staged remote, scratch database and backup destination is addressed
  by `<db>-<role>-<txn-id>`; a path already holding data is a blocker, never an overwrite.
  `refs/dolt/data` is a single hard-coded ref per Git repository, so a git target can hold one
  database; object-store targets namespace by prefix inside one bucket. (repo: `bh-0wewj`.)
- **URL guidance.** `s3://<bucket>/<db>?endpoint=<url>&path-style=true&region=<region>` for an
  S3-compatible endpoint, server mode only (repo: descriptor ADR, `bh-82gpi`). Credentials are
  in the server's start-up environment, never the URL; the Garage URL above is the target
  form. Because the server reads the URL, a plain-http endpoint is acceptable only on a trusted
  LAN and is recorded as such in the assessment.
- **Garage has no object versioning.** An overwritten or deleted object cannot be recovered
  from the remote; therefore P3 attests *before* P4 pushes, backups are kept off the Garage
  host, and PBS snapshots of the Garage host are a separate, operator-owned control.
- **History is the archive.** No phase compacts, flattens, squashes, force-pushes over a
  non-attested SHA, or GCs. `bd compact` / `bd flatten` are out of scope forever for this verb.

### 6. Retirement is explicit, per transport, and never implied

Cut-over proves the target; it never authorises deleting the source. Retirement is the
separate command in P10, gated by `migration.retire.enabled` (default **false**) and by a
transport capability table. Each row also states its *reversibility*:

| Source family | May retire? | Mechanism | Reversible? | Default |
|---|---|---|---|---|
| `git` (`refs/dolt/data`) | yes, operator-confirmed | delete the ref, or replace it with an audited store via guarded force-with-lease sync (the `l3zvc.5` path) | only while another copy of the exact SHA exists | disabled |
| `file` | yes | remove directory after the retained copy is verified | no | disabled |
| `s3` / `aws` (versioned bucket) | yes | delete prefix | only if versioning is on | disabled |
| `s3` on Garage | **no automatic path** | none; manual operator action after restore-tested backup and PBS snapshot | no (no versioning) | disabled |
| `gs`, `hosted`, unknown | **no** | unsupported | n/a | disabled |

Rules: retirement requires `held` for at least the configured window, a fresh fingerprint of
the target equal to the P6 value, a restore-attested backup no older than a configured age, and
an interactive typed confirmation naming the hive and source URL. **Retire never restores**:
no phase of retirement pushes the old SHA anywhere. For the Beadhive instance, retiring the
code-origin ref is the *replacement* in `l3zvc.5`, which is not this verb and not a
restoration; its guard is the existing never-restore rule.

### 7. Retries and rollback by phase

Retries are per phase, bounded (default 3), with jittered backoff, and only for classified
transient errors (network, lock contention, server restarting). Authentication, mismatch, and
"already exists with different data" are never retried.

| Phase | Retry | Rollback if it fails or is abandoned |
|---|---|---|
| P0 `assess` | free | nothing to undo |
| P1 `quiesce` | on lock contention | release lock, lift fence |
| P2 `snapshot` | per artifact | delete this transaction's incomplete set only (it is new, unique) |
| P3 `attest-backup` | re-run restore | drop scratch DB; keep the backup set; abort |
| P4 `stage` | resume push | remove only the temporary remote *registration*; leave target data (Garage cannot undelete, and the target is new) and record it for the operator |
| P5 `hydrate` | re-clone | drop scratch store |
| P6 `verify` | none on mismatch | abort; keep target as evidence; source untouched |
| P7 `cutover` | **none** | restore the recorded registry snapshot (`retained-<txn>` back to `origin`), restore HQ descriptor, verify registry equals the P2 snapshot, then `needs-operator` |
| P8 `prove` | transient only | same as P7 rollback; the target remains staged |
| P9 `hold` | n/a | the P7 rollback remains available for the whole window |
| P10 `retire` | none | not reversible by this verb; see section 6 |

Rollback is a *registry* operation: it re-points names, it does not move data. If rollback
itself fails the transaction goes to `needs-operator` with the snapshot paths printed, and no
further automation runs.

### 8. Phase A and phase B

**Phase A (`bh-ksmt0.11`, shippable alone, gates `bh-l3zvc.1`)** delivers exactly what a
supervised, operator-present cut-over inside a four-hour freeze needs, as primitives and
inspection, not as an unattended verb:

- `.1` this ADR.
- `.2` primitives: snapshot (P2), fingerprint (section 4), per-hive lock (P1), phase ledger
  (section 3), each independently testable.
- `.3` Engine remote and registration APIs: read all Dolt-level remotes and backups below bd's
  abstraction, probe-then-add, explicit replace, conflict detection, redacted rendering.
- `.4` restore-attested native backups with general `verify` and `restore` verbs (P3's
  engine), including a `bh backup restore` entry point.

During the freeze the operator drives P0 to P8 by hand with those primitives and the runbook,
recording ledger entries; no automated cut-over is claimed. Phase A requires a release and a
frame rotation before `bh-l3zvc` can use the new verbs.

**Phase B (`bh-ksmt0.12`, `.5`–`.9`, deferred)** composes the primitives into the automated
transactional relocation verb with resume, retire, and the cross-hive orchestration. Nothing
in phase A depends on B, and no phase A bead may grow B behaviour.

### 9. Configuration (all off by default)

| Key | Default | Meaning |
|---|---|---|
| `migration.enabled` | `false` | master switch for the relocation verbs |
| `migration.fingerprint.tables` | dependencies, labels, comments, events, config, metadata | extra tables in the fingerprint (a `dolt_ignore`'d one is local-only: compared in the working scope, reported in the committed scope) |
| `migration.fingerprint.local_digests` | off | digest the local-only tables in the committed-scope report |
| `migration.attest.max_age` | `24h` | oldest accepted P3 attestation at cutover/retire |
| `migration.hold.window` | `7d` | minimum `held` time before retire |
| `migration.retry.max` | `3` | per-phase transient retries |
| `migration.retire.enabled` | `false` | permits P10 at all |

## Reconciliation with existing beads

| Bead | Disposition |
|---|---|
| `bh-yxbq` (own backup + schema-migrate + divergence recovery) | **Partly absorbed.** Pre-flight detection (P0), the three-level backup including verification (P2, P3), verify-before-publish with a write check (P8) and the round-trip confirm are this ADR. The remote-side `refs/backup/...` ref copy is a git-transport-only extra, optional under P2. The schema-migrate step (`bd migrate --force`, single designated migrator) and divergence resolution (which side is authoritative) remain `bh-yxbq`'s and phase B's concern. |
| `bh-0wewj` (relocatable data plane per hive) | **Absorbed for the data move**; its remote-configuration slice is settled by the descriptor ADR; its public-repository exposure fix is the `bh-l3zvc` cut-over and retirement, using this ADR's phases and section 6. |

## Classification of related defects

Each was read and checked against the code in `src/beadhive` at `ee62c79c`.

| Bead | Verified finding | Class |
|---|---|---|
| `bh-xyt8c` migrate-storage destroys the Dolt remote | `storage_migrate` captures no remote registration and never restores one (`bd bootstrap` drops the database); `dolt_remote_reconcile` only re-adds from an HQ descriptor, and is not invoked by migration. The registry snapshot (P2), engine remote APIs (`.3`) and post-state check (P7/P8) are this same defect class. | **Absorbed** by `.2`/`.3` (primitives); wiring them into `migrate-storage` is then a small consumer change |
| `bh-j42f0` missing git-remote-cache after migration | Cache lives under the database (`transport_locator`); nothing rebuilds it after bootstrap, so push and pull fail with a misleading credentials hint. Transport caches are derived: rebuilt and re-proven by P8 (push and pull rc=0), never copied. | **Absorbed** (with `bh-xyt8c`; same fix, same proof) |
| `bh-pcsv3` interrupted-migration "re-run to finish" dead end | The guard in `storage_migrate.py` still returns `failed` for `dolt_mode=server` with `embeddeddolt/` present, and the same guard fires on every re-run. Engine-mode contract; relocation resumes by ledger and never touches `dolt_mode`. | **Independent** (may reuse the ledger primitive later) |
| `bh-ycmb` schema_version constant | Already fixed and closed: `verify_migration` now uses `dolt_health.probe_raw_schema_version` and records `unknown` with a problem when undeterminable. The fingerprint reuses that probe. | **Independent** (done; nothing to absorb) |
| `bh-wkp6l` fixture databases on the production server | `detect_target_collisions` compares registered-hive target names only; it never lists the live server's databases. Hygiene of the shared server, not relocation. Constraint inherited: scratch databases use unique role/txn names so they cannot collide with fixtures or real hives. | **Independent** |
| `bh-2hvmd` dropped-database copies | No code reads or reclaims `.dolt_dropped_databases`. P3/P5 scratch drops will each leave one; they must be reported in the ledger. Reclaim is a separate, destructive, operator-gated decision (and must never use `dolt gc` shortcuts without the operator). | **Independent** (constraint noted above) |
| `bh-bmwl7` bh sync should detect half-migrated hives | `hub_bulk` already refuses embedded hives for co-location, but `bh sync` gives no pointer to `migrate-storage`. Engine-mode diagnostics. | **Independent** |
| `bh-1sug` HQ backup versus hive rotation share bd's `default` slot | Still true: `hq._backup_dolt_native` and `backup.rotate_hive_backup` both register the single slot, and `registry.hive_dir_for` falls back to cwd's hive. The attested backup's unique destination and slot save/restore (section 5) is the structural fix. | **Absorbed** by `.4` |
| `bh-wov4i` repoint cannot heal HQ: `backup_export` remote | `repoint_bd_backup` assumes `.beads/backup` is free; no code looks below bd's remote listing. The registry snapshot and conflict detection (section 5) are required for P2 and P3. | **Absorbed** by `.3` (inspection) and `.4` |
| `bh-q0kf` `bh hq restore` orphaned from `bh backup` | `bh backup` still has `export`, `usage`, `migrate-layout`, `reclaim` and no `restore`. `.4` adds `restore`. | **Absorbed** by `.4` |

## Consequences

- The supervised cut-over has a written, checkable path: backup, restore-test, fingerprint,
  stage, hydrate, compare, switch, prove, hold, with a rollback that is a name change.
- The fingerprint can fail where `schema_version: 1` and row counts could not.
- Retirement is never a side effect; on Garage it has no automatic path at all.
- Costs: P3 and P5 need scratch space and time inside the freeze; the registry snapshot
  requires reading below bd's listing (`.3`).
- Open for phase B: the exact relocation verb name under `bh hive remote`, the cross-hive
  orchestration, and whether `held` windows are enforced by clock or by operator attestation.
