python_source(
    name="root-conftest",
    source="conftest.py",
    dependencies=[":watchdog-diagnostics"],
)

python_source(
    name="watchdog-diagnostics",
    source="harness/watchdog_diagnostics.py",
)

python_source(
    name="stateful-fixtures",
    source="stateful_fixtures.py",
)

python_sources(
    name="harness",
    sources=["harness/**/*.py"],
)

python_tests(
    name="pure-tests",
    sources=["unit/**/test_*.py"],
    dependencies=[":root-conftest"],
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
)

resources(
    name="test-resources",
    sources=[
        "harness/*.json",
        "fixtures/**/*",
    ],
)
