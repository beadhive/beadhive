python_sources(
    name="lib",
    sources=["**/*.py"],
    tags=["category:code", "attest:package"],
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

pex_binary(
    name="bh",
    entry_point="beadhive.bootstrap.cli:main",
    dependencies=[
        ":lib",
        ":package-data",
    ],
    output_path="bh.pex",
    tags=["category:code", "attest:demos", "attest:package"],
)
