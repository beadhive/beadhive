#!/bin/sh
# check-hq-remote.sh — is Factory HQ on rung 2, with BOTH halves published?
#
# Used twice: as step 090's verify, and as step 092's PREREQUISITE gate. Rung 4 hard-requires
# rung 2 (docs/ADOPTION.md) because a second machine joins by CLONING HQ, and you cannot clone
# an HQ that exists only on one laptop. Checking that here, before the role choice, is what
# stops the user meeting the requirement as a provisioning failure halfway through setting up
# the new machine — the lesson docs/ONBOARDING.md's "Adding a second machine" opens with.
#
# WHY IT PARSES BOTH LINES. `bh hq status` reports the git half and the Dolt half separately,
# and a green git half over an unpushed Dolt half is the failure that looks like success: the
# fleet config published, the bead state did not, and the new host clones an HQ with no work in
# it. So both lines are extracted and echoed, and only the both-halves-current marker passes.
#
# EXIT CODES:
#   0  remote wired AND both halves current.
#   1  no remote configured — this is rung 1. `bh hq init --create` then `bh hq push`.
#   3  remote wired but at least one half is not current — run `bh hq push`.
#   2  `bh` is absent or `bh hq status` could not be read at all.
#
# `bh hq status` is read-only (it fetches; it does not push).

set -u

if ! command -v bh >/dev/null 2>&1; then
	printf 'INCONCLUSIVE: no `bh` on PATH — cannot read HQ status.\n' >&2
	exit 2
fi

# stdout only. `bh`'s stderr can carry OTEL exporter noise that has nothing to do with HQ, and
# folding it into the captured status would put JSON log lines into this script's own stdout.
out=$(bh hq status 2>/dev/null) || {
	printf 'INCONCLUSIVE: `bh hq status` failed. Is HQ initialised (`bh hq init`)?\n' >&2
	exit 2
}

printf '%s\n' "$out"

if printf '%s\n' "$out" | grep -q 'has no remote configured'; then
	printf 'RUNG 1: HQ has no remote.\n' >&2
	printf '  That is the deliberate rung-1 posture, not a broken install — but it is also the\n' >&2
	printf '  one hard prerequisite for rung 4: a second host joins by cloning HQ.\n' >&2
	printf '  Wire it with `bh hq init --create`, then `bh hq push`.\n' >&2
	exit 1
fi

git_line=$(printf '%s\n' "$out" | grep -E '^[[:space:]]*git:' | head -n 1)
dolt_line=$(printf '%s\n' "$out" | grep -E '^[[:space:]]*dolt:' | head -n 1)

if [ -z "$git_line" ] || [ -z "$dolt_line" ]; then
	printf 'INCONCLUSIVE: `bh hq status` did not report both halves.\n' >&2
	printf '  Expected a `git:` line AND a `dolt:` line; got neither or only one.\n' >&2
	exit 2
fi

printf 'git half:  %s\n' "$git_line" >&2
printf 'dolt half: %s\n' "$dolt_line" >&2

if printf '%s\n' "$out" | grep -q 'up to date with its remote'; then
	printf 'RUNG 2: HQ has a remote and BOTH halves are current.\n' >&2
	exit 0
fi

printf 'RUNG 2, UNPUBLISHED: a remote is wired but at least one half is behind or ahead.\n' >&2
printf '  Run `bh hq push` — it publishes the git half AND the Dolt half — then re-run this.\n' >&2
printf '  Do not treat a wired remote as a published one: an unpushed Dolt half clones empty.\n' >&2
exit 3
