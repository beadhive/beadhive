# Pytest fixture ownership

The authoritative native test commands continue to collect the complete checkout. They include
`tests/stateful_fixtures.py`, so root `tests/conftest.py` registers the compatibility plugin at
initial conftest import on the controller and every xdist worker. Selective runners may omit that
module only for a qualified pure-unit sandbox. The import-time module probe is deliberately
annotated `pants: no-infer-dep`: build inference must not turn the optional compatibility edge
back into a dependency of every test.

This split changes ownership, not native pytest behavior. Unknown consumers and ambiguous dynamic
fixture use stay on the native validation route.

## Consumer inventory

| Concern | Consumers | Ownership and fallback |
| --- | --- | --- |
| Watchdog diagnostics plugin | Every native or selective pytest process | `tests/conftest.py` always registers `harness.watchdog_diagnostics`; the runner must include that one module. |
| Legacy stateful compatibility scope | Test modules under `tests/` other than `tests/unit/` | `stateful_fixtures.py` attaches `legacy_stateful_test_scope` during collection. These tests remain native/full until a later change inventories narrower concern scopes. |
| Pure-unit policy | Test modules under `tests/unit/` in native/full runs | The compatibility plugin attaches only `pure_test_scope`; qualified pure-unit sandboxes omit the plugin and its source dependency entirely. |
| Explicit unit concern scopes | `tests/unit/testing/test_real_adapter_conformance_example.py`, `tests/unit/test_pure_module_independence.py`, `tests/unit/modules/config/test_yaml_store.py`, and `tests/unit/kernel/operations/test_executor.py` | These consumers explicitly request `config_test_scope`, `plugin_test_scope`, or `runtime_test_scope`. A selective target must own the requested fixture module or fall back to native/full. |
| `world` fixture and harness helpers | Flat tests that request `world`, plus test files with direct `harness.*` imports | Imports are ordinary narrow source dependencies. The `world` fixture remains in the stateful compatibility plugin, so its consumers remain native/full until modeled explicitly. |
| Dynamic fixture lookup | `tests/test_machine_json.py` (`request.getfixturevalue`) | Native/full only. A selector must not infer safety from fixture names assembled at runtime. |
| Stateful plugin subprocess contract | `tests/test_dolt_server_sweep.py` | Native/full only; its subprocess explicitly loads `stateful_fixtures`. |
| Watchdog plugin subprocess contract | `tests/test_test_watchdog.py` | Owns `harness/watchdog_diagnostics.py` explicitly in addition to universal runner registration. |
| Shared conftests | `tests/conftest.py` is the only repository conftest | Any new conftest, plugin declaration, dynamic fixture lookup, or test outside the inventoried roots is unknown test infrastructure and routes to native/full validation. |

The concern scopes themselves remain lazy and composable:
`config_test_scope`, `identity_test_scope`, `dolt_test_scope`, `validation_test_scope`,
`telemetry_test_scope`, `runtime_test_scope`, and `plugin_test_scope`. The aggregate
`legacy_stateful_test_scope` preserves the historical native isolation and cleanup order. Moving a
test out of that compatibility scope requires naming its concerns and proving it against a
same-tree native oracle; path placement alone is not evidence of safety.

## Build-graph contract

A pure-unit target may declare root conftest plus watchdog diagnostics and intentionally exclude
`stateful_fixtures.py`. Stateful, harness-dynamic, integration-shaped, and unknown targets include
the compatibility plugin or use the native/full command. Build configuration, resolver/lock,
plugin, generated-resource, installed-console-script, and fixture-infrastructure changes also use
native/full validation. Pants activation and concrete BUILD target edges are owned by the next
adoption stage; this change only establishes the semantics-preserving source boundary they use.
