# Bootstrap + tools mise can't cleanly provide. Run: brew bundle --file=Brewfile
#
# Everything else (just, gh, jq, yq, docker-cli, docker-compose) is pinned in
# .mise.toml and installed by `mise install`.
#
# TWO PLATFORMS, ONE FILE, NO CONDITIONALS (bh-q160.1). Every formula below installs on macOS
# and Linux alike, so there is nothing to branch on. A second Brewfile.linux would let the pins
# drift the way .mise.toml and docker-bake.hcl already have; a platform conditional is the same
# drift in one file. Linux support exists so a second host can be stood up from a git clone
# (bh-q160); it is not speculative portability. KEEP IT CONDITIONAL-FREE — a formula that needs
# `if OS.mac?` is usually a formula that does not belong here at all (see DOCKER RUNTIME below).
#
# RETIREMENT CONDITION for this whole file: when bh-0gpn.5 lands mise as the
# native plane's pin source, these are DELETED, not ported to it
# (docs/design/deployment-isolation-direction-adr.md, Decision 1 — brew is a distribution
# mechanism, not a pinning mechanism). Check that bead before extending this file.
#
# RUNTIME vs DEV, settled rather than left ambiguous: `osv-scanner` is dev-only (it serves
# `just license-check` / `just cve-report`, never `bh` at runtime) and mise's toolchain is a
# development toolchain. Homebrew Bundle has no groups, and the design above rules out a second
# file, so this installs the SUPERSET on both platforms and says so here. A runtime-only host
# gets one extra Go binary; that is cheaper than a manifest that can drift.
#
# Linux bottle availability verified 2026-08-04 against the formulae.brew.sh API — mise, dolt
# and osv-scanner all publish x86_64_linux AND arm64_linux bottles, so none of them fall back to
# a source build there. `beads` is a HEAD pin (below), which is a source build on every platform
# by construction.

brew "mise"      # tool-version manager — provides everything in .mise.toml
# DOCKER RUNTIME IS A PREREQUISITE, NOT A DEPENDENCY — nothing here installs one. On Linux the
# daemon is native and comes from the distro (docker-ce / docker.io), outside brew. On macOS it
# is the operator's choice of Docker Desktop, colima or OrbStack, and a bootstrap has no
# business picking one for them. Docker mode's own docs state the runtime as a prerequisite;
# this file just stops contradicting them.
#
# `brew "colima"` used to sit here unguarded. MEASURED on a bare Debian 13 host, 2026-08-05:
# colima has a linux bottle, so `brew bundle` installs it happily — and its dependency closure
# alone took the prefix from 1.1G / 12 kegs to 5.7G / 98 kegs BEFORE colima's own keg landed
# (llvm 2.6G, mesa 397M, the X11 stack, QEMU's deps — on a headless server, for a VM that would
# never run). `if OS.mac?` would fix Linux and leave macOS installing a runtime it may already
# have; deleting the line fixes both and costs the file its last conditional.
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
#
# LINUX: the HEAD build works there — verified 2026-08-04 against the formula's own metadata.
# Its head url is a plain git clone of gastownhall/beads, build dep `go`, runtime deps `dolt`
# and `icu4c@78`, and NO macOS requirement, so Linuxbrew builds it the same way. Note this is a
# CLONE build, so it never hits the module-path trap that bites `go install
# github.com/gastownhall/beads/...` (go.mod still declares github.com/steveyegge/beads after
# the org transfer — see bh-q160.4, which builds bd that other way for the image).
brew "beads", args: ["HEAD"]

# supply-chain scanner (license gate + CVE signal — `just license-check` / `just cve-report`).
# Not in mise's registry; homebrew-core carries it.
brew "osv-scanner"
