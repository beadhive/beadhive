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
