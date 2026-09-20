# Ownership for tracked files at the repo root that aren't under /src, /tests, or another
# owned directory (bh-1j3ei.2). No target infers deps from these; they exist so
# `scripts/check_pants_ownership.py` (run from `just architecture-check`) sees zero unowned
# tracked files.
files(
    name="root-config",
    sources=[
        ".env.example",
        ".git-blame-ignore-revs",
        ".gitignore",
        ".markdownlint-cli2.jsonc",
        ".mise.toml",
        "Brewfile",
        "BUILD",
        "CHANGELOG.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "INSTALL.md",
        "LICENSE",
        "README.md",
        "SECURITY.md",
        "docker-bake.hcl",
        "docker-compose.yml",
        "flake.lock",
        "flake.nix",
        "justfile",
        "lefthook.yml",
        "osv-scanner.toml",
        "pants.toml",
        "pyproject.toml",
        "uv.lock",
    ],
)
