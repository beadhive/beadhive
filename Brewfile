# Bootstrap + tools mise can't cleanly provide. Run: brew bundle
#
# Everything else (just, gh, jq, yq, docker-cli, docker-compose) is pinned in
# .mise.toml and installed by `mise install`.

brew "mise"      # tool-version manager — provides everything in .mise.toml
brew "colima"    # Docker daemon/VM for macOS (system deps; not a plain binary)
brew "dolt"      # Dolt CLI — backups, diagnostics, SQL shell (not in mise registry)
# the `bd` issue tracker (homebrew-core; not in mise registry).
#
# HEAD, NOT stable — deliberate, and load-bearing. `bd dolt pull` hangs indefinitely
# (upstream #4770: quadratic git cat-file) on any build embedding dolt < v2.2.0. Every
# tagged bd release through v1.1.2 (2026-07-26) pins the SAME dolt commit,
# 1bf533220ab0 dated 2026-06-05 — 168 commits behind v2.2.0 (2026-07-15), so v1.1.2
# structurally cannot contain the fix despite shipping after it. Verified by decoding
# go.mod at each tag, not inferred from release notes.
#
# Drop `args: ["HEAD"]` when EITHER: a tagged release pins dolt >= v2.2.0 (see bh-bmsg
# for the evidence + re-check log), OR bh-00cq lands — running bd against an external
# `dolt sql-server` decouples the dolt version from bd's release cadence entirely, since
# bd issues fetch/pull as `CALL DOLT_FETCH(...)` over the connection, so the SERVER's dolt
# does the work. Note the migration caveat in bh-bmsg: reverting to stable is NOT a clean
# rollback once the store has migrated.
brew "beads", args: ["HEAD"]
