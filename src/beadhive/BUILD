python_sources(
    name="lib",
    sources=["**/*.py"],
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
)

pex_binary(
    name="bh",
    entry_point="beadhive.bootstrap.cli:main",
    dependencies=[
        ":lib",
        ":package-data",
    ],
    output_path="bh.pex",
)
