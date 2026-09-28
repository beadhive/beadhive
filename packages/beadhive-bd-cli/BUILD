# Generator-shaped like packages/_template: recursive globs in src/BUILD and tests/BUILD, so a
# new module or test never edits a BUILD file. Every target carries `attest:bd-cli`, this
# package's own dedicated key (bh-vq34o) rather than the shared `attest:packages` key -- it
# implements beadhive-core's ports and changes far more often than the rest of packages/*.
files(
    name="metadata",
    sources=["pyproject.toml", "README.md"],
    tags=["category:build-system", "attest:bd-cli"],
)

files(
    name="recipes",
    sources=["justfile"],
    tags=["category:config", "attest:bd-cli"],
)
