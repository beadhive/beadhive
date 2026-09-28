# Generator-shaped like packages/_template: recursive globs in src/BUILD and tests/BUILD, so a
# new module or test never edits a BUILD file. Every target carries `attest:packages`, the one
# shared key for all packages (a dedicated beadhive-bd-cli key is the follow-up bead bh-vq34o).
files(
    name="metadata",
    sources=["pyproject.toml", "README.md"],
    tags=["category:build-system", "attest:packages"],
)

files(
    name="recipes",
    sources=["justfile"],
    tags=["category:config", "attest:packages"],
)
