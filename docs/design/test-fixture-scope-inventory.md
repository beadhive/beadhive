# Test fixture scope inventory

Status: implemented current-state compatibility inventory (reviewed 2026-09-22).

This inventory is the fixture/test-isolation companion to the
[`repository-physical-organization` ADR](repository-physical-organization-adr.md), the exact-tip
[`repository physical-layout baseline`](repository-physical-layout-baseline.md), and the checked
[`root-module-ownership.toml`](root-module-ownership.toml). Historical introduction under
`bh-inqwc.3` remains part of Git history; this file describes the current fixture boundary.

The root `tests/conftest.py` only registers pytest plugins. It imports no Beadhive runtime module,
reads no environment or file, and defines no autouse fixture or session hook. Stateful setup is
owned by `tests/stateful_fixtures.py` and exposed through these explicit concern scopes:
`config_test_scope`, `identity_test_scope`, `dolt_test_scope`, `validation_test_scope`,
`telemetry_test_scope`, `runtime_test_scope`, and `plugin_test_scope`.
The same plugin owns the controller-only session start/end Dolt orphan backstop. It is a pytest
lifecycle safety hook rather than a test fixture: xdist workers skip it, so it runs exactly once
outside every test's lifecycle without adding stateful code to the root conftest.

## Consumers and incremental migration

The checked compatibility inventory in `pytest_collection_modifyitems` requests
`legacy_stateful_test_scope` for collected tests under `tests/`, except `tests/unit/`. This is the
explicit consumer list for the current flat suite: root `tests/test_*.py`, `tests/proof/**`, and
`tests/spikes/**`. Helper and data paths do not collect tests and therefore consume no fixture.

`tests/unit/**` is the pure-module scope. It receives no stateful setup. If a test moved there
discovers a real dependency, it must request the named concern fixture as a function argument or
with `pytest.mark.usefixtures`. Its side-effect-free diagnostic guard names the required scope on
an outer-layer import, operator-home lookup, plugin discovery, Dolt/network access, telemetry
startup, process spawn, or runtime-thread start instead of falling through to operator state.
This makes migration incremental: move one test, run it, and add or remove the smallest named
dependency exposed by its failure.

Physical cleanup must move a module's internal tests and fixtures with its owner. A module-local
test may depend on module-owned fakes and explicitly requested concern scopes, but it may not
recover a legacy root implementation through `legacy_stateful_test_scope`. Higher-layer tests
substitute the module's public port; real adapters retain focused contract/integration coverage.
The corresponding direct, shared-contract, fixture/resource, and reverse-dependent selectors
remain explicit in `tests/closures.toml` and are documented in [`TEST-CLOSURES`](../TEST-CLOSURES.md).

## Removed root initialization

Every former root autouse fixture or hook has the following destination and consumers.

| Removed root setup | Destination | Consumers |
| --- | --- | --- |
| Runtime thread leak check | `runtime_test_scope` | Inventoried legacy tests; pure tests only when explicitly requested. |
| Session-start/session-end Dolt sweep | Controller hooks in `tests/stateful_fixtures.py` | Every pytest session, exactly once on the controller; xdist workers skip it. |
| Marked Dolt-server concurrency bound | `dolt_test_scope` | Tests marked `dolt_server`; the fixture is otherwise a no-op. |
| Isolated `BH_HOME`, config, image manifest, and setup default | `config_test_scope` | Inventoried legacy tests; explicit config consumers under `tests/unit/**`. |
| Empty Claude plugin registry | `plugin_test_scope` | Inventoried legacy tests; explicit Claude plugin-discovery consumers. |
| Empty Codex plugin registry | `plugin_test_scope` | Inventoried legacy tests; explicit Codex plugin-discovery consumers. |
| Isolated global Git config | `identity_test_scope` | Inventoried legacy tests; explicit host-identity consumers. |
| Isolated Git workspace root | `identity_test_scope` | Inventoried legacy tests; explicit workspace-discovery consumers. |
| Worktree-root override scrub | `runtime_test_scope` | Inventoried legacy tests; explicit lifecycle/runtime consumers. |
| Isolated validation host | `validation_test_scope` | Inventoried legacy tests; explicit validation consumers. |
| Fresh local-bd version memo | `dolt_test_scope` | Inventoried legacy tests; explicit Dolt-health consumers. |
| Git fact cache reset | `identity_test_scope` | Inventoried legacy tests; explicit identity/auth consumers. |
| Unsealed validation ledger | `validation_test_scope` | Inventoried legacy tests; explicit attestation consumers. |
| Isolated shared Dolt target and reap | `dolt_test_scope` | Inventoried legacy tests; explicit real-bd consumers. |
| Neutral telemetry and setup-check environment | `telemetry_test_scope` | Inventoried legacy tests; explicit telemetry consumers. |
| Logging/caplog process-state reset | `runtime_test_scope` | Inventoried legacy tests; explicit logging/runtime consumers. |

The explicit `world` and `fake_plugin` fixtures moved to the same plugin without becoming
autouse. Their consumers remain exactly the test functions that name them. The independence
sentinel in `tests/unit/test_pure_module_independence.py` guards the pure scope against operator
config reads, Dolt/network access, plugin discovery, telemetry/runtime imports, and process or
thread spawning.

## Preserved boundary

The compatibility scope preserves the existing environment, process, Dolt cleanup, and
concurrency behavior inside the hermetic runner owned by `bh-1c04h`. It does not add another
sandbox, change marker selection, or weaken the fenced `just test` and `just check` commands.
