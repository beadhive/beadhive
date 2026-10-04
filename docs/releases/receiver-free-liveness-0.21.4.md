# Receiver-free liveness in 0.21.4

The factory frame kept losing eligibility because frame liveness and hive-lease
renewal both depended on the separately deployed SQL trusted receiver. When the
receiver stalled for 20-30 minutes, heartbeat acceptance stopped, the two hour
hive lease lapsed, re-adoption bumped the claim epoch and invalidated in-flight
claims, and `bh work merge` failed with `HqLeaseUnknown`. 0.21.4 removes that
dependency from the development flow behind one default-off, host-local switch.
Git HQ mode is untouched.

## What ships

1. **Control-plane failures no longer fail write verbs** (bh-jto52, all
   modes). `renew_if_due` now swallows SQL control-plane failures
   (`HqLeaseUnknown`, `ControlPlaneError`, `SqlRuntimeError` and
   `SqlTransportError`) instead of failing the write verb that triggered the
   renewal.
2. **`hq.sql.liveness: receiver | signed` switch** (bh-0acs8). The default is
   `receiver`, which is the existing behavior unchanged. The environment
   variable `BH_HQ_SQL_LIVENESS` overrides the config value and wins over
   `host.yaml`; an invalid value in either place fails loudly. In `signed`
   mode, eligibility verifies the frame's newest signed heartbeat envelope from
   its own inbox at read time instead of using the receiver's receipt. Age is
   the reader's clock minus the signed `renewTime`, with a 30 second skew
   clamp, and the age basis is reported as `signed-envelope-reader-clock`.
3. **Advisory lease expiry and no receiver renewals** (bh-7y6b2). In `signed`
   mode, hive-lease expiry is advisory: it is a failover hint rather than a
   gate, and the development path makes no receiver renewals.

## Trust delta

Switching to `signed` trades receiver-side evidence for availability. Review
these before enabling it.

- **First-seen audit and floors are lost.** The receiver's first-seen audit
  record and its monotonic floors are not consulted in `signed` mode.
- **The age basis is the frame's clock.** Freshness is derived from the signed
  `renewTime`, which the frame sets, rather than from the receiver's receipt
  time. The reader clock bounds it, with the 30 second skew clamp.
- **A receiver is still required for adopt, release and failover.** Those
  operations are unchanged and still need a running receiver. Only the
  steady-state development flow becomes receiver-free.
- **Mixed modes are unsupported.** Do not run some readers in `receiver` mode
  and others in `signed` mode against one frame.
- **The inbox grows.** Each heartbeat adds about one row to the frame's inbox
  table, and there is no pruning yet.
- **Split-brain exposure is unchanged.** Because expiry is advisory, a stale
  holder is not stopped by the lease clock. That exposure equals today's
  raw-push exposure. The hive-remote fence and the claim epoch tokens remain
  the data-plane guard, and epoch monotonicity is unchanged.

## Rollout order

`HqSqlConfig` forbids unknown keys, so a 0.21.3 process reading a `host.yaml`
that contains `hq.sql.liveness` fails to load its configuration. Roll out in
this order.

1. Upgrade every process that reads `host.yaml` to 0.21.4.
2. Only then set `hq.sql.liveness: signed` in `host.yaml`.

Until every reader runs 0.21.4, leave `host.yaml` alone and set
`BH_HQ_SQL_LIVENESS=signed` only in the environment of the 0.21.4 `bh`, for
example through a small shim that exports the variable and execs that binary.

To revert, unset the variable or set it to `receiver`, and remove or reset
`hq.sql.liveness` in `host.yaml` if you set it there.

## Release digest on an active frame

Installing a new version changes the measured release digest. For an active
frame, `release_matches` and the operator heartbeat script require the signed
`desired.release` to match, and rotating the release of an active frame is not
supported yet. Follow-ups: bh-cszmo, bh-vfrem and bh-rjjjo.

The operator-approved interim for the factory frame is therefore:

- Install 0.21.4 into a parallel `uv tool` environment used only for the
  dev-flow `bh`, with `BH_HQ_SQL_LIVENESS=signed` set for it.
- Leave the heartbeat sender on the attested 0.21.3 environment so the
  measured digest still matches the signed `desired.release`.
- Clean up the parallel environment under bh-cszmo once active-frame release
  rotation is supported.
