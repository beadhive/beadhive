# Frame release ranges in 0.29.0

0.29.0 lets a SQL frame grant admit a range of `bh` releases instead of one pinned release
(`bh-vfrem`). It is opt-in: no grant changes until an operator signs a `release_policy`, and a
grant without one behaves exactly as in 0.28.x. Operator procedure:
[release-range runbook](../design/frame-release-upgrade-runbook.md#release-ranges-in-range-upgrades-without-rotation).
Design: [frame-release-range-adr.md](../design/frame-release-range-adr.md).

## What ships

1. **A signed release manifest in CI.** The release workflow's `attest` job signs
   `{version, measured_digest, algorithm, file_count, wheel_sha256, commit}` for each published
   wheel with a GitHub attestation. The digest is computed from the wheel by
   `scripts/release-manifest.py`, which shares one fold with `installed_release()`.
2. **A release catalog.** `hq_release_catalog` is a committed, append-only table that binds a
   version to its measured digest. `bh hq release-catalog provision|import|verify|revoke` manage
   it. Import verifies the Sigstore bundle against the release workflow's identity and recomputes
   the digest from the wheel. Verification needs `beadhive[release-verify]`. A revocation is a
   tombstone. `hq.release_catalog.auto_import` (default off) imports new PyPI releases.
3. **A release policy in the grant.** `bh hq authority release-policy plan|apply --frame F
   --spec 0.29.x` signs the range without an epoch advance, with a downgrade guard.
4. **Range-aware eligibility.** Both the session and signed-beat carriers admit a frame whose
   measured digest is an active catalog row whose version satisfies the spec.
5. **A frame that reports what it runs.** The heartbeat carries the measured release, and
   refuses only outside the policy.

## Upgrading

- **Upgrade every frame to 0.29.0 or later before setting any policy.** Older readers cannot parse
  `release_policy`, and a single policy would make the authority state unreadable to them.
  `release-policy plan` refuses while any frame attests an older release.
- Nothing changes for exact-pin grants, the Git-backed HQ, emergency admission or out-of-range
  upgrades: those keep the reviewed rotation (`bh host release-upgrade`).
- `admit` still pins to the grant's reference release. Admit or re-provision a host on that
  release, or re-plan the reference.
- The measurement algorithm is unchanged (`beadhive-tree-sha256-v1`), so existing digests hold.
- Existing HQs gain the catalog only when an operator runs `provision`; it is a reviewed,
  one-time commit that the restore floor accepts.
