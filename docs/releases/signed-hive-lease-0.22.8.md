# Receiver-free hive leases in signed mode (0.22.8)

0.22.0 made frame liveness receiver-free in `hq.sql.liveness: signed` mode, but adopt,
release and failover still waited for the separately deployed trusted receiver to write
the protected `hq_live_hive_leases` row. 0.22.8 removes that last receiver dependency from
the signed-mode development flow. It is a bridge for 0.22.x. In 0.23 the receiver leaves
this path entirely (bh-a94qw, bh-o46mn). Receiver mode and Git HQ are unchanged.

## Why: what agent-hitch hit

`bh host lease adopt github/briancripe/agent-hitch` failed four times without `--force`,
at epochs 18, 19, 20 and 21. Each attempt advanced the hive's `refs/bh/epoch` fence and
then reported `verified HQ runtime connection unavailable` about 15 seconds later. No hive
lease was recorded. agent-hitch was left fenced at epoch 21 by the factory frame, with no
lease, so no host could write to it.

A read-only diagnosis on 2026-10-06 (bh-qv8ig) used only SELECTs through the frame and
config-reader principals. It found the following.

- **The receiver was running.** It accepted the factory frame's heartbeats around all
  four request times: 9 of 9 within 30 minutes of the first three, and 5 of 10 around the
  fourth. Acknowledgment latency reached about 13 minutes. Its last accepted result was
  at 16:17:27Z, after the epoch-21 request.
- **The proposals were rejected, not lost.** All four `ah` adopt proposals are committed
  in the frame inbox and verify. None of them could ever be accepted, because `ah` has no
  `frame_policy` in the committed fleet catalog. The only prefixes in the operator-signed
  hive policy, at every authority revision involved, are `bh`, `bh-ap` and `bh-skls`. For
  any other prefix, `accept_hive_lease` raises `protected hive policy differs from
  canonical catalog`.
- **Rejections leave no trace.** The receiver writes `hq_live_results` rows only for
  accepted requests: the table holds 373 rows, all `accepted`. A rejected proposal is
  therefore indistinguishable from one that was never processed, and the CLI gave up with
  a generic connection error.
- **`bh-cp` had the same problem.** Of the frame's 19 hive-lease proposals, 7 have no
  result. Six of them (four for `ah`, two for `bh-cp`) are for prefixes with no signed
  policy. The seventh is `bh-skls` at epoch 3. Its expected revision was overtaken while it
  waited in the receiver's backlog: an earlier `bh-skls` proposal was accepted about 21
  minutes after submission. The result was a CAS conflict, which left `bh-skls` fenced at
  epoch 3 while its lease row stayed at epoch 2.
- **Nothing stopped the fence advancing.** Client-side eligibility checks the local
  registry's `requires`, not the signed hive policy. Each retry therefore advanced the
  fence toward a lease that could never be accepted.

## What ships

1. **Signed hive-lease acceptance** (`hq.sql.liveness: signed`, which the factory runs
   through `BH_HQ_SQL_LIVENESS=signed`). Adopt, renew and release commit the frame's
   signed proposal and then read it back. They do not wait for a receiver result.
   `current_hive_lease_holder` and every other write gate that reads the HQ lease resolve
   the holder at read time, from the frame's own signed proposals bound to the hive's
   current `refs/bh/epoch`. This is the same approach 0.22.0 takes for heartbeats.

   A proposal counts only if every one of these holds:
   - It passes the request checks the receiver makes today: signature under the active
     grant's key; exact request shape and payload digest; principal, frame, holder,
     instance, epoch, key, audience, config revision and beadyard identity; an unforced
     `adopt`, `renew` or `release`; and a well-formed lease bounded to 24 hours.
   - The current active grant is the one it signed under. Hive requirements and capacity
     are met. A bound frame's registration has an accepted result.
   - The prefix was in the operator-signed hive policy at the authority revision the frame
     signed against. That revision must also be in the current authority history. A
     proposal the receiver would have rejected therefore never becomes a lease later.
   - Its lease epoch equals the hive's current fence epoch, and the fence names its holder.
2. **The fence replaces the receiver's global lease CAS.** The fence's Git CAS already
   admits exactly one adopter per epoch. The rules at the fence epoch are:
   - A losing adopter's proposal at the same epoch is ignored.
   - These cases fail closed, and no holder is granted:
     - two different tenures at one epoch;
     - a release that does not match the tenure;
     - a protected receiver row at the fence epoch that names a different host from the
       fence.
   - A receiver-accepted row still counts while its epoch equals the fence epoch, so
     leases accepted before the upgrade keep working. A row at an older epoch is
     superseded.
   - When the fence names another host, the reader reports that host as an advisory holder.
     It never reports the hive as free, so an unforced adopt cannot take it.
   - When the fence cannot be read (the hive is not cloned on this host, or the remote is
     unreachable), the read fails closed.
3. **Adopt checks hive policy before moving the fence.** The check applies to every SQL
   HQ, receiver mode included. If the signed authority has no frame policy for the hive,
   adopt refuses with a `PLACEMENT:` message, and neither remote is touched. No more epochs
   are burned.
4. **A switch.** `BH_HQ_SQL_HIVE_LEASE=proposal|receiver`. `proposal` is the default in
   signed mode. `receiver` restores 0.22.7 behavior, where only the receiver's row counts.
   The switch is validated in both modes and has no effect in receiver mode. Any other
   value is an error.

## Trust delta (in addition to 0.22.0's)

- The receiver's checks at acceptance time are no longer applied: a fresh conformant
  heartbeat receipt, and the first-seen audit. Write gates still require a fresh verified
  signed heartbeat and current admission on every holder read, exactly as before.
- A frame holding its signing key can mint a lease for its own grant. It cannot move the
  fence without winning the hive remote's CAS, and the fence is now the single-writer
  guarantee.
- A lease accepted only as a proposal does not survive a release rotation, which changes
  the grant epoch and the inbox. Adopt again after a rotation. Rotating an active frame's
  release is not supported yet anyway.

## Mixed versions and upgrade order

A 0.22.7-or-older reader in signed mode reads only the receiver's row. For a lease that
exists only as a proposal, such as a recovered agent-hitch, it sees no lease and fails
closed: every write gate refuses. For a hive whose receiver row is at an older epoch than
the fence (today: `bh-skls`, row at epoch 2 and fence at epoch 3), an older reader still
reports the row's holder. Its managed push is refused, because the fence epoch differs.

1. Install 0.22.8 into every `bh` that runs with `BH_HQ_SQL_LIVENESS=signed`. On the
   factory, that is the parallel dev-flow environment. Leave the heartbeat sender on its
   attested release; it does not read hive leases.
2. Only then adopt with 0.22.8. To return to 0.22.7 behavior without downgrading, set
   `BH_HQ_SQL_HIVE_LEASE=receiver`.

After the upgrade, `bh-skls` resolves to its committed epoch-3 proposal. That proposal is
signed under the current grant, its prefix was policed, and it matches the fence. `bh-skls`
becomes writable again without any action. `bh` (epoch 221) and `bh-ap` (epoch 1) are
unchanged, because their receiver rows match their fences.

## Recovering agent-hitch (no ref surgery, no `--force`)

agent-hitch's prerequisite is placement. A lease cannot be accepted for it until the
operator puts it under frame policy. Its four stranded proposals never count, even after
policy is added: they were signed when `ah` was outside the signed policy.

1. In the canonical `fleet.yaml`, give the `ah` `managed_repos` entry a `frame_policy`.
   Its `config_revision` must equal the factory grant's, currently
   `skills-migration-2026-10-03`, and it needs an `evict_after_s` and the `requires`
   matching the factory frame. Publish the config.
2. Renew the HQ authority (`bh hq authority renew`) so the signed hive policy projects `ah`.
3. On the factory, using the 0.22.8 dev-flow `bh`, run
   `bh host lease adopt github/briancripe/agent-hitch` with no `--force`.

The adopt checks the signed policy first. It then moves the fence from 21 to 22 by the
normal CAS, commits a signed epoch-22 proposal, and reads it back as the lease.
`current_hive_lease_holder` then holds for the factory frame. If step 1 or step 2 is
missing, the adopt refuses with `PLACEMENT:` and the fence stays at 21.

## Coordination

- **bh-a94qw (director placement).**
  - Must keep: the five-field lease record in `hq_live_hive_leases`, and a placement row
    whose epoch matches `refs/bh/epoch`. In signed mode a row whose epoch differs from the
    fence is superseded. A row that names a different host from the fence fails closed.
  - Should drop: the proposal resolver and `BH_HQ_SQL_HIVE_LEASE`, once director
    placement owns the row.
- **bh-o46mn (receiver retirement).**
  - Removing `publish_inbox`, the inbox tables and `hq_live_results` removes what this
    bridge reads, the frame inbox and the registration result witness.
  - Must retire `beadhive.hq_signed_hive_lease`, the evidence read in
    `read_frame_composite`, and the precheck's dependency on the signed hive policy, in the
    same change or after a94qw lands.
  - The `hq_live_*` export must include the `hive_lease` inbox rows. They are the only
    record of tenures accepted this way.
- **bh-ktw0o.** Owns the receiver-mode acknowledgment polling and timeout classification.
  Signed mode does not poll.
