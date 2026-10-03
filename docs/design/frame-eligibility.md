# Frame intake eligibility

`bh host eligible <host-or-frame> [--hive <hive>] [--json]` explains the same
candidate predicates used by adoption, renewal, claims and local dispatch. With a hive,
it also reports `candidate_eligible` and current hive lease ownership; the final
`eligible` result includes ownership. An eligible candidate can adopt without already
holding a hive lease.

Frames require current protected admission, active uncordoned lifecycle, an authenticated
fresh heartbeat, matching release and capabilities, passing conformance, positive session
capacity, compatible canonical hive requirements and enabled dispatch. A container cannot
satisfy a KVM requirement. Heartbeat freshness is strictly `age < TTL`; equality denies.
Unknown authority, changed protected revision, unavailable receipt, malformed requirements
and unresolved hive catalog deny intake. Claims recheck immediately at their API/CLI write,
and adoption rechecks after remote reads before the first fence mutation and lease CAS.
Supported `bh bd` write passthrough also checks authenticated frame intake per target;
read passthrough remains available. A raw `bd` invocation is outside this managed boundary.

`hq.mode` selects configuration storage and does not enroll an executor. Existing non-frame
hosts retain their cached legacy lease behavior through config-only cutover. A manifest with
`frame_id`, enrollment-owned `host.frame_id`, or a verified frame-role anchor requires frame
qualification; missing enrolled inventory fails closed. Operator/observer anchor roles must
pass protected custody, digest and recovery-generation validation before legacy fallback.
The Factory GUI bridge identity alone does not enroll a Fleet frame.

Dispatch composition uses `local_intake_decision(hive, cfg=..., hive_dir=...,
legacy_primary=guard.primary_state)`. The explicit legacy reader preserves existing
non-frame policy without making the eligibility module depend on its caller. An omitted
legacy reader denies; frame ownership always comes from the authenticated selected port.

## Lease custody

Non-frame hosts retain their existing five-field unsigned blob leases. Frames publish an
SSH-signed parentless `hive-lease.json` carrier at the same per-hive ref through the selected
HQ lease port. Its binding includes the exact frame holder, instance, signer, frame epoch,
audience and desired config revision, plus operation, expected protected authority head and
expected lease SHA. Frame epoch, hive lease epoch and protected authority revision are
separate values. A frame cannot treat an unsigned legacy blob as intake authorization;
an expired legacy incumbent can be replaced by a signed frame adoption.

The protected receive guard verifies signer and exact incarnation, current accepted heartbeat
receipt, admission, release, conformance, hive requirements and both CAS expectations.
Automatic live takeover needs protected quarantine/retirement or authenticated staleness
strictly beyond `host.lease.evict_after_s` and heartbeat TTL. Recovery before CAS refuses
automatic takeover. Explicit operator force remains separate; a frame principal cannot
assert operator override. An exact owner can release while cordoned or after catalog
projection drift: release preserves hive epoch and incarnation, writes a tombstone, and
cannot extend expiry or authorize new intake.

The Git adapter brackets constituent reads against a monotonic protected authority head;
it does not claim an atomic combined SQL transaction. It verifies the configuration head's
signature, recovery generation and rollback witnesses, then rechecks authority after the
config read. The SQL adapter reads committed config, signed authority, public observer receipt
and hive lease through one physical Dolt transaction. Exact config backend/generation/HEAD
cross-reference denies split publications. A separate fresh authoritative reread fences
completed config, authority and public-receipt changes at the eligibility boundary; every
subsequent claim requalifies. A config snapshot cache TTL cannot waive revocation. SQL frame
intake needs the bound runtime and separate receiver, not a Git checkout or cached Git lease.

## Canonical hive policy projection

The bounded Git fixture/setup API `install_guard(..., hive_policies=...)` accepts a validated
operator-provisioned projection of canonical hive requirements. Each prefix has
`config_revision`, `config_head`, `valid_until`, `requires` and `evict_after_s`.
`config_revision` binds desired frame policy; `config_head` separately binds the exact
protected committed fleet configuration head. Empty head is accepted only while that head
is absent. Holder reads and adopt/renew publication reject an expired or mismatched
projection. Publishing a new fleet config head fences the old projection without regranting
a frame. Empty policy denies frame intake and publication.

This projection is not another fleet configuration source. Runtime frames cannot provision
or alter it. The current production provisioning CLI does not expose projection installation
or refresh; operators must not infer frame readiness from an empty-policy default.
For SQL, the typed optional `managed_repos[].frame_policy` in committed `fleet.yaml` supplies
`config_revision`, `requires` and positive finite `evict_after_s`. Omission denies that hive.
The operator projects all policies from the exact committed ordered config snapshot, binds
each to its Dolt config HEAD and a finite signed authority expiry, and signs that projection
in protected runtime authority. A short reader cache TTL is not the durable policy expiry.
Publishing config without a matching protected projection denies frame work until an operator
refreshes it. This is the supported canonical-catalog-to-projection seam, not another fleet
source. The regular config publisher does not enroll a frame. The fresh empty-schema seed
path belongs to `bh-4shu3`; broad effective-config consumer routing belongs to `bh-9ej9n`.
Existing production Beads stores, remotes and executor enrollment remain unchanged.
