# Generator-shaped: see ../BUILD. Recursive globs cover every module and data file.
python_sources(
    name="lib",
    sources=["**/*.py"],
    # Pants does not infer importlib.resources reads; this one edge covers every resource the
    # glob below will ever match.
    dependencies=[":resources"],
    tags=["category:code", "attest:packages"],
)

resources(
    name="resources",
    sources=["**/py.typed"],
    tags=["category:code", "attest:packages"],
)
