# bh development tasks. Run `just` to list.
# Operational commands (bd, rigs, labels, dolt) now live in the `bh` CLI.

# list available recipes
default:
    @just --list

# install the toolchain (Homebrew bundle + mise) and dev deps + git hooks
bootstrap:
    brew bundle
    mise install
    uv sync
    just hooks

# fast gate: ruff + markdown + unit tests (the default validate_cmd + pre-commit hook)
check: lint lint-md test

# full gate: ruff + markdown + the COMPLETE suite (unit + integration) — wire at main-merge points
check-all: lint lint-md (test FULL)

# enable the tracked git hooks (pre-commit → just check)
hooks:
    git config core.hooksPath .githooks

# lint
lint:
    uv run ruff check

# lint markdown docs (config: .markdownlint-cli2.jsonc)
lint-md:
    markdownlint-cli2

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
test set=FAST:
    uv run pytest {{ if set == "" { "" } else { "-m " + quote(set) } }}

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

# bump version (pyproject.toml) + CHANGELOG.md, tag, and commit as one unit (--changelog keeps
# them atomic — never a version bump without its changelog entry or vice versa); then sync
# uv.lock (cz bump doesn't touch it, so a separate follow-up commit — not an amend, which would
# orphan cz's signed tag)
bump:
    uv run cz bump --changelog
    uv lock
    git add uv.lock
    git commit -m "fix(deps): sync uv.lock to the version bump"

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

# buildx builder for image bakes. The default "docker" driver can only build ONE platform,
# so a docker-container builder is a hard prerequisite for `image-cross`.
BUILDER := "beadhive"

# the platform `just image` bakes: whichever one this host runs natively
NATIVE_PLATFORM := "linux/" + if arch() == "x86_64" { "amd64" } else { "arm64" }

# Idempotent by design: every image recipe depends on this one, so a host provisioning itself
# unattended creates its own prerequisite instead of following a README.
# create the docker-container buildx builder unless it already exists
image-builder:
    docker buildx inspect {{BUILDER}} > /dev/null 2>&1 \
        || docker buildx create --name {{BUILDER}} --driver docker-container --bootstrap

# target: default (core+agent) | core | agent
# bake the NATIVE arch and --load it into the local image store (docker compose can use it)
image target="default": image-builder
    BUILD_SHA="$(git rev-parse HEAD)" docker buildx bake \
        --builder {{BUILDER}} --set '*.platform={{NATIVE_PLATFORM}}' --load {{target}}

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
    BUILD_SHA="$(git rev-parse HEAD)" docker buildx bake --builder {{BUILDER}} {{target}}

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
