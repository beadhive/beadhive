python_sources(
    name="lib",
    sources=["*.py"],
    tags=["attest:architecture-contracts"],
    # These scripts import a same-directory sibling by its bare name (the convention for
    # `python scripts/<file>.py` direct execution, which puts scripts/ itself on sys.path).
    # Pants can't infer that edge because the module lives under the `scripts.` namespace
    # from the repo root; the imports carry `# pants: no-infer-dep` and the real edge is
    # declared here instead, so the dependents graph still stays complete (bh-1j3ei.2).
    overrides={
        "capability_closeout.py": {"dependencies": ["./check_import_boundaries.py:lib"]},
        "capability_module_map.py": {"dependencies": ["./check_import_boundaries.py:lib"]},
        "check_pants_ownership.py": {"dependencies": ["./check_attest_catalog.py:lib"]},
        "render_transport_composition_evidence.py": {
            "dependencies": ["./check_import_boundaries.py:lib"]
        },
        "test_closure_shadow_verifier.py": {
            "dependencies": ["./test_closure_shadow_policy.py:lib"]
        },
    },
)

files(
    name="pants-proven-manifest",
    sources=["pants_proven_tests.json"],
    tags=["attest:architecture-contracts"],
)

files(
    name="shell",
    sources=[
        "*.sh",
        "bh-worktree-prune",
        "bh-worktree-prune.service",
        "bh-worktree-prune.timer",
    ],
)
