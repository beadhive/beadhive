# Active SQL frame release rotation ADR (bh-mucmd)

> Status: **shipped in 0.22.1.** Epic bh-cszmo. The operator procedure is in
> [frame-release-upgrade-runbook.md](frame-release-upgrade-runbook.md#active-frame-release-rotation).

## Context

An admitted frame's protected grant pins one operator-approved release digest
(`desired.release`). Eligibility (`release_matches`) requires the frame's signed
heartbeat, its canonical manifest and that grant to agree. Installing any new `bh`
on an active frame therefore made the frame ineligible, and `bh host
release-upgrade` refused active frames ("explicitly unsupported ... need a
separately reviewed upgrade mechanism"). The only other path, superseding with a
new candidate, retires the host UUID: that is identity churn, not an upgrade. The
factory frame worked around this with a parallel tool environment whose attested
release was not the code running dev verbs.

## Decision

`bh host release-upgrade` also accepts a **sole active incarnation**: no
candidate beside it, `state: active`, and no live or unreviewed emergency
authorization. Plan, apply and check keep the pending flow's request fields and
the same reviewed CAS. The plan digest has its own domain
(`beadhive/sql-active-release-rotation/v1`), so a pending plan can never be
replayed as an active one or the other way round.

The transition, recomputed byte for byte inside the authority writer's
transaction (`validate_publication`):

- keeps frame ID, host UUID, instance, signer key, audience and HQ ID;
- sets `epoch = epoch_floor + 1` and installs a fresh principal route and empty
  observer floor;
- sets the reviewed release, profile and policy revision, resets the accepted
  receipt and clears `candidate_expires_at`. The request expiry bounds only the
  plan;
- stays `active`, keeps the cordon bit, and drops a reviewed historical emergency
  grant, which was bound to the old authority and stays in the archive;
- appends the replaced grant to the signed `release_upgrade_history`, keeping the
  newest 16.

The hive lease envelope names the full grant authority, epoch included. A
lease whose authority is an archived **active** predecessor of the current
grant counts as the same incumbent (`same_incumbent_after_rotation`). This
applies in the receiver's renew/release/eviction checks and in the frame's
incumbent and holder lease reads. The frame therefore *renews* its lease after
rotation instead of re-adopting it, and the lease epoch, which is the claim
fencing token, does not change.

## Why this is safe

- **Same reviewed authority as pending upgrade.** Apply needs the operator key,
  `--confirm` and the exact `plan_sha256`. That digest covers the original
  runtime head, the protected state, the config head, the canonical manifest
  (which must already carry the new release and `state: active`), the host UUID,
  the epoch, the old and new digests and a finite expiry of at most 24 hours. Any
  drift refuses. Replaying the plan fails, because the runtime head and epoch have
  moved. The operator key stays in the operator's HOST, and the frame never
  holds it.
- **No evidence carries over.** The new epoch starts with an empty receipt and
  floor. Old beats, registrations and receipts stay immutable under the old epoch
  and cannot satisfy the new one. The old principal's route row survives for
  audit, but no current grant names its epoch, so its heartbeats, lease
  proposals and composite reads are refused. Eligibility on the new epoch needs
  a fresh verified beat whose release equals the newly signed digest and whose
  conformance passes the reviewed profile. A beat carrying the old digest at the
  new epoch fails `release_matches`, and the receiver refuses to renew a lease on
  it.
- **Why no three-beat re-admission.** Admission's streak exists to qualify an
  *unknown* incarnation before it first holds work. Here the incarnation was
  already admitted, and its identity and signer are unchanged. The operator's
  signed plan is the explicit decision to admit the new digest, and every intake
  decision still re-verifies a fresh conformant beat on it. Requiring
  re-admission would also force the lease to lapse (only active grants may
  renew), which strands every claim. That is the failure this change exists to
  remove.
- **Lease continuity is narrow.** Only an *active* archived grant in the
  current record's signed history qualifies. A pending archive never held a lease.
  Archive entries are validated to share the current stable identity and key, with
  strictly increasing epochs. Ordinary publications cannot add, drop or rewrite
  archives (`preserve_history`). Renewal still needs the current grant to be
  active and uncordoned with a fresh conformant receipt at the new epoch. A
  *different* host can still evict only by the existing staleness or retirement
  rules, now evaluated against the current grant.
- **No identity retirement.** Nothing moves to `retired`, and the global
  signer and holder uniqueness rule is unchanged.

## Claims, lease and rollback

Claims are fenced by the hive lease epoch, not the frame epoch. During the
cut-over window the frame is ineligible: no new intake and no lease renewal.
Finishing an existing claim reads the incumbent lease, which accepts the
predecessor. After the first accepted new-epoch beat, the routine renewal
rebinds the lease to the new grant at the same lease epoch. If the window
outlasts the lease, the frame re-adopts at a new lease epoch. In-flight claims
then refuse only their bead write, and re-acking with `bh work claim` recovers
them. The runbook budgets the window against the lease TTL.

Rollback is forward-only: a second reviewed rotation targets the previous
measured digest at a new epoch, and the lease and claims carry over the same
way. If an apply acknowledgment is uncertain, run `check` first. Never
manufacture another epoch to retry a rotation that may have committed.

## Rejected alternatives

- **Demote to pending and re-admit.** Strands claims: a pending grant cannot
  renew the lease. It also adds an outage of at least three beats with no
  safety gain for an already admitted identity.
- **Supersede with a new candidate.** Retires the host UUID. This is identity
  churn and is explicitly forbidden.
- **Mutate the release in place without an epoch advance.** Old-release
  evidence and principal would stay valid for the new digest, so old evidence
  could satisfy the new release.
