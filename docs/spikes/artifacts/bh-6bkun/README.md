# Spike `bh-6bkun` artifacts — binding a bead store to the `tracker-github` adapter plugin

Prototype code for [the spike](../../bh-6bkun-tracker-binding.md). Nothing under `src/` or
`packages/` imports any of it. Every test runs offline: `conftest.py` makes a real socket
connection, `time.sleep` or a subprocess raise, and clears `BH_*` from the environment (the config
root is a pydantic-settings class).

| File | Stands in for |
|---|---|
| `binding_proto.py` | (T1) `ProtoStoreDescriptor`, `ProtoManagedRepoStores`, `ProtoManagedRepoEntry`, `ProtoBeadhiveConfig`: **subclasses of the real config models** that add exactly one store key (`adapter`) and close the bh-rktrh C1 gap (`plugins`, `plugin_kernel`); (T3) `resolve_store_adapter`, the composition-root step a consumer calls; (T2) `binding_check`, the `hive ready` / `doctor` row |
| `plugin.json` | the bh-rktrh `tracker-github` manifest, copied (the real kernel discovers it) |
| `test_binding.py` | the assertions the spike's Evidence and Recommendation cite |

It reuses, unchanged, the [`bh-rktrh`](../bh-rktrh/README.md) composition and stub-adapter
prototypes and the [`bh-gj1hb`](../bh-gj1hb/tracker_contracts.py) port prototype, plus the
repository's real `validate_config`, `BeadhiveConfig`, `discover_plugins` and
`bind_application_port`.

## Reproduce

From the repository root, in the project environment; `-c /dev/null` keeps the project's pytest
configuration out of it:

```sh
uv run python -m pytest -c /dev/null --rootdir=docs/spikes/artifacts/bh-6bkun \
  -p no:cacheprovider docs/spikes/artifacts/bh-6bkun -q
```

## Recorded output (2026-10-25, Python 3.12.13)

```text
58 passed
```

Mutation checks (each applied to a throwaway copy, never to the files here):

| Mutation | Tests that failed |
|---|---|
| `adapter` defaults to `tracker-github` instead of unset | the existing-config equality and dump tests (8 of the corpus), the one-field test, the exhaustive 48-combination test, the defaults test |
| the shape error echoes the offending value | the five malformed-value cases and the no-credential-in-message test |
| skip the operation-enabled check | write-requested and read-requested rules, and the no-credential test built on them |
| report a disabled plugin as `error` in the `hive ready` row | never-fails-a-disabled-binding |
| skip the unknown-adapter rule | unknown-adapter |
| fix the repository instead of deriving it from the hive triplet | reader-binds-this-hives-repository, identity, per-hive repository, ready-row |

Two tests record facts about **today's** code (`test_recorded_*`): the real models reject the new
keys (the schema bead flips them), and the real `store_target` echoes a bad store value and fails
on an unknown store key.
