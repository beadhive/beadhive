# bh development tasks. Run `just` to list.
# Operational commands (bd, rigs, labels, dolt) now live in the `bh` CLI.

# list available recipes
default:
    @just --list

# install the toolchain (Homebrew bundle + mise) and dev deps + git hooks
# --file=Brewfile is REQUIRED, not decoration: a bare `brew bundle` honours a global
# $HOMEBREW_BUNDLE_FILE, so on any machine that sets one (a personal ~/.config/homebrew/Brewfile
# is common) bootstrap would silently install from THAT file and skip this repo's pins —
# including the deliberate `beads` HEAD pin the pull-hang fix depends on.
bootstrap:
    brew bundle --file=Brewfile
    mise install
    uv sync
    just hooks

# fast gate: ruff + markdown + licenses + unit tests (the default validate_cmd)
check: lint lint-md license-check test

# full gate: ruff + markdown + licenses + the COMPLETE suite (unit + integration) — wire at
# main-merge points
check-all: lint lint-md license-check (test FULL)

# convention gate (~3s): what lefthook's pre-commit runs. Deliberately NOT `just check` (~6min) —
# a six-minute pre-commit gets --no-verify'd within a week, leaving the repo ungated while looking
# gated. `check`/`check-all` stay the real gates, run deliberately.
conventions:
    uv run ruff check
    uv run pytest tests/test_naming_conventions.py -q

# install lefthook's git hooks (see lefthook.yml + docs/design/git-hooks-entrypoint-adr.md).
# --reset-hooks-path clears any core.hooksPath a previous tool claimed; lefthook installs into
# the default .git/hooks, and everything else chains from lefthook.yml.
hooks:
    lefthook install --reset-hooks-path

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

# license gate — BLOCKING by default (BH_LICENSE_MODE=warn to downgrade)
license-check: sbom
    @scripts/osv-gate.sh {{license_mode}} "license gate" \
        scan source -L bom.json --licenses="{{license_allow}}" --config osv-scanner.toml

# CVE signal — ADVISORY by default (BH_CVE_MODE=enforce to block on it)
cve-report: sbom
    @scripts/osv-gate.sh {{cve_mode}} "CVE signal" scan source -L bom.json

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
# Parallel for the unit set only (pytest-xdist `-n auto`, one worker per CPU): 268s → 65s on 10
# cores. The real-bd integration harness is NOT parallel-safe — its cases share state — so any
# selection that can include it stays serial. `uv run pytest -n0 ...` forces serial for
# debugging a cross-test interaction.
test set=FAST:
    uv run pytest {{ if set == FAST { "-n auto" } else { "" } }} {{ if set == "" { "" } else { "-m " + quote(set) } }}

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
image target="default": image-builder
    BUILD_SHA="$(git rev-parse HEAD)" docker buildx bake \
        --builder "$(docker context show)" --set '*.platform={{NATIVE_PLATFORM}}' --load {{target}}

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
