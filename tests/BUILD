python_source(
    name="root-conftest",
    source="conftest.py",
    dependencies=[":watchdog-diagnostics"],
    tags=["attest:stateful"],
)

python_source(
    name="watchdog-diagnostics",
    source="harness/watchdog_diagnostics.py",
    tags=["attest:stateful"],
)

python_source(
    name="stateful-fixtures",
    source="stateful_fixtures.py",
    tags=["attest:stateful"],
)

python_sources(
    name="harness",
    sources=["harness/**/*.py"],
    tags=["attest:stateful"],
)

python_tests(
    name="pure-tests",
    sources=["unit/**/test_*.py"],
    dependencies=[":root-conftest"],
    tags=["attest:unit"],
)

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
    tags=["attest:stateful"],
)

resources(
    name="test-resources",
    sources=[
        "harness/*.json",
        "fixtures/**/*",
    ],
)

# Package marker for tests/unit/integrations so imports resolve; the test_*.py siblings there
# are already covered by :pure-tests.
python_sources(
    name="package-markers",
    sources=["unit/**/__init__.py"],
    tags=["attest:unit"],
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
)
