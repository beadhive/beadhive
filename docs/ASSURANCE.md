# Assurance plane — the cross-cutting gate layer (`warden`, security + policy)

The Assurance plane is the **exception** to Beadflow's "one plane, one sequential handoff"
tenet. It is not a stage that ideas or beads flow *through* — it is a **cross-cutting gate
layer** that attaches verdicts to work already in flight on other planes. Its one operational
seat, the **warden** (`warden/`), owns a single remit: **security + policy** — secret-scan,
SBOM, policy-as-code. It reads a change or release and returns a block-or-clear verdict; it
never writes code.

> **Security + policy only.** The warden's scope is deliberately narrow. The Contribution
> **provenance** scrub and the human-only publish gate stay owned by the `contributor` seat
> (`contrib/`) — do **not** fold provenance into the warden. Acceptance / e2e / QA is a separate
> concern kept as the **verifier lens** (below), not part of the warden's remit.

See also [AGF.md](AGF.md) for the overall flow, [CONTROL-PLANE.md](CONTROL-PLANE.md) and
[PLANNING-PLANE.md](PLANNING-PLANE.md) for the operational planes, and
[docs/design/roles-rbac-matrix.md](design/roles-rbac-matrix.md) (§1, §2.3, §4) for the canonical
seat + RBAC definitions this document operationalizes.

## Why cross-cutting, not sequential

The other operational planes hand off in one direction — Planning → Integration → (roadmap:
Release → Delivery). Assurance instead **attaches a gate at multiple points** in that pipeline:

| Attach point | Plane gated | What the warden checks |
|---|---|---|
| pre-merge | Integration | the bead/molecule diff before it lands on the green line |
| pre-cut *(roadmap)* | Release | the release contents before a version is tagged |
| pre-publish *(roadmap)* | Contribution | the outbound change before it is pushed upstream |

Only the **pre-merge** attach point is operational today; pre-cut and pre-publish arrive with the
roadmap Release and Contribution planes. Because one seat gates several planes, Assurance cannot be
a sequential plane of its own — it is a gate *layer* laid across the others.

## The warden seat

| Field | Value |
|---|---|
| Seat | **warden** |
| Identity | `warden/` |
| Plane | Assurance (cross-cutting gate layer) |
| Owns | a change / release under gate → **security + policy** verdict + findings |
| Decision authority | high — **block** |
| Technologies | git read-only, secret-scan, SBOM, policy-as-code |
| Permissions | **read + block**; no writes; provenance **not** in scope |
| Enforcement | **hard** gate — the `security:*` gate, parallel to review |

The warden is a **read-and-block** seat: like the reviewer it holds no Edit/Write over the
codebase, and unlike the merger it never lands anything. Its only output is a gate verdict.

## The `security:*` gate — parallel to review

The warden's verdict is carried by a **`security:*` bd gate**, the Assurance analogue of the review
gate. It is opened alongside the review gate on a bead and **blocks the merge in parallel with
review**: the merge path already refuses to land while **any** gate naming the bead is open, so a
change lands only when **both** the review gate **and** the security gate have cleared.

- **Distinguishable from review/kickoff.** A security gate is identified by a `security:` marker in
  its bd-gate reason (parallel to the review gate being matched on `reason: review`), so it is never
  confused with the review or kickoff gates.
- **Warden-only to resolve.** Only a `warden/<name>` seat may **resolve** a `security:*` gate. The
  security + policy verdict cannot be self-cleared by the change's author or reviewer — a non-warden
  actor targeting a security gate is refused, and the merge stays blocked until a warden signs off.
- **Provenance is not on this gate.** The `security:*` gate covers secret-scan / SBOM /
  policy-as-code only. The Contribution provenance scrub + human publish gate are a separate,
  `contributor`-owned gate — the two never merge.

This mirrors the seat-prefix convention used across the factory: just as only a `contrib/` seat may
reach the gated external-push path, only a `warden/` seat may resolve the Assurance verdict.
(Implementation: `src/beadhive/guard.py` — `is_security_gate` / `guard_security_gate_resolution`.)

## License policy — the wheel's allowed set

The first concrete policy the Assurance remit enforces. It runs as `just license-check` (wired
into `just check` and `check-all`), not as a warden-resolved gate — the warden's `security:*`
gate is the human verdict layer, while this is the mechanical check that feeds it.

**The pipeline.** `uv.lock` → `uv export --format cyclonedx1.5` → `bom.json` → `osv-scanner`.
One generator, one document, and **two invocations with independent exit codes**: the license
check and the CVE report. That separation is the design, not an accident — license policy is
near-deterministic and worth blocking on, while a CVE feed is noisy and continuously changing.
A blocking CVE gate gets switched off within a month, taking the license gate with it. Each has
its own toggle (`BH_LICENSE_MODE`, `BH_CVE_MODE`, each `enforce | warn`); what must never happen
is one switch controlling both.

**The allowed set** lives in the `justfile` (`license_allow`) as the single source of truth —
this document explains it rather than restating it, so the two cannot drift:

| Identifier | Note |
|---|---|
| `MIT`, `Apache-2.0`, `BSD-3-Clause`, `BSD-2-Clause`, `ISC`, `PSF-2.0`, `Unlicense` | ordinary permissive set |
| `MPL-2.0` | **deliberate** — see below |
| `HPND` | for the Scintilla component bundled in `pywin32` |

**Why MPL-2.0 is allowed.** `certifi` is MPL-2.0 — file-level (weak) copyleft — and it is
unavoidable: it *is* the CA bundle, arriving via `httpcore`/`httpx` on the core path and via
`requests` on the `[otel]` path. Its obligations attach per-file to MPL-covered files that are
*modified*, and nothing here modifies or vendors it. §3.2's source-availability requirement is
satisfied by construction because certifi is pure Python — the shipped bytes are the source.
Removing `MPL-2.0` as an apparent oversight turns the gate red on every run.

**The exception mechanism.** Packages whose declared metadata cannot be mapped to SPDX are
dispositioned in `osv-scanner.toml` via `[[PackageOverrides]]`. Every override carries:

- a **`reason`** citing the primary source — the license file inside the distributed wheel,
  read directly. Not a summary, not recollection.
- an **`effectiveUntil`** date. This is deliberate: an override is a standing exception to the
  policy, and an exception nobody revisits is simply invisible policy. Re-verify at expiry
  rather than extending on reflex.

Two behaviors worth knowing before editing that file: a multi-value `license.override` is
**conjunctive** (every listed identifier must also be in the allowed set), and a lapsed
`effectiveUntil` **stops applying silently** — the package just reappears as an ordinary
violation with no hint that a policy decision expired.

**Never allowlist `UNKNOWN`.** osv-scanner accepts it as a valid SPDX token, so adding it would
silently permit every unlicensed package in the tree. The failure is invisible, which is exactly
what makes it worth naming. (`non-standard`, by contrast, is rejected outright as non-SPDX —
which is why those packages need overrides instead.)

**Exit codes.** `0` clean; `1` findings (fails under `enforce`, reported under `warn`); `2`
invalid mode, rejected before the scanner runs; **`127` the scan never ran** — a malformed
allowlist, or an SBOM filename osv-scanner refuses to dispatch on. 127 is fatal in *both* modes:
swallowing it under `warn` would print a clean pass over a tree nothing examined.

### What this policy does not cover

- **Licenses are declared, not scanned.** The data comes from deps.dev — what each package
  declares about itself. It catches a transitive that declares itself copyleft; it does **not**
  catch one that lies, or that vendors copyleft code without saying so. That is an accepted
  limitation at this risk profile, recorded so nobody assumes more coverage than exists.
- **The container image.** deps.dev has no apk/deb license data, so the image is governed
  separately — see "The image's own policy" below. Neither supersedes the other; they cover
  different layers.
- **The Homebrew tap** needs no separate pipeline: the formula is a pointer, so its SBOM is the
  wheel's.
- **Signing and provenance.** `uv export` is not reproducible — `metadata.timestamp` and
  `serialNumber` change on every run — so nothing here can be signed to a stable digest. That
  remains open.

Evidence for every claim above: `docs/spikes/bh-vf8h.1-osv-sbom-ingest.md`,
`bh-vf8h.2-osv-deps-dev-enrichment.md`, `bh-vf8h.3-osv-gate-mechanics.md`.

## The image's own policy — what Beadhive redistributes

Publishing an image makes us a **redistributor** of everything inside it, which is a different
exposure from the wheel's dependencies and needs its own statement.

**The image ships redistributable components only.** Every component declares a permissive or
public-domain-equivalent licence, measured from its own source of truth rather than assumed.
`tests/test_component_licenses.py` makes that binding: an undeclared component fails, and so does a
licence outside the allowed set. Adding a component is a reviewed licence decision, not a one-line
change.

That measurement now comes from two places, because the components do:

- **The nix-supplied toolchain** — `bd`, `dolt`, `gh`, `git-workspace`, `jq`, `yq`, `just` — is
  described by `docker/toolchain-metadata.json`, generated from `flake.nix` out of nixpkgs' own
  `meta.license`. No hand-written row to forget. It is committed like a lockfile so the gate runs
  on a host with no nix, and the docker build regenerates and diffs it so it cannot go stale.
- **What nix does not supply** — the Python base image, `uv`, and this project — stays declared by
  hand in `docker-bake.hcl`'s licence-policy block. The test asserts that block matches the pins
  exactly, so a row left behind by a component that moved to the export fails too.

One caveat worth stating plainly: nix's `allowUnfree` being off blocks **proprietary** packages,
not **copyleft** ones. GPL is free software and evaluates happily. The allowlist, not nix, is what
stops copyleft reaching the image.

The wording is deliberately **redistributable**, not "permissively licensed". The latter would be
false: the image is Debian-derived and its base layer carries hundreds of GPL/LGPL packages — git
itself is GPL-2 — as every Debian-derived image does. Those are separate programs invoked as
programs. GPL-2 §3 attaches source-availability obligations to redistributing them; it does not
contaminate our code, and it is not something an allowlist over *our* pins can or should govern.
That layer is acknowledged here, not audited.

**Copyleft and proprietary tools are user-brought, never baked in.** Two live examples:

| tool | licence | why it is not in the image |
|---|---|---|
| `repowise` | AGPL-3.0 | a plugin the user installs; naming it in a comment is not depending on it |
| Claude Code | `SEE LICENSE IN README.md` | proprietary — shipping it would redistribute it under Anthropic's commercial terms |

Claude Code is installed at runtime with `bh dep install claude`, which names the licence before
acting so accepting those terms is the user's own choice.

**Codex used to be the exception, and no longer is (bh-lnrn).** It declares Apache-2.0 and passes
the allowlist outright — it was shipped for exactly that reason. It is now excluded anyway, by
decision: the image ships the runtime and the means, never the harness. The rule reads "no
harness", not "no proprietary harness", because a rule with an *except the permissive one* clause
is the rule that admits the next one. Node left with it, having had no other consumer.

**This repo declares no project-scope MCP servers.** A committed `.mcp.json` would impose its
servers on everyone who clones and every container built from the repo. `.gitignore` prevents one,
and `tests/test_dependency_policy.py` asserts both the rule and the property it is meant to produce
— that no such file is tracked — since the rule alone does nothing against `git add -f`.

### What the image policy does not cover

- **The Debian base layer is acknowledged, not audited.** Its copyleft content is inherent to the
  choice of base image and governs redistribution, not our source.
- **Declared, not scanned** — the same limitation as the wheel's policy. A component that
  misdeclares its own licence is not caught here.
- **The nix closure's transitive dependencies are base-layer, not pinned components.** The image
  carries ~24 store paths, and only the seven binaries we NAME are governed by the allowlist. The
  glibc and support libraries underneath them are the same category as the Debian base image's own
  copyleft content: separate programs invoked as programs, governed by redistribution obligations
  rather than by an allowlist over our pins. Naming this explicitly matters because a closure is
  easy to mistake for a dependency list — it is a runtime graph, and auditing it is the job of an
  image SBOM (`bh-btry`), not of this gate.
- **Two SBOMs, with different scopes.** See the next section — and note that neither is a
  vulnerability gate today.

## Two SBOMs, and what each one answers (bh-btry)

They describe different things and are not interchangeable. Reaching for the wrong one gives a
confident answer to a question you did not ask.

| | `bom.json` | `dist/image-sbom.cdx.json` |
|---|---|---|
| scope | the wheel's **Python dependency graph** | the **nix closure the image ships** |
| built by | `just sbom` (uv, CycloneDX 1.5) | `just image-sbom` (sbomnix, CycloneDX 1.4) |
| size | 79 packages | 19 components |
| committed | yes | no — derived, re-stamped on every run |
| scanned by | `osv-scanner` (`just license-check`, `just cve-report`) | **nothing yet** — see below |

`docker/toolchain-metadata.json` is a third file and not an SBOM at all: it names the **7 binaries
we pin**, in this repo's own shape, to feed the licence gate and the image manifest. The image SBOM
carries 19 components because a closure includes what those seven pull in.

### osv-scanner cannot scan the image SBOM, and says so quietly

Measured, not assumed:

```text
$ osv-scanner scan source -L dist/image-sbom.cdx.json
Scanned .../image-sbom.cdx.json file and found 19 packages
Filtered 19 local/unscannable package/s from the scan.
No issues found
```

Nineteen found, nineteen filtered, zero scanned — and it exits reporting no issues. **OSV has no
nix ecosystem**, so `pkg:nix/…` purls match nothing in its database. Wiring a gate on this would be
permanently green while checking nothing: the failure this repo already fought in bh-vf8h.3 (an
allowlist matching no packages) and bh-dfz2 (`check-all` running zero integration tests). Do not
wire it.

### The path that does work is CPE, and the SBOM already carries it

Every component in the image SBOM has a CPE alongside its purl:

```json
{ "name": "bash", "purl": "pkg:nix/bash@5.3p15",
  "cpe": "cpe:2.3:a:gnu:bash:5.3:15:*:*:*:*:*:*" }
```

That is the translation layer, and it is why sbomnix emits CPEs at all. CPE-based scanners work
against nix targets where ecosystem-based ones cannot:

- **grype** consumes this CycloneDX file directly and matches on CPE.
- **vulnix** (nix-community) matches derivations against NIST NVD, taking a nix path rather than an
  SBOM.
- **vulnxscan** (sbomnix's own suite) aggregates vulnix + grype + an OSV client — but upstream
  documents that its OSV path queries *without* an ecosystem, so its nix results carry false
  positives. Prefer grype or vulnix over the aggregate.

Choosing and wiring one is **`bh-e6uk`**, deliberately not this bead: producing an SBOM and
scanning it are separate decisions, and a scanner brings a blocking-vs-advisory policy question of
the kind `license_mode` / `cve_mode` already answers on the Python side.

## The verifier lens (not a seat yet)

**Acceptance / e2e / QA** is kept as a **lens**, not a staffed seat. Today it is covered by the
existing developer self-check, the reviewer's local demo, and CI — no separate `verify/` identity
runs. It is promoted to a real Assurance seat only when end-to-end testing needs its own test-env
identity (likely alongside the roadmap Delivery plane). Until then it is documented here as a lens
on the Assurance layer, distinct from the warden's security + policy remit.

## Scope boundary — what Assurance does not own

- **Provenance** stays with the `contributor` seat (`contrib/`), Contribution plane.
- **Acceptance / e2e** is the verifier lens, not the warden.
- **Release** (`releaser`, version + changelog + tag) and **Delivery / Deployment** (`operator`,
  gitops reconcile + IaC apply) are **roadmap** planes — not yet operational. Their plane docs
  (`RELEASE-PLANE.md`, `DELIVERY-PLANE.md`) and agent defs (`releaser`, `operator`) are
  **intentionally deferred to the roadmap backlog** (tracked by beads
  releaser + Release gate, and operator + Delivery env gate). The warden's
  pre-cut and pre-publish attach points light up when those planes land.
