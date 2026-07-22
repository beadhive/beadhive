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

# preview the next version bump from conventional commits (no writes)
bump-dry:
    uv run cz bump --dry-run

# bump version (pyproject.toml), tag, and commit; then sync uv.lock (cz bump doesn't touch it,
# so a separate follow-up commit — not an amend, which would orphan cz's signed tag)
bump:
    uv run cz bump
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
