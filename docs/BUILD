# Ownership only (bh-1j3ei.2) — these targets make every tracked doc file resolve in the
# repo-wide dependents graph. They are deliberately split by subdirectory (rather than one
# `docs/**/*` target) so a later bead (bh-1j3ei.4, "declare runtime file inputs ... starting
# with the 74 doc readers") can attach `attest:*` tags or narrow dependencies per group
# without re-touching every doc-reading test's BUILD entry.
files(
    name="docs-root",
    sources=["*.md"],
    tags=["attest:docs"],
)

files(
    name="docs-assets",
    sources=["assets/**/*"],
    tags=["attest:docs"],
)

files(
    name="docs-design",
    sources=["design/**/*"],
    tags=["attest:docs"],
)

files(
    name="docs-examples",
    sources=["examples/**/*"],
    tags=["attest:docs"],
)

files(
    name="docs-proof",
    sources=["proof/**/*"],
    tags=["attest:architecture-contracts"],
)

files(
    name="docs-releases",
    sources=["releases/**/*"],
    tags=["attest:docs"],
)

files(
    name="docs-schemas",
    sources=["schemas/**/*"],
    tags=["attest:architecture-contracts"],
)

files(
    name="docs-spikes",
    sources=["spikes/**/*"],
    tags=["attest:docs"],
)

files(
    name="docs-upstream",
    sources=["upstream/**/*"],
    tags=["attest:docs"],
)
