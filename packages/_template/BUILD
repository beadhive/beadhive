# Generator-shaped on purpose (bh-3fcl0.1): every BUILD in a package uses recursive globs, so
# adding a module, test, or data file never edits one. Keep it that way — any BUILD edit matches
# the `**/BUILD` global impact input and invalidates every attest key, not just `packages`.
#
# Three BUILD files, not one: Pants derives a target's source root from its BUILD directory, so
# the Python targets live in src/BUILD and tests/BUILD (under the `/packages/*/src` and
# `/packages/*/tests` roots). A package-root BUILD would put `.` on the test sys.path instead.
#
# Every target carries `attest:packages`, the one shared key for all packages.

# The package's own distribution metadata. A dependency change here also rewrites uv.lock,
# which is already a global input, so the build-system category costs nothing extra.
files(
    name="metadata",
    sources=["pyproject.toml"],
    tags=["category:build-system", "attest:packages"],
)

# Package-local developer recipes (`just pkg <name> <recipe>`). No attest key runs them.
files(
    name="recipes",
    sources=["justfile"],
    tags=["category:config", "attest:packages"],
)
