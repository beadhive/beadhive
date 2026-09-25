# Pants script compatibility shim ledger

The Pants tooling implementations moved into `beadhive-pants` in commit `a45740e5`. The old
paths remain executable compatibility shims so checked command strings, CI entry points, and
path-loaded downstream tests keep the same interface during migration.

| Shim | Package target | Current consumers | Expiry trigger |
| --- | --- | --- | --- |
| `scripts/pants_ci.py` | `beadhive_pants.runner` | `justfile`; `beadhive_pants.attest`; `docs/ATTEST-KEYS.md`; `docs/proof/bh-t8t7r-pants-graduation.json`; `tests/test_pants_ci.py`; `tests/test_pants_activation.py`; `tests/test_test_closure_certification_process.py` | Every command consumer uses `bh plugin pants test …`, package callers use `beadhive_pants.runner`, path-loading compatibility tests are retired, and the resulting attest command-hash transition is explicitly approved. |
| `scripts/pants_routes.py` | `beadhive_pants.routes` | `justfile`; `docs/design/attested-green-adr.md`; `docs/proof/bh-70ewe.5-pants-shadow.json`; `tests/test_pants_routes.py`; `tests/test_pants_activation.py` | Recipes and evidence no longer execute the path, and path-loading compatibility tests are retired after equivalent package-module coverage exists. |
| `scripts/pants_cache.py` | `beadhive_pants.cache` | `justfile`; `scripts/hermetic.sh`; `scripts/check_pants_ownership.py`; `beadhive_pants.runner`; `beadhive_pants.routes`; `beadhive_pants.attest`; `docs/PANTS.md`; `docs/proof/bh-70ewe.5-pants-shadow.json`; `tests/test_pants_cache.py`; `tests/test_beadhive_pants_artifacts.py` | Every process entry uses `bh plugin pants cache` or the package API, package subprocesses no longer call the legacy path, and the path-loading compatibility test is retired. |
| `scripts/pants_launcher.py` | `beadhive_pants.launcher` | `justfile`; `scripts/hermetic.sh`; `scripts/check_pants_ownership.py`; `beadhive_pants.impact`; `tests/test_beadhive_pants_artifacts.py`; `tests/test_test_closure_certification_process.py` | All callers import the package launcher or use the plugin CLI, and no checked recipe or compatibility test names the old path. |
| `scripts/pants_attest.py` | `beadhive_pants.attest` | `justfile`; `tests/test_pants_activation.py` | The attest recipe deliberately changes to `bh plugin pants attest-check`, its command-hash transition is approved, and the path-loading compatibility test is retired. |
| `scripts/pants_ci_benchmark.py` | `beadhive_pants.benchmark` | `justfile`; `docs/proof/bh-t8t7r-ci-benchmark.json`; `tests/test_pants_ci.py`; `tests/test_test_closure_certification_process.py` | The benchmark recipe and evidence collector use the package entry point, and both path-loading compatibility assertions are retired. |
| `scripts/pants_shadow_evidence.py` | `beadhive_pants.shadow_evidence` | `justfile`; `beadhive_pants.attest`; `docs/PANTS-SHADOW-QUALIFICATION.md`; `tests/test_pants_shadow_evidence.py`; `tests/test_pants_activation.py`; `tests/test_test_closure_certification_process.py` | The recipe and package attest flow use the package entry point, and all path-loading compatibility tests are retired. |
| `scripts/test_impact_selector.py` | `beadhive_pants.impact_selector` | `justfile`; `docs/TEST-CLOSURES.md`; `docs/proof/bh-ck1t6.3-shadow-activation.json`; `tests/test_test_impact_selector.py` | The advisory recipe and evidence use the package entry point, and the standalone copied-script compatibility contract is explicitly ended or replaced. |

Removal requires every row's trigger in one reviewed change. A zero result from an exact search
for the shim path, excluding this ledger and the shim itself, is the final deletion check.
