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

## Replan of `.8`

Keep `bh-7ks1c.8` because it still needs to select current-host worker caps and report the
cache-locality results. Remove the requirement that the molecule improve combined test wall
time by 15%: that target was tied to the rejected server-reuse experiment and is not a useful
success criterion for a cache-placement change. The final report must instead:

- retain the no-go result and keep `.7` out of the final implementation;
- provide at least three green repetitions for the historical worker comparison and a
  current-host comparison that includes the repository's 16-worker default;
- compare unit, integration, and combined wall times, variance, slow phases, process/server
  counts, and available Dolt queue/hold telemetry, clearly labeling unavailable telemetry;
- record same-device hardlink behavior, cross-device copy fallback, cache bytes/inodes,
  capacity, and warnings;
- recommend phase-specific and uniform worker caps from current-host measurements, preserve
  the 16-worker default unless evidence supports a change, and keep global validation
  admission at two; and
- document any test-wall regression over 5% that is not explained by host variance or the
  measured optimization, without requiring a fixed whole-suite speedup from cache locality.

The matrix remains an operator-run benchmark; it does not enter the per-commit validation
gate. Future CPU grants through `bh-nzck2` remain separate from the global validation-admission
limit.
