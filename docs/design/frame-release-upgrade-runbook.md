# Reviewed pending SQL frame release upgrade

`bh host release-upgrade plan|apply|check <frame>` rotates a pending SQL
incarnation to a newly measured release while preserving frame ID, host UUID,
canonical HQ ID, substrate instance, signer and capabilities. It advances the
frame epoch, creates a fresh principal route and resets accepted evidence for
the new incarnation. It does not admit work.

Active, quarantined, emergency-enabled and coexisting active/candidate states
are explicitly unsupported. Do not retire or remove identity records to evade
this restriction. Those cases need a separately reviewed upgrade mechanism.

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

The archive is bounded to 16 pending upgrades and cannot contain recursive
history. Ordinary authority publications preserve it exactly. This API retains
the global signer/holder uniqueness rule; it does not enable general identity
reuse, release mutation of an existing registration, or legacy admission.
