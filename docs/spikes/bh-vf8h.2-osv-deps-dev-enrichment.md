# Spike `bh-vf8h.2` — deps.dev license enrichment over the real tree

**Bead:** `bh-vf8h.2` · **Seat:** `dev/osv-probe` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-vf8h.4` — adopt osv-scanner over uv CycloneDX, or fall back

## Question

osv-scanner sources license data from the deps.dev API rather than from package metadata. Two
named predictions were recorded in `bh-pc2a.21`'s notes from a dist-info dry run; predicted is
not measured. Does deps.dev enrichment actually resolve this repo's real runtime tree, and do
the two predictions hold?

- `certifi` returns `MPL-2.0`
- `caio` returns `UNKNOWN`

Also measuring what the ad-hoc dist-info script could not: total resolve rate, and whether
returned identifiers are already SPDX-normalized — the question that decides whether the
"17 spellings" concern carried in `bh-okux` is real or an artifact.

## Method

`osv-scanner 2.4.0`, license scan over the isolated `bom.json` produced by
`uv export --format cyclonedx1.5 --no-dev` (79 components, ingest proven in `bh-vf8h.1`).

Per-package licenses are **not** exposed by a bare `--licenses` run — that emits only a
`license_summary` array of `{name, count}` objects, and `results` comes back empty when there
are no violations. To obtain per-package detail, the scan must be run **with an allowlist**, so
that non-conforming packages are reported as violations in `results[].packages[]`:

```bash
osv-scanner scan source -L bom.json \
  --licenses="MIT,Apache-2.0,BSD-3-Clause,BSD-2-Clause,ISC,PSF-2.0,Unlicense" \
  --format json | jq -r '.results[]?.packages[]? | "\(.package.name)@\(.package.version)\t\(.license_violations|join("|"))"'
```

Baseline for comparison: the 2026-08-02 dist-info dry run over the same 79 components — 31 MIT,
12 BSD-3-Clause, 9 Apache-2.0, 7 "MIT License", 2 ISC, 1 MPL-2.0 (certifi), 1 UNKNOWN (caio),
remainder single-count variants; zero GPL/AGPL/LGPL/SSPL/EUPL/CDDL/EPL; 72 of 79 verified.

## Evidence

1. **Full distribution returned from deps.dev over all 79 components:**

   ```text
   MIT                          39      Apache-2.0 OR BSD-2-Clause    1
   BSD-3-Clause                 14      Apache-2.0 OR BSD-3-Clause    1
   Apache-2.0                   10      Apache-2.0 OR MIT             1
   non-standard                  5      BSD-2-Clause                  1
   ISC                           2      MPL-2.0                       1
   UNKNOWN                       2      PSF-2.0                       1
                                        Unlicense                     1
   ```

   Sums to 79. **Resolve rate: 72/79 (91%)** into real SPDX identifiers; 5 `non-standard`,
   2 `UNKNOWN`.

2. **PREDICTION 1 CONFIRMED — `certifi@2026.6.17` returns `MPL-2.0`.** Exactly one MPL-2.0
   entry in the tree, and it is certifi.

3. **PREDICTION 2 CONFIRMED — `caio@0.9.25` returns `UNKNOWN`.** deps.dev cannot resolve it,
   exactly as the dist-info reading predicted (ships an Apache-2.0 `COPYING` but declares no
   `License` field and no classifier).

4. **The 8 packages failing a permissive allowlist, named:**

   ```text
   beartype@0.22.9        non-standard
   caio@0.9.25            UNKNOWN
   certifi@2026.6.17      MPL-2.0
   colorama@0.4.6         non-standard
   pyperclip@1.11.0       non-standard
   pywin32@312            UNKNOWN
   shellingham@1.5.4      non-standard
   uncalled-for@0.3.2     non-standard
   ```

5. **`pywin32@312` is a SECOND `UNKNOWN`, not predicted.** The dist-info dry run could not have
   found it — pywin32 is Windows-only and was one of the 7 components that never installed
   locally. It surfaces here only because uv's export is a universal resolution.

6. **`non-standard` is a real, unpredicted category — 5 packages.** These are not unlicensed;
   they are packages whose declared license string deps.dev could not map to an SPDX
   identifier. All five are well-known permissive packages (beartype MIT, colorama
   BSD-3-Clause, pyperclip BSD-3-Clause, shellingham ISC, uncalled-for unverified).

7. **`non-standard` is NOT accepted in an allowlist; `UNKNOWN` IS.** Passing both:

   ```text
   --licenses requires comma-separated spdx licenses.
   The following license(s) are not recognized as spdx: non-standard
   ```

   Only `non-standard` was rejected — `UNKNOWN` passed validation silently.

8. **Normalization confirmed on the resolved portion.** The dist-info run's split of
   `MIT` (31) + `MIT License` (7) = 38 appears here as a single `MIT` bucket of 39; likewise
   `ISC`/`ISC License` collapse to `ISC`. deps.dev does return normalized SPDX for everything it
   resolves.

## Verdict — **GO**

deps.dev enrichment resolves the tree well enough to gate on: 91% into clean SPDX identifiers,
both named predictions confirmed, and the residue is small, enumerable, and individually
dispositionable via the override mechanism proven in `bh-vf8h.3`.

The GO is qualified by one finding that **partially reverses a conclusion recorded in
`bh-okux`**. That design field currently states the normalization concern was "an artifact of
the measurement" and that sourcing from deps.dev means "the whack-a-mole does not exist". That
is **half right**: normalization is genuinely solved for the 72 components deps.dev resolves
(Evidence 8), but 5 more are swept into a `non-standard` bucket that hides their real licenses
rather than mapping them (Evidence 6) — and that bucket **cannot be allowlisted** (Evidence 7),
so each one needs an explicit override. The work did not disappear; it changed shape, from
normalizing 17 spellings to dispositioning 7 packages.

## Recommendation

1. **Amend `bh-okux`'s normalization paragraph.** Its current "the whack-a-mole does not exist"
   is too strong. Replace with: deps.dev normalizes what it resolves (72/79); the remainder
   requires per-package overrides, which is a smaller and more honest job than hand-rolling
   normalization, but it is not zero.
2. **The policy needs 7 overrides at minimum**, not the 1 (`caio`) anticipated in
   `bh-pc2a.21`'s disposition. Add `pywin32` and the 5 `non-standard` packages.
3. **`uncalled-for@0.3.2` licence is UNVERIFIED.** The other six have licenses known from
   reading or common knowledge; this one was assumed MIT as a placeholder in the probe config
   and must be verified by reading the package before any override is committed. Flagged rather
   than quietly asserted.
4. **Never allowlist `UNKNOWN`.** Evidence 7 shows it passes SPDX validation, which makes
   `--licenses="...,UNKNOWN"` a silent, plausible-looking way to permit every unlicensed
   package in the tree. Worth an explicit note in the policy doc, since the failure is invisible.
5. **Gap #3 in `bh-okux` is closed** for this path — Evidence 5 shows platform-conditional
   components are present and enriched. See `bh-vf8h.1` recommendation 2.
