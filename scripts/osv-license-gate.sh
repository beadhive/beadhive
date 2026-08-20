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
#    75    a NETWORK dependency (deps.dev/osv.dev) was unreachable — RETRYABLE, not a verdict.
#          ALWAYS FATAL, in BOTH modes, same reasoning as 127 below, but callers (`bh work
#          submit`) should treat it as "try again", never as "the policy failed" (bh-u9ip).
#   127    osv-scanner could not run the scan (bad allowlist, bad SBOM filename, ...), or produced
#          no report to inspect. ALWAYS FATAL, in BOTH modes — see scripts/osv-gate.sh's header
#          for why swallowing this under `warn` is the failure mode that must never happen.
#
# WHY 75 AND NOT A NEW GENERIC NUMBER (bh-u9ip): it is sysexits.h's `EX_TEMPFAIL` — "temporary
# failure, indicating something that is not really an error ... user is invited to retry". That
# convention is exactly the distinction this gate needs to make, and reusing a named one avoids
# inventing meaning nothing else in this repo would recognise.
set -uo pipefail

# See the exit-code table above.
readonly _RETRYABLE_EXIT=75

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

# ASSERT the report flag exists rather than assume it (bh-e27ep). This gate reads a file
# osv-scanner writes, so the flag that names that file is load-bearing — and it is NOT stable
# across releases: the script was written against `--output-file`, osv-scanner renamed it to
# `--output`, and from that day every `just check` in this repo died at exit 127 with a message
# blaming the allowlist. Nothing pins the scanner version, so the only defence available is to
# ask the installed binary what it accepts before depending on it, and to fail with the flag
# named.
#
# WHICH command to ask is the hard part, and getting it wrong is how this preflight shipped a
# misdiagnosing 127 of its own. `--output` is a PER-COMMAND flag — measured on osv-scanner 2.3.3,
# `scan`, `scan source` and `scan image` each list it and the top-level help does not — so the
# probe has to name a subcommand. The first cut took *every* leading non-flag arg as the
# subcommand path, which is unsound: this script's documented interface is
# `<osv-scanner args...>`, and osv-scanner's canonical form is `scan source <path>`, so a
# POSITIONAL SCAN TARGET is indistinguishable from a subcommand word by that rule. Feeding one
# back to `--help` gets `No help topic for '.'` — exit 0, and NO flag listing — and an empty
# listing contains no `--output`, so the preflight asserted the exact opposite of the truth and
# killed a working scanner at 127.
#
# So the path is DISCOVERED one word at a time, and a word joins it only on POSITIVE EVIDENCE
# that the installed binary recognises it as a help topic: the reply must look like a flag
# listing. The test is what a real topic HAS (flags), never what an unknown one SAYS, so
# rewording "No help topic" cannot resurrect the bug — and it cannot fail the same way, because
# the listing this preflight judges is always one osv-scanner itself vouched for. Discovery
# stops at the first unrecognised word, which is exactly where the caller's positionals begin.
#
# The help EXIT CODE is deliberately ignored: measured on osv-scanner 2.3.3, `osv-scanner scan
# source --help` prints the full flag listing and then exits 127. Gating on that status would
# make this preflight reject every scanner including the working one — the same assume-don't-
# measure mistake one level up. Only the listing is evidence, so only the listing is read.
subcmd=()
output_flag_seen=0
for arg in "$@"; do
  case "$arg" in -*) break ;; esac
  probe=$(osv-scanner ${subcmd[@]+"${subcmd[@]}"} "$arg" --help 2>&1)
  # A recognised topic prints flags; `No help topic for '.'` prints none. Evidence, not absence.
  grep -qE -- '(^|[[:space:]])--[a-z]' <<<"$probe" || break
  subcmd+=("$arg")
  # A flag may be declared on any level of the path (parent-persistent or leaf-local), so any
  # recognised level that names `--output` is proof enough that the spelling still exists.
  if grep -qE -- '(^|[[:space:]])--output([[:space:],]|$)' <<<"$probe"; then
    output_flag_seen=1
  fi
done

# No leading word was recognised, so there is no per-command listing to read. Asserting from the
# top-level help instead would repeat the very bug above — concluding "the flag is gone" from a
# listing that was never going to mention it. Skipping costs only diagnostic sharpness: an
# actually-missing flag still makes the scan below exit 127, which is fatal in both modes.
if [ ${#subcmd[@]} -gt 0 ] && [ "$output_flag_seen" -eq 0 ]; then
  echo "osv-gate: the installed osv-scanner does not accept '--output', which ${label} needs to" >&2
  echo "  read its report — so this gate cannot run. Toolchain drift, not a policy finding:" >&2
  echo "  osv-scanner renamed '--output-file' to '--output', and it may have renamed it again." >&2
  echo "  Check:  osv-scanner ${subcmd[*]} --help | grep -- --output   (installed: $(osv-scanner --version 2>&1 | head -1))" >&2
  echo "  Fatal in both modes — a gate that cannot read its report must not report CLEAN." >&2
  exit 127
fi

report=$(mktemp)
errfile=$(mktemp)
trap 'rm -f "$report" "$errfile"' EXIT

# stderr is captured, not just inherited, so it can be READ (for the transport-failure probe
# below) as well as shown — then replayed verbatim so a caller watching the terminal sees
# nothing different than before.
osv-scanner "$@" --format json --output "$report" 2>"$errfile"
raw_rc=$?
cat "$errfile" >&2

# A NETWORK failure reaching deps.dev/osv.dev and a malformed-input config bug both come back
# from osv-scanner as the SAME exit code (measured, bh-u9ip: a broken proxy and a non-SPDX
# allowlist entry are both 127) — so telling them apart means reading stderr, there is no
# cheaper signal. The patterns are the shapes actually seen from a live probe against a severed
# connection (`dial tcp ... connection refused`, gRPC's `rpc error: code = Unavailable`) plus
# the rest of Go's net/http transport-failure vocabulary (DNS, timeout, TLS, reset) — this is a
# text probe, not a mapping, and only as durable as osv-scanner's own message strings.
if grep -qE \
  'dial tcp|proxyconnect tcp|rpc error: code = Unavailable|no such host|i/o timeout|TLS handshake timeout|context deadline exceeded|connection reset by peer' \
  "$errfile"
then
  echo "osv-gate: ${label} could not reach its network dependency (deps.dev/osv.dev) — exit" >&2
  echo "  ${raw_rc}, network unreachable (see the osv-scanner message above). This is NOT a" >&2
  echo "  license-policy verdict — the scan never got an answer to judge, so it neither passed" >&2
  echo "  nor failed the allowlist. RETRYABLE: re-run once connectivity recovers. Fatal in both" >&2
  echo "  modes, same reasoning as exit 127 — a gate that could not run must not report CLEAN." >&2
  exit "$_RETRYABLE_EXIT"
fi

if [ "$raw_rc" -eq 127 ]; then
  echo "osv-gate: ${label} FAILED TO RUN (exit 127) — osv-scanner rejected its input." >&2
  echo "  osv-scanner's own message above names the cause; this list is NOT exhaustive, and" >&2
  echo "  reading it as exhaustive is how bh-e27ep cost an hour on the allowlist while the" >&2
  echo "  real cause was a renamed flag. Known shapes: a non-SPDX identifier in the allowlist;" >&2
  echo "  an SBOM filename it will not dispatch on (bom.json or *.cdx.json — sbom.json is" >&2
  echo "  rejected); or an argument this script passes that the installed osv-scanner no longer" >&2
  echo "  defines ('flag provided but not defined')." >&2
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

# POSITIVE PROOF that license analysis actually ran, BEFORE trusting a zero-violation answer
# (bh-ymvn). Every query below leans on `// []` and `?` so a missing field degrades to "empty"
# — which means "no violations" and "the field this gate reads was renamed or moved" are the
# SAME OBSERVATION. Without this check the gate reports clean while enforcing nothing, and it
# reports clean LOUDLY, in the voice of a gate that ran.
#
# `license_summary` is osv-scanner's own per-license tally, emitted whenever `--licenses` is
# honoured. MEASURED on osv-scanner 2.4.0 against this repo: 12 entries on a clean scan, present
# on a violating scan too. Absent or empty means the scan did no license analysis (no
# `--licenses` reached it) or the schema moved. Either way this gate can no longer answer the
# question it was asked, and "cannot answer" is 127 — never 0. Same reasoning as the empty-report
# and unparseable-report branches above; this is the third way a scan can be uninspectable.
if ! license_summary_count=$(jq '[.license_summary[]?] | length' "$report" 2>&1); then
  echo "osv-gate: ${label} produced a report jq could not parse — treating this as a scan" >&2
  echo "  failure, not a clean pass. Fatal in both modes. jq said: ${license_summary_count}" >&2
  exit 127
fi
if [ "${license_summary_count:-0}" -eq 0 ]; then
  echo "osv-gate: ${label} — the report carries no 'license_summary', so either the scan did no" >&2
  echo "  license analysis or osv-scanner's schema changed. Refusing to report CLEAN from a scan" >&2
  echo "  whose license findings this gate can no longer locate. Fatal in both modes." >&2
  echo "  Re-derive the queries against a live report:  osv-scanner ... --licenses=... \\" >&2
  echo "      --format json --output /tmp/r.json && jq 'keys' /tmp/r.json" >&2
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
