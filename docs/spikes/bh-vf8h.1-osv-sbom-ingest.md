# Spike `bh-vf8h.1` — SBOM ingest vs directory re-scan

**Bead:** `bh-vf8h.1` · **Seat:** `dev/osv-probe` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-vf8h.4` — adopt osv-scanner over uv CycloneDX, or fall back

## Question

Does `osv-scanner` genuinely parse and use a `uv export --format cyclonedx1.5` document as its
input, or does it ignore the document and walk the directory to find `uv.lock` itself?

This matters beyond tidiness. If it re-scans, the SBOM is decorative in the pipeline and the
gate's real input is the lockfile — which changes what has to be produced, stored, and
attested, and means the published document and the gated document are different artifacts that
can drift apart.

Critically **not** asking: whether the licenses returned are correct (that is `bh-vf8h.2`), or
whether the gate can be operated (that is `bh-vf8h.3`).

## Method

Tooling: `osv-scanner 2.4.0` (osv-scalibr 0.4.5), installed via Homebrew. `uv 0.11.23`.

A naive run against the repo root cannot answer this — it would succeed whether the SBOM was
ingested or the lockfile was found. Two discriminating tests were used instead:

1. **Isolation.** `uv export --format cyclonedx1.5 --no-dev` into a scratch directory
   containing *only* the SBOM — no `uv.lock`, no `pyproject.toml`, no source tree. If results
   appear, the document was genuinely read.
2. **Mutation.** Remove a component from the SBOM and confirm the output follows the document
   rather than the tree.

Filesystem-walk telemetry (`dirs visited` / `inodes visited` / `Extract calls`, printed by
osv-scalibr) was used as corroborating evidence for what was actually opened.

## Evidence

1. **A `sbom.json` filename is REJECTED outright.** Both invocation styles fail before scanning:

   ```text
   could not determine extractor suitable to this file: ".../isolated/sbom.json"
   exit=127
   ```

   ```text
   Warning: --sbom has been deprecated in favor of -L
   Failed to parse SBOM ".../isolated/sbom.json": Invalid SBOM filename.
   If you believe this is a valid SBOM, make sure the filename follows format per your
   SBOMs specification.
   ```

   osv-scanner dispatches its extractor on the **filename**, not on content sniffing.

2. **CycloneDX spec-compliant names are accepted.** Renaming the identical bytes to `bom.json`
   or `beadhive.cdx.json` works:

   ```text
   Scanned .../isolated/bom.json file and found 79 packages
   End status: 0 dirs visited, 1 inodes visited, 1 Extract calls, 3.222875ms elapsed
   exit=0
   ```

   ```text
   Scanned .../isolated/beadhive.cdx.json file and found 79 packages
   End status: 0 dirs visited, 1 inodes visited, 1 Extract calls, 1.938041ms elapsed
   exit=0
   ```

3. **INGEST IS PROVEN BY ISOLATION.** The scan above ran in a directory containing nothing but
   the SBOM. `0 dirs visited, 1 inodes visited, 1 Extract calls` — exactly one file opened, and
   it was the SBOM. All 79 components were recovered. There was no lockfile anywhere in the
   tree to re-scan.

4. **`--licenses` works against SBOM input.** No directory target is required; the full license
   distribution is returned from the document alone (79 components, detail in `bh-vf8h.2`):

   ```text
   +----------------------------+-------------------------+
   | LICENSE                    | NO. OF PACKAGE VERSIONS |
   +----------------------------+-------------------------+
   | MIT                        |                      39 |
   | BSD-3-Clause               |                      14 |
   | Apache-2.0                 |                      10 |
   ...
   ```

5. **The mutation test, as originally specified, was inconclusive and was replaced.** Removing
   `certifi` and diffing the *vulnerability* output proved nothing because the tree has zero
   known vulnerabilities — both runs correctly reported nothing. The discriminator was re-run
   against the *license* output instead, where the mutated document does drop the component
   (see `bh-vf8h.3` evidence, where per-package violation lists differ by exactly the mutated
   entry). Evidence 3 is the stronger and load-bearing proof regardless.

6. **uv's export is a universal resolution.** All 7 platform-conditional components
   (`backports-tarfile`, `importlib-metadata`, `zipp`, `jeepney`, `secretstorage`, `pywin32`,
   `pywin32-ctypes`) are present in the SBOM even though none install on this machine —
   confirmed by direct lookup, each `in_sbom=1`.

## Verdict — **GO**

osv-scanner ingests uv's CycloneDX as a genuine document. The concrete enabler is Evidence 3:
79 packages recovered from a directory containing only the SBOM, with filesystem telemetry
showing a single inode opened. The "one generator, one enrichment step" shape in `bh-okux`
survives contact.

Two operational constraints attach to the GO, neither fatal:

- **The output filename is load-bearing.** `uv export -o sbom.json` — the obvious name, and the
  one used in `bh-okux`'s draft pipeline — is rejected. The file must be named `bom.json` or
  `*.cdx.json`. This is a one-word fix that would otherwise present as a confusing
  `exit=127 / could not determine extractor` at gate-wiring time.
- **Dispatch is by filename, not content.** Anything renaming or streaming the document must
  preserve a compliant name.

## Recommendation

On the strength of this spike alone, GO. For the implementation molecule:

1. Emit the wheel SBOM as `bom.json` (or `<name>.cdx.json`) — never `sbom.json`. Consider
   asserting the filename in whatever produces it, since the failure mode is opaque.
2. Evidence 6 **closes gap #3 in `bh-okux`** ("platform-conditional deps are their own unscanned
   layer"). Because `uv export` emits the universal resolution, a gate driven from the SBOM sees
   all 79 components regardless of the platform it runs on. The under-reporting worry applies to
   scanning an *installed environment*, which this pipeline does not do. `bh-okux.1`'s fifth
   scope item should be amended to reflect that the wheel path is already covered.
3. The `--sbom` flag is deprecated in favour of `-L`; write new tooling against `-L`.
