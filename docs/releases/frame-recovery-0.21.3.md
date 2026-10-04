# Frame recovery in 0.21.3

The 0.21.2 HOST marker correction restored configuration loading. It did not change
an accepted registration, renew a heartbeat, admit a frame, or acquire a hive lease.
Version 0.21.3 adds explicit recovery operations for those remaining conditions.

## Authority and permission boundaries

Release-upgrade and emergency-admission apply operations require the separately
approved operator signing key and original protected-authority compare-and-swap.
Frame and observer credentials cannot authorize them. Install the compatible
operator and trusted receiver implementation before using these operations;
upgrading a frame's PyPI installation alone does not update a separately deployed
receiver or its reviewed trust configuration.

Retain the canonical HQ, frame, host UUID, substrate instance and signer. Preserve
old signed state and accepted SQL evidence. Never edit an accepted registration's
release digest or remove `host.frame_id` to force a legacy execution path.

## Emergency admission

Emergency permission is an explicit signed authority record, not a general
`BH_DEBUG` or environment-variable bypass. It requires a human reason, a specific
hive, a short expiry, and the approved executing package digest. Existing signed
registration and heartbeat identity are still required. Only narrowly declared
admission/freshness/conformance conditions can be waived. Unavailable authority,
foreign identity, quarantined or retired state, dispatch policy, compatibility,
and hive-lease exclusivity remain enforced.

The default lifetime is ten minutes and the maximum is thirty minutes. Lease
validity must not outlive the permission. Revocation and expiry stop emergency
intake; ordinary admission requires fresh complete evidence and operator review.
Emergency use does not retroactively turn failed conformance evidence into a pass.

Activation, use and revocation emit structured warning events, and the
signed authority record retains the reason, scope and expiry. Configure the existing
log/telemetry collector to alert on these events. This release does not provision a
notification destination or claim delivery to an unconfigured external service.
Alert on `event=frame_emergency_admission`; the `action` field distinguishes
activation, revocation and use attempts across the client and receiver logs.
An operator-controlled signing key is the permission boundary; possession of that
key must be limited independently of frame and observer credentials.

The client remeasures the approved executing digest before emergency intake or
lease publication. The receiver independently enforces the signed permission,
identity, scope, lifetime and lease ownership; it cannot remotely measure a
process holding the frame signing key. Protecting that key remains essential.

Use `bh host emergency-admit plan FRAME --hive HIVE --reason REASON
--execution-digest sha256:... --expected-revision REV --expected-host-id UUID
--expected-release sha256:...` to review the authorization. Apply the same reviewed
inputs with `apply --operator-key PATH --confirm`. `check` reports the scope and
expiry. `bh host emergency-revoke plan|apply|check FRAME` uses the same existing
operator lifecycle and original revision/host/release CAS options.

The executing digest is the `digest` returned by
`beadhive.heartbeat_report.installed_release()` from the installed wheel's Python
environment. It can differ from the older registered release digest; the operator
must review that difference explicitly. These are separate CLI inputs.

## Normal recovery

Prepare a reviewed canonical manifest for the newly measured installed artifact.
Use the release-upgrade plan/apply/check operation to advance a pending candidate's
epoch without changing its identity. Keep the original authority and config
revisions in the plan, and explicitly provision the new frame-principal/inbox
binding. Existing accepted rows remain historical evidence, never replacement data.
An active frame must follow its separately documented controlled lifecycle; the
pending-only release upgrade refuses an active incarnation.

Generate a fresh measured heartbeat report. The report checks the installed package,
configuration, stable identity and configured hive databases, and reports actual
results. Publication is explicit and rechecks the installed/granted binding. An
unknown or failed check remains non-conformant; a caller-supplied capacity is not
proof of a scheduler's measured free capacity.

`bh host heartbeat-report --free-sessions 0` generates a report without writing.
`bh host heartbeat-send --free-sessions N` remeasures and explicitly publishes it;
neither command grants or admits a frame. Use `bh host release-upgrade --help` for
the exact original authority, epoch, config-head and reviewed plan-digest options.

Publish the new signed registration and at least three consecutive fresh accepted
heartbeats, apply reviewed ordinary admission, then acquire the target hive lease.
Check authority status, frame eligibility and hive readiness before enabling recurring
sender/receiver services. Keep heartbeat publication running within its declared TTL
once normal enrollment is healthy.

## This release's evidence

The operator explicitly authorized manual development and a 0.21.3 patch release
bypassing all workflow and release gates. Focused implementation checks, where
recorded, are not a full release-gate attestation. Shipping the APIs is distinct from
applying production operator authority or declaring this frame admitted.
