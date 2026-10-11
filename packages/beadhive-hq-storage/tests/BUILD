# Generator-shaped: see ../BUILD. Tests run in the Pants sandbox with only this package's own
# fixtures (no stateful fixture plugin), so they never join //tests:legacy-stateful-tests or
# the native pytest partition.
python_tests(
    name="tests",
    sources=["**/test_*.py"],
    # The contract test checks the README coverage table against contract.SCENARIOS (bh-9fg5k).
    dependencies=["//packages/beadhive-hq-storage:metadata"],
    tags=["category:test-only", "attest:hq-storage"],
)

# The package's conftest (hypothesis profile selection, bh-9fg5k). Recursive glob: a nested
# conftest never edits this file.
python_test_utils(
    name="test_utils",
    sources=["**/conftest.py"],
    tags=["category:test-only", "attest:hq-storage"],
)
