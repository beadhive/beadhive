# ws development tasks. Run `just` to list.
# Operational commands (bd, rigs, labels, dolt) now live in the `ws` CLI.

# list available recipes
default:
    @just --list

# install the toolchain (Homebrew bundle + mise) and dev deps + git hooks
bootstrap:
    brew bundle
    mise install
    uv sync
    just hooks

# run all checks: ruff + markdown + tests (used by the pre-commit hook)
check: lint lint-md test

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

# run fast unit tests (excludes the real-bd integration harness)
test:
    uv run pytest -m "not integration"

# run the real-bd AGF topology harness (needs the bd binary; slower)
test-int:
    uv run pytest -m integration

# run the harness and render each git history (mode=all) or only divergent ones (mode=diff)
# streams live per-bead progress; -v shows which test is running
render-int mode="all":
    AGF_RENDER={{mode}} uv run pytest -m integration -s -v

# demo the ws CLI against the real app (used by `ws work review --demo`); extend per feature
demo:
    uv run ws --help

# build the wheel/sdist
build:
    uv build

# install ws on PATH (~/.local/bin/ws)
install:
    uv tool install --force .

# live OTel verification: start a collector first, then run to export traces+metrics+logs.
# Needs the otel+mcp extras (uv sync --extra otel --extra mcp) and a running OTLP collector.
# Default endpoint: gRPC on localhost:4317 (grafana/otel-lgtm or any OTLP-capable collector).
# HTTP transport: just otel-verify http://localhost:4318 (set WS_OTEL_PROTOCOL=http/protobuf).
# After running, check your collector for service.name=ws spans/metrics/logs.
otel-verify endpoint="http://localhost:4317":
    WS_OTEL_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT={{endpoint}} uv run pytest tests/test_otel_verify.py -v -s
