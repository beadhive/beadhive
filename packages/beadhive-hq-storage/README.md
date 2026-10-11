# beadhive-hq-storage

The backend-neutral HQ storage **port** (scaffold from bh-wnhkn). A **library package** under
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md): no
`plugin.json`, stdlib only, and it never imports the root `beadhive` distribution. The design is
[hq-storage-port-adr.md](../../docs/design/hq-storage-port-adr.md).

## Status

Scaffold only. The package imports and has an empty public surface:

| Module | Will hold | Owner |
|---|---|---|
| `beadhive_hq_storage` | Port protocols, value and outcome types, errors, `InMemoryHqStorage` fake | `bh-9fg5k` |
| `beadhive_hq_storage.contract` | Reusable contract scenarios; imports `pytest` / `hypothesis` only inside functions | `bh-9fg5k` |

## Import rules

- Imports nothing from `beadhive` and has no third-party runtime dependency.
- Consumers import it through `beadhive_hq_storage.__all__` only (`scripts/check_package_imports.py`).
- The Dolt adapter is [`beadhive-hq-storage-dolt`](../beadhive-hq-storage-dolt/README.md).

## Gate

Both HQ storage packages share the `hq-storage` attest key (`attest:hq-storage`, recipe
`just hq-storage-check`, opaque command `just attest-hq-storage`). Run the tests with:

```sh
just hq-storage-check
```
