# Decision: no-go on Dolt server reuse for integration tests

> Status: **no-go**, 2026-09-23 · Epic: **bh-7ks1c** · Decision bead: **bh-7ks1c.7**

## Context

Bead `bh-7ks1c.7` tried to amortize real-Dolt startup by reusing server processes for
integration tests whose assertions did not depend on server lifecycle. The initial focused
profile showed fewer and faster startups, but the more recent three-run comparison exposes a
large end-to-end regression. A one-worker diagnostic remained slow, so adding xdist workers is
not an adequate explanation or remedy.

## Evidence

The operator-provided three-run comparison reports:

| Measure | Result |
|---|---:|
| Dolt server startup time | 50.5% lower |
| Focused test wall time | 30.20s → 62.88s (+108.2%) |
| Dolt slot-hold time | +106.4% |
| One-worker diagnostic | Still slow |

The wall-time and slot-hold regressions outweigh the startup-time reduction. The earlier
profile in `docs/proof/bh-7ks1c.7-server-startup-profile.json` remains historical evidence for
the prototype and is superseded for the adoption decision by these later measurements.

## Decision

**NO-GO:** do not adopt the shared-per-worker Dolt server reuse implementation from `.7`.
Keep fresh private servers for these tests and keep the existing Dolt slot ceiling. The
rejected reuse change must be removed from the candidate branch before landing the molecule.

Continue the independent cache-locality work. Same-filesystem UV cache placement, hardlink
materialization, and safe copy fallback address dependency materialization and remain worth
measuring independently of Dolt fixture reuse.

## Current scope for `.8`

The operator approved a correctness-only cache-locality closeout. The `.7` NO-GO evidence above
remains historical provenance for the rejected Dolt server-reuse proposal; it does not set a
performance target for `.8`. The rejected reuse implementation stays out of the final candidate,
and this decision record remains intact.

The `.8` handoff proves same-device UV and pnpm hardlink behavior through target-file filesystem
identity, and explicit cross-device copy fallback with capacity and warning evidence. Focused
tests cover resolver safety and worker/environment propagation. The benchmark tools remain
opt-in, and any timings they record are descriptive and non-normative.

Preserve the repository's 16 xdist workers and two globally admitted heavyweight validations.
No worker matrix, tuning recommendation, regression budget, or performance claim is required to
gate `.8` landing. The ordinary correctness validation remains required; benchmarks and
repeated timing matrices stay outside that gate.
