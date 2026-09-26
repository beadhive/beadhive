# Root-level modules only. This is migration debt (physical-organization ADR, Decision 7): the
# governed directories (modules/<capability>, kernel/<concern>, adapters/<boundary>,
# integrations/herdr, bootstrap, testing) own their sources through their own BUILD files,
# tagged `owner:<path>` and `role:<kind>`.
python_sources(
    name="lib",
    sources=["*.py"],
    tags=["category:code", "attest:package", "owner:root", "role:migration-debt"],
    overrides={
        # The supervised Beads service owner resolves the Beads client package by name
        # (bh-bwnys.4); Pants cannot infer that edge, so it is declared here.
        "host_beads.py": {"dependencies": ["//packages/beadhive-beads-client/src:lib"]},
        # The review composition seam resolves the API-first core by name (bh-bwnys.1); Pants
        # cannot infer that edge, so it is declared here.
        "work_review.py": {
            "dependencies": [
                "//packages/beadhive-core/src:lib",
                "//packages/beadhive-beads-client/src:lib",
            ],
        },
    },
)

resources(
    name="package-data",
    sources=[
        "**/*.json",
        "**/*.md",
        "**/*.yaml",
        "**/*.yml",
        "**/*.sh",
        "**/*.js",
        "**/*.example",
        "**/.gitkeep",
    ],
    tags=["category:code", "attest:package"],
)

# Every first-party Python source target under src/beadhive. The pex and the demos depend on this
# set rather than on import inference from the entry point, so narrowing :lib cannot drop a
# dynamically loaded module from their closure. A new source target under src/beadhive must be
# listed here; tests/test_pants_foundation.py enforces it.
target(
    name="sources",
    dependencies=[
        ":lib",
        "./adapters:impact",
        "./adapters:package",
        "./adapters:telemetry",
        "./adapters/cli",
        "./bootstrap",
        "./integrations",
        "./integrations/herdr",
        "./kernel",
        "./kernel/daemon",
        "./kernel/lifecycle",
        "./kernel/operations",
        "./kernel/plugins",
        "./kernel/telemetry",
        "./modules",
        "./modules/agents",
        "./modules/config",
        "./modules/hives",
        "./modules/planning",
        "./modules/state",
        "./modules/work",
        "./modules/worktrees",
        "./testing",
    ],
    tags=["category:code", "attest:package"],
)

pex_binary(
    name="bh",
    entry_point="beadhive.bootstrap.cli:main",
    dependencies=[
        ":sources",
        ":package-data",
        "//packages/beadhive-pants/src:lib",
        "//packages/beadhive-core/src:lib",
        "//packages/beadhive-beads-client/src:lib",
    ],
    output_path="bh.pex",
    tags=["category:code", "attest:demos", "attest:package"],
)
