# Development gateway v1 immutable handoff

This is the core/runtime handoff to infra bead `bh-infra-lum.3`. The machine-readable record is
[`development-gateway-v1-handoff.json`](development-gateway-v1-handoff.json). Every candidate
reference below is immutable; no branch name is a deployment input.

## Candidate

| Field | Exact value |
| --- | --- |
| Git commit | `3a43e045bbfa07d9fe1e98e50d1e89ed6f1c2fba` |
| Git tree | `39433e8572d5d3f7a07dea400731d5333424a7e7` |
| Wheel | `beadhive-0.15.1-py3-none-any.whl` |
| Wheel SHA-256 | `f1f069560dda55ae859752fb7e5f758c4e0acfdc26ce02e54e0c184ee0567509` |
| Wheel bytes | `1372265` |
| Contract | `gateway.v1`; conformance schema 1 |
| Logical instance | `dev/demo` only |

The wheel was built twice from the candidate commit with
`SOURCE_DATE_EPOCH=1787813110 uv build --wheel`. Both byte streams produced the recorded digest.
The source tree passed `just check`: 6,560 passed, 41 skipped, zero failed. The focused gateway and
real loopback runtime profile contributed 56 passing tests.

## Conformance map

| Required behavior | Executable evidence |
| --- | --- |
| Discovery, snapshot, exact CORS, redaction | `test_authorized_subject_discovers_only_dev_demo_and_reads_redacted_snapshot`; `test_exact_cors_preflight_and_response_allowlists_are_closed` |
| Command/result, stale scope, hidden capability | `test_authorized_subject_invokes_advertised_refresh_and_receives_correlated_result`; `test_refresh_reauthorizes_scope_and_fails_closed_for_hidden_stale_and_revoked_access` |
| Ordered SSE and retained reconnect | `test_stream_starts_from_snapshot_cursor_and_delivers_monotonic_redacted_events`; `test_stream_replay_has_no_duplicates_and_stale_cursor_requires_resnapshot` |
| Retention gap, restart, epoch/sequence gap | `test_stream_gap_or_epoch_change_emits_one_resnapshot_control`; `test_stream_retention_gap_and_restart_require_resnapshot` |
| Expiry, revocation, changed scope | `test_invalid_identities_share_one_non_disclosing_failure`; `test_idle_stream_closes_promptly_when_authorization_changes`; `test_refresh_rechecks_changed_instance_policy_after_discovery` |
| Cross-instance, origin, audience denial | `test_wrong_signature_origin_and_instance_fail_before_runtime_access`; `test_invalid_identities_share_one_non_disclosing_failure` |
| Offline and bounded outage | `test_offline_runtime_is_disclosed_but_snapshot_fails_bounded`; `test_hung_snapshot_saturation_times_out_without_starving_discovery` |
| Real source adapter and restart cleanup | `test_real_loopback_profile_maps_snapshot_refresh_and_retained_events`; `test_lifespan_cancels_runtime_work_and_allows_clean_process_restart` |

## Host operations

Install the wheel by its digest into `/opt/beadhive-gateway`, then use the reviewed
[`beadhive-gateway-dev.service.example`](../../deploy/systemd/beadhive-gateway-dev.service.example).
The launcher binds only loopback port 8787. The service sandbox independently denies every
non-loopback address. It consumes the existing loopback Beadhive host daemon on port 8420 and
resolves exactly `github/beadhive/beadhive`; there is no fixture fallback or hive selector.
Cloudflared owns a separate service and credential.

- Health: use the exact local probe in [`REMOTE_GATEWAY_V1.md`](../REMOTE_GATEWAY_V1.md). An
  authenticated discovery result of `offline` is readiness evidence; `/healthz` is liveness only.
- Capacity: at most 16 live streams; stream opens, commands, availability, and snapshots have
  independent bulkheads and five-second deadlines. Stream policy is rechecked every second.
- Outage/restart: the client retains its last cursor. A retained cursor replays exactly once; an
  expired cursor or new producer epoch returns `resnapshot_required`. Service shutdown cancels and
  joins admitted source work.
- Redaction: browser shapes are exact allowlists. Work descriptions, local paths, transcripts,
  source coverage details, raw operator events, credentials, and internal exceptions are dropped.
- Identity rotation: atomically replace the mode-0600 JWKS credential, restart the gateway, and
  prove the old signing key is refused and a new signed session succeeds. Subject removal uses the
  same replace-and-restart procedure and closes live streams at the next one-second check.
- Global disable: stop the gateway and Cloudflared services. The loopback host daemon remains
  private and the public route has no healthy origin.
- Rollback: stop the gateway, reinstall the previously accepted wheel by its recorded digest,
  restore its matching two credential files, restart, and require a fresh snapshot after the
  producer epoch changes.

## Scan and mutation evidence

The frozen wheel was searched, without printing values, for the three available encrypted
Development credential values; matches: zero. Candidate gateway source, contract, service unit,
and conformance fixtures were structurally scanned for deferred-environment origins, audiences,
instance IDs, secret variable names, state access, and generic external mutation clients;
matches: zero. The candidate build and tests made zero provider, DNS, Tunnel, identity, or other
external mutations.

Infra must repeat its exact-value scan after materializing the two host credential files and
before starting either service. It must also verify the installed wheel digest, candidate commit,
`gateway.v1`, and the matched UI handoff before any provider plan or apply.
