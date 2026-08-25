# Validation records and runner result protocol

Validation persistence separates an execution (`validation/runs/<run-id>/manifest.json`) from
each decision that consumes it (`validation/uses/<use-id>.json`). Both IDs are random 128-bit
identifiers allocated with exclusive filesystem creation. Consequently, two processes checking
the same tree and command do not share a mutable row. A reuse creates only a new use that points
to the completed run; it never claims that another execution occurred.

Run lifecycle is `running`, `completed`, or `abandoned`. Verdict is independently `green`, `red`,
or `none`. Checkout/setup failures, missing executables, and interruptions are execution facts
with verdict `none`; ordinary command exit zero is green and ordinary nonzero is red. The active
directory contains reconstructable `{run_id}` pointers only. Ownership and terminal state remain
authoritative in the manifest, so migration refuses a legacy marker whose host, PID, or process
start token disagrees with its referenced running manifest.

## Version 1 typed runner protocol

A trusted runner integration is enabled only by setting:

```yaml
work:
  validation_protocol: beadhive-validation-result/v1
```

Bh then exports `BH_VALIDATION_RESULT_PATH` to the validation command. The runner may write this
JSON object to that exact path:

```json
{
  "protocol": "beadhive-validation-result",
  "version": 1,
  "verdict": "none",
  "reason": "runner refused because its service was unavailable"
}
```

The four keys shown are the complete v1 schema; `reason` may be a string or null. Unknown keys,
an unknown protocol/version, invalid types/verdicts, or green paired with a nonzero/missing
process exit are rejected. Rejection falls back to process semantics, so malformed,
contradictory, absent, or untrusted output can never turn a nonzero command into anything less
than red. Protocol data is never discovered implicitly from ordinary command output.
