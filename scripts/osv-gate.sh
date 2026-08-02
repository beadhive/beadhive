#!/usr/bin/env bash
# Run an osv-scanner invocation and apply an enforce|warn mode to its exit code.
#
# Shared by `just license-check` and `just cve-report` so the two gates cannot drift apart in
# how they treat findings — the modes must behave identically; only their DEFAULTS differ.
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
