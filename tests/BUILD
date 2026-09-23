python_source(
    name="root-conftest",
    source="conftest.py",
    dependencies=[":watchdog-diagnostics"],
    tags=["category:test-only", "attest:stateful"],
)

python_source(
    name="watchdog-diagnostics",
    source="harness/watchdog_diagnostics.py",
    tags=["category:test-only", "attest:stateful"],
)

python_source(
    name="stateful-fixtures",
    source="stateful_fixtures.py",
    tags=["category:test-only", "attest:stateful"],
)

python_sources(
    name="harness",
    sources=["harness/**/*.py"],
    tags=["category:test-only", "attest:stateful"],
)

# Fields shared by every pure unit-test target below. `_unit_tests` hands each target only the
# overrides for files under its own directory and adds that target's owner/role tags.
_UNIT_TEST_FIELDS = dict(
    dependencies=[":root-conftest"],
    tags=["category:test-only", "attest:unit"],
    overrides={
        # bh-t8t7r: these files pass from a Pants sandbox with no stateful fixture
        # target and form the first CAS-backed slice of attest:stateful.  Keep the
        # list byte-for-byte aligned with pants_proven_tests.json; the partition
        # verifier fails closed if either side drifts.
        (
            "unit/integrations/test_herdr_adapter.py",
            "unit/integrations/test_herdr_agent_lifecycle_adapter.py",
            "unit/kernel/lifecycle/test_contracts.py",
            "unit/kernel/lifecycle/test_dispatcher.py",
            "unit/kernel/plugins/test_binding.py",
            "unit/kernel/plugins/test_discovery.py",
            "unit/kernel/telemetry/test_instrumentation.py",
            "unit/modules/agents/test_agent_contracts.py",
            "unit/modules/agents/test_agent_services.py",
            "unit/modules/config/test_config_contracts.py",
            "unit/modules/config/test_consumer_settings.py",
            "unit/modules/config/test_migrations.py",
            "unit/modules/config/test_plugin_fragments.py",
            "unit/modules/config/test_resolution.py",
            "unit/modules/hives/test_hive_identity.py",
            "unit/modules/hives/test_hive_independence.py",
            "unit/modules/hives/test_hive_services.py",
            "unit/modules/planning/test_planning_services.py",
            "unit/modules/state/test_state_services.py",
            "unit/modules/work/test_impact_pants.py",
            "unit/modules/work/test_impact_pants_conformance.py",
            "unit/modules/work/test_work_independence.py",
            "unit/modules/work/test_work_lifecycle_services.py",
            "unit/modules/worktrees/test_adapters.py",
            "unit/modules/worktrees/test_policy.py",
            "unit/modules/worktrees/test_services.py",
            "unit/testing/test_conformance_testkit.py",
            "unit/testing/test_impact_conformance_kit.py",
        ): {
            "tags": [
                "category:test-only",
                "attest:unit",
                "attest:stateful",
                "pants:proven",
            ]
        },
    },
)


def _unit_tests(area, owner, role):
    """One pure unit-test target per `unit/<area>`, tagged with the source owner it covers.

    An empty area is the root of `unit/` (non-recursive). A test file under a directory with no
    target here is unowned, and `check_pants_ownership.py` fails closed on it.
    """
    prefix = f"unit/{area}/" if area else "unit/"
    ownership = [f"owner:{owner}", f"role:{role}"]
    overrides = {}
    for paths, fields in _UNIT_TEST_FIELDS["overrides"].items():
        owned = tuple(
            path
            for path in paths
            if path.startswith(prefix) and (area or "/" not in path[len(prefix) :])
        )
        if owned:
            overrides[owned] = dict(fields, tags=[*fields["tags"], *ownership])
    python_tests(
        name="unit-" + (area.replace("/", "-") if area else "root"),
        sources=[f"{prefix}**/test_*.py" if area else f"{prefix}test_*.py"],
        dependencies=_UNIT_TEST_FIELDS["dependencies"],
        tags=[*_UNIT_TEST_FIELDS["tags"], *ownership],
        overrides=overrides,
    )


_unit_tests("", "root", "migration-debt")
_unit_tests("integrations", "integrations/herdr", "integration")
_unit_tests("kernel/lifecycle", "kernel/lifecycle", "kernel")
_unit_tests("kernel/operations", "kernel/operations", "kernel")
_unit_tests("kernel/plugins", "kernel/plugins", "kernel")
_unit_tests("kernel/telemetry", "kernel/telemetry", "kernel")
_unit_tests("modules/agents", "modules/agents", "capability")
_unit_tests("modules/config", "modules/config", "capability")
_unit_tests("modules/hives", "modules/hives", "capability")
_unit_tests("modules/planning", "modules/planning", "capability")
_unit_tests("modules/state", "modules/state", "capability")
_unit_tests("modules/work", "modules/work", "capability")
_unit_tests("modules/worktrees", "modules/worktrees", "capability")
_unit_tests("testing", "testing", "testing-kit")

python_tests(
    name="legacy-stateful-tests",
    sources=[
        "test_*.py",
        "contracts/**/test_*.py",
        "spikes/**/test_*.py",
    ],
    dependencies=[
        ":root-conftest",
        ":stateful-fixtures",
    ],
    overrides={
        # Sandbox-qualified by bh-1j3ei.4. These are the complete runtime file inputs of
        # test_docs_role_vocabulary.py: three root docs plus INSTALL.md.
        "test_docs_role_vocabulary.py": {
            "dependencies": ["//docs:docs-root", "//:root-guide-doc"],
        },
    },
    tags=["category:test-only", "attest:stateful"],
)

resources(
    name="test-resources",
    sources=[
        "harness/*.json",
        "fixtures/**/*",
    ],
    tags=["category:test-only"],
)

# Package marker for tests/unit/integrations so imports resolve; the test_*.py siblings there
# are already covered by the :unit-<area> targets.
python_sources(
    name="package-markers",
    sources=["unit/**/__init__.py"],
    tags=["category:test-only", "attest:unit"],
)

# Ownership only (bh-1j3ei.2): config/fixture files no test target's sources glob reaches,
# plus standalone proof scripts that are invoked directly (justfile), never imported.
files(
    name="test-config",
    sources=[
        "closures.toml",
        "fixture_config.yaml",
        "fixture_workspace.toml",
        "selective-ci-policy.toml",
        "proof/**/*",
        "schemas/**/*",
    ],
    tags=["category:test-only"],
)
