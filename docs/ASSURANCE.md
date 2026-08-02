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
- **OS packages in the container image.** deps.dev has no apk/deb license data, so the
  base-image layer is governed separately by the component allowlist in `docker-bake.hcl`.
  Neither supersedes the other — they cover different layers.
- **The Homebrew tap** needs no separate pipeline: the formula is a pointer, so its SBOM is the
  wheel's.
- **Signing and provenance.** `uv export` is not reproducible — `metadata.timestamp` and
  `serialNumber` change on every run — so nothing here can be signed to a stable digest. That
  remains open.

Evidence for every claim above: `docs/spikes/bh-vf8h.1-osv-sbom-ingest.md`,
`bh-vf8h.2-osv-deps-dev-enrichment.md`, `bh-vf8h.3-osv-gate-mechanics.md`.

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
