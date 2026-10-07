# Authority and config-edit fixes in 0.23.1

0.23.1 is a patch for the single-operator factory that keeps a long HQ authority valid and
bound (`bh-81nac`). Verifier-side only: the signed authority format is unchanged.

## What ships

1. **Config-edit tolerance** (`bh-u67ve`). An unrelated fleet-config publish no longer fences SQL
   frames. A frame_policy, prefix, kind or repo identity, beadyard identity, `hosts/*`,
   `allowed_signers` change, or a non-descendant head, still fences. Frames keep enforcing the
   signed snapshot. Details: [HQ](../HQ.md#config-edit-tolerance).
2. **Long authorities are not shortened** (`bh-oywx8`). SQL grant, observe, lifecycle and
   release-upgrade keep `max(original expiry, now + 1 h)`.
3. **Renew hint and durations** (`bh-u4cip`). The hint uses the resolved ceiling and
   `--max-duration` only above 7 d; durations accept weeks (`2w`).
4. **Bound-head surfaces** (`bh-3h6al`). `status`, `check`, `bh doctor` and the publish notice
   treat a tolerated head as bound.

## Rollout order

Upgrade **receivers first**, then frames. A 0.23.0 frame or receiver keeps fencing on any head
move until upgraded.

## Trust delta

- The key-less SQL config publisher can commit `fleet.yaml` edits frames do not enforce without
  fencing them. It still cannot change a `frame_policy`, hive identity, host manifest,
  `allowed_signers` or beadyard identity, or rewrite history, without an operator renew.
- A longer authority stays valid longer by design: revocation by cordon, retire or emergency
  action is unchanged, but a stolen authority is valid up to the configured ceiling.

## Not in this release

Laptop-free renewal (a scoped delegate key, `bh-rjjjo`) is deferred to 0.24.0: frames pin one
operator key, so 0.23.0 frames would reject a delegate-signed record. No unattended-renewer
runbook is provided.
