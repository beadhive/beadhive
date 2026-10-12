# beadhive-hq-storage-dolt

The Dolt adapter for the [`beadhive-hq-storage`](../beadhive-hq-storage/README.md) port
(`bh-br1cd`). A **library package** under
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md). Design:
[hq-storage-port-adr.md](../../docs/design/hq-storage-port-adr.md), including its "Settled by
`bh-br1cd`" section.

## What it holds

| Name | Satisfies | Over |
|---|---|---|
| `DoltHqStorage` | `VersionedRecords`, `AtomicRecords`, `AppendLog`, `Liveness`, `CommittedHistory`, `CapabilityHost` | a root-injected DB-API `ConnectionFactory` (`Callable[[Role], Connection]`); with `runtime_tables=True`, root's failover-policy and per-incarnation tables |
| `DoltPlacementStore` | `PlacementStore` | `DoltHqStorage`'s `PLACEMENT` records |
| `DoltConfigHistory` | `CommittedHistory` | root's committed `hq_config_*` tables in the HQ config database |
| `DoltHiveFence` | `HiveFence`, `CapabilityHost` | a `FenceStatementEngine` (root's `DoltCliEngine` / `BdServerEngine`, or `SqlConnectionFenceEngine`) |
| `DoltHistory`, `DoltMerge`, `DoltAccounts` | the Dolt-only capabilities (`bh-3kl1k`) | a `SqlRunner` over the factory, the engine, a server login or an operator cursor |
| `DoltSqlCapabilities` | `CapabilityHost` | one `SqlRunner`, offering any of the three |
| `DoltRuntimeReads` | the Dolt-only signed-authority read | one principal-checked, read-only `READER` transaction yielding `HqRuntimeStatements` |
| `HqRuntimeStatements` | the HQ runtime database's Dolt-only statements | a caller-owned DB-API cursor (root's runtime authority, receiver, operator, session rows and the committed config reads they share) |

Every SQL string, Dolt procedure call and error-code classification for the port lives here.
Root keeps the transport (`hq_sql_transport.connect`, PyMySQL) and composes the adapter in
`beadhive.hq_storage_composition`.

## Operation mapping

Every mutation reaches exactly one commit point (`COMMIT`, `CALL DOLT_COMMIT`, `CALL DOLT_PUSH`
or the engine's push), even when its outcome is already a `Conflict`. So a lost acknowledgment is
`Unknown` with the original expectation, as on the fake.

| Operation | Dolt | `Applied` | `Conflict` | `Unknown`, and the witness its lookup finds |
|---|---|---|---|---|
| `compare_and_set` on `PLACEMENT` | `UPDATE hq_live_hive_leases ... WHERE prefix AND revision` | a fresh `sha256(record ‖ uuid4)` revision; a caused carrier write's revision is bound to its `OperationId` | `rowcount 0`, or `1213` at `COMMIT` | the `OperationId` in `request_id`, or for a caused carrier write the revision bound to it under a verifying witness; `lookup` |
| `compare_and_set` on the other kinds | `INSERT ... WHERE NOT EXISTS` from `ABSENT`, else a guarded `UPDATE` of `hq_live_records` | a fresh revision | `rowcount 0`, or `1213` | the `operation_id` column; `lookup` |
| `compare_and_set` on `FAILOVER_POLICY` (`runtime_tables`) | read the scope's `hq_live_failover_policy` rows, compare their content digest, then a `DELETE` / upsert per changed setting | the content digest of the new rows | a different digest (or rows where `ABSENT` was expected), or `1213` | none: the table has no witness column, so `lookup` is always `NotConfirmed` |
| `compare_and_set_all` | every member's write in ONE transaction, then `COMMIT`; a failed member rolls the whole transaction back | `writes[0]`'s fresh revision; every member carries the `OperationId` | the first failing member's current value (nothing written), or `1213` | `lookup` of any member (the placement's `request_id` witness for the director's write) |
| `renew` / `ages` / `stamped_at` on `SESSION` / `EVIDENCE` (`runtime_tables`) | the provisioned `frame_<principal>_<epoch>_session` / `_evidence` row's `UPDATE` (root's renewal or evidence statement); ages in ONE `UNION ALL` statement of `TIMESTAMPDIFF(MICROSECOND, <stamp>, UTC_TIMESTAMP(6))` | the server stamp | not applicable: a missing table or row is `NotProvisioned`, never an `INSERT` | none: a lost acknowledgment raises `StorageError` |
| `append` | `INSERT` into `hq_live_append` | a new row, or the same key, operation and body again | any other replay of the key, a duplicate key, or `1213` | the natural key plus `operation_id`; `lookup_append` |
| `prune` | `DELETE ... TIMESTAMPDIFF(MICROSECOND, recorded_at, UTC_TIMESTAMP(6)) > window` | `PruneReport` | not applicable | none: a lost acknowledgment raises `StorageError` |
| `renew` | `UPDATE` or `INSERT` of `hq_live_liveness`; a trigger stamps `UTC_TIMESTAMP(6)` | the server stamp | not applicable | none: a lost acknowledgment raises `StorageError` |
| `publish` | head check, sequence CAS, documents, publication row, `DOLT_ADD` of an explicit list, then `DOLT_COMMIT` | the exact commit hash, parent verified | a moved or `ABSENT` head, a lost sequence CAS, or `1213` | the `publication_id` row and its parent; `lookup_publication` joins `dolt_history_*` to `dolt_commit_ancestors` |
| `CommittedHistory.push` | `DOLT_FETCH`, then `DOLT_PUSH` | the local head | not a fast-forward (`dolt_merge_base`) | the expectation is the remote head before the push |
| `commit_bump` | the engine runs the bump statements plus the `ReclaimSet` writes (lifecycle revert, `claim-frame` cleared, `recovery:resumable`, audit comments; each guarded on `in_progress`), then commits once | the new local head | not applicable: at or below the floor raises `IntegrityViolation` | a failed statement batch discards its partial working set and raises `StorageUnavailable` (nothing committed); a raise from the engine commit, or from the head read after it, is `Unknown` with the pre-bump local head as the expectation (bh-xxkf7.6). Never re-issued: `push`, then the `adopt-<epoch>` sentinel on the remote head; `lookup_bump` |
| `HiveFence.push` | engine fetch, then engine push | the local head | the push returns `False`, or not a fast-forward | the `adopt-<epoch>` sentinel on the remote head; `lookup_bump` |

**The placement carrier** (`bh-sqfvq`, `_placement.py`). A `PLACEMENT` body of `authority` and
`lease`, plus an optional `cause` and `director`, is the trusted receiver's row:

- `lease_json` is exactly `canonical({"authority", "lease"})`;
- a `cause` (`failover` / `planned`) rides in `request_id` as root's cause token, bound to the
  prefix and revision, so 0.23.x frames still read it;
- an uncaused write puts its `OperationId` in `request_id`;
- `request_sha256` is the director witness.

A read of a canonical carrier row returns `director` (whether the witness verifies) and, on a
director row, the `cause`. Any other row decodes as plain JSON without `director`. The
operator's seed `INSERT` (`placement_seed_statement`) is here too; root's conformance check reads
grants and triggers through `DoltAccounts`.

**The HQ config publication** (`bh-x7xtc`, `_config.py`). `DoltConfigHistory` maps
`CommittedHistory` onto root's committed config layout, column for column as `hq_sql_config`
always wrote it:

- `ChangeSet.tables` must be exactly `hq_config_meta`, `hq_config_documents` and
  `hq_config_publications`, which is also the explicit `DOLT_ADD` list;
- the documents are `hq_config_meta/schema_version` (ASCII decimal), then
  `hq_config_documents/<path>` raw UTF-8 bytes in publication order (`config_change_set`);
- `publish` checks the original head, a clean `dolt_status`, the exact base-table allowlist and
  an unused publication id, moves the meta singleton by a sequence CAS, replaces the documents
  (ordinal, kind, `content_sha256`), appends the witness row with the original parent, then
  `DOLT_COMMIT`s. A dirty tree is `IntegrityViolation`, a changed allowlist `NotProvisioned`;
- `lookup_publication` confirms the witness row `AS OF` the current head against the original
  parent, with `HAS_ANCESTOR`. The config reader has no grant on `dolt_log`,
  `dolt_commit_ancestors` or `dolt_history_*`, so `Confirmed.revision` is the head the witness
  is visible at and `recorded_at` the server clock at lookup. `Applied.revision` is the exact
  commit.

The document kinds (`config_document_kind`) and digest (`config_documents_digest`) restate
root's `hq_sql_config._kind` and `ordered_documents_digest`. Initial seeding into the empty
schema is a separate root contract (`bh-4shu3`) and is not here.

**`principals`** makes every session for a role first check `CURRENT_USER()`, `DATABASE()` and
`ACTIVE_BRANCH()`, refusing a mismatch with `StorageUnavailable`.

**Root's runtime database** (`bh-xnn5p`, `_runtime.py`, `_incarnation.py`, `_failover.py`).
`HqRuntimeStatements` names every statement root's runtime authority, trusted receiver, operator
and session rows run, exactly as root ran them, over root's own cursor: `AS OF` authority and
registry reads, `HAS_ANCESTOR`, `dolt_commit_ancestors`, `DOLT_DIFF_SUMMARY`, `dolt_schemas`, the
receipt/floor/observation/result acceptance writes and the authority `DOLT_COMMIT`. These are
Dolt-only (commit-graph and history reads, multi-table acceptance in one commit), so root keeps
its connection, transaction and deadline and holds no SQL. `DoltRuntimeReads.pinned()` replaced
the transitional `DoltHqSessions`: one read-only transaction with no raw cursor and no write.
The incarnation DDL, the eligibility statement (which returns ages and committed TTLs; the
thresholds are root's) and the failover-policy grant are rendered here too.

**The Dolt-only capabilities** (`_capabilities.py`, final shapes settled by `bh-3kl1k`). Each
runs over a `SqlRunner` (`query`, `execute`, and `session` for a one-session batch):

- `DoltHistory`: `head`, `log` (optionally by subject prefix), `commit`, `parents`, `messages`,
  `ahead`, `merge_base`, `is_ancestor`, `remote_branches`, and the fence tables at a revision:
  `fence_tables_at`, `writer_at`, `live_epoch_at`, `stale_marks_at`, `mark_ids_at`,
  `writer_epochs`, `history_max_epoch`, `table_history`. Every data table it reads is a
  `FenceTable`.
- `DoltMerge`: `preview_conflicts`, `merge`, `merge_restamping` (the writer's one-session orphan
  merge), `residue`, `dirty_tables`, `commit_working_set`, `abort`, `reset_hard`, `publish`
  (fast-forward-only push of the branch) and `publish_branch` (the orphan divert). Over a fence
  engine, the engine's own `commit`, `push`, `push_branch` and `reset_hard` win.
- `DoltAccounts`: `users`, `create_user` (from a password or its `mysql_native_password` hash),
  `set_password`, `drop_user`, `grants`, `grant` and `revoke` (both return the statements they
  ran), `base_tables`, `sessions`, `kill`, `global_variable`, `server_identity`, `triggers`
  (from `dolt_schemas`, never `information_schema.triggers`) and `provision_hq_schema`.

A capability refuses a bad argument with `InvalidRequest` before any I/O and otherwise lets the
runner's exception through unchanged. `DoltHiveFence` offers `DoltHistory` and `DoltMerge`;
`DoltHqStorage` offers all three; `DoltAccounts` over a hive server is composed from that
server's login in root.

**Error classification** is by DB-API `args[0]` code; the adapter never imports a driver.

- **Before the commit point:** `1213` or `1205` is a `Conflict`. Anything else is
  `StorageUnavailable`.
- **At the commit point:** `1213` is a `Conflict`. Otherwise the reason is:
  - `"deadline"` for `TimeoutError`, `3024`, `1317` or root's deadline predicate;
  - `"transport"` for `2006`, `2013` or `2055`;
  - `"commit"` for `1180` and anything unclassified.

## Tests

| File | What | Needs |
|---|---|---|
| `test_hq_storage_dolt_contract.py` | the shared contract suite over `[fake, dolt]`, and the differential state machine fake against Dolt | `dolt` and PyMySQL, else the `dolt` cases skip |
| `test_hq_storage_dolt_adapter.py` | classification over a scripted DB-API stub; the placement wire format, the config publication's `hq_config_*` rows, ignored live tables, server stamps and the three Dolt-only capabilities (`bh-3kl1k`) on real Dolt; the runtime-table mappings (failover policy rows, incarnation liveness, the atomic placement-plus-policy write) and the pinned runtime read | Dolt for the real-Dolt cases |
| `test_dolt_semantics_int.py` | Dolt 2.3.5 server contracts (grants, triggers, accounts, globals, history, fence engine), folded from root | `dolt` (some cases also need a `127.0.0.2` alias) |

`beadhive_hq_storage_dolt.contract` holds the `dolt` `ContractAdapter`, its harnesses, real
fault injection at the adapter's commit points, and `DoltSqlServer`: one private
`dolt sql-server` that takes a `BH_DOLT_SLOTS` slot from root's lock files and waits for the
server's ready line instead of sleeping. The gate runs only a smoke of the machine against
Dolt (2 examples x 8 steps, derandomized, shrinking excluded). The full budget
(`dolt_machine_full`, `gate` profile 25 x 20 without shrinking; any other profile shrinks,
capped at 100 examples) is deselected from `just hq-storage-check` and runs under
`just hq-storage-dolt-machine [profile=gate]`.

## Import rules

- Imports `beadhive_hq_storage` only through its `__all__`, and nothing from `beadhive`.
- Never imports PyMySQL; tests inject `pymysql.connect` into `DoltSqlServer` themselves.

## Gate

The package shares the `hq-storage` attest key with the port package
(`just hq-storage-check`). Real-Dolt tests carry the `integration` and `dolt_server` markers,
and the folded canary cases keep `fence_canary` (`just fence-canary` includes this package).
