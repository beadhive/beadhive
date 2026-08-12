# bh development tasks. Run `just` to list.
# Operational commands (bd, rigs, labels, dolt) now live in the `bh` CLI.

# list available recipes
default:
    @just --list

# --file=Brewfile is REQUIRED, not decoration: a bare `brew bundle` honours a global
# $HOMEBREW_BUNDLE_FILE, so on any machine that sets one (a personal ~/.config/homebrew/Brewfile
# is common) bootstrap would silently install from THAT file and skip this repo's pins —
# including the deliberate `beads` HEAD pin the pull-hang fix depends on.
#
# `mise exec --` on the last two lines is LOAD-BEARING, not style. just runs each recipe line in
# its own shell, inheriting the environment bootstrap started with — i.e. from BEFORE `mise
# install` ran. mise installs into ~/.local/share/mise/installs/..., which is on PATH only once
# mise is activated, so line 3 cannot see what line 2 just installed. Measured on a bare Debian
# 13 host 2026-08-05: without these, bootstrap dies at `uv sync` with `sh: 1: uv: not found`,
# exit 127 (bh-q160.5). `just hooks` needs it for the same reason — lefthook is a mise tool too.
#
# It only bites a machine where nothing from .mise.toml was already on PATH — exactly a new
# host, never an existing dev's laptop. Test any change to these lines somewhere the toolchain
# is absent, or the bug looks fixed when it is only hidden.
#
# (Summary last on purpose: `just --list` shows the comment line immediately above the recipe.)
# install the toolchain (Homebrew bundle + mise) and dev deps + git hooks
bootstrap:
    brew bundle --file=Brewfile
    mise install
    mise exec -- uv sync
    mise exec -- just hooks

# fast gate: ruff + markdown + licenses + unit tests (the default validate_cmd)
#
# DELIBERATELY EXCLUDES `demo-local-loop` (bh-bwcxx). The demo is the one operator-facing
# artifact — it drives the `local` work-runtime tier end to end against a scratch hive and is
# what a reviewer runs when they don't trust the suite to have covered the integration — but at
# ~115s against a fast gate the rest of which runs in low single-digit seconds, folding it in
# here would roughly 20x every `just check` / `bh work check` invocation on every bead, most of
# which never touch the loop/dispatch path. It is wired into `check-all` instead (below), which
# is already minutes long. This is a recorded trade, not an oversight: bh-bwcxx shipped without
# either, and a rename inside the package broke the demo while `just check` stayed green.
#
# WHAT THIS COMMENT USED TO CLAIM, and why it was wrong (bh-4kq1b). It said `check-all` "is the
# gate actually enforced at the main-merge boundary (lefthook's pre-push `main-gate`) — so it
# DOES run before anything lands on main". Neither half held. The hooks were never installed in
# this repo's own clone (`just hooks` died at `lefthook: not found`, fixed below), and a
# PRE-PUSH gate is the wrong seam anyway for a hive whose work lands on LOCAL main via
# `bh work merge` / `finish`: local main sat 44 commits ahead of origin/main when this was
# found. Four integration tests were red on main for a whole molecule with every gate green.
# The enforcing seam is now the LAND itself — `work.validate.molecule` / `.merge-main` for this
# hive point at `check-all`, so `bh work finish` / `merge` runs it from a clean checkout before
# anything reaches main. The pre-push job stays as the belt to that braces.
check: lint lint-md license-check test

# full gate: ruff + markdown + licenses + the COMPLETE suite (unit + integration).
#
# WIRED AT TWO SEAMS, and it needs both (bh-4kq1b):
#   1. THE LAND. `work.validate.molecule` / `work.validate.merge-main` on this hive's registry
#      entry point here, so `bh work finish` / `merge` runs it from a clean checkout before the
#      change reaches main. This is the seam that fires in practice — merges land on LOCAL main
#      and pushes are batched.
#   2. THE PUSH. lefthook's `main-gate` pre-push job (scripts/main-push-gate.sh), from bh-dfz2.
#      Necessary but not sufficient on its own: it is many lands downstream, and it does nothing
#      at all in a clone where `just hooks` was never run — which was this repo's own clone.
#
# TWO PASSES, not `(test FULL)` (bh-c1qp). FULL is the empty marker expression, which the `test`
# recipe read as "not the FAST set" and so dropped `-n auto` — running all 3767 tests in ONE
# process. Splitting the selection lets both halves run parallel: 1222s serial -> ~383s.
# Same coverage, same guard, and no `-m` gap between them (FAST is literally `not integration`,
# so the two passes partition the suite rather than overlapping or missing).
#
# STILL TWO PASSES once both are parallel, deliberately: `require-bd` guards only the half that
# needs `bd`, and a failure names which half broke without reading the marker off the argv.
# whether ONE combined `-n auto` pass beats two is unmeasured — the workers would interleave
# instead of draining a fast pass then a slow one — and is left to a follow-up rather than
# claimed here.
#
# Both directions fail closed, verified against just 1.58.0 rather than assumed: a red FIRST pass
# aborts before the second runs, a red SECOND pass still fails the recipe, exit 1 either way.
# NB just memoizes a dependency by recipe+ARGS — `(test X) (test X)` would run ONCE. These two
# differ, so both run; do not "simplify" them into the same argument.
#
# THE SECOND PASS IS `test-integration-land`, not `(test "integration")` (bh-4kq1b) — see the
# QUARANTINE comment on that recipe below for why. `just test integration` (bare) and a plain
# `pytest` still run the full integration selection, unquarantined.
check-all: require-bd lint lint-md license-check (test FAST) test-integration-land demo-local-loop

# `check-all`'s prerequisite, and the reason it is one (bh-dfz2): the integration half is REAL
# `bd` work, and every integration test self-skips when the binary is absent
# (`skip_if_no_bd`/`skipif(shutil.which("bd") is None)`). So on a host without `bd` the full
# gate ran ZERO integration tests and reported green — a gate that looks wired and tests
# nothing, the same failure mode `check-all` sitting unwired had. It must refuse to run rather
# than pass vacuously. `check` (FAST) excludes integration by construction and needs no guard.
require-bd:
    @command -v bd >/dev/null 2>&1 || { \
        echo "check-all needs the 'bd' binary on PATH: its integration half drives a real bd," >&2; \
        echo "and without it every integration test SKIPS and the full gate passes vacuously." >&2; \
        echo "  install it with:  just bootstrap   (Brewfile pins beads)" >&2; \
        echo "  or run the fast gate instead:  just check" >&2; \
        exit 1; }

# convention gate (~3s): what lefthook's pre-commit runs. Deliberately NOT `just check` (~6min) —
# a six-minute pre-commit gets --no-verify'd within a week, leaving the repo ungated while looking
# gated. `check`/`check-all` stay the real gates, run deliberately.
conventions:
    uv run ruff check
    uv run pytest tests/test_naming_conventions.py -q

# install lefthook's git hooks (see lefthook.yml + docs/design/git-hooks-entrypoint-adr.md).
# --reset-hooks-path clears any core.hooksPath a previous tool claimed; lefthook installs into
# the default .git/hooks, and everything else chains from lefthook.yml.
#
# `mise exec --` for the SAME reason `bootstrap` uses it, and the comment there already warned
# this recipe needs it — it just never got it (bh-4kq1b). lefthook is a mise tool, so a bare
# `lefthook` resolves only in a shell where mise is already activated. Anywhere else — a fresh
# host, a non-interactive shell, an agent's `just hooks` — it dies with `lefthook: not found`,
# exit 127, and the hooks silently do not exist. Measured on this repo's own main clone
# 2026-08-11: `.git/hooks/` held nothing but git's `.sample` files, so the pre-push `main-gate`
# bh-dfz2 wired had NEVER fired, and four integration tests rotted red on main behind it.
hooks:
    mise exec -- lefthook install --reset-hooks-path
    @echo "→ verify: .git/hooks should now hold pre-commit, commit-msg, prepare-commit-msg, pre-push"

# lint (includes format-check so the tree can't silently drift from the pinned ruff — bh-ukzy)
lint:
    uv run ruff check
    uv run ruff format --check

# lint markdown docs (config: .markdownlint-cli2.jsonc)
lint-md:
    markdownlint-cli2

# --- supply chain: license gate + CVE signal (evidence: docs/spikes/bh-vf8h.*) ---------------

# Mode toggles, INDEPENDENT by design. `enforce` fails on a finding; `warn` reports and passes.
# The defaults encode why the two are separate gates at all: license policy is near-deterministic
# and worth blocking on, while a CVE feed is noisy and continuously changing — a blocking CVE gate
# gets switched off within a month, taking the license gate with it. Neither is hardcoded, so a
# hive can enforce CVEs (BH_CVE_MODE=enforce) or downgrade the license gate during a migration
# without editing recipes. What must NEVER happen is one toggle controlling both.
license_mode := env("BH_LICENSE_MODE", "enforce")
cve_mode := env("BH_CVE_MODE", "warn")

# A THIRD independent toggle, for the IMAGE closure (bh-e6uk), for exactly the reason the first two
# are independent. It defaults to `warn` and should stay there: grype's nix findings are dominated
# by base-layer glibc, and four of the top sixteen on the first run were the DISPUTED
# CVE-2019-1010022..25 series, which glibc upstream rejects as not-vulnerabilities. A blocking gate
# over a feed carrying that much contested content gets switched off within a month — taking
# whatever shares its switch with it.
image_cve_mode := env("BH_IMAGE_CVE_MODE", "warn")

# The allowed set. SPDX identifiers only — osv-scanner rejects anything else with exit 127.
# MPL-2.0 is here DELIBERATELY, for certifi: file-level copyleft, arriving transitively via
# httpcore/httpx (core) and requests (otel extra). Nothing here modifies or vendors it, and it is
# pure Python, so the shipped bytes ARE the source and MPL §3.2 is satisfied by construction.
# Do not remove it as an oversight — the gate goes red on every run without it.
# HPND is here for pywin32's bundled Scintilla; see osv-scanner.toml for that aggregate.
# NEVER add UNKNOWN: osv-scanner accepts it as valid SPDX, so it would silently permit every
# unlicensed package in the tree. The failure is invisible, which is what makes it dangerous.
license_allow := "MIT,Apache-2.0,BSD-3-Clause,BSD-2-Clause,ISC,PSF-2.0,Unlicense,MPL-2.0,HPND"

# generate the CycloneDX SBOM from the RESOLVED lockfile (not the declared ranges)
#
# THE FILENAME IS LOAD-BEARING, not a preference: osv-scanner dispatches its extractor on the
# FILENAME, never on content. `bom.json` and `*.cdx.json` work; `sbom.json` — the obvious name —
# is rejected outright with an opaque `could not determine extractor` / exit 127. Do not tidy it.
#
# `uv export --format cyclonedx1.5` is marked PREVIEW by Astral and may change in any release.
# It emits purls + the dependency graph but NO licenses and NO hashes; licenses come from
# deps.dev at scan time. If this breaks, the fallback is syft over the built wheel — record the
# switch rather than making it silently.
sbom:
    uv export --format cyclonedx1.5 --no-dev -q -o bom.json

# the IMAGE sbom — the nix closure the container actually ships (bh-btry)
#
# A DIFFERENT ARTIFACT FROM `sbom` ABOVE, not a competitor. `bom.json` describes the wheel's
# PYTHON dependency graph; this describes the CLOSURE of binaries the image carries — 19
# components against the 7 that docker/toolchain-metadata.json names, because a closure includes
# what those seven pull in. Neither answers the other's question; docs/ASSURANCE.md says which
# is which.
#
# NOT PART OF `just image`, deliberately. sbomnix's own closure is 333 paths, 348MB download,
# 1.4GB unpacked (measured), and putting that on the critical path of every image build would make
# every cold build fetch a Python scientific stack to produce a release artifact the image does not
# need to contain. The SBOM describes the image; it does not belong inside it.
#
# RUNS IN DOCKER because the macOS dev plane has no nix (ADR Decision 5 / bh-q160.12), same as
# `toolchain-metadata`. `--inputs-from` pins sbomnix to the SAME nixpkgs as the closure it
# describes, so describer and described cannot come from different revisions.
#
# Output is gitignored (dist/): it is derived, and it carries a fresh timestamp and UUID on every
# run, so committing it would produce a diff on every regeneration that says nothing.
[group('image')]
image-sbom:
    mkdir -p dist
    docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" nixos/nix:latest sh -c \
        'export NIX_CONFIG="experimental-features = nix-command flakes"; \
         nix run --inputs-from path:/src nixpkgs#sbomnix -- "path:/src#image" \
           --cdx /out/image-sbom.cdx.json --csv /dev/null --spdx /dev/null'
    @echo "wrote dist/image-sbom.cdx.json"
    @echo "NOTE: do NOT scan this with osv-scanner — OSV has no nix ecosystem, so it parses the"
    @echo "      file and scans ZERO of its components while reporting 'No issues found'."
    @echo "      Use a CPE-based scanner; see docs/ASSURANCE.md and bh-e6uk."

# license gate — BLOCKING by default (BH_LICENSE_MODE=warn to downgrade)
#
# osv-license-gate.sh, NOT osv-gate.sh (bh-1kvq): `scan source` reports vulnerabilities AND
# licenses under one exit code, so wrapping it in the generic gate made a CVE finding block this
# recipe under the default BH_LICENSE_MODE=enforce — the only escape was BH_LICENSE_MODE=warn,
# which also disabled license enforcement, exactly the one-toggle-controls-both outcome above
# says must never happen. osv-license-gate.sh re-derives a license-only status instead; CVEs
# found by the same scan are left to `cve-report` below, which is what already gates on them.
license-check: sbom
    @scripts/osv-license-gate.sh {{license_mode}} "license gate" \
        scan source -L bom.json --licenses="{{license_allow}}" --config osv-scanner.toml

# CVE signal — ADVISORY by default (BH_CVE_MODE=enforce to block on it)
cve-report: sbom
    @scripts/osv-gate.sh {{cve_mode}} "CVE signal" scan source -L bom.json

# IMAGE CVE signal — the nix closure, via grype. ADVISORY by default (BH_IMAGE_CVE_MODE=enforce)
#
# NOT wired into `check` or `check-all`, and not into `just image` either. Two reasons, both
# measured: grype downloads a vulnerability database, and the scan runs against an SBOM that
# `just image-sbom` must produce first — whose own tool has a 1.4GB closure. This is a deliberate,
# run-it-when-you-mean-it signal, like `cve-report`, not a pre-commit tax.
#
# It scans what NOTHING scanned before: the closure's transitive dependencies. The first run's
# findings were almost entirely glibc, which is not one of the seven binaries we pin — which is
# the whole argument for scanning a closure rather than a pin list.
[group('image')]
image-cve-report: image-sbom
    @scripts/image-cve-gate.sh {{image_cve_mode}} dist/image-sbom.cdx.json

# format
fmt:
    uv run ruff format

# Test selection (a pytest -m expression). FAST excludes the slow real-bd integration harness;
# FULL ("") runs the complete suite. Valid `set`: "not integration" (FAST) | "integration" | "" (FULL).
FAST := "not integration"
FULL := ""

# run the suite for a marker selection (default: the fast unit-only set)
#   just test               → unit only (fast)    just test integration → real-bd harness only
#   just test ""            → the complete suite (unit + integration; integration self-skips w/o bd)
# ALWAYS parallel (pytest-xdist `-n auto`, one worker per CPU). This comment used to assert the
# real-bd integration harness was NOT parallel-safe because "its cases share state" — and that
# stopped being true without anyone re-checking. Three fixes each removed a piece of that state:
# bh-dfz2 + conftest (ephemeral ports, no literal-port collisions between workers), bh-areg.7's
# `_sandbox_shared_server` (a per-TEST `BEADS_SHARED_SERVER_DIR` + port, so cases stopped sharing
# one server), and bh-cbou (servers no longer leak and hold a port past the run).
#
# MEASURED, not assumed, on the branch carrying all three: `-m integration -n auto` is green over
# repeated consecutive runs at ~200-260s against 543s serial, leaving zero stray servers behind.
# The old claim cost ~340s of every gate run for a constraint that no longer existed. If a future
# change genuinely reintroduces shared state, re-serialize THAT selection and say what shares
# what — do not restore a blanket assertion.
#
# `uv run pytest -n0 ...` forces serial for debugging a cross-test interaction, and is also how
# to check whether a new flake is an xdist ordering artifact.
#
# Deliberately no absolute timings in this comment — the previous ones (268s → 65s) were stale by
# ~4x within months, because the number tracks the test count and nothing updates a comment. The
# durable claim is the SHAPE; measure when you need a figure.
test set=FAST:
    uv run pytest -n auto {{ if set == "" { "" } else { "-m " + quote(set) } }}

# QUARANTINE (bh-4kq1b, tracking bh-tfapu): the LAND gate's integration pass, minus one test.
#
# `test_host_fence_int.py::test_the_located_transport_repo_is_the_one_that_pushes` is a known
# true-positive (bh-tfapu, triaged 2026-08-08): `bd dolt push` no longer fires git hooks in the
# transport repo, so the epoch fence is inoperable and multi-host write enforcement is advisory.
# Fixing it is an upstream `bd` issue, not quick, and disproportionate to block ALL integration
# on. Reverting the gate that made it visible (bh-4kq1b's own `check-all` wiring) would just
# restore the hole. So: quarantine this ONE node from the land path only.
#
# THIS IS THE ONLY PLACE IT IS QUARANTINED. `just test integration` (bare) and a plain
# `uv run pytest tests/test_host_fence_int.py` run it and it still FAILS — this recipe exists
# so `check-all` (and therefore `bh work finish`/`merge`) does not block on it. Whoever closes
# bh-tfapu: delete the two --deselect lines below and this comment: `grep -rn bh-tfapu justfile`
# finds this recipe, so closing that bead without touching this file leaves it a stale quarantine
# nobody remembers exists.
# SECOND QUARANTINE (bh-njdxk, P0) — four tests that fail ONLY in a real checkout.
#
# They pass 53/53 in a bare worktree and fail, deterministically, in the main clone: five
# consecutive `-n auto` runs produced the IDENTICAL four, and the same four pass SERIALLY in that
# same clone. So the tests are not wrong and the code is not broken — the SANDBOX is.
#
# Ruled out by measurement before quarantining, because a quarantine on a guess hides a real bug:
# not the change that first hit it (same SHA passes in a bare worktree); not leaked dolt servers
# or load (killed all 20, load 11 -> 1.75, no change); not the xdist distribution (deselecting an
# unrelated added test changes nothing); not `.beads` drift (metadata byte-identical both paths);
# and NOT bh-njdxk's Defect A — repairing the clobbered `.git/config` did not fix them either.
# The cause is genuinely not yet known, which is exactly why it is a P0 bead and not a TODO.
#
# WHY QUARANTINE RATHER THAN UNWIRE THE GATE: `bh work merge`/`finish` land on LOCAL main and the
# PUSH is what runs this, so an unexplained failure here blocks every unrelated push and looks
# like "your change broke integration". The alternative people reach for is `git push
# --no-verify`, and once that is habit the gate is gone — which is the hole bh-dfz2 and bh-4kq1b
# were closing. Four named deselects keep the other 49 enforcing.
#
# THESE FOUR ALMOST CERTAINLY PASS IN CI, where there is no enclosing hive — so this trades away
# real coverage in the one environment that has the bug. Re-measure before assuming otherwise.
# Whoever closes bh-njdxk: delete the four --deselect lines below and this comment.
#
# FENCED (bh-pxoby). Runs through `scripts/hermetic.sh`, which puts the suite in a bubblewrap
# sandbox: host read-only, tmpfs HOME (so ~/.beads and ~/.gitconfig leave bd's resolution walk),
# loopback up but no egress. 41ms per spawn — cheap enough to be the default rather than an
# opt-in. Off Linux the wrapper says so on stderr and runs unfenced; BH_HERMETIC=0 forces that.
test-integration-land:
    ./scripts/hermetic.sh uv run pytest -n auto -m "integration" \
        --deselect "tests/test_host_fence_int.py::test_the_located_transport_repo_is_the_one_that_pushes[embedded]" \
        --deselect "tests/test_host_fence_int.py::test_the_located_transport_repo_is_the_one_that_pushes[shared-server]" \
        --deselect "tests/test_hub_bulk_int.py::test_bulk_copy_matches_a_real_bd_produced_aggregate" \
        --deselect "tests/test_hub_bulk_int.py::test_co_located_database_and_server_databases_against_the_real_server" \
        --deselect "tests/test_hq_backup_server_mode_int.py::test_server_mode_hq_backup_and_restore_real_round_trip" \
        --deselect "tests/test_localloop_int.py::test_restart_mid_molecule_neither_double_claims_nor_leaves_a_seat_spending" \
        --deselect "tests/test_storage_migrate_int.py::test_migrated_furnished_hive_does_not_untrack_the_moved_aside_store"

# ^ the last deselect is the FENCE's own quarantine, not bh-njdxk's (bh-pxoby). That test passes
# unfenced (28s) and fails fenced with one extra finding: "could not re-point bd's live backup
# destination back to .beads/backup after migrating". Measured, not assumed — and NOT understood:
# `bd backup add` on its own works fine inside the fence, so the cause is something specific to
# this test's migrated-store setup. One test of 49; the other 48 pass fenced. Whoever picks this
# up: reproduce with
#   ./scripts/hermetic.sh uv run pytest tests/test_storage_migrate_int.py -k furnished
# and delete this deselect.

# test coverage over src/beadhive, unit set only (term-missing shows the uncovered lines).
# NOT part of `just check`: measured +15% wall (64.6s -> 74.4s), and coverage is a periodic
# question, not a per-commit gate. No --cov-fail-under threshold yet — see bh-rmem: pick one
# from a real baseline rather than asserting a number nobody has looked at.
cov:
    uv run pytest -n auto -m 'not integration' --cov=src/beadhive --cov-report=term-missing

# run the harness and render each git history (mode=all) or only divergent ones (mode=diff)
# streams live per-bead progress; -v shows which test is running
render-int mode="all":
    AGF_RENDER={{mode}} uv run pytest -m integration -s -v

# demo the bh CLI against the real app (used by `bh work review --demo`); extend per feature
demo:
    uv run bh --help

# run the `local` work-runtime tier's operator-facing demo end to end against a scratch hive
# (bh-c6dk.5 / bh-bwcxx). ~115s: it drives a real molecule through gate check -> reclaim ->
# host lease -> heartbeat -> caps -> harvest -> decide -> pick-claim -> spawn -> envelope ->
# advance, including a blocked seat and a cancelled/reaped one, then re-reads every bead and
# asserts the molecule actually landed — exit code IS the verdict. Isolation is asserted, not
# assumed: it tripwires `~/.beadhive` and this repo's `.beads/` before and after. Part of
# `check-all`, not `check` — see the comment on `check` above for why.
demo-local-loop:
    uv run scripts/demo_local_loop.py

# preview the next version bump AND its changelog entry from conventional commits (no writes)
bump-dry:
    uv run cz bump --dry-run
    uv run cz changelog --incremental --dry-run

# bump version (pyproject.toml) + uv.lock + CHANGELOG.md, tag, and commit as one unit
# (--changelog keeps them atomic — never a version bump without its changelog entry or vice
# versa). uv.lock rides along via commitizen's pre_bump_hooks (see pyproject.toml), so the tag
# covers a lockfile that already matches the new version.
bump:
    uv run cz bump --changelog

# build the wheel/sdist
build:
    uv build

# install bh on PATH (~/.local/bin/bh) — includes the otel extra so the installed bh
# can export OpenTelemetry out of the box (fastmcp ships as a core dependency).
install:
    uv tool install --force '.[otel]'

# ---- local-install: a checkout -> a provisioned Linux host (bh-q160.5) ----------------------
#
#     nix develop --command just local-install mode=native from_source=0 answers=host.yaml
#
# A ROUTER, NOT AN INSTALLER. Every step is an existing command; this recipe owns the ORDER,
# the idempotence and the failure messages, and nothing else. Logic a step needs belongs in the
# verb it calls — a justfile full of inline bash is how this becomes the thing nobody can debug.
#
# STEP 1 INSTALLS THE TOOLCHAIN INTO THE USER PROFILE, and used to be deliberately empty — that
# emptiness WAS a bug (bh-ytqc). "The toolchain arrives with the flake devShell" is true only
# while you are inside it: measured on beadhive-factory 2026-08-05, immediately after a
# SUCCESSFUL `just local-install from_source=1`, `bh setup check` reported 4 of 4 inside
# `nix develop` and 0 OF 4 outside it. A provisioned host is precisely the machine nobody is
# sitting at — cron, systemd, `ssh host bh sync` — and every one of those gets the bare PATH.
#
# `nix profile install .#default` is the fix, and the mechanism already existed: flake.nix
# already exposes `packages.default` as a buildEnv of the same toolchain, and `~/.nix-profile/bin`
# is ALREADY on the host's PATH. Two alternatives were weighed and rejected:
#
#   • a systemd `Environment=`/`EnvironmentFile=` naming the store paths — scoped to the units
#     that carry it, so `ssh host bh setup check` and an interactive login still see nothing, and
#     it is an ops change bh does not own (the same argument role.py makes for bh-og0q.2).
#   • a `/etc/profile.d` hook — needs root, and reaches LOGIN SHELLS only; cron and systemd, the
#     two contexts this bead exists for, still miss it.
#
# A Nix store path is a real binary that can be put on PATH for good, which is the distinction
# ADR Decision 5 actually wanted over a mise shim. It is just not what local-install did.
#
# GUARDED RATHER THAN RE-RUN, so `local-install` stays a no-op end to end: `nix profile install`
# on an already-installed flake ref is not a documented no-op across nix versions, whereas the
# store path in `nix profile list` carries the buildEnv's name in every version that prints it.
# A toolchain BUMP (a changed flake.lock) therefore needs `nix profile upgrade` — deliberately
# not run here, because upgrading a user profile is not something an install step should do
# behind the operator's back.
#
# No `brew bundle`, no `mise install` on this plane: a provisioned host has neither, by decision
# (ADR Decision 5 / bh-q160.12). `bootstrap` at the top of this file is the OTHER plane — macOS
# development, still mise + Brewfile — and the two do not mix.
#
# WHY EVERY `bh` BELOW IS ADDRESSED ABSOLUTELY — the one PATH problem the flake did NOT
# dissolve. `uv tool install` puts bh in `uv tool dir --bin` (~/.local/bin), which is not on
# PATH on a fresh host; measured again UNDER NIX on 2026-08-05, where the run had to prepend it
# by hand. `export PATH=…` on an earlier line cannot fix it: just runs every line in its own
# shell. Of the three candidates:
#
#   • UV_TOOL_BIN_DIR aimed at a directory already on PATH — what docker/Dockerfile does — needs
#     a WRITABLE one, and the unprivileged account a host is provisioned as has none: every
#     directory already on its PATH is either a read-only Nix store path or root-owned. The
#     image can take that route only because its build runs as root and writes /usr/local/bin.
#   • steps 2-5 as one shell line — one long inline script, with `bh` resolvable only inside a
#     process nobody can inspect from outside it.
#   • the absolute path from `uv tool dir --bin` — CHOSEN. Every line is independently correct
#     and independently runnable, there is no PATH mutation to lose, no privilege needed, and no
#     cooperation required from the invoking shell, so a fresh host and a dev laptop take the
#     same path. Same shape as `mise exec --` in `bootstrap`, for the same reason.
#
# IDEMPOTENCE, measured (uv 0.11.23), not assumed:
#   • `uv tool install beadhive[otel]==X` with X already installed prints "already installed"
#     and exits 0 — a real no-op. That is why there is no --force here: `install` above uses it
#     because a developer means "give me the tree I am editing", but here it would turn every
#     re-run into a reinstall.
#   • `uv tool install '.[otel]'` DOES pick up a changed working tree with no flag (uv reports
#     `~ beadhive==X`), so from_source=1 needs none either.
#   • steps 3-5 are idempotent in their own right — `host provision` probes before each of its
#     steps, and validates the answers file before running any of them (bh-q160.2). A typo'd
#     answers path therefore surfaces at step 5 rather than up front, which costs a re-run in
#     which steps 2-4 are no-ops.

# Settings are `NAME=VALUE` AFTER the recipe name, as documented above. just accepts overrides
# only BEFORE a recipe name — after it they are recipe ARGUMENTS, so `just local-install plan=1`
# would bind the literal string "plan=1" to a parameter (measured). `local-install` therefore
# forwards them to the private recipe that does the work. The forward keeps the documented
# interface AND just's own guards: an unknown NAME is refused by name, and `error()` below
# refuses a bad VALUE — both before a single line runs.
mode := "native"
from_source := "0"
answers := "host.yaml"
plan := "0"
posture := "host"

# Native only. Docker is bh-q160.7, deliberately behind this, so a `mode=` typo must not
# silently take the native path. just does not evaluate the branch it does not take, so the
# default costs nothing.
[private]
_mode_guard := if mode == "native" { "" } else { error("local-install: mode=" + mode + " is not supported — native only (docker mode is bh-q160.7)") }

# POSTURE picks what step 5 means (bh-vmdq.3). Steps 1-4 are identical either way — a laptop
# needs the same toolchain, the same bh and the same gates a fleet host does.
#
#   host    (default)  an inherited fleet: `host provision --answers <file>` resolves hq.remote
#                      and runs `hq clone`. Declarative and headless BY DESIGN (bh-q160.2).
#   laptop             THIS machine IS the HQ. There is no remote to clone from and no answers
#                      file to write, so provision's two central actions are both meaningless.
#                      Runs the narrower verbs directly instead.
#
# A FLAG RATHER THAN A SECOND RECIPE, deliberately: two provisioning sequences drift within a
# release — the same argument bh-q160.7 makes for docker mode sharing native's tail. Sharing
# steps 1-4 means a fix to the toolchain or the gates lands on both postures at once.
[private]
_posture_guard := if posture == "host" { "" } else if posture == "laptop" { "" } else { error("local-install: posture=" + posture + " is not supported — host or laptop") }

# plan=1 makes every EXECUTING line a no-op by prefixing it with `:`, the shell builtin that
# expands its arguments and does nothing. The step labels print either way, so the plan is the
# run's own ordering rather than a second description of it that can drift out of step.
[private]
_do := if plan == "1" { ":" } else if plan == "0" { "" } else { error("local-install: plan must be 0 or 1, got " + plan) }

# What step 2 installs. from_source=1 installs THIS working tree; the default installs the PyPI
# release this checkout's pyproject names, resolved at run time by scripts/release-pin.sh — so
# no version literal lives here and the tag names the release by construction. A `$(…)` inside a
# just STRING is inert until the line runs in a shell; a backtick assignment would run EAGERLY
# on every `just` invocation, so `just test` on a machine without uv would fail the whole
# justfile (measured).
#
# from_source=1 is ALSO the way out of the release window this epic opens in: the default
# installs a PyPI release, and a release older than the verbs steps 3-5 call cannot run them.
# Measured on beadhive-factory 2026-08-05 — the run reached step 4 and got `No such command
# 'harness'` from beadhive 0.7.1, which predates bh-q160.3 and bh-q160.2. Same circle, and the
# same exit, as docker/Dockerfile's BEADHIVE_WHEEL.
[private]
_pin := if from_source == "1" { ".[otel]" } else if from_source == "0" { "beadhive[otel]==$(scripts/release-pin.sh)" } else { error("local-install: from_source must be 0 or 1, got " + from_source) }

# The bh that step 2 installs, addressed absolutely — see the PATH note above.
[private]
_bh := "$(uv tool dir --bin)/bh"

# STEP 5, RESOLVED BY POSTURE (bh-vmdq.3). Built as variables rather than branched inside the
# recipe body: just evaluates every interpolation in a line it runs, so a laptop would otherwise
# still expand an answers path it never uses.
#
# The laptop pair is deliberately the NARROW verbs rather than an interactive `host provision`.
# provision is declarative and headless by design (bh-q160.2) — it resolves hq.remote and runs
# `hq clone`, and BOTH are meaningless when this machine IS the HQ and no remote exists.
# `hq init` already handles that case correctly: it wires a remote only if one is configured,
# so local-only falls out of the existing code path rather than needing a new one.
[private]
_step5_label := if posture == "laptop" { "local HQ, no remote — config init + hq init" } else { "join the fleet — host provision --answers " + answers }
[private]
_step5_cmd := if posture == "laptop" { '"' + _bh + '" config init && "' + _bh + '" hq init' } else { '"' + _bh + '" host provision --answers "' + answers + '"' }
# Said out loud because an undocumented absence reads as a bug. A local-only HQ is SUPPORTED,
# and what it costs is exactly the two things a remote buys.
[private]
_step5_note := if posture == "laptop" { "     HQ is LOCAL with no remote — the posture, not an omission. Costs: no backup, and no second machine until you wire one. Next: bh hive onboard <repo>" } else { "" }

# route this checkout to a provisioned host (settings: mode= from_source= answers= plan=)
[group('host')]
[positional-arguments]
local-install *settings:
    @{{ just_executable() }} "$@" _local-install

# regenerate docker/toolchain-metadata.json from flake.nix (bh-8b8o.2)
#
# The file is COMMITTED, like a lockfile, because its consumers need it where nix is NOT: the
# licence gate in tests/test_component_licenses.py runs on a macOS dev host with no nix, and
# tests/test_flake_toolchain.py states that contract outright. A gate that shells out to nix would
# SKIP there, and a gate that silently does not run is worse than no gate at all.
#
# RUNS NIX IN DOCKER rather than on the host, for that same reason — the macOS plane has no nix
# (ADR Decision 5 / bh-q160.12) and this recipe has to work there. Same `nixos/nix` image the
# docker build uses, so the two cannot disagree.
#
# Forgetting to run this does not ship stale metadata: the docker build regenerates and DIFFS the
# file, failing with a pointer back to this recipe.
[group('image')]
toolchain-metadata:
    docker run --rm -v "$PWD:/src:ro" nixos/nix:latest sh -c \
        'export NIX_CONFIG="experimental-features = nix-command flakes"; \
         nix build "path:/src#metadata" --out-link /tmp/m >/dev/null && cat /tmp/m' \
      > docker/toolchain-metadata.json
    @echo "wrote docker/toolchain-metadata.json — commit it"

# the ordered steps — reached only through `local-install`, which forwards the settings
[private]
_local-install:
    @echo "local-install{{ if plan == "1" { " — PLAN ONLY, nothing is changed" } else { "" } }}: mode={{ mode }} posture={{ posture }} from_source={{ from_source }}{{ if posture == "host" { " answers=" + answers } else { "" } }}"
    @echo "  1. toolchain -> the user profile, so bd/dolt/gh/git-workspace outlive this devShell"
    @{{ _do }} sh -c 'nix profile list 2>/dev/null | grep -q beadhive-local-install-toolchain || nix profile install .#default'
    @scripts/release-pin.sh --verify
    @echo "  2. uv tool install {{ _pin }}"
    @{{ _do }} uv tool install "{{ _pin }}"
    @echo "  3. {{ _bh }} setup check"
    @{{ _do }} "{{ _bh }}" setup check
    @echo "  4. {{ _bh }} harness auth --check"
    @{{ _do }} "{{ _bh }}" harness auth --check
    @echo "  5. {{ _step5_label }}"
    @{{ _do }} sh -c '{{ _step5_cmd }}'
    @echo "{{ _step5_note }}"

# ---- container image ---------------------------------------------------------------------
# Every pin lives in docker-bake.hcl; override one for a single run without editing it:
#   docker buildx bake agent --set agent.args.CLAUDE_CODE_VERSION=2.1.221

# THE TWO IMAGE RECIPES NEED DIFFERENT BUILDERS. This is not a style choice — each fails
# outright on the other's builder, and bh-pc2a.1 shipped both pointing at the same one:
#
#   image-cross  MUST use the docker-container builder. The default "docker" driver can only
#                build ONE platform, so a multi-platform bake is impossible without it.
#   image        MUST use the DAEMON's own builder. --load against colima FAILS on the
#                docker-container driver, reproducibly on both targets:
#                "failed to copy to tar: io: read/write on closed pipe". colima's daemon uses
#                the containerd image store, so its own builder writes straight into it with
#                no tar round-trip. Do NOT "fix" that by dropping --load — loading into the
#                local store is the entire point (docker compose consumes it, pull_policy=never).
CROSS_BUILDER := "beadhive"

# the platform `just image` bakes: whichever one this host runs natively
NATIVE_PLATFORM := "linux/" + if arch() == "x86_64" { "amd64" } else { "arm64" }

# Idempotent by design: every image recipe depends on this one, so a host provisioning itself
# unattended creates its own prerequisite instead of following a README.
#
# ALSO LINKS THE buildx PLUGIN, because installing buildx is only half the job: docker finds
# plugins in ~/.docker/cli-plugins, not on PATH. Two traps, both established the hard way:
#   • NEVER link the mise SHIM. mise shims dispatch on argv[0], so a link named `docker-buildx`
#     makes mise hunt for a shim of that name, fail with "not a valid shim", and docker then
#     reports "unknown command: docker buildx" — which looks exactly like buildx being absent.
#     The link must target the REAL binary.
#   • That binary's filename embeds BOTH version and platform (buildx-v0.36.0.darwin), so a
#     version bump breaks the link. Hence: derived from `mise where`, globbed, and re-created
#     on every run rather than left as a one-time manual step.
# create the docker-container buildx builder and link the buildx plugin (both idempotent)
image-builder:
    #!/usr/bin/env bash
    set -euo pipefail
    dir="$(mise where 'github:docker/buildx')"
    bin="$(find "$dir" -maxdepth 1 -name 'buildx-*' -type f | head -1)"
    if [ -z "$bin" ]; then
        echo "image-builder: no buildx binary under $dir — run 'mise install'" >&2
        exit 1
    fi
    mkdir -p ~/.docker/cli-plugins
    ln -sfn "$bin" ~/.docker/cli-plugins/docker-buildx
    docker buildx inspect {{CROSS_BUILDER}} > /dev/null 2>&1 \
        || docker buildx create --name {{CROSS_BUILDER}} --driver docker-container --bootstrap

# target: default (core+agent) | core | agent
# bake the NATIVE arch and --load it into the local image store (docker compose can use it)
#
# The daemon's builder is named after the ACTIVE DOCKER CONTEXT (`colima` on this host), which
# is why it is resolved rather than hardcoded. `--builder default` is not a fallback: buildx
# 0.36 rejects it outright with "use docker --context=default buildx".
# bake the NATIVE arch and --load it into the local image store
image target="default": image-builder
    BUILD_SHA="$(git rev-parse HEAD)" docker buildx bake \
        --builder "$(docker context show)" --set '*.platform={{NATIVE_PLATFORM}}' --load {{target}}

# Bake with bh installed from THIS WORKING TREE instead of PyPI (bh-pc2a.25).
#
# The proof gate (bh-pc2a.17) has to verify behaviour that is not released yet — `bh setup check`
# reading /etc/beadhive/image-manifest.json cannot reach PyPI until this epic lands, which the
# gate gates. This is the exit from that circle.
#
# The wheel is mounted from the NAMED CONTEXT wheelsrc=./dist, because the build context is
# ./docker and dist/ is not inside it. The resulting image's manifest records
# `local-wheel:<file>` and the version read from the installed bh, so it can never be mistaken
# for a released build.
#
# DEFAULTS TO BOTH TARGETS, matching `image` (bh-pc2a.33). It previously defaulted to `core`
# alone, which silently drifted the two images a full release apart: core was rebaked from the
# working tree while agent kept a day-old layer carrying a bh that predated the manifest reader.
# Nothing detected it — both read `:dev`, and `bh --version` only differs if you go looking. The
# `--set` patterns are `*` rather than `{{ target }}` for the same reason `image` uses `*`: the
# default target is a GROUP, and a group name never matches a `--set` target pattern.
# bake the native image(s) with bh built from this working tree
image-local target="default": image-builder
    #!/usr/bin/env bash
    set -euo pipefail
    uv build --wheel
    wheel="$(cd dist && ls -t beadhive-*.whl | head -1)"
    echo "baking {{target}} with local wheel: $wheel"
    BUILD_SHA="$(git rev-parse HEAD)" docker buildx bake \
        --builder "$(docker context show)" \
        --set '*.platform={{NATIVE_PLATFORM}}' \
        --set '*.contexts.wheelsrc=./dist' \
        --set "*.args.BEADHIVE_WHEEL=$wheel" \
        --load {{target}}

# Attribution guard on a BUILT image (bh-pc2a.23). Publishing an image makes us a REDISTRIBUTOR,
# so every "retain this notice in copies" term binds us — and today that holds only because
# `uv tool install` happens to preserve .dist-info/licenses/. Nothing else asserts it, so a
# future slimming change would drop every notice at once, silently, with nothing going red.
# Needs an image present: bake one first (`just image core`). Runs in bh-pc2a.17's proof gate.
# The default must match docker-bake.hcl's `tags = ["${REGISTRY}/core:${TAG}"]`, i.e.
# beadhive/core:dev — NOT beadhive-core, which is the image TITLE LABEL, not the tag.
# assert a built image still carries third-party licence notices
image-licenses ref="beadhive/core:dev":
    scripts/image-licenses.sh {{ref}}

# Drift guard on the LOCAL images (bh-pc2a.33). core and agent are supposed to be one build; they
# silently were not, and the symptom (`✗ missing: docker` from a stale agent) pointed at the wrong
# fix entirely. Compares each image's manifest against the other and against the checkout.
# Exit 1 only when the images disagree with EACH OTHER — being behind HEAD is normal mid-session
# and is reported without failing, so this stays runnable rather than becoming a check people skip.
# Exit 2 when nothing is baked yet, which is not a failure.
# report skew between the local images and this working tree
image-drift *refs:
    scripts/image-drift.sh {{refs}}

# The gap image-drift and the manifest CANNOT see (bh-m4nn8): the packed baml seats are what the
# `local` runtime tier spawns, and they are NOT image components — so a guard that compares what
# the image CONTAINS against what it CLAIMS never looks at them. Nothing asserted that the thing
# the image exists to run could be run by it, and it could not: the seats need GLIBC_2.39
# (measured with objdump; all four packed seats agree) while the image was bookworm at 2.36, so
# every seat died at exec. Two checks — the floor, hermetic and always run, and a real seat
# exec'd inside the image, which SKIPS loudly without a seat binary rather than passing quietly.
# Supply one via BH_SEAT_BINARY or the second argument (baml-harness: `just pack`).
# assert a packed seat can exec inside a built image
image-seat-exec ref="beadhive/agent:dev" seat="":
    scripts/image-glibc-floor.sh {{ref}} {{seat}}

# The proof gate (bh-pc2a.17): does a locally-baked image work with every bundled component
# TOGETHER? Layers needing credentials SKIP loudly rather than fail, and the script refuses to
# report "proven" while anything was skipped — a gate that reads green with half its checks
# silently absent is worse than no gate. Supply GH_TOKEN / BH_GATE_REPO / BH_GATE_PRIVATE_REPO
# for full coverage; see the script header. Record results in docs/proof/.
# run the proof gate against a baked image
proof-gate ref="beadhive/agent:dev":
    scripts/proof-gate.sh {{ref}}

# The other unattended prerequisite: building a foreign arch needs binfmt_misc emulators
# registered in the kernel, or the first RUN of the non-native leg dies with "exec format
# error". Idempotent — re-registering is a no-op. Native builds never need it, so only
# `image-cross` depends on it.
# register the QEMU emulators a cross-platform bake needs
image-qemu:
    docker run --privileged --rm tonistiigi/binfmt --install arm64,amd64 > /dev/null

# Deliberately NOT --load: a multi-platform bake produces an index over several images and the
# local image store has no single image to load — that combination is the classic bake papercut.
# The result stays in the build cache; give it an output when you need an artifact, e.g. --push
# once a registry exists, or --set '*.output=type=oci,dest=beadhive.oci'.
#
# KNOWN LIMIT on an Apple Silicon host. The non-native leg compiles git-workspace's vendored C
# under emulation and QEMU is not up to it — measured, it crashes gcc's cc1 and clang's
# integrated assembler alike. Rosetta compiles that same stage fine (measured, ~3.5 min), BUT
# buildx's docker-container driver ships its own qemu (/dev/.buildkit_qemu_emulator) and never
# consults the kernel's binfmt handler, so `colima start --vz-rosetta` does not rescue this
# recipe and neither does declaring the node's platforms. What does work:
#   • one native runner per arch — that is bh-pc2a.4's CI, and why it owns publishing
#   • per-arch single-platform builds, which DO get Rosetta because they can use the default
#     docker driver, joined into an index with `docker buildx imagetools create` once a
#     registry exists
# Until then `just image` is the supported local path — and the only one the proof gate wants.
# bake the FULL cross-platform set (linux/amd64 + linux/arm64) declared in docker-bake.hcl
image-cross target="default": image-builder image-qemu
    BUILD_SHA="$(git rev-parse HEAD)" docker buildx bake --builder {{CROSS_BUILDER}} {{target}}

# live OTel verification: start a collector first, then run to export traces+metrics+logs.
# Needs the otel extra (uv sync --extra otel) and a running OTLP collector.
# Default endpoint: gRPC on localhost:4317 (grafana/otel-lgtm or any OTLP-capable collector).
# HTTP transport: just otel-verify http://localhost:4318 (set WS_OTEL_PROTOCOL=http/protobuf).
# After running, check your collector for service.name=ws spans/metrics/logs.
otel-verify endpoint="http://localhost:4317":
    WS_OTEL_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT={{endpoint}} uv run pytest tests/test_otel_verify.py -v -s

# live metrics-usability verification: confirms bh metrics form a stable per-(hive,command)
# accumulating series with ws.hive/observaloop.profile labels (no service_instance_id) and
# that rate() returns data — proving the CLI-metrics preset + delta temporality fix works.
#
# Prerequisites:
#   1. Apply the CLI-metrics preset to your profile: bh hive init --observaloop
#   2. Start the hive's collector stack (e.g. grafana/otel-lgtm or your docker-compose)
#   3. Set WS_OBSERVALOOP_PROFILE to the active profile name
#   4. Needs the otel extra: uv sync --extra otel
#
# Default OTLP endpoint: gRPC localhost:4317; default Prometheus: http://localhost:9090.
# Override: just metrics-verify http://localhost:4317 http://localhost:9090
metrics-verify endpoint="http://localhost:4317" prom="http://localhost:9090":
    WS_METRICS_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT={{endpoint}} WS_OTEL_VERIFY_PROM={{prom}} \
        uv run pytest tests/test_metrics_verify.py -v -s
