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
| `compare_and_set` on `PLACEMENT` | `UPDATE hq_live_hive_leases ... WHERE prefix AND revision` | a fresh `sha256(record ‖ uuid4)` revision | `rowcount 0`, or `1213` at `COMMIT` | the `OperationId` in `request_id`; `lookup` |
| `compare_and_set` on the other kinds | `INSERT ... WHERE NOT EXISTS` from `ABSENT`, else a guarded `UPDATE` of `hq_live_records` | a fresh revision | `rowcount 0`, or `1213` | the `operation_id` column; `lookup` |
| `append` | `INSERT` into `hq_live_append` | a new row, or the same key, operation and body again | any other replay of the key, a duplicate key, or `1213` | the natural key plus `operation_id`; `lookup_append` |
| `prune` | `DELETE ... TIMESTAMPDIFF(MICROSECOND, recorded_at, UTC_TIMESTAMP(6)) > window` | `PruneReport` | not applicable | none: a lost acknowledgment raises `StorageError` |
| `renew` | `UPDATE` or `INSERT` of `hq_live_liveness`; a trigger stamps `UTC_TIMESTAMP(6)` | the server stamp | not applicable | none: a lost acknowledgment raises `StorageError` |
| `publish` | head check, sequence CAS, documents, publication row, `DOLT_ADD` of an explicit list, then `DOLT_COMMIT` | the exact commit hash, parent verified | a moved or `ABSENT` head, a lost sequence CAS, or `1213` | the `publication_id` row and its parent; `lookup_publication` joins `dolt_history_*` to `dolt_commit_ancestors` |
| `CommittedHistory.push` | `DOLT_FETCH`, then `DOLT_PUSH` | the local head | not a fast-forward (`dolt_merge_base`) | the expectation is the remote head before the push |
| `commit_bump` | the engine runs root's bump statements plus the `ReclaimSet` revert, then commits once | the new local head | not applicable: at or below the floor raises `IntegrityViolation` | none: a failed bump discards its partial working set and raises `StorageUnavailable` |
| `HiveFence.push` | engine fetch, then engine push | the local head | the push returns `False`, or not a fast-forward | the `adopt-<epoch>` sentinel on the remote head; `lookup_bump` |

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
| `test_hq_storage_dolt_adapter.py` | classification over a scripted DB-API stub; the placement wire format, ignored live tables, server stamps and capabilities on real Dolt | Dolt for the second half |
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
