python_tests(
    name="tests",
    sources=["**/test_*.py"],
    # The recorded GitHub exchanges are read from disk next to the tests.
    dependencies=[":recorded"],
    tags=["category:test-only", "attest:packages"],
)

# The offline-by-construction conftest and the MockTransport stand-in the tests import.
python_test_utils(
    name="test_utils",
    sources=["**/conftest.py", "tracker_github_fake.py"],
    tags=["category:test-only", "attest:packages"],
)

# Synthetic recorded-response fixtures (GitHub REST 2022-11-28 shaped; no live capture).
resources(
    name="recorded",
    sources=["recorded/*.json"],
    tags=["category:test-only", "attest:packages"],
)
