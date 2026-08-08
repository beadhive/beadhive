#!/bin/sh
# verify-bh-version.sh — did the install actually take?
#
# THE MEASURED FAILURE THIS EXISTS FOR (INSTALL.md:120-126): on macOS with 0.7.1 installed, an
# unforced `uv tool install` printed "Installed 2 executables: bh, bh-mcp", EXITED 0, and
# `bh --version` still said 0.7.1. Exit status said success; the machine had not moved. So this
# script never looks at the installer's exit code — it compares the version string `bh` reports
# against the version the package manager says it installed. A stale binary earlier on PATH, or
# a no-op install, makes those two disagree, and that disagreement is the whole check.
#
# EXIT CODES — three outcomes, not two, because "could not tell" must not read as "fine":
#   0  bh's version matches what the package manager reports installed.
#   1  MISMATCH — the two disagree. A stale `bh` is still first on PATH; re-run with --force.
#   2  INCONCLUSIVE — bh is absent, or no package manager could be queried, so there is nothing
#      to corroborate against. Deliberately NOT 0: an unverifiable install is not a verified one.
#
# Human-readable narration goes to stderr; stdout carries the two version strings so a walk
# records what was actually compared.

set -u

say() { printf '%s\n' "$*" >&2; }

if ! command -v bh >/dev/null 2>&1; then
	say "INCONCLUSIVE: no \`bh\` on PATH."
	say "  If the install just reported success, PATH is the suspect: \`uv tool\` puts binaries"
	say "  in ~/.local/bin. Add it to your shell profile and re-open the shell:"
	say '    export PATH="$HOME/.local/bin:$PATH"'
	exit 2
fi

# `bh --version` prints the bare version (e.g. `0.8.4`). Take the last whitespace-separated
# token of the first line so a future "bh version X" form still parses, and strip a leading `v`.
reported=$(bh --version 2>/dev/null | head -n 1 | tr -d '\r' | awk '{print $NF}' | sed 's/^v//')
bh_path=$(command -v bh)

if [ -z "$reported" ]; then
	say "INCONCLUSIVE: \`bh --version\` printed nothing. ${bh_path} is on PATH but not answering."
	exit 2
fi

# --- what does the package manager say it installed? ------------------------------------------
# Ordered by INSTALL.md's method preference. The managed route ends in `uv tool install --force`
# too, so uv answers for both routes; brew is last for the same reason it is last there.
expected=""
manager=""

if [ -z "$expected" ] && command -v uv >/dev/null 2>&1; then
	# `uv tool list` prints `beadhive v0.8.4` then an indented executables block.
	line=$(uv tool list 2>/dev/null | grep -E '^beadhive[[:space:]]' | head -n 1)
	if [ -n "$line" ]; then
		expected=$(printf '%s' "$line" | awk '{print $2}' | sed 's/^v//')
		manager="uv tool"
	fi
fi

if [ -z "$expected" ] && command -v pipx >/dev/null 2>&1; then
	line=$(pipx list --short 2>/dev/null | grep -E '^beadhive[[:space:]]' | head -n 1)
	if [ -n "$line" ]; then
		expected=$(printf '%s' "$line" | awk '{print $2}' | sed 's/^v//')
		manager="pipx"
	fi
fi

if [ -z "$expected" ] && command -v pip >/dev/null 2>&1; then
	line=$(pip show beadhive 2>/dev/null | grep -E '^Version:' | head -n 1)
	if [ -n "$line" ]; then
		expected=$(printf '%s' "$line" | awk '{print $2}')
		manager="pip"
	fi
fi

if [ -z "$expected" ] && command -v brew >/dev/null 2>&1; then
	line=$(brew list --versions beadhive 2>/dev/null | head -n 1)
	if [ -n "$line" ]; then
		expected=$(printf '%s' "$line" | awk '{print $NF}')
		manager="homebrew"
	fi
fi

if [ -z "$expected" ]; then
	say "INCONCLUSIVE: \`bh\` reports ${reported} from ${bh_path}, but no package manager on this"
	say "  machine claims to have installed \`beadhive\`, so there is nothing to compare against."
	say "  Do not read this as a pass. Ask the human which route was used, then re-run."
	printf 'bh=%s manager=none\n' "$reported"
	exit 2
fi

printf 'bh=%s %s=%s path=%s\n' "$reported" "$manager" "$expected" "$bh_path"

if [ "$reported" != "$expected" ]; then
	say "MISMATCH: ${manager} installed beadhive ${expected}, but \`bh --version\` says ${reported}."
	say "  ${bh_path} is a DIFFERENT, older binary that is earlier on PATH — the exact failure"
	say "  INSTALL.md:120-126 measured. The install exiting 0 did not mean it took."
	say "  Fix: re-run the route command WITH --force, then re-open the shell and re-run this."
	exit 1
fi

say "OK: \`bh --version\` (${reported}) matches what ${manager} installed (${expected})."
exit 0
