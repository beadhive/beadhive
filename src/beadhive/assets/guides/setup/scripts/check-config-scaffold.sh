#!/bin/sh
# check-config-scaffold.sh — is ~/.beadhive scaffolded?
#
# Used as step 050's verify AND as its BEFORE-probe: `bh config init` may already have run,
# because INSTALL.md's `configure[]` block runs it before this Guide is ever invoked. Already
# scaffolded is the NORMAL case here, so this script's job is to say "already satisfied" out
# loud rather than let a step re-run and report a mutation that did not happen.
#
# EXIT CODES:
#   0  ~/.beadhive/config.yaml exists and is non-empty — scaffolded.
#   1  not scaffolded (or the file is empty, which is a half-write, not a scaffold).
#
# BH_HOME is honoured because the test suite and containerised installs relocate the root; a
# check that only ever looks at $HOME reports a false negative there.

set -u

root="${BH_HOME:-${HOME}/.beadhive}"
cfg="${root}/config.yaml"

if [ ! -d "$root" ]; then
	printf 'ABSENT: %s does not exist — nothing has been scaffolded yet.\n' "$root" >&2
	exit 1
fi

if [ ! -f "$cfg" ]; then
	printf 'PARTIAL: %s exists but %s does not.\n' "$root" "$cfg" >&2
	printf '  `bh config init` is idempotent — running it now completes the scaffold.\n' >&2
	exit 1
fi

if [ ! -s "$cfg" ]; then
	printf 'EMPTY: %s exists but has no content — a half-written scaffold, not a done one.\n' "$cfg" >&2
	exit 1
fi

printf '%s\n' "$cfg"
printf 'OK: %s is scaffolded (config.yaml present, non-empty).\n' "$root" >&2
exit 0
