#!/bin/sh
# check-mcp-wired.sh — is the bh MCP server registered with the harness?
#
# Step 060's probe AND its verify. Probing FIRST is the point: `bh mcp install` shells out to
# `claude mcp add bh --scope user`, and INSTALL.md's `configure[]` block may already have run it
# before this Guide started. Re-running an already-satisfied wiring is noise at best.
#
# EXIT CODES — three, because "no Claude on this machine" is not the same as "not wired":
#   0  the `bh` MCP entry is registered.
#   1  `claude` is present but no `bh` entry — this step has work to do.
#   3  NOT APPLICABLE: no `claude` CLI on PATH. MCP wiring is Claude Code specific, and on
#      another harness this step is skipped, not failed. 3 rather than 0 so the walk records a
#      SKIP and not a false green — the step's `on_failure` routes 3 to a clean skip.
#
# `claude mcp list` is read-only. It is the only wiring probe available: the registry it writes
# is Claude's, not bh's, so there is nothing of ours to inspect instead.

set -u

if ! command -v claude >/dev/null 2>&1; then
	printf 'NOT-APPLICABLE: no `claude` CLI on PATH.\n' >&2
	printf '  The bh MCP server is wired into Claude Code specifically. On another harness this\n' >&2
	printf '  step is skipped: OpenCode is furnished through `bh hive onboard --opencode` at step\n' >&2
	printf '  080 instead, and nothing later in this Guide depends on MCP being wired.\n' >&2
	exit 3
fi

out=$(claude mcp list 2>/dev/null)

# TWO registration shapes count as wired, and only matching one of them is how this probe
# produces a false "missing" on a machine that is already done:
#   `bh: …`               — the user-scope entry `bh mcp install` adds.
#   `plugin:bh:bh: …`     — the SAME server supplied by the bh Claude plugin (step 065), which
#                           Claude namespaces as `plugin:<plugin>:<server>`.
# docs/ONBOARDING.md:212's `grep -q '^bh '` only sees the first, and reports a plugin-wired
# machine as unwired. Measured against `claude mcp list` output, 2026-08-08.
if printf '%s\n' "$out" | grep -qE '^(plugin:[^:]+:)?bh[[:space:]:]'; then
	printf '%s\n' "$out" | grep -E '^(plugin:[^:]+:)?bh[[:space:]:]'
	printf 'OK: the `bh` MCP server is already registered — nothing to do.\n' >&2
	exit 0
fi

printf 'MISSING: `claude mcp list` has no `bh` entry.\n' >&2
printf '  Wire it with `bh mcp install` (which runs: claude mcp add bh --scope user -- bh mcp serve).\n' >&2
exit 1
