#!/bin/sh
# preflight.sh — read-only probe of this machine, emitted as one JSON object on stdout.
#
# CONTRACT (step 010, bh-0olv9.4):
#   * Mutates NOTHING. No file is written, no cache is refreshed, no config is scaffolded.
#     In particular it does NOT call `bh setup check`, which writes ~/.beadhive/setup-state.json
#     — probing is not allowed to change the thing being probed.
#   * Writes the JSON to STDOUT only. Every human-readable note goes to stderr, so the walk can
#     capture stdout verbatim as machine state (`verify.output_schema: json`).
#   * Always exits 0. "nix is absent" is an ANSWER, not a failure; a probe that exits nonzero
#     for a normal machine turns step 010 into a gate it was never meant to be.
#
# Every later step reads this instead of re-probing. Two probe implementations is how the Guide
# and `bh setup check` start disagreeing about the same machine; bh-0olv9.7 collapses them into
# one. Until it lands, this is the Guide's single source and nothing downstream calls `command
# -v` again.
#
# POSIX sh on purpose: it runs BEFORE `bh` exists, on a machine whose shell we do not choose.

set -u

# --- JSON emission ---------------------------------------------------------------------------
# Hand-rolled because this runs before any dependency exists — no jq, no python guaranteed.
# Values are tool version strings and paths; `esc` covers the two characters JSON forbids raw
# (backslash, double quote) and drops control characters rather than trying to \u-escape them.
esc() {
	printf '%s' "$1" | tr -d '\000-\037' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

jstr() { printf '"%s"' "$(esc "$1")"; }

# A tool's version string, or the JSON literal null when the binary is not on PATH.
# stderr is discarded: `bd --version` on a broken install must read as "present, version
# unknown", not spray diagnostics into the JSON consumer's terminal.
version_of() {
	binary="$1"
	shift
	if ! command -v "$binary" >/dev/null 2>&1; then
		printf 'null'
		return 0
	fi
	out=$("$@" 2>/dev/null | head -n 1)
	if [ -z "$out" ]; then
		# On PATH but silent — still present. Say so rather than reporting it missing.
		jstr "present"
		return 0
	fi
	jstr "$out"
}

present() {
	if command -v "$1" >/dev/null 2>&1; then printf 'true'; else printf 'false'; fi
}

exists() {
	if [ -e "$1" ]; then printf 'true'; else printf 'false'; fi
}

# --- platform --------------------------------------------------------------------------------
uname_s=$(uname -s 2>/dev/null || printf 'unknown')
uname_m=$(uname -m 2>/dev/null || printf 'unknown')

case "$uname_s" in
Darwin) os="macos" ;;
Linux) os="linux" ;;
*) os="other" ;;
esac

case "$uname_m" in
arm64 | aarch64) arch="arm64" ;;
x86_64 | amd64) arch="x86_64" ;;
*) arch="$uname_m" ;;
esac

# --- can the managed route run here at all? ---------------------------------------------------
# Two separable facts, deliberately not collapsed into one boolean:
#   supported  — does a managed path EXIST for this OS/arch? Intel macOS is gone from nixpkgs,
#                so there is none, and step 020 must FORCE PyPI rather than offer a fork it
#                cannot honour.
#   nix        — is nix installed RIGHT NOW? Absent is recoverable: the human can install it.
#                This Guide never does (ADR Decision 3).
managed_supported=true
managed_blocked=null
if [ "$os" = "macos" ] && [ "$arch" != "arm64" ]; then
	managed_supported=false
	managed_blocked=$(jstr "Intel macOS: nixpkgs dropped darwin-x86_64, so there is no managed path on this machine at all. PyPI is the route here, and that is not a downgrade — it is the only one.")
elif [ "$os" = "other" ]; then
	managed_supported=false
	managed_blocked=$(jstr "the managed path targets macOS and Linux only; this machine reports neither.")
fi

# --- harness ----------------------------------------------------------------------------------
# Steps 060 and 065 are Claude Code specific and must skip cleanly elsewhere. `claude` on PATH
# is the only signal available before anything is installed; "unknown" is an honest answer and
# the steps treat it as not-Claude rather than guessing.
if command -v claude >/dev/null 2>&1; then
	harness="claude-code"
elif command -v opencode >/dev/null 2>&1; then
	harness="opencode"
else
	harness="unknown"
fi

beadhive_dir="${HOME}/.beadhive"

cat <<JSON
{
  "schema": "beadhive-setup-preflight/1",
  "platform": {
    "os": $(jstr "$os"),
    "arch": $(jstr "$arch"),
    "uname_s": $(jstr "$uname_s"),
    "uname_m": $(jstr "$uname_m")
  },
  "managed_route": {
    "supported": ${managed_supported},
    "blocked_reason": ${managed_blocked},
    "nix_present": $(present nix)
  },
  "package_managers": {
    "uv": $(present uv),
    "pipx": $(present pipx),
    "pip": $(present pip),
    "brew": $(present brew),
    "nix": $(present nix)
  },
  "tools": {
    "bh": $(version_of bh bh --version),
    "bd": $(version_of bd bd --version),
    "dolt": $(version_of dolt dolt version),
    "gh": $(version_of gh gh --version),
    "git-workspace": $(version_of git-workspace git-workspace --version),
    "git": $(version_of git git --version),
    "nix": $(version_of nix nix --version),
    "claude": $(version_of claude claude --version)
  },
  "harness": $(jstr "$harness"),
  "config": {
    "beadhive_dir": $(exists "$beadhive_dir"),
    "config_yaml": $(exists "${beadhive_dir}/config.yaml"),
    "setup_state": $(exists "${beadhive_dir}/setup-state.json"),
    "hq": $(exists "${beadhive_dir}/hq")
  }
}
JSON

exit 0
