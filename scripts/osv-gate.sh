#!/usr/bin/env bash
# Run an osv-scanner invocation and apply an enforce|warn mode to its exit code.
#
# USED BY `just cve-report` ONLY, not `just license-check` (bh-1kvq split them apart —
# osv-license-gate.sh is license-check's gate now, re-deriving a license-only status so a CVE
# finding in the same scan can't block it). NOT in the `just check` / `bh work submit`
# validate_cmd path — `check` never runs `cve-report`, so a network blip here never touches
# submit. This file's docstring said "shared by both" until bh-u9ip; it was stale.
#
# Usage: osv-gate.sh <enforce|warn> <label> <osv-scanner args...>
#
# Exit-code contract (measured: docs/spikes/bh-vf8h.3-osv-gate-mechanics.md):
#     0   clean
#     1   findings — fails under `enforce`, reported-and-passed under `warn`
#   127   osv-scanner could not run the scan it was asked to: a malformed allowlist, or an SBOM
#         filename it refuses to dispatch on. ALWAYS FATAL, in BOTH modes. This is the important
#         one: swallowing 127 under `warn` would print a clean-looking pass for a scan that
#         never examined anything.
#
# NOT GIVEN osv-license-gate.sh's exit-75 network/config split (bh-u9ip), deliberately: since
# this gate is off the submit hot path and `cve_mode` defaults to `warn` (advisory), a network
# blip here already fails soft rather than blocking a review. If `cve_mode` is ever flipped to
# `enforce` by default, or this script is wired into `check`, revisit — the same collapse (a
# broken proxy and a malformed allowlist both landing on exit 127) is present here too; see
# osv-license-gate.sh's header for the fix shape and the measured evidence.
set -uo pipefail

# Deliberately `${1-}` rather than `${1:?}`: an EMPTY mode and a MISPELLED mode are the same
# class of error, so they take the same path and the same exit code. Letting `:?` handle the
# empty case would exit 1 — indistinguishable from "findings were reported".
mode=${1-}
label=${2:?label required}
shift 2

case "$mode" in
  enforce | warn) ;;
  *)
    # Fail loudly rather than defaulting. A typo'd BH_LICENSE_MODE silently becoming `warn`
    # would disable the gate while leaving it looking enabled — the exact failure this exists
    # to prevent.
    echo "osv-gate: invalid mode '$mode' for ${label} (expected: enforce | warn)" >&2
    exit 2
    ;;
esac

# Preflight the binary BEFORE running it. Bash returns 127 for "command not found", the SAME
# code osv-scanner uses for "I could not run the scan you asked for" — so without this, a
# machine that has not run `brew bundle` gets the input-error diagnostic below and goes hunting
# a malformed allowlist that does not exist.
if ! command -v osv-scanner >/dev/null 2>&1; then
  echo "osv-gate: osv-scanner is not installed, so ${label} could not run." >&2
  echo "  Install it with:  brew bundle --file=Brewfile   (or: brew install osv-scanner)" >&2
  exit 127
fi

osv-scanner "$@"
rc=$?

if [ "$rc" -eq 127 ]; then
  echo "osv-gate: ${label} FAILED TO RUN (exit 127) — osv-scanner rejected its input." >&2
  echo "  Usually one of: a non-SPDX identifier in the allowlist, or an SBOM filename it will" >&2
  echo "  not dispatch on (it must be bom.json or *.cdx.json — sbom.json is rejected)." >&2
  echo "  This is a configuration bug, not a policy finding, and is fatal in both modes." >&2
  exit 127
fi

if [ "$rc" -ne 0 ] && [ "$mode" = "warn" ]; then
  echo "osv-gate: ${label} reported findings above — mode=warn, not failing the build." >&2
  exit 0
fi

exit "$rc"
