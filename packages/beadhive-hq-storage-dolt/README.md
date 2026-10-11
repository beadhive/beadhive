# beadhive-hq-storage-dolt

The Dolt adapter for the [`beadhive-hq-storage`](../beadhive-hq-storage/README.md) port
(scaffold from bh-wnhkn). A **library package** under
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md). Design:
[hq-storage-port-adr.md](../../docs/design/hq-storage-port-adr.md).

## Status

Scaffold only; the adapter, its SQL, Dolt procedure calls and error-code classification land in
`bh-br1cd`.

## Import rules

- Imports `beadhive_hq_storage` only through its `__all__`, and nothing from `beadhive`.
- Never imports PyMySQL: it takes a DB-API connection factory `Callable[[Role], Connection]`
  injected by root, which keeps transport (`hq_sql_transport.connect`) and settings in root.

## Gate

Shares the `hq-storage` attest key with the port package (`just hq-storage-check`). Real-Dolt
tests will carry the `integration` + `dolt_server` markers and self-skip without Dolt, like
`packages/beadhive-bd-cli/tests/test_ports_real_bd.py`.
