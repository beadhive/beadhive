# Frame registration and the 0.21.2 ownership correction

Version 0.21.2 classifies the durable enrollment marker `host.frame_id` as HOST
configuration. Setting, saving and loading that marker no longer conflicts with
the FLEET-owned `host` namespace. `host.dispatch.enabled` remains FLEET policy.
An enrolled executor still requires protected admission and current authenticated
frame evidence; this correction does not authorize legacy lease fallback.

## Existing pending SQL registration

Deploying a corrected artifact changes its measured release digest. An accepted
SQL registration binds that digest to its incarnation. The receiver accepts an
exact replay, but rejects different evidence for the same frame, holder, instance
and epoch (`hq_sql_receiver.py`, `accept_registration`). Its compatibility
exception only adds the canonical HQ ID to a legacy registration with every other
manifest field unchanged. That exception cannot update a release digest.

The current SQL grant API also rejects a holder identity or signer fingerprint
already present in an active, candidate or retired record (`hq_control_plane.py`,
`SqlControlPlane.grant`; `hq_authority_guard.py`, `records`). Retiring the pending
incarnation therefore does not enable a replacement grant that preserves its
host UUID and signer. Version 0.21.2 has no supported API for a release upgrade
of this pending registration that preserves both. Version 0.21.3 adds the explicit
pending-only operation described in [Frame recovery](frame-recovery-0.21.3.md).
Do not edit accepted evidence,
replace its digest, remove the enrollment marker, or claim that retirement and
regranting provide that upgrade.

Keep the host UUID, canonical HQ identity and signer intact. A supported,
operator-reviewed release/incarnation upgrade is a prerequisite for
upgrading that pending enrollment; it remains separate from this ownership fix.
After the 0.21.3 release upgrade, the corrected measured artifact must supply
new signed registration and at least three consecutive fresh accepted heartbeats
matching the protected desired release, capabilities and conformance profile.
Admission still requires the reviewed operator plan/apply/check flow described
in [Frame Fleet Membership](../FRAME-FLEET-MEMBERSHIP.md). Validate config,
authority status, hive readiness and eligibility on the enrolled machine before
starting recurring sender/receiver services.

The ownership regression tests exercise local configuration and fresh CLI
startup. They do not establish that the remote macOS enrollment is healthy or
that its immutable registration can accept the new artifact.
