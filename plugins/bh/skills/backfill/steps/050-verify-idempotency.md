---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: verify-idempotency
  title: Re-run the reconcile and confirm zero proposed changes
  requires: [apply]
  performer: agent
  action:
    type: script
    script: scripts/reconcile.sh
    timeout_seconds: 120
  verify:
    type: script
    script: scripts/reconcile.sh
    args: ["--verify"]
    success_exit: 0
  on_failure:
    strategy: ask
  effect: read-only
  estimated_duration_minutes: 2
  tags: [verify, idempotency, terminal]
---

# What this step does

Runs the reconcile a second time. Now that every doc-backed bead carries its `external_ref`, the
deterministic bridges and the stored refs match exactly: every doc classifies `PRESENT-in-sync`
and the tool proposes nothing. `--verify` asserts no stamp is pending and exits 0.

# Why it matters

Idempotency is the whole safety property of the reconcile model. A second run that proposes
changes means a stamp did not take or a NEW bead was mis-created — the run is not done. A clean
second run is the proof that re-running this Guide on the same rig is a no-op, which is what makes
it safe to re-run after every future doc is added.

# Success criteria → terminal

- `scripts/reconcile.sh <rig-path> --verify` exits 0.
- No `UNMATCHED` doc remains that the classify step judged NEW/PRESENT (those are now filed or
  stamped).

On success the walk terminates at **`reconciled`** — or **`nothing-to-do`** if the very first
run already proposed nothing.

# Notes

Read-only. This is the terminal verification; no further writes.
