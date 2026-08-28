# ADR: Orca in the first host-runtime channel

**Status:** Rejected for first channel (2026-08-28) · **Decision bead:** `bh-icn6b.3`

## Context

The accepted one-flake catalog needs a first optional application. Delivery/license spike
`bh-icn6b.2` is GO (MIT + measured pinned Nix lifecycle). Empirical v1.4.190 execution changed
`bh-icn6b.1` to NO-GO: serve found profile binaries but injected an Orca-private Codex home, and
desktop was unmeasured.

## Decision

**NO-GO: Orca does not enter the first host-runtime channel.** Its lawful delivery mode would be
`nix`, but delivery GO cannot compensate for the failed runtime-ownership requirement. Required
channel core and other optional catalog selections still come from one locked flake.

Target-user resolution order is explicit: CLI flag/answer → configured seat user → invoking
non-root user; never root by accident. Beadhive owns catalog metadata, target-user desired state,
the dedicated profile at `$HOME/.local/state/beadhive/profiles/host`, reconciliation and
verification. The selected user owns credentials, versioned license receipts and normal
`~/.codex`/`~/.claude`. Orca owns its own application state, never provider binaries/homes.
Upstream owns the authoritative source/release path; Beadhive may cache MIT-covered artifacts
with notice and exact hash when Orca is reconsidered.

Catalog `required` entries are always present; `optional` entries are present iff selected.
Reconciliation computes an exact set and removes deselected packages without deleting user
state. Install and enable are separate: installation mutates only the profile; enabling may
create/start desktop/serve integration after explicit exposure choices. Channel upgrade builds a
new complete profile generation, verifies every executable/version and Orca-launched environment,
then atomically switches the profile symlink; failure leaves the old generation selected.
Rollback switches a retained generation.

License receipts bind user, stable license ID, authoritative source URL, content SHA-256 and
accepted-at time. A changed license ID or license-content hash forces re-acceptance; version-only
changes under unchanged MIT terms do not. No generic forever-valid acceptance flag exists.

## Existing-work disposition

- `bh-eqvhe`: remains a **separate serve-provisioning concern**, not absorbed into a catalog that
  rejected Orca. Its Electron dependency, derived service PATH, pairing, idempotence and exposure
  evidence remains valid; its manual AppImage assumption may be amended only after re-admission.
- `bh-pc2a.19`: dependency/non-overlap. It decides Claude's signed apt install channel for
  containers; host-runtime consumes the resulting harness declaration and does not duplicate it.
- `bh-2igmr`: dependency/non-overlap. Profile lifecycle must use/probe the supported Nix verb;
  this ADR does not opportunistically fix its remaining code sites.
- `bh-h5if`: explicit non-overlap. Container-volume persistence is container-plane work; the host
  profile and passwd home do not solve or duplicate it.

## Consequences

No speculative Orca implementation molecule is filed. The general host-runtime architecture may
proceed with another first optional item. Orca requires a released, documented real-home Codex
selection verified inside both desktop and serve, followed by re-planning. `bh-eqvhe` remains
gated independently by its explicit private-network/exposure decision.
