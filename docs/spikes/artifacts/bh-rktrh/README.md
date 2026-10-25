# Spike `bh-rktrh` artifacts — the `tracker.adapter` plugin category, exercised on the real kernel

Prototype code for [the spike](../../bh-rktrh-tracker-plugin-category.md). Nothing under `src/` or
`packages/` imports any of it. Every test runs offline: `conftest.py` makes a real socket
connection, `time.sleep` or a subprocess raise. The tests import the repository's real
`beadhive.kernel.plugins` (discovery and `bind_application_port`) and the
[`bh-gj1hb` port prototype](../bh-gj1hb/tracker_contracts.py); the GitHub adapter is a stub.

| File | Stands in for |
|---|---|
| `plugin.json` | `packages/beadhive-tracker-github/src/beadhive_tracker_github/plugin.json` and its byte-identical copy `src/beadhive/kernel/plugins/manifests/tracker-github.json` |
| `compose_proto.py` | root `bootstrap/tracker.py`: fold the plugin's own `enabled` flag into kernel enablement, discover, load lazily, `bind_application_port` |
| `credentials_proto.py` | the stdlib-only shared credential module (`ResolvedToken`, `TokenSource`, `scrub`) and root's token-source builder (open item O1) |
| `stub_adapters.py` | the package factory: writer methods exist only for enabled write operations |
| `status_proto.py` | root's generic, port-driven status and doctor rendering |
| `config_proto.py` | the proposed `plugins.tracker-github` subtree and its defaults |
| `test_category.py` | the assertions the spike's Evidence and Recommendation cite |

## Reproduce

From the repository root, in the project environment (the tests import `beadhive` and
`jsonschema`); `-c /dev/null` keeps the project's pytest configuration out of it:

```sh
uv run python -m pytest -c /dev/null --rootdir=docs/spikes/artifacts/bh-rktrh \
  -p no:cacheprovider docs/spikes/artifacts/bh-rktrh -q
```

## Recorded output (2026-10-25, Python 3.12.13)

```text
32 passed
```

Mutation checks (each applied to a throwaway copy, never to the files here):

| Mutation | Tests that failed |
|---|---|
| drop the `enabled` fold (`own and ...`) | default-binds-nothing, disabled-second-provider, stable-disabled-text |
| default `enabled` to true when the key is absent | only-an-exact-true-enables (absent) |
| make every stub carry both writer methods | writer-absence and flag-to-writer tests, and the tests built on them |
| make `scrub` ignore the token-shape pattern | negative-control leak test |

Two tests record gaps in **today's** config layer
(`test_recorded_gap_*`). They assert the gap exists and are expected to flip when the
implementation molecule closes it.
