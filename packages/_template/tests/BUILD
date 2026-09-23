# Generator-shaped: see ../BUILD. Tests run in the Pants sandbox with only this package's own
# fixtures (no stateful fixture plugin), so they never join //tests:legacy-stateful-tests or
# the native pytest partition.
python_tests(
    name="tests",
    sources=["**/test_*.py"],
    tags=["category:test-only", "attest:packages"],
)
