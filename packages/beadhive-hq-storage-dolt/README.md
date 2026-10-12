# beadhive-hq-storage-dolt

The Dolt adapter for the [`beadhive-hq-storage`](../beadhive-hq-storage/README.md) port
(`bh-br1cd`). A **library package** under
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md). Design:
[hq-storage-port-adr.md](../../docs/design/hq-storage-port-adr.md), including its "Settled by
`bh-br1cd`" section.

## What it holds

| Name | Satisfies | Over |
|---|---|---|
| `DoltHqStorage` | `VersionedRecords`, `AppendLog`, `Liveness`, `CommittedHistory`, `CapabilityHost` | a root-injected DB-API `ConnectionFactory` (`Callable[[Role], Connection]`) |
| `DoltPlacementStore` | `PlacementStore` | `DoltHqStorage`'s `PLACEMENT` records |
| `DoltConfigHistory` | `CommittedHistory` | root's committed `hq_config_*` tables in the HQ config database |
| `DoltHiveFence` | `HiveFence`, `CapabilityHost` | a `FenceStatementEngine` (root's `DoltCliEngine` / `BdServerEngine`, or `SqlConnectionFenceEngine`) |
| `DoltHistory`, `DoltMerge`, `DoltAccounts` | the Dolt-only capabilities | `SqlRunner` over the factory or the engine |

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
| `append` | `INSERT` into `hq_live_append` | a new row, or the same key, operation and body again | any other replay of the key, a duplicate key, or `1213` | the natural key plus `operation_id`; `lookup_append` |
| `prune` | `DELETE ... TIMESTAMPDIFF(MICROSECOND, recorded_at, UTC_TIMESTAMP(6)) > window` | `PruneReport` | not applicable | none: a lost acknowledgment raises `StorageError` |
| `renew` | `UPDATE` or `INSERT` of `hq_live_liveness`; a trigger stamps `UTC_TIMESTAMP(6)` | the server stamp | not applicable | none: a lost acknowledgment raises `StorageError` |
| `publish` | head check, sequence CAS, documents, publication row, `DOLT_ADD` of an explicit list, then `DOLT_COMMIT` | the exact commit hash, parent verified | a moved or `ABSENT` head, a lost sequence CAS, or `1213` | the `publication_id` row and its parent; `lookup_publication` joins `dolt_history_*` to `dolt_commit_ancestors` |
| `CommittedHistory.push` | `DOLT_FETCH`, then `DOLT_PUSH` | the local head | not a fast-forward (`dolt_merge_base`) | the expectation is the remote head before the push |
| `commit_bump` | the engine runs root's bump statements plus the `ReclaimSet` revert, then commits once | the new local head | not applicable: at or below the floor raises `IntegrityViolation` | none: a failed bump discards its partial working set and raises `StorageUnavailable` |
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
operator's seed `INSERT` (`placement_seed_statement`) and the grant and trigger reads of root's
conformance check (`placement_conformance_rows`) are here too.

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

**`DoltHqSessions`** (transitional, Dolt-only) hands one transaction to both the port and the
SQL not yet on it: `snapshot()` for the director's ONE-transaction survey, and `transact()` for
the placement CAS plus its failover-policy rows, committed only when the CAS is `Applied`. It
goes when `bh-xnn5p` moves the signed-authority read, the failover policy and session staleness
onto the port.

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
| `test_hq_storage_dolt_adapter.py` | classification over a scripted DB-API stub; the placement wire format, the config publication's `hq_config_*` rows, ignored live tables, server stamps and capabilities on real Dolt | Dolt for the real-Dolt cases |
| `test_dolt_semantics_int.py` | Dolt 2.3.5 server contracts (grants, triggers, accounts, globals, history, fence engine), folded from root | `dolt` (some cases also need a `127.0.0.2` alias) |

`beadhive_hq_storage_dolt.contract` holds the `dolt` `ContractAdapter`, its harnesses, real
fault injection at the adapter's commit points, and `DoltSqlServer`: one private
`dolt sql-server` that takes a `BH_DOLT_SLOTS` slot from root's lock files and waits for the
server's ready line instead of sleeping. The machine runs the `gate` profile with shrinking
excluded. Any other profile shrinks, capped at 100 examples.

## Import rules

- Imports `beadhive_hq_storage` only through its `__all__`, and nothing from `beadhive`.
- Never imports PyMySQL; tests inject `pymysql.connect` into `DoltSqlServer` themselves.

## Gate

The package shares the `hq-storage` attest key with the port package
(`just hq-storage-check`). Real-Dolt tests carry the `integration` and `dolt_server` markers,
and the folded canary cases keep `fence_canary` (`just fence-canary` includes this package).
