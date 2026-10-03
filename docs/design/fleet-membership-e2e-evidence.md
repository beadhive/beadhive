# Fleet membership cutover proof

`tests/test_fleet_membership_e2e_int.py::test_public_seed_then_signed_two_frame_lifecycle_with_one_clock`
is the composed isolated SQL-primary scenario. It creates one canonical HQ identity in a
temporary Git source, uses the public `hq_seed.plan/prepare/apply` path to initialize an
empty TLS Dolt config store, then uses a separate signed runtime schema and real SQL
inbox/receiver/operator paths. The runtime schema and test credentials are fixture
provisioning; the config seed is not fixture-inserted. A fresh selected HOST with no HQ
checkout resolves the committed config, and a separate temporary Git hive origin
publishes an issue and Dolt state. The host uses supported `bd bootstrap` to hydrate
the canonical `bh` database and reads back the same issue revision. This checks
CONFIG_READY and BEADS_READY separately; neither alone grants frame admission.

One injected receiver/control-plane clock drives authentic signed beats, operator
admission, protected lease operations, and the policy thresholds. At initial grant,
lease adoption, just before/at the exclusive heartbeat TTL, before/after the
`evict_after_s` threshold, takeover, drain, supersession, drift and quarantine, the
test compares every named eligibility predicate with its expected value and reads
the physical protected holder and epoch. It also asks the production selected-HOST
intake adapter at each admission boundary. A draining incumbent's physical lease
remains recorded while its qualified new-intake holder predicate is false. A separate
selected HOST with an unavailable SQL reader proves the picker gate returns denied,
the claim-session guard never calls its underlying claim, and the lease keeper never
calls its underlying renew; no HQ checkout or Git fallback exists in that host.
`tests/test_dispatch_process.py` separately exercises the actual per-spawn picker
callback and both dispatch backend keepers. The composed scenario does not launch a
worker child or claim a real issue through the dispatcher.

After the lifecycle, the composed scenario publishes a later valid SQL config revision
and retries the original seed intent. The recovery retains the original publication
identity and committed revision while reporting the newer observed head; it neither
reseeds nor discards the later edit.

Complementary existing real-carrier checks cover parts that cannot share the
SQL test clock:

| Acceptance edge | Existing regression |
| --- | --- |
| Real signed Git broker heartbeat admission and live-holder takeover | `tests/test_hq_authority_backend.py::test_real_cli_repeated_beats_observation_and_admission`; `test_real_foreign_holder_takeover_requires_authenticated_incumbent_evidence` (uses wall-clock age at the Git receive hook) |
| Same actor/key/incarnation legacy-to-bound history, immutable identity, wrong HQ, retired signer, lease CAS/replay | `tests/test_hq_authority_backend.py::test_protected_legacy_git_adoption_preserves_live_grant_then_binds_same_incarnation`; `test_bound_git_frame_grant_and_eligibility_reject_foreign_hq`; `test_raw_retired_key_cannot_publish_after_operator_retire`; `test_frame_hive_lease_guard_rejects_forgery_binding_and_cas_replays` |
| SQL legacy-to-bound same-incumbent lease and grant continuity, first-renew bridge, foreign beat refusal and config-head fencing | `tests/test_hq_sql_runtime_int.py::test_committed_signed_runtime_authority_and_separate_frame_grants` |
| Public latest Git-to-SQL-to-signed-Git config/workspace round trip, one UUID, original-history retry after later edits, verified writer suspension, mirror and selected-HOST readback | `tests/test_hq_seed_roundtrip_int.py::test_real_public_seed_latest_signed_git_mirror_and_selector` |
| Fresh UUID4 setup, concurrent seed intent, malformed/missing/foreign ID and bound mutation denial | `tests/test_beadyard_identity.py`; `tests/test_hq_seed.py::test_concurrent_prepare_keeps_one_logical_uuid_and_confirmed_record` |
| SQL config revision CAS/outage, and per-pick/per-spawn/renew requalification | `tests/test_fleet_config_consumers_int.py::test_normal_consumers_use_committed_revision_cas_and_never_fall_back`; `tests/test_dispatch_process.py` |

The composed test is an isolated acceptance proof, not a production cutover. It does
not change an existing Beads installation, real HOST selector, protected HQ remote,
actor, signing key, or lease. The signed Git receiver keeps its production wall
clock; the deterministic SQL test clock is passed only to the isolated in-process
SQL control plane and receiver.
