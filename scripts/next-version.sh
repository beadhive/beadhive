#!/usr/bin/env bash
# THE ONE next-version lookup: what `cz bump` WOULD create, asked without creating it.
#
# TWO CALLERS, ONE PATH, ON PURPOSE (bh-k5te9). `just bump-preview` is the offline, instant
# answer; `bh release preview --next` is that PLUS the network checks (tag on the remote, version
# on PyPI), so the two are a SUPERSET, not siblings. Both run this script rather than each
# spelling `cz bump --dry-run` for itself — two paths to the same number is how they drift.
#
# NEVER COMPUTED, ALWAYS ASKED. commitizen owns the increment and this repo sets
# `major_version_zero = true`, so a `feat` bumps MINOR (0.11.5 → 0.12.0) — exactly what a
# hand-rolled semver guess gets wrong. cz's own output is passed through unchanged, so a human
# reads the increment reasoning and a caller that wants only the number greps the
# `bump: version X → Y` line.
#
# READ-ONLY: `--dry-run` writes no version, no changelog, no tag, and pushes nothing.
#
# EXIT CODE IS cz's OWN, and non-zero includes the ordinary "no commits found to bump" case.
# Callers read any failure as "could not determine the next version" — never as a guess and
# never as a refusal.
set -uo pipefail

# The checkout is the script's own parent, not the cwd — same reason as release-pin.sh: `just`
# already runs from the justfile directory, and deriving it keeps the script correct when it is
# run by hand, or by a `bh` invoked from somewhere else entirely.
cd "$(dirname "$0")/.." || exit 2

exec uv run cz bump --dry-run
