# Spike `bh-vf8h.3` — `Can license.override + effectiveUntil be operated, and can the CVE signal stay non-blocking independently?`

**Bead:** `bh-vf8h.3` · **Seat:** `dev/osv-probe` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-vf8h.4` — adopt osv-scanner v2 over uv-exported CycloneDX for the wheel, or fall back

## Question

Two halves, both required for a GO:

- **(a) Exception mechanics.** Does `[[PackageOverrides]]` with `license.override` clear the
  `caio` UNKNOWN? Does `effectiveUntil` behave as a real expiry, and what happens on the day it
  lapses — hard failure, warning, or silence?
- **(b) Separability.** Can a license violation and a CVE finding be distinguished such that one
  can fail a build and the other cannot? `bh-okux`'s central constraint is that license policy
  BLOCKS while the CVE feed is a non-blocking signal; its design predicts that conflating them
  gets the whole gate switched off within a month.

## Method

`osv-scanner 2.4.0` against the isolated `bom.json` (79 components). Allowlist under test:
`MIT,Apache-2.0,BSD-3-Clause,BSD-2-Clause,ISC,PSF-2.0,Unlicense` — 8 violations baseline, per
`bh-vf8h.2`.

Exit codes captured with `>/dev/null 2>&1; echo $?`. Override configs written to scratch and
passed with `--config`; **nothing was committed to the repo root**. `effectiveUntil` tested at
both a future (`2099-01-01`) and a lapsed (`2020-01-01`) date, holding everything else constant.

## Evidence

1. **Exit codes:**
   ```
   license violations present                    -> exit=1
   CVE scan only, no vulnerabilities             -> exit=0
   license SUMMARY only (bare --licenses)        -> exit=0
   allowlist containing a non-SPDX identifier    -> exit=127
   clean gate (all violations dispositioned)     -> exit=0
   ```

2. **`exit=127` is a malformed-input error, not a scan result:**
   ```
   --licenses requires comma-separated spdx licenses.
   The following license(s) are not recognized as spdx: non-standard
   ```
   A typo'd or bucket-name allowlist fails loudly rather than silently passing. Note it shares
   its exit code with the "invalid SBOM filename" failure from `bh-vf8h.1`.

3. **`license.override` WORKS.** With:
   ```toml
   [[PackageOverrides]]
   name = "caio"
   ecosystem = "PyPI"
   license.override = ["Apache-2.0"]
   reason = "Ships Apache-2.0 COPYING in the wheel; declares no License field or classifier"
   ```
   the violation list drops from 8 to 7 — `caio@0.9.25` is gone, every other entry unchanged.
   `version` omitted matches every version, as documented.

4. **`effectiveUntil` with a FUTURE date — override still applies.** `effectiveUntil = 2099-01-01`:
   caio violations = 0.

5. **`effectiveUntil` with a LAPSED date — EXPIRY IS ENFORCED.** `effectiveUntil = 2020-01-01`:
   caio violations = 1. The override stops applying and the package reverts to violating.

6. **The lapse is SILENT.** Grepping the lapsed run's full output for `expir|effective|warn`
   returned only the ordinary violation table row for caio — no warning, no notice that a
   configured override had expired. The package simply reappears as a violation as though the
   override had never been written.

7. **A fully clean, enforceable gate is reachable.** With `MPL-2.0` added to the allowlist
   (disposition 1 from `bh-pc2a.21`) and 7 overrides covering `caio`, `pywin32`, and the 5
   `non-standard` packages: `remaining violations: 0`, `exit=0`.

8. **Config does NOT propagate to child directories — CONFIRMED.** An `osv-scanner.toml` in a
   parent directory was not picked up when scanning an SBOM in a child directory: caio still
   violating = 1. `--config <path>` is required to apply one config across a tree.

9. **Separability holds structurally, via two invocations:**
   ```
   license gate (blocking)      -> exit=0
   CVE report (never gated on)  -> exit=0
   ```
   The two concerns are separate invocations of the same binary with independent exit codes;
   CI gates on the first and merely reports the second.

## Verdict — **GO**

Both halves clear. `license.override` works exactly as documented, `effectiveUntil` is a real
enforced expiry rather than decoration, and a clean `exit=0` gate is demonstrably reachable on
this tree. Separability is achieved by running the license gate and the CVE report as two
invocations with independent exit codes — no output parsing required.

**One limitation this spike could NOT settle, stated rather than glossed:** because the tree has
zero known vulnerabilities, it was never observed whether a *CVE finding* also produces
`exit=1`. The separability recommendation therefore does not depend on it — the CVE invocation
is simply never gated on, whatever it returns — but anyone wiring this must not assume
`exit=0` means "no CVEs found". That assumption is untested here.

**The finding that most affects the design is Evidence 6.** `effectiveUntil` was identified in
`bh-pc2a.21`'s notes as "the single most valuable field in the schema" precisely because it
stops exceptions rotting into permanent invisible policy. It does expire — but it expires
*silently*, converting a deliberate, documented exception into an ordinary-looking violation
with no indication that a policy decision just lapsed. The failure mode is the reverse of
dangerous (it fails closed, blocking rather than permitting) but it is confusing: the gate goes
red with no hint that the cause is an expired override rather than a new dependency problem.

## Recommendation

1. **Use two invocations, not one.** License gate: `--licenses="<allowlist>" --config <path>`,
   gate CI on exit 1. CVE report: bare scan, report only, never gate. This satisfies
   `bh-okux`'s blocking/non-blocking split with no output parsing.
2. **Own the expiry-lapse UX.** Since lapse is silent (Evidence 6), the implementation molecule
   should add a cheap check that reads the config's `effectiveUntil` dates and warns *before*
   they lapse. Roughly ten lines; without it, an expiring exception presents as a mystery
   red gate.
3. **Always pass `--config` explicitly.** Do not rely on implicit discovery (Evidence 8),
   especially if the gate ever runs across more than one hive.
4. **Treat `exit=127` as a config bug, not a policy failure.** It covers both a malformed
   allowlist and a bad SBOM filename; CI should distinguish it from `exit=1` so a typo doesn't
   read as a license violation.
5. **Do not commit `osv-scanner.toml` to the repo root yet** — that is implementation work for
   the molecule, gated on the `bh-vf8h.4` verdict and on verifying `uncalled-for`'s license
   (see `bh-vf8h.2` recommendation 3). The configs used here were throwaway and live only in
   this spike's scratch.
