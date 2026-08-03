#!/usr/bin/env bash
# Run an osv-scanner license invocation and apply an enforce|warn mode to LICENSE findings
# ONLY, ignoring any vulnerability findings the same scan surfaces.
#
# WHY THIS SCRIPT EXISTS (bh-1kvq): `osv-scanner scan source` conflates two finding types into
# one exit code — a license violation and a CVE finding both produce exit=1, and no documented
# flag (`osv-scanner scan source --help`) narrows the exit code to one or the other; `--licenses`
# only ADDS license reporting, it does not remove vulnerability scanning. `scripts/osv-gate.sh`
# applies its mode correctly to whatever exit code it is handed, so feeding it that conflated
# code made a CVE finding block `license-check` — the only way past it was
# `BH_LICENSE_MODE=warn`, which also disabled license enforcement. This script re-derives a
# LICENSE-only status by scanning with `--format json` and inspecting each package's
# `license_violations` field (non-empty array iff that package violates the allowlist — verified
# empirically against a live scan, not documented in `--help`). Vulnerabilities the same scan
# finds are left entirely to `just cve-report`, which runs its own unfiltered
# `scripts/osv-gate.sh` invocation against `cve_mode` — this script only changes what COUNTS as
# a finding for the license gate, not where CVEs get reported.
#
# Usage: osv-license-gate.sh <enforce|warn> <label> <osv-scanner args...>
#
# Mirrors scripts/osv-gate.sh's enforce|warn/127/invalid-mode contract
# (docs/spikes/bh-vf8h.3-osv-gate-mechanics.md) so the two gates feel identical to a caller —
# only what counts as a finding differs:
#     0    clean — no license violations. Vulnerabilities, if any, are reported but not gated.
#     1    license violation(s) present — fails under `enforce`, reported-and-passed under `warn`
#   127    osv-scanner could not run the scan (bad allowlist, bad SBOM filename, ...), or produced
#          no report to inspect. ALWAYS FATAL, in BOTH modes — see scripts/osv-gate.sh's header
#          for why swallowing this under `warn` is the failure mode that must never happen.
set -uo pipefail

# Same reasoning as scripts/osv-gate.sh: an EMPTY mode and a MISPELLED mode must take the same
# path, so `${1-}` rather than `${1:?}`.
mode=${1-}
label=${2:?label required}
shift 2

case "$mode" in
  enforce | warn) ;;
  *)
    echo "osv-gate: invalid mode '$mode' for ${label} (expected: enforce | warn)" >&2
    exit 2
    ;;
esac

# Preflight both binaries BEFORE scanning — same rationale as scripts/osv-gate.sh: a missing
# tool must be reported as "not installed", not misread as a malformed-input or parse failure.
if ! command -v osv-scanner >/dev/null 2>&1; then
  echo "osv-gate: osv-scanner is not installed, so ${label} could not run." >&2
  echo "  Install it with:  brew bundle --file=Brewfile   (or: brew install osv-scanner)" >&2
  exit 127
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "osv-gate: jq is not installed, so ${label} could not filter its report." >&2
  exit 127
fi

report=$(mktemp)
trap 'rm -f "$report"' EXIT

osv-scanner "$@" --format json --output-file "$report"
raw_rc=$?

if [ "$raw_rc" -eq 127 ]; then
  echo "osv-gate: ${label} FAILED TO RUN (exit 127) — osv-scanner rejected its input." >&2
  echo "  Usually one of: a non-SPDX identifier in the allowlist, or an SBOM filename it will" >&2
  echo "  not dispatch on (it must be bom.json or *.cdx.json — sbom.json is rejected)." >&2
  echo "  This is a configuration bug, not a policy finding, and is fatal in both modes." >&2
  exit 127
fi

# No report to inspect (e.g. osv-scanner exited on a signal, or wrote nothing) — treat this as a
# scan failure rather than risk reading a filtered status of zero findings out of an empty file.
if ! [ -s "$report" ]; then
  echo "osv-gate: ${label} produced no report to inspect (raw exit ${raw_rc}) — treating this" >&2
  echo "  as a scan failure, not a clean pass. Fatal in both modes." >&2
  exit 127
fi

if ! violations=$(jq -c '[.results[]?.packages[]? | select((.license_violations // []) | length > 0)]' "$report" 2>&1); then
  echo "osv-gate: ${label} produced a report jq could not parse — treating this as a scan" >&2
  echo "  failure, not a clean pass. Fatal in both modes. jq said: ${violations}" >&2
  exit 127
fi
violation_count=$(jq 'length' <<<"$violations")
vuln_count=$(jq '[.results[]?.packages[]? | select((.vulnerabilities // []) | length > 0)] | length' "$report")

if [ "$violation_count" -gt 0 ]; then
  echo "osv-gate: ${label} — ${violation_count} package(s) with license violations:" >&2
  jq -r '.[] | "  - " + .package.name + "@" + .package.version + ": " + (.license_violations | join(", "))' <<<"$violations" >&2
fi

if [ "$vuln_count" -gt 0 ]; then
  echo "osv-gate: ${label} — ${vuln_count} vulnerability finding(s) also present in this scan," >&2
  echo "  not gated here by design (bh-1kvq) — see: just cve-report" >&2
fi

if [ "$violation_count" -gt 0 ]; then
  rc=1
else
  rc=0
fi

if [ "$rc" -ne 0 ] && [ "$mode" = "warn" ]; then
  echo "osv-gate: ${label} reported license findings above — mode=warn, not failing the build." >&2
  exit 0
fi

exit "$rc"
