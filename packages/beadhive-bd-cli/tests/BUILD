# Generator-shaped: see ../BUILD. Tests run in the Pants sandbox with only this package, the
# core and Beads client packages and their fixtures (no stateful fixture plugin). The real-bd
# tests self-skip without a `bd` binary on PATH.
python_tests(
    name="tests",
    sources=["**/test_*.py"],
    tags=["category:test-only", "attest:packages"],
)
