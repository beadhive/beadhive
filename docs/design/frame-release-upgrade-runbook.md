# Reviewed SQL frame release upgrade (pending and active)

`bh host release-upgrade plan|apply|check <frame>` rotates a SQL incarnation to
a newly measured release while preserving frame ID, host UUID, canonical HQ ID,
substrate instance, signer and capabilities. It advances the frame epoch,
creates a fresh principal route and resets accepted evidence for the new
incarnation. Two exact shapes are supported, selected by the protected state
that the reviewed `--expected-revision` pins:

- a **pending** candidate without an active incarnation (steps 1-7 below). It
  stays pending and needs normal admission; the command does not admit work.
- a **sole active** incarnation ([Active frame rotation](#active-frame-release-rotation)).
  It stays active (and keeps its cordon bit) at the new epoch; it is eligible
  again only on fresh authenticated evidence for the new digest. Its hive lease
  and every claim fenced by that lease survive. The safety argument is in
  [active-frame-release-rotation-adr.md](active-frame-release-rotation-adr.md).

Quarantined, draining/drained/parked, live or unreviewed emergency, and
coexisting active/candidate states are explicitly unsupported. Do not retire or
remove identity records to evade this restriction.

0. Preflight: run `BH_HQ_AUTHORITY_MIN_REMAINING=6h bh hq authority check` (add `BH_HQ_OPERATOR_SETTINGS=<file>`
   from an off-host operator). It must exit 0; an expired or soon-expiring authority is rebound first
   ([HQ: Authority modes](../HQ.md#authority-expiry)).
1. Keep the enrollment marker, UUID, canonical HQ and frame signing key intact.
   Measure and review the corrected installed artifact's release ID and SHA-256
   digest and its conformance profile. Record the current runtime authority
   revision, candidate epoch and approved old release digest. Authority must be
   current; an expired pending grant can be renewed by this explicit operator
   transition, but an expired authority carrier must first be restored through
   its supported operator publication path.
2. Through the separately credentialed config publisher, prepare and publish
   the canonical `hosts/<uuid>.yaml` with the new release and explicit
   `state: pending`. Preserve identity and capabilities. Keep the complete
   ordered snapshot and all unrelated documents. The existing typed
   `SqlFleetConfigRevisionStore.publish_snapshot(documents,
   expected_revision=<original-config-head>)` API validates and publishes that
   config with its original CAS; `bh hq push` does not publish SQL configuration.
   Record the resulting committed config HEAD. Config publication alone cannot
   replace accepted registration evidence or admit a frame.
3. Review a plan using the exact current inputs and a finite candidate expiry
   strictly in the future, at most 24 hours away:

   ```sh
   bh host release-upgrade plan <frame> \
     --expected-revision <runtime-head> --expected-host-id <uuid> \
     --expected-epoch <old-epoch> --expected-release <old-sha256-digest> \
     --expected-config-head <prepared-config-head> \
     --release-id <new-release-id> --release-digest <new-sha256-digest> \
     --profile <measured-profile> --config-revision <canonical-hive-policy-revision> \
     --candidate-expires-at <unix-seconds>
   ```

   The JSON plan identifies the new epoch, principal, inbox table, authority and
   `plan_sha256`. The config revision is the declared frame policy revision;
   it must match the intended canonical hive policy to permit later intake.
4. On the SQL server, explicitly provision the plan's new account and inbox
   using `hq_sql_runtime_schema.inbox_ddl(principal, epoch)` and the existing
   least-privilege frame grants. Broker credentials must match that exact new
   principal. Preserve the old principal registry, inbox, accepted registration,
   receipts, observer floors and results. Do not insert or edit protected
   registry rows: the signed apply operation installs the new route and empty
   observer floor transactionally. Account/DDL provisioning is separate from
   this operator publication and cannot itself authorize work.
5. Repeat the same plan arguments with action `apply`, adding
   `--plan-sha256 <reviewed-plan-digest> --operator-key <approved-key> --confirm`.
   The separately credentialed authority writer rechecks the original runtime
   and config revisions, complete canonical manifest, stable identities,
   preserved grant history, new route and finite expiry inside its publication
   transaction; it verifies the operator signature. Any changed input requires
   a new plan. Replaying the old request cannot rotate the new incarnation.
   If the commit acknowledgment is uncertain, use `check` and inspect the
   protected authority before retrying; do not manufacture another epoch.
6. Update only the HOST runtime broker account binding to the reviewed new
   principal. Preserve the existing frame signing key and all identity pins.
   Read back with `bh host release-upgrade check <frame>` and verify the new
   epoch, release and pending state. Previous grants and their recorded receipts
   remain in signed `release_upgrade_history`; SQL evidence remains immutable
   under its old epoch. Archives authorize no runtime principal.
7. Publish new signed registration matching the prepared canonical manifest,
   then at least three consecutive fresh, accepted heartbeats binding the new
   epoch, release, profile and policy revision. Old evidence cannot satisfy the
   new incarnation. Complete the normal reviewed `bh host admit plan|apply|check`
   flow. Verify config, authority, hive readiness and host eligibility before
   enabling recurring sender/receiver services.

## Active frame release rotation

Use this to move an admitted, active frame (for example the factory frame) onto
a newly installed, measured `bh` release. The operator key never goes to the
frame: plan/apply/check run in the operator's separately credentialed HOST
binding (`hq.sql.authority_writer`, `runtime: null`).

1. **Measure.** In the new installed environment (not an editable checkout):

   ```sh
   <new-env>/bin/python -c 'import json; from beadhive.release_measurement import installed_release; print(json.dumps(installed_release()))'
   ```

   Review the `id` and `digest`, and the conformance profile its heartbeat
   report will declare (`bh host heartbeat-report --free-sessions 0` from that
   environment). Keep the old environment installed until step 9: it is the
   rollback target.
2. **Read the current authority.** `bh host release-upgrade check <frame>`
   prints `revision`, `state: active`, `authority` (`holder_identity`,
   `epoch`, `config_revision`) and `release`. Record them. The authority must be
   current; renew it through its ordinary operator path first if it expires
   within the procedure.

   `check` (like `plan` and `apply`) needs the off-host `hq.sql.authority_writer`
   binding: run it from an operator host with `BH_HQ_OPERATOR_SETTINGS=<file>`
   (a `--operator-settings` flag arrives in 0.23.0; see
   [HQ: Rebinding from an operator host](../HQ.md#authority-laptop-renew)). From a
   frame host without that binding it refuses with "separate authority writer
   capability unavailable".
3. **Budget the lease window.** From step 4 until the first accepted beat at
   the new epoch (step 8) the frame is ineligible, so it neither takes new
   claims nor renews its hive lease. Begin only with more than that window left
   on the lease (`bh host list --lease-hive <prefix>`; default TTL 30 minutes)
   and do not cordon the frame first (a cordoned frame cannot renew). Claims
   already in flight keep finishing: their incumbent lease read accepts the
   reviewed predecessor.
   `BH_FRAME_HEARTBEAT=advisory` does not cover this window: it waives only
   the `authenticated_fresh_heartbeat` predicate. With no beat at the new epoch
   there is no lease evidence, so `current_frame_incarnation`, `release_matches`
   and the other lease-derived predicates still fail and the frame stays
   ineligible.
4. **Prepare config.** Through the separately credentialed config publisher,
   publish the canonical `hosts/<uuid>.yaml` with the new `release` and
   explicit `state: active`, identity and capabilities unchanged
   (`SqlFleetConfigRevisionStore.publish_snapshot(documents,
   expected_revision=<original-config-head>)`, complete ordered snapshot).
   Record the committed config HEAD.
5. **Plan** with the exact current inputs; for an active frame
   `--candidate-expires-at` is the plan's finite expiry (strictly future, at most
   24 hours) and is not stored on the admitted grant:

   ```sh
   bh host release-upgrade plan <frame> \
     --expected-revision <runtime-head> --expected-host-id <uuid> \
     --expected-epoch <current-epoch> --expected-release <current-sha256-digest> \
     --expected-config-head <prepared-config-head> \
     --release-id <new-release-id> --release-digest <new-sha256-digest> \
     --profile <measured-profile> --config-revision <current-policy-revision> \
     --candidate-expires-at <unix-seconds>
   ```

   Review `rotation: active`, `state: active`, `old_epoch`/`new_epoch`, the new
   `principal`/`inbox_table`, the unchanged identity in `authority` and the
   `plan_sha256`. Any changed input (runtime head, epoch, release, config head)
   refuses; re-plan instead of editing.
6. **Provision** on the SQL server the plan's new account and inbox with
   `hq_sql_runtime_schema.inbox_ddl(principal, epoch)` and the existing
   least-privilege frame grants (as in pending step 4). Leave the old account,
   inbox, registry row and evidence in place.
7. **Apply** the same arguments with action `apply`, adding
   `--plan-sha256 <reviewed-plan-digest> --operator-key <approved-key> --confirm`.
   The authority writer recomputes the transition inside its transaction from
   the original runtime and config heads and refuses any difference. On an
   uncertain acknowledgment run `check` before anything else: a new `epoch` and
   `release` mean it committed; never re-plan to "retry" a committed rotation.
8. **Cut over the frame.** Update only the frame HOST runtime broker binding to
   the plan's new principal, keeping the signing key and identity pins. Restart
   the frame's services (host daemon, frame bridge, heartbeat sender) on the new
   environment. Publish a new signed registration and heartbeats. Verify with
   `bh host release-upgrade check <frame>` and `bh host eligible --hive
   <prefix>`: every predicate, including `release_matches`, must pass on the new
   digest. Claims minted before the rotation stay valid because the lease epoch
   does not change. Do not expect a renewal to rebind the lease to the new
   grant: in signed liveness mode nothing renews the lease (`renew_if_due`
   returns without a round trip), so the lease stays bound to the archived
   predecessor and survives only through `same_incumbent_after_rotation`. In the
   legacy receiver mode the next lease renewal does rebind it.
9. **Retire the old environment** only after the frame is eligible and its
   lease has renewed. The old principal can no longer publish (no current grant
   names its epoch); disabling that SQL account is optional hygiene and must not
   delete its evidence.

**Rollback.** There is no rewind: epochs only advance and the old principal stays
dead. To go back, run steps 2-8 again with the previous measured release as the
target (republish the manifest with that release, plan, provision a new
principal, apply, cut over to the old environment). The frame lands on a new
epoch with the old digest, its lease and claims carry over the same way, and both
rotations remain in the signed archive. If the lease lapsed during a window, the
frame re-adopts at a new lease epoch and in-flight claims refuse their bead write
with the stale-claim message. The code is not lost: re-ack with `bh work claim`
and submit again.

## Session-only provisioning on 0.24.3

This runtime repair targets package patch 0.24.3. Its reviewed optional CLI
provisioning inputs are recorded in the independent additive wire 2.7.0 contract;
existing rotation inputs retain their order and behavior.

Use `bh host release-upgrade check <frame> --operator-settings <settings>` to
read both the runtime `revision` and the verified `canonical_config_head` and
`canonical_release`. Prepare the target manifest through the public CLI before
planning the rotation. The operator settings must include the existing separate
`hq.sql.publisher` credential for configuration publication:

```sh
bh host release-upgrade prepare-config <frame> \
  --operator-settings <settings> \
  --expected-revision <runtime-head> --expected-host-id <uuid> \
  --expected-epoch <current-epoch> --expected-release <current-sha256-digest> \
  --expected-config-head <canonical-config-head> \
  --release-id <target-release-id> --release-digest <target-sha256-digest> \
  --profile <target-profile> --config-revision <target-config-revision> \
  --candidate-expires-at <review-expiry-within-24h>
```

This prints the exact incumbent and target releases and a preparation
`plan_sha256` without writing. Review it, then repeat the same command with
`--plan-sha256 <preparation-digest> --confirm`. Only the selected canonical host
manifest's release changes. Its identity, capabilities, state, remaining fields,
document ordering, and every unrelated document are preserved. Use the returned
`config_head` in the normal rotation plan; the preparation digest is separate
from the rotation plan digest.

The config publisher keeps its original config-head CAS. Separate runtime-writer
and config-publisher credentials do not provide an atomic cross-database CAS:
the CLI checks runtime HEAD immediately before publication and again before
reporting success. A concurrent runtime change refuses and reports the prepared
config HEAD; it never grants authority. If the publication acknowledgment is
uncertain, use public `check` to read back `canonical_config_head` and
`canonical_release`, then review preparation again with the current heads.
An already prepared exact target is an idempotent no-op; never repeat using a
stale preparation digest or infer that a refused acknowledgment means no change.

### Cancel an unapplied preparation {#cancel-an-unapplied-preparation}

If provisioning or `apply` fails after `prepare-config`, the canonical manifest names the
target release, but the authority still admits the old one. To return the manifest to the
admitted release, run `prepare-config` with that release as the target. Use the same
`--expected-*` values (the current runtime HEAD, epoch and admitted digest), the current
`canonical_config_head` from `check`, and `--release-id`/`--release-digest` set to the
**admitted** release. Leave out `--target-route`:

```sh
bh host release-upgrade prepare-config <frame> --operator-settings <settings> \
  --expected-revision <runtime-head> --expected-host-id <uuid> \
  --expected-epoch <current-epoch> --expected-release <admitted-sha256-digest> \
  --expected-config-head <canonical-config-head> \
  --release-id <admitted-release-id> --release-digest <admitted-sha256-digest> \
  --profile <profile> --config-revision <config-revision> \
  --candidate-expires-at <review-expiry-within-24h>
```

The review reports `rollback: true`, the prepared `canonical_release_before` and a
preparation digest. Repeat it with `--plan-sha256 <digest> --confirm`. Only that manifest's
release changes. The authority, its identity and epoch, every other document and every restore
floor stay as they are. No private publisher API, SQL edit or rebind is needed. The same
config-head CAS and runtime-HEAD fences apply as for a forward preparation, so a concurrent
change refuses. An already-restored manifest is an idempotent no-op (`already_prepared:
true`). If the acknowledgment is uncertain, read `canonical_config_head` and
`canonical_release` back with `check`, then review again at that head. `plan` refuses a
rollback target, because returning to the admitted release is not a rotation. A rollback after
`apply` has committed is the [rollback rotation](#active-frame-release-rotation), not this.

For a frame already routed to its session table, `plan`, `apply`, and `check`
report `route: session`, the actual `session_table` and `evidence_table`, and
whether provisioning is complete. The compatibility field `inbox_table` names
the actual session table. A legacy inbox route retains the existing inbox
provisioning procedure above; the session provision action refuses that route
family rather than silently changing a reviewed plan.

Use the existing `--operator-settings` file format for server-local provisioning.
Its `hq.sql.authority_writer` credential must have DDL, account creation, and grant
privileges on the isolated HQ server, as listed in
[HQ: Runtime SQL privileges](../HQ.md#runtime-sql-privileges). Use the same server-local
settings for the session-route `apply`. Keep this provisioning credential off the
frame and separate from the ordinary least-privilege authority writer. The
runtime database must already have its committed `frame_*` ignore rule and
liveness policy. Provisioning never initializes or changes those protections.

After reviewing the session plan, repeat its exact arguments with action
`provision`, adding these flags:

```sh
bh host release-upgrade provision <frame> \
  <the exact reviewed plan arguments> \
  --operator-settings <server-local-provisioning-settings> \
  --plan-sha256 <reviewed-plan-digest> --confirm \
  --frame-address <pinned-frame-address> --password-stdin
```

Supply the new account password on stdin through the normal credential workflow.
The command provisions only the reviewed principal's session/evidence tables,
timestamp triggers, pinned TLS account, and required read grants. It cannot
publish an authority grant, admit a frame, or rewrite heartbeat evidence.

Provisioning commits its trigger pair while the signed authority sequence stays
unchanged. All operator, frame, and observer restore floors accept that movement
only after proving a bounded, linear descendant history containing additive
canonical timestamp-trigger pairs at a next frame epoch. Every existing schema
fragment and its metadata stays identical. Added pairs must be absent from the
prior protected principal registry and signed authority records, including
archived grants; they cannot repair stamps on an already authorized incarnation,
even when another frame has the same next epoch. This structural proof can accept
an unused canonical principal's inert timestamp pair. It does not prove that the
principal was reviewed or authorize it: the public provision and apply actions
separately bind the exact planned principal and incarnation to the reviewed
digest, original heads, supported table schema, account, and grants.
Authority, principal registry,
policy, ignore-rule, mixed changes, partial pairs, merges, forks, and rollbacks
refuse. Commit messages supply no proof and no restore floor needs editing.

When `replan_required` is true, record the returned `revision`, repeat `plan`
with that value as `--expected-revision`, and review the new plan digest before
`apply`. Keep all other reviewed expectations. If provisioning was interrupted,
repeat `plan` and `provision` against the currently verified revision. The
command completes missing supported objects and refuses conflicting schemas,
triggers, account bindings, or unrelated dirty state. A successful retry keeps
the existing password, session renewal, and conformance evidence unchanged.
If `apply` has already committed, use `check` and cut over to that incarnation;
do not provision another epoch to retry it. If a remote `check` or `plan` refuses with
"restore-floor verification denied" after provisioning, grant that binding
`SELECT ON <runtime>.*` ([HQ: Runtime SQL privileges](../HQ.md#runtime-sql-privileges)). The
message reports a privilege gap, not a rollback. Do not edit a restore floor.

### Activate the reviewed frame credentials through the public CLI

After `apply` and protected `check` confirm the new epoch and principal, perform
this step **on the existing factory frame**, using its published 0.24.3 interpreter.
The daemon uses the configured SQL account; it cannot discover or invent the new
account password. Preserve the old Fnox entries until cutover is verified.

Read the complete local SQL subtree through the public CLI:

```sh
bh config get hq.sql --scope host > sql-before-0243.json
```

Review the existing `runtime.credential` and `reader.credential` references.
For each existing config file/profile, store the provisioned account password
under a **new** approved key through the existing provider. Do not put the
password in an argument, the reviewed JSON, or a diagnostic log:

```sh
fnox --config "$RUNTIME_FNOX_CONFIG" --profile "$RUNTIME_FNOX_PROFILE" \
  set "$NEW_RUNTIME_FNOX_KEY" --provider "$APPROVED_RUNTIME_PROVIDER"
fnox --config "$READER_FNOX_CONFIG" --profile "$READER_FNOX_PROFILE" \
  set "$NEW_READER_FNOX_KEY" --provider "$APPROVED_READER_PROVIDER"
```

Use Fnox's hidden prompt or the approved stdin secret workflow. If both bindings
share the same Fnox config/profile, they may reference the same new entry.
Create a reviewable copy of the complete subtree, changing only each account
username and its credential key. The `NEW_PRINCIPAL` value must match the public
provision/apply/check output:

```sh
"$FACTORY_PYTHON" - "$NEW_PRINCIPAL" "$NEW_RUNTIME_FNOX_KEY" \
  "$NEW_READER_FNOX_KEY" <<'PY' > sql-reviewed-0243.json
import json, sys
with open("sql-before-0243.json") as source:
    settings = json.load(source)
for binding, key in zip(("runtime", "reader"), sys.argv[2:], strict=True):
    settings[binding]["user"] = sys.argv[1]
    settings[binding]["credential"]["key"] = key
print(json.dumps(settings, indent=2))
PY
```

Review the old and proposed JSON. Keep every endpoint, database, TLS/server pin,
backend identity, generation, initial revision, runtime initial revision,
operator public key, and floor path unchanged. Preserve all other SQL settings
and HOST identity fields. The complete subtree write is atomic and requires a
committed config readback using the proposed reader credentials:

```sh
bh config set hq.sql "$(cat sql-reviewed-0243.json)" --json --scope host
bh config get hq.sql --scope host > sql-after-0243.json
"$FACTORY_PYTHON" -m beadhive.heartbeat_sender renew
"$FACTORY_PYTHON" -m beadhive.heartbeat_sender conformance
"$FACTORY_PYTHON" -m beadhive.heartbeat_sender status
```

Require successful exits, a renewal naming the new session table, and fresh
conformant evidence for the exact approved release. These are documented module
CLI entrypoints ([HQ heartbeat sender](../HQ.md)), not private API calls. If
background units still reference the old interpreter, regenerate them with the
published interpreter's `-m beadhive.heartbeat_sender units --install` and reload
the existing systemd/launchd manager using its normal supported workflow. If the
frame daemon retains old settings, restart it through the existing public
`bh host daemon stop` / `bh host daemon start` commands and its configured
supervisor. Keep the existing frame and hive adoption intact.

Finally use `bh host list --lease-hive bh --json`, `bh host eligible --hive
<factory-hive> --json`, and the normal guarded factory operation from the release
runbook. Require agreement on the current holder, unchanged hive lease/writer
epochs, approved release, fresh session, and conformance. A credential/readback
failure leaves activation incomplete: review the exact bindings and existing
provider access; do not alter restore floors or re-adopt the hive.

## Archive

Each rotation archives the replaced grant (`state` pending or active) with its
authority revision, config head and plan digest in signed
`release_upgrade_history`. A pending incarnation's archive is bounded to 16
upgrades; an active incarnation keeps the newest 16 rotations (older SQL
evidence stays immutable under its own epoch, and epochs never repeat). The
archive cannot contain recursive history.

**16-entry horizon.** `HISTORY_LIMIT = 16` trims the active archive to the newest
16 entries on every rotation. In signed mode nothing renews the lease, so it
stays bound to the grant it was adopted under and survives only while that grant
is still in the archive. Count active-frame rotations per lease: before a 16th
rotation without a renewal or re-adopt, re-adopt the lease deliberately, rather
than let its grant fall out of the archive and force an unplanned re-adopt at a
new lease epoch (which makes in-flight claims refuse their bead write). Ordinary authority publications
preserve it exactly. This API retains the global signer/holder uniqueness rule;
it does not enable general identity reuse, release mutation of an existing
registration, or legacy admission.
