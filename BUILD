# Root ownership is split by change category.  Category tags are consumed by the same Pants
# graph query as attest tags; they are not a parallel path classifier (bh-bsb38.3).
files(
    name="root-build-system",
    sources=[
        ".mise.toml",
        "Brewfile",
        "BUILD",
        "docker-bake.hcl",
        "flake.lock",
        "flake.nix",
        "justfile",
        "lefthook.yml",
        "pants.toml",
        "pyproject.toml",
        "uv.lock",
    ],
    tags=["category:build-system"],
)

files(
    name="root-config",
    sources=[
        ".env.example",
        ".git-blame-ignore-revs",
        ".gitignore",
        ".markdownlint-cli2.jsonc",
        "docker-compose.yml",
        "osv-scanner.toml",
    ],
    tags=["category:config", "attest:demos"],
)

# Keep INSTALL separate: test_docs_role_vocabulary.py reads it at runtime.  Giving that test
# this exact dependency prevents unrelated root prose (especially README.md) from acquiring a
# reverse dependency on the stateful lane.
files(
    name="root-guide-doc",
    sources=["INSTALL.md"],
    tags=["category:docs", "attest:docs"],
)

files(
    name="root-prose",
    sources=[
        "CHANGELOG.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
    ],
    tags=["category:docs", "attest:docs"],
)
