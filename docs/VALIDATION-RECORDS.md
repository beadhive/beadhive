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

## Durable artifacts and CI handoff

Each execution allocates its own `<artifact-root>/<run-id>/` directory containing `reports/` and
`gate.log`. The default root is `<hive>/.bh/validation/runs`; an absolute
`work.validation_artifact_root`, or the higher-precedence `BH_VALIDATION_ARTIFACT_ROOT`, relocates
the whole layout. Relative roots are rejected before the validation command starts.

Run manifests remain compact control records and carry report counts/tree metadata. Bounded retry
summaries live at `.bh/validation/runs/.summary/<tree>/results.json` (20 runs per tree, 50 trees),
where `latest_raw_run` points at the actual raw run directory; raw XML and logs are never copied
into a second per-tree store.

After an external uploader has accepted the *complete* run directory, acknowledge it explicitly:

```sh
bh work artifacts-uploaded <run-id> --hive <hive>
```

Only this acknowledgement permits pruning superseded raw output. Running runs, canonical verdict
pointers under `.git/bh/validation/verdicts/`, and the newest red/retry raw run stay protected.
`validation/uses` is audit history, not an indefinite raw-artifact lease.

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
