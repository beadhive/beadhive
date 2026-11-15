# HQ lifecycle port: call-site inventory and port contract

- **Status:** Proposed (design for `bh-l8q86`; inventory taken on `main` at 0.36.0)
- **Date:** 2026-11-12
- **Bead:** `bh-tekmw`. **Epic:** `bh-l8q86` (HQ port: SQL and hq-service adapters for bh's
  HQ calls).
- **Consumers of this contract:** `bh-v3dyp` (move the SQL code behind the port, no behaviour
  change), `bh-g76hp` (hq-service adapter over the Gateway hq plane), `bh-j99c8` (that adapter
  verifies signed authority and the restore floor), `bh-u0osy` (heartbeat sender through the
  port).
- **Upstream route work:** `beadhive/hq-service` slices `bh-hq-e9z` (liveness), `bh-hq-3sa`
  (admission), `bh-hq-9lh` (reads), `bh-hq-mz3` (operator publication); `beadhive-gateway`
  `bh-gw-ssn0` (route table and scopes) and `bh-gw-27js` (attestation claims).
- **Inputs:** gateway ADR 0005 (rev 3, HQ write service; cited through `hq-service`
  `docs/DESIGN.md`, not present in this checkout of `beadhive-gateway`);
  [hq-storage-port-adr.md](hq-storage-port-adr.md);
  [frame-dolt-server-hq-mode-adr.md](frame-dolt-server-hq-mode-adr.md);
  [HQ.md](../HQ.md).

## Why a second port

ADR 0005 makes hq-service the single writer of HQ state. Every caller reaches it through the
Gateway `hq` plane, and no host except the HQ host opens the HQ database. Today bh opens the HQ
`dolt sql-server` itself, through `hq_sql_transport.connect`, from about a dozen classes and
functions. Each of them needs its own SQL login and secret.

The existing [HQ storage port](hq-storage-port-adr.md) (`beadhive_hq_storage`) does not solve
this. It sits *below* the SQL code: it abstracts statements and the Dolt adapter, and every
`hq_sql_*` class still opens its own connection and runs its own transaction around it. The
port in this document sits one level *up*. It is the set of HQ **lifecycle operations** bh
performs, such as "renew my session", "read my verified eligibility" or "publish this signed
authority under CAS". Each operation is one hq.lifecycle.v0 route in the service adapter and
one of today's transactions in the SQL adapter.

```text
SqlControlPlane / CLI verbs / heartbeat sender      <- business rules stay here
            |
       HQ lifecycle port  (this document)
        /                      \
  SQL adapter (default)      hq-service adapter
  = today's hq_sql_* classes   = HTTP to the Gateway hq plane (hq.lifecycle.v0)
        |
  HQ storage port (beadhive_hq_storage) -> Dolt adapter
```

## Method

The inventory was built from the code, not from memory:

1. Every caller of `hq_sql_transport.connect`:
   `rg "hq_sql_transport|connect\(binding|connect\(observer" src/beadhive`.
2. Every constructor of an opener class: `SqlRuntimeAuthority(`, `SqlRuntimeOperator(`,
   `SqlFleetConfigRevisionStore(`, `SqlPlacementDirector(`, `SqlTrustedReceiver(`,
   `ReleaseCatalogStore(`, `hq_connection_factory(`, `placement_storage(`, `config_history(`.
3. Every caller of `control_plane()`, `select_plane()`, `operator_plane()`,
   `attach_fleet_config()` and `placement_director()`, then each `plane.<method>(` they call.

**Excluded, with reasons.** `hive_forward.connect` uses the same transport but opens a *hive*
primary's Dolt server, not HQ. `dolt_remote_credentials`, `beadlog_store_credentials` and
`backfill_judge_setup` only use `FnoxBroker` to resolve secrets. `hq_git_broker` is the Git HQ
broker socket. `GitControlPlane` never opens SQL.

## 1. Openers: where bh opens the HQ SQL server

These are the only places that call `hq_sql_transport.connect` for HQ. Every row below in
section 2 reaches the server through one of them.

| # | Opener (`file:function`) | SQL binding (login) | Runs on |
|---|---|---|---|
| O1 | `hq_sql_runtime.py:SqlRuntimeAuthority._open` | `hq.sql.runtime` (the frame's own principal) | every frame |
| O2 | `hq_sql_runtime.py:SqlRuntimeAuthority.fresh_config_head_fence`, `fresh_runtime_head_fence`, `fresh_public_observation_fence`, `fresh_hive_lease_fence` | `observer`, else `runtime` or `authority_writer`; `reader` for the config fence (a second connection, outside the read snapshot) | every frame, operator, receiver |
| O3 | `hq_sql_operator.py:SqlRuntimeOperator._open` | `authority_writer` | operator host (operator settings file) |
| O4 | `hq_sql_config.py:SqlFleetConfigRevisionStore._open` | `reader` / `publisher` (config database) | every host with `hq.sql` |
| O5 | `hq_storage_composition.py:hq_connection_factory` (via `config_history`, `placement_storage`, `dolt_hq_storage`) | `reader`, `publisher`, `placement_writer` | config reads/publication, director |
| O6 | `heartbeat_report.py:_session_opener` | `runtime` | every frame (heartbeat sender) |
| O7 | `hq_sql_receiver.py:SqlTrustedReceiver._open` | `observer` (the trusted receiver) | HQ host only |
| O8 | `hq_release_catalog.py:ReleaseCatalogStore._open` | release-catalog writer | operator host |
| O9 | `hq_placement_ops.py:_connect` (`check`) | `placement_writer` / operator account | operator host |

## 2. Call-site inventory

Each row is a function that opens the HQ SQL server, through one of the openers above, with
what it reads (R) or writes (W) and its entry points. The **Port method** and **Route** columns
map it onto section 3. Route paths are under `/hq/lifecycle/v0`. Route status is one of:

- **live**: served by hq-service today;
- **planned**: named in an open hq-service slice bead;
- **GAP**: no route exists or is planned yet;
- **local**: HQ-host-only work that stays on SQL and does not go behind the port.

### 2.1 Liveness (frame side)

| Call site (`file:function`) | Reads / writes | Entry points | Port method | Route (status) |
|---|---|---|---|---|
| `hq_sql_session.py:SessionRenewer.tick` (O6) | R own `_session` route (`resolve_route`); W one `UPDATE` of the frame's session row, server-stamped | `heartbeat_sender._renew` (`heartbeat_sender renew [--loop]`), via `heartbeat_report.session_renewer` | `HqLiveness.renew_session` | `POST /sessions/{principal}/{epoch}/renew` (planned, `bh-hq-e9z`) |
| `hq_sql_session.py:publish_evidence` (O6), called by `heartbeat_report.py:publish_session_evidence` | R own route (`resolve_route`, a separate connection first); W one `UPDATE` of the frame's evidence row | `heartbeat_sender._publish_evidence` (after a conformance run) | `HqLiveness.publish_evidence` | `POST /sessions/{principal}/{epoch}/evidence` (planned, `bh-hq-e9z`) |
| `hq_control_plane.py:SqlControlPlane.heartbeat` (O1) | R `load_state`, `load_frame_binding`; W signed FrameLease envelope into the frame's inbox (`publish_inbox("heartbeat")`) | `bh host heartbeat`, `bh host heartbeat-send`, `heartbeat_sender beat` (`heartbeat_report.send_cached_beat`) | `HqLiveness.submit_heartbeat` | `PUT /heartbeats/{request_id}` (live) |
| `hq_control_plane.py:SqlControlPlane.propose_hive_lease` (O1) | R `load_state`, `load_frame_binding`; W signed hive-lease request into the inbox (`publish_inbox("hive_lease")`) | `SqlControlPlane.publish_hive_lease` | `HqLiveness.submit_hive_lease` | `POST /hive-leases/{prefix}` (live) |
| `hq_control_plane.py:SqlControlPlane.publish_hive_lease` (O1, O2) | R placement (`require_frame_placeable`), then polls `read_public_result` for the receiver's verdict | `host_lease.adopt` / `renew` / `release` (`bh host adopt`, `release`, `packup`) | `HqLiveness.submit_hive_lease` + `HqLiveness.read_result` | `POST /hive-leases/{prefix}` (live; the response is the verdict, so no poll) |
| `hq_sql_runtime.py:SqlRuntimeAuthority.read_public_result` (O1) | R `hq_live_results` row for one request id, no private receipts | `SqlControlPlane.publish_hive_lease` | `HqLiveness.read_result` | none needed: the service answers synchronously |
| `hq_sql_runtime.py:SqlRuntimeAuthority.publish_inbox` (O1) | W one row into the principal's routed inbox table | `heartbeat`, `propose_hive_lease`, `publish_registration_evidence` | adapter-internal (the three `submit_*` methods) | carried by the three POST/PUT routes |

### 2.2 Admission (incarnation, manifest, registration, admit)

| Call site (`file:function`) | Reads / writes | Entry points | Port method | Route (status) |
|---|---|---|---|---|
| `hq_sql_operator.py:SqlRuntimeOperator.provision_release_route` (O3) | R authority head + verified state; W per-incarnation tables and stamp triggers (`resume_incarnation`), `CREATE USER` + password, `GRANT SELECT` on the runtime and three config tables | `frame_release_upgrade.release_upgrade` (`bh host release-upgrade ... apply`) | `HqAdmission.register_incarnation` | `POST /incarnations` (planned, `bh-hq-3sa`; no login or secret is created) |
| `hq_sql_operator.py:SqlRuntimeOperator.describe_release_route` (O3) | R which of inbox/session/evidence tables exist; R latest registry route for the frame | `frame_release_upgrade.release_upgrade` (plan and apply) | `HqAdmission.describe_incarnation` | **GAP** (could fold into the `POST /incarnations` plan response) |
| `hq_sql_operator.py:SqlRuntimeOperator.preflight` (O3) | R own grants and probe reads (`hq_sql_capabilities.report`); never writes | `frame_release_upgrade.release_upgrade` | `HqAdmission.preflight` | **GAP** (only meaningful for the SQL adapter; the service adapter can return "not applicable") |
| `hq_control_plane.py:SqlControlPlane.publish_registration` (O1) | R `read_frame_composite` (snapshot for `hosts/<id>.yaml` when given a host id) | `hq.publish_host_manifest` (`bh host init`, `bh hq` registration, `host_provision._step_hq_publish`) | `HqAdmission.submit_registration` | `POST /registrations` (live) |
| `hq_control_plane.py:SqlControlPlane.publish_registration_evidence` (O1) | R `read_frame_composite`; W signed registration envelope into the inbox (`publish_inbox("registration")`) | `publish_registration` | `HqAdmission.submit_registration` | `POST /registrations` (live) |
| `fleet_roster.py:save` -> `config_store.publish_fleet_document` -> `SqlFleetConfigRevisionStore.publish_snapshot` (O4, O5) | R committed config snapshot; W one `hosts/<host_id>.yaml` document under the snapshot's revision CAS | `bh host init` (`host_cli` manifest write) | `HqAdmission.publish_manifest` | `PUT /manifests/{host_id}` (planned, `bh-hq-3sa`) |
| `fleet_roster.py:remove` (O4, O5) | W delete of `hosts/<host_id>.yaml` under revision CAS | `bh host rm`, `bh host retire` (`host_retire`) | `HqAdmission.withdraw_manifest` | **GAP** (no `DELETE /manifests/{host_id}`) |
| `hq_control_plane.py:SqlControlPlane.admit_open` -> `grant(admitted=True)` (O3) | R `operator.load`, committed snapshot; W signed authority CAS with a trusted (open) grant, and the registry route and floor seed when a route was provisioned | `hq_open_admission.join` (`bh hq authority join`, `hq._open_admission` during `bh host init`) | `HqAdmission.admit` (`kind="trusted"`) | `POST /admissions/{frame}` (planned, `bh-hq-3sa`) |
| `hq_control_plane.py:SqlControlPlane.grant` (O3) | R `operator.load`; W signed authority CAS adding a candidate grant | `bh hq authority grant --record` | `HqAdmission.admit` (`kind="candidate"`) | `POST /admissions/{frame}` (planned, `bh-hq-3sa`) |

### 2.3 Reads

| Call site (`file:function`) | Reads / writes | Entry points | Port method | Route (status) |
|---|---|---|---|---|
| `hq_sql_runtime.py:SqlRuntimeAuthority.load_state` (O1) | R authority row at HEAD, signature, `validate_state`, expiry, restore floor (`_check_floor`) | `SqlControlPlane.authority_status` (`bh hq authority status/check`, `hq_authority_expiry.warn_if_expiring` in `bh plan`, intake and validation admission; the herdr operator view), `heartbeat`, `propose_hive_lease`, `hq_authority_expiry.read_state`, `hq_authority_fence_notice.probe_authority` | `HqReads.read_authority` | `GET /authority` (live) |
| `hq_sql_operator.py:SqlRuntimeOperator.load` (O3) | Same verified authority read, through the writer login, expired allowed | every operator verb (rebind, grant, observe, lifecycle, release-policy, release-upgrade, join, fleet mode, bind-beadyard), `hq_authority_expiry.read_state` | `HqReads.read_authority(allow_expired=True)` | `GET /authority` (live; the operator principal reads it with scope `lifecycle:read`) |
| `hq_sql_runtime.py:SqlRuntimeAuthority.load_frame_binding` (O1) | R this principal's registry route (frame, holder, instance, epoch, inbox table, signer) at HEAD | `heartbeat`, `propose_hive_lease`, `heartbeat_report` measured beat | `HqReads.read_frame_anchor` | **GAP** (should ride on `GET /eligibility/{frame}`; see section 4) |
| `hq_sql_runtime.py:SqlRuntimeAuthority.read_frame_composite` (O1, O2) | R in one snapshot: authority, route, grant slot and record, committed config snapshot, signed hive policies, observation (session/evidence via `hq_sql_session.read_eligibility`, or inbox beat), optional hive-lease row and evidence; then head fences | see the next rows | `HqReads.read_eligibility` | `GET /eligibility/{frame}` (planned, `bh-hq-9lh`) |
| `hq_control_plane.py:SqlControlPlane.read_eligibility` / `observe` | via `read_frame_composite(session_liveness=True)` plus `read_release_catalog_row` | `frame_eligibility.load_facts` (`bh host eligible`, `require_eligible`, intake admission), `host_heartbeat` observer | `HqReads.read_eligibility` | `GET /eligibility/{frame}` (planned) |
| `hq_control_plane.py:SqlControlPlane.eligibility_authority_status` | via `read_frame_composite` | `frame_eligibility.evictable` | `HqReads.read_eligibility` | `GET /eligibility/{frame}` (planned) |
| `hq_control_plane.py:SqlControlPlane.fetch_config`, `watch_state`, `load_config_authority_snapshot`, `heartbeat_reference` | via `read_frame_composite` | `host_heartbeat.watch_state` (daemon), `bh host heartbeat`, config authority snapshot | `HqReads.read_eligibility` | `GET /eligibility/{frame}` (planned) |
| `hq_control_plane.py:SqlControlPlane.hive_policy_binding` / `require_hive_policy` | via `read_frame_composite(prefix=...)` | `bh host eligible --hive` (`host_cli._hive_policy_predicate`), `host_adopt._require_hive_policy` | `HqReads.read_eligibility(prefix=...)` | `GET /eligibility/{frame}` (planned; needs a `prefix` parameter, see section 4) |
| `hq_control_plane.py:SqlControlPlane.read_hive_lease_record` / `read_hive_lease` | via `read_frame_composite(prefix=..., hive_lease_epoch=...)`: hive-lease row, signed evidence, incumbent | `frame_eligibility.incumbent_primary`, `host_lease.read_record`, `localloop` lease probe, `host_cli._scan_leases` (`packup`, `rm`), `bh host eligible --hive` | `HqReads.read_eligibility(prefix=...)` | **GAP** (no hive-lease read; `GET /eligibility/{frame}` would need the lease row) |
| `hq_control_plane.py:SqlControlPlane.read_placement` / `require_frame_placeable` | via `read_frame_composite(session_prefix=...)`: director placement row | `publish_hive_lease`, `host_adopt` placement check | `HqReads.read_eligibility(session_prefix=...)` | `GET /eligibility/{frame}` (planned; placement holder is one of its inputs) |
| `hq_sql_runtime.py:SqlRuntimeAuthority.read_release_catalog_row` (O1) | R the release catalog row for a measured digest `AS OF` the verified head | `SqlControlPlane.read_eligibility`, `heartbeat_report` (measured beat release admission) | `HqReads.read_release_catalog_row` | **GAP** (or part of the eligibility payload, to settle in `bh-hq-9lh`) |
| `hq_sql_config.py:SqlFleetConfigRevisionStore.load_snapshot` / `load_snapshot_or_cached` (O4, O5) | R committed `hq_config_*` documents at HEAD (or a revision), the config restore floor, then the cached snapshot in trusted mode | `hq_control_plane.attach_fleet_config` -> `config_store.load_fleet` / `fleet_snapshot` (every fleet-scope config read), `SqlControlPlane.load_host_manifest`, `fleet_roster.load` / `exists` / `manifest_paths` (`bh host list`, `bh host show`), `hq_open_admission`, `frame_release_upgrade`, `hq_authority_fleet_mode.show` | `HqReads.read_config` | `GET /config` (planned, `bh-hq-9lh`) |
| `hq_sql_config.py:SqlFleetConfigRevisionStore.authority_binding` (O4) | R bound config head vs latest, and whether every commit between them is a tolerated edit | `hq_authority_expiry._config_bound`, `hq_authority_fleet_mode.refresh`, `hq_authority_fence_notice.probe_authority` | `HqReads.read_history` | `GET /history?since={floor}` (planned, `bh-hq-9lh`) |
| `hq_sql_runtime.py:SqlRuntimeAuthority._check_floor`, `_check_provisioning_descendant` (inside O1/O3 reads) | R `dolt_log` ancestry and `DOLT_DIFF_SUMMARY` since the local floor | every verified read above | adapter-internal (`bh-j99c8`) | `GET /history?since={floor}` (planned) |

### 2.4 Operator publication

| Call site (`file:function`) | Reads / writes | Entry points | Port method | Route (status) |
|---|---|---|---|---|
| `hq_control_plane.py:SqlControlPlane.renew` -> `SqlRuntimeOperator.publish` (O3, O2) | R `operator.load`, latest config, hive-policy projection; W signed authority CAS (`revision+1`), Dolt commit, head fences, read-back | `bh hq authority rebind` (and deprecated `renew`), `hq_authority_fleet_mode.set_fleet_mode` | `HqOperator.rebind` | rebind plan/apply (planned, `bh-hq-mz3`; path not yet named) |
| `hq_control_plane.py:SqlControlPlane.release_policy` -> `frame_release_policy.release_policy` (O3) | R `operator.load`, `attested_releases`; W signed authority CAS | `bh hq authority release-policy plan/apply` | `HqOperator.release_policy` | release-policy plan/apply (planned, `bh-hq-mz3`; path not yet named) |
| `hq_sql_operator.py:SqlRuntimeOperator.attested_releases` (O3) | R signed registration evidence of the frame's readers | `frame_release_policy.release_policy` | `HqOperator.release_policy` (plan step) | planned with release-policy (`bh-hq-mz3`) |
| `hq_control_plane.py:SqlControlPlane.accept_observation` (O3) | R `operator.load`, `operator.evidence` (registration and consecutive accepted receipts); W signed authority CAS | `bh hq authority observe` | `HqOperator.publish_authority` | **GAP** |
| `hq_sql_operator.py:SqlRuntimeOperator.evidence` (O3) | R protected registration and observer receipts | `accept_observation`, `lifecycle` | `HqOperator.read_evidence` | **GAP** |
| `hq_control_plane.py:SqlControlPlane.lifecycle` (O3) | R `operator.load`, `operator.evidence`; W signed authority CAS for the verb | `bh host admit/cordon/drain/park/resume/quarantine`, `frame-retire`, `emergency-admit`, `emergency-revoke` | `HqOperator.publish_authority` | **GAP** (only the admit transition is near `POST /admissions/{frame}`) |
| `hq_control_plane.py:SqlControlPlane.release_upgrade` -> `frame_release_upgrade.release_upgrade` (O3, O4) | R `operator.load`, config snapshot, `describe_release_route`, `preflight`; W `provision_release_route`, config publication, signed authority CAS with `release_upgrade` and `provisioned_route` | `bh host release-upgrade plan/apply` | `HqAdmission.register_incarnation` + `HqOperator.publish_authority` | **GAP** for the authority step (`POST /incarnations` covers provisioning) |
| `hq_control_plane.py:SqlControlPlane.bind_beadyard` (O3, O4) | R config snapshot, `operator.load`; W signed authority CAS binding the beadyard id | `bh hq authority bind-beadyard` | `HqOperator.publish_authority` | **GAP** |
| `hq_sql_config.py:SqlFleetConfigRevisionStore.publish_snapshot` / `repair_snapshot` (O4, O5) | R snapshot and authority probe; W `hq_config_*` documents under revision CAS, Dolt commit and push; post-commit fence notice | `config_store.publish_fleet_document` / `save_fleet` (`bh config set --scope fleet` and every fleet-scope edit), `hq_authority_fleet_mode.set_fleet_mode`, `hq_beadyard` adopt/bind, `frame_release_upgrade` | `HqOperator.publish_config` | **GAP** (config publication is a later hq-service slice; `PUT /manifests/{host_id}` covers host documents only) |
| `hq_sql_config.py:SqlFleetConfigRevisionStore.recover_publication` (O4, O5) | R the publication witness for an unknown-outcome publish | `hq_beadyard` recovery | `HqOperator.recover_publication` | **GAP** |
| `hq_control_plane.py:SqlControlPlane.prune_inbox` -> `SqlRuntimeOperator.prune_inbox` (O3) | R inbox rows; W deletes past retention | `bh hq authority prune-inbox` | none (inbox retirement) | local |
| `hq_sql_placement.py:SqlPlacementDirector.read` / `survey` / `place` / `failover_policy` / `set_failover_policy` / `release` (O5) | R verified authority, registry, session ages; W placement and failover-policy rows | `bh hq placement`, `director_failover` (host daemon), `doctor` placement check | `HqOperator` placement methods | **GAP** (no placement route; director stays on SQL until one exists) |
| `hq_placement_ops.py:check` (O9) | R `mysql.user`, other accounts' grants, triggers | `bh hq placement check` | none | local |
| `hq_release_catalog.py:ReleaseCatalogStore.rows` / `add` / `revoke` (O8) | R catalog rows; W append-only catalog rows | `bh hq release-catalog import/revoke/verify` | `HqOperator` catalog methods | **GAP** |

### 2.5 HQ-host-local work (stays on SQL, outside the port)

| Call site (`file:function`) | Reads / writes | Entry points | Why it stays local |
|---|---|---|---|
| `hq_sql_receiver.py:SqlTrustedReceiver.accept_hive_lease` / `accept_heartbeat` / `accept_registration` / `recover_heartbeat` (O7) | R inbox, authority, receipts; W results, receipts, floors, observations | integration tests and the receiver script | hq-service *is* this receiver (slices 1 to 3); nothing on a frame calls it |
| `hq_sql_session_provision.py:provision_liveness_schema` / `provision_incarnation` | W liveness schema, incarnation tables, accounts | server-local operator tooling and tests | schema bootstrap on the HQ host |
| `hq_sql_operator.py:SqlRuntimeOperator.provision_release_catalog` (O3) | W catalog table DDL | `bh hq release-catalog provision` | schema bootstrap on the HQ host |
| `hq_sql_config.py:SqlFleetConfigRevisionStore.inspect_initial_destination` / `initialize_snapshot` / `recover_initial_snapshot` / `inspect_raw_for_repair` (O4, O5) | R/W the empty config destination and first seed | `hq_seed` via `hq_migrate_cli.migrate_cmd` (`bh hq migrate`) | one-time migration on the HQ host |

## 3. The port

One port, four slice-shaped protocols. A single adapter class implements all four, and one
`hq.route: sql | api` setting per host selects the adapter (`bh-g76hp`). There is no silent
fallback between adapters.

### 3.1 Rules every method follows

- **Verified results only.** A method returns values its adapter has already verified the
  way a frame verifies them today: the authority signature in signed mode, `validate_state`,
  expiry, and the restore floor (sequence and head). The SQL adapter does this with
  `verified_state_at` and `_check_floor`. The hq-service adapter does it over the response plus
  `GET /history?since={floor}` (`bh-j99c8`). Callers never see an unverified record, and the
  floor is not a port method.
- **Keys never cross the port boundary.** Methods that publish signed records take a
  `signer` callable, not a key. The SQL adapter calls it inside its transaction, as
  `SqlRuntimeOperator.publish` signs today. The hq-service adapter calls it between a plan read
  and an apply. That is the plan/apply split `bh-hq-mz3` describes, since the signed body
  includes the config head and the hive-policy projection the service reads. Trusted mode
  passes a signer that returns `UNSIGNED_SIGNATURE`.
- **Identity comes from the caller, not from arguments.** Frame methods carry no principal.
  The SQL adapter derives it from its login (`runtime` binding). The hq-service adapter derives
  it from the attested caller, and the Gateway checks that path parameters naming a frame, host
  or principal match the caller's binding (`bh-gw-jozq`).
- **Every method takes `deadline`** (a `time.monotonic()` instant), as every opener does
  today. A spent deadline raises `HqDeadline`, never `HqUnavailable` (bh-ktw0o).
- **One error taxonomy** (section 3.3). Both adapters map their failures onto it, so callers
  stop catching `SqlRuntimeError`, `SqlOperatorError` and `SqlConfigError` separately.

### 3.2 Protocols

This is the contract `bh-v3dyp` implements with today's code and `bh-g76hp` implements over
HTTP. Types named here and not yet in `src/` (`FrameAnchor`, `FrameComposite`, `Submission`,
`HistorySinceFloor`, `AdmissionRequest`, `Signer`) are new value objects. Their fields are the
tuples the SQL code returns today.

```python
from typing import Callable, Literal, Protocol

Signer = Callable[[dict], str]  # signs a canonical authority record in-process


class HqLiveness(Protocol):
    """Frame-side liveness. Scope lifecycle:session / lifecycle:heartbeat / lifecycle:hive-lease."""

    def renew_session(self, anchor: FrameAnchor, *, deadline: float) -> SessionRoute:
        """One server-stamped renewal of this incarnation's session row.
        SQL: SessionRenewer.tick.  API: POST /sessions/{principal}/{epoch}/renew."""

    def publish_evidence(
        self, anchor: FrameAnchor, report: EvidenceReport, *, deadline: float
    ) -> SessionRoute:
        """One server-stamped update of this incarnation's evidence row.
        SQL: hq_sql_session.publish_evidence.  API: POST /sessions/{principal}/{epoch}/evidence."""

    def submit_heartbeat(
        self, anchor: FrameAnchor, envelope: dict, *, request_id: str, deadline: float
    ) -> Submission:
        """A signed FrameLease envelope.
        SQL: publish_inbox("heartbeat") -> pending.  API: PUT /heartbeats/{request_id}."""

    def submit_hive_lease(
        self, anchor: FrameAnchor, prefix: str, envelope: dict, *, deadline: float
    ) -> Submission:
        """A signed hive-lease request.
        SQL: publish_inbox("hive_lease") -> pending.  API: POST /hive-leases/{prefix}."""

    def read_result(self, submission: Submission, *, deadline: float) -> Submission:
        """The verdict of a pending submission.
        SQL: read_public_result.  API: returns `submission` unchanged (already final)."""


class HqAdmission(Protocol):
    """Incarnation, manifest, registration and admission.
    Scopes lifecycle:provision / lifecycle:fleet / lifecycle:registration / lifecycle:admission."""

    def describe_incarnation(
        self, principal: str, epoch: int, frame: str, *, target_route: str | None, deadline: float
    ) -> dict:
        """SQL: SqlRuntimeOperator.describe_release_route.  API: GAP."""

    def preflight(self, *, route: str, deadline: float) -> dict:
        """SQL: SqlRuntimeOperator.preflight.  API: GAP (adapter reports not applicable)."""

    def register_incarnation(
        self, route: tuple, *, expected_revision: str, frame_address: str, deadline: float
    ) -> str:
        """Session/evidence tables and stamp triggers for one incarnation; returns the new head.
        SQL: provision_release_route (also creates the login).  API: POST /incarnations
        (no login, no secret; `frame_address` and the password are SQL-adapter-only inputs)."""

    def publish_manifest(self, manifest: HostManifest, *, expected_revision: str,
                         deadline: float) -> str:
        """SQL: publish_fleet_document("hosts/<id>.yaml").  API: PUT /manifests/{host_id}."""

    def withdraw_manifest(self, host_id: str, *, expected_revision: str, deadline: float) -> str:
        """SQL: publish_fleet_document(path, None).  API: GAP."""

    def submit_registration(self, anchor: FrameAnchor, envelope: dict, *,
                            deadline: float) -> Submission:
        """SQL: publish_inbox("registration").  API: POST /registrations."""

    def admit(self, frame: str, request: AdmissionRequest, *, signer: Signer,
              expected_revision: str, deadline: float) -> str:
        """Trusted (open) or candidate admission; returns the new authority head.
        SQL: SqlControlPlane.admit_open / grant -> SqlRuntimeOperator.publish.
        API: POST /admissions/{frame} (principal from committed authority)."""


class HqReads(Protocol):
    """Verified reads. Scope lifecycle:read (and lifecycle:authority for GET /authority)."""

    def read_authority(self, *, allow_expired: bool = False,
                       deadline: float) -> tuple[str, dict, tuple, dict]:
        """(head, state, config crossref, hive policies).
        SQL: SqlRuntimeAuthority.load_state (frame) / SqlRuntimeOperator.load (operator).
        API: GET /authority."""

    def read_frame_anchor(self, *, expected_head: str | None, deadline: float) -> FrameAnchor:
        """This caller's registry route: principal, frame, holder, instance, epoch, carrier.
        SQL: load_frame_binding.  API: GAP (proposed: part of GET /eligibility/{frame})."""

    def read_eligibility(
        self,
        frame: str,
        *,
        prefix: str | None = None,
        session_prefix: str | None = None,
        hive_lease_epoch: int | None = None,
        deadline: float,
    ) -> FrameComposite:
        """Authority, route, grant slot and record, config snapshot, signed policies,
        observation, optional hive lease, read at one point.
        SQL: read_frame_composite.  API: GET /eligibility/{frame}."""

    def read_release_catalog_row(self, head: str, catalog: str, measured: dict, *,
                                 deadline: float) -> dict | None:
        """SQL: SqlRuntimeAuthority.read_release_catalog_row.  API: GAP."""

    def read_config(self, *, revision: str | None = None, cached_ok: bool = False,
                    deadline: float) -> FleetConfigSnapshot:
        """SQL: SqlFleetConfigRevisionStore.load_snapshot(_or_cached).  API: GET /config."""

    def read_history(self, *, since: str, deadline: float) -> HistorySinceFloor:
        """Commits and diff summary since `since`, for the caller's own append-only check
        and the config-edit tolerance (authority_binding).
        SQL: dolt_log / DOLT_DIFF_SUMMARY.  API: GET /history?since={floor}."""


class HqOperator(Protocol):
    """Operator publication. Scope lifecycle:operator. Every write is a CAS on
    `expected_revision`, signed in-process by `signer`."""

    def rebind(self, state: dict, *, signer: Signer, expected_revision: str,
               deadline: float) -> str:
        """SQL: SqlRuntimeOperator.publish.  API: rebind plan/apply (bh-hq-mz3)."""

    def release_policy(self, frame: str, state: dict, *, signer: Signer,
                       expected_revision: str, deadline: float) -> str:
        """SQL: SqlRuntimeOperator.attested_releases + publish.
        API: release-policy plan/apply (bh-hq-mz3)."""

    def publish_authority(
        self,
        state: dict,
        *,
        signer: Signer,
        expected_revision: str,
        provisioned_route: tuple | None = None,
        release_upgrade: dict | None = None,
        deadline: float,
    ) -> str:
        """Any other signed authority transition: observe, lifecycle verbs, bind-beadyard,
        release-upgrade.  SQL: SqlRuntimeOperator.publish.  API: GAP."""

    def read_evidence(self, frame: str, holder_identity: str, *, expected_revision: str,
                      deadline: float) -> tuple:
        """SQL: SqlRuntimeOperator.evidence.  API: GAP."""

    def publish_config(self, documents: tuple, *, expected_revision: str,
                       deadline: float) -> FleetConfigSnapshot:
        """SQL: SqlFleetConfigRevisionStore.publish_snapshot.  API: GAP."""

    def recover_publication(self, publication_id: str, *, expected_revision: str,
                            deadline: float):
        """SQL: SqlFleetConfigRevisionStore.recover_publication.  API: GAP."""


class HqPort(HqLiveness, HqAdmission, HqReads, HqOperator, Protocol):
    """What `hq.route` selects: one adapter object per host."""
```

`Submission` carries `request_id`, `digest`, `outcome` (`pending`, `accepted`, `replayed` or
`refused`), `revision` and `refusal` (a stable code). The SQL adapter returns `pending` from a
`submit_*` method and `read_result` resolves it. The hq-service adapter returns the final
outcome directly. Callers that wait for a verdict, as `publish_hive_lease` does today, call
`read_result` either way.

`FrameAnchor` is today's `PrincipalBinding` plus the authority head it was read at. The SQL
adapter needs both for `publish_inbox`'s `expected_head` and routed-table check. The
hq-service adapter needs `principal` and `epoch` for the session paths.

### 3.3 Errors

| Port error | Meaning | SQL adapter source today | hq-service adapter source |
|---|---|---|---|
| `HqRefused(code, reason)` | A definite, stable refusal; nothing was written | `SqlRuntimeFloorRefusal`, `SqlRuntimeCapabilityMissing`, `SqlOperatorError` before commit, `ControlPlaneError` identity mismatches, receiver refusals in `read_result` | `409 {outcome: refused, refusal: <code>}` and the codes in `conformance/*/v1/refusal-codes.json` |
| `HqUnavailable` | HQ could not be reached or read; nothing was written | `SqlRuntimeUnavailable`, `SqlConfigUnavailable`, `SqlTransportError` | connect failure, Gateway refusal before the service, and `5xx` on a read, an idempotent submission (heartbeat, hive lease) or the plan phase of a plan/apply write |
| `HqDeadline` | The caller's deadline ran out | `SqlRuntimeDeadline`, transport deadline | client timeout on the deadline |
| `HqOutcomeUnknown(expected_revision)` | The write may have committed | `AuthorityPublicationUnknown`, `PublicationUnknown`, `ReceiverUnknown` | timeout or connection loss after the request body was sent, and any `5xx` (including `500`) on a non-idempotent write (register incarnation, publish manifest, admit, plan/apply apply phase): a Gateway `502/503/504` can follow the commit, and a `500` can be a post-commit error, so "nothing was written" cannot be claimed |

`HqRefused.code` uses the hq-service refusal-code vocabulary where a code exists. That gives
`bh-g76hp` its "refusals map to stable codes" criterion. The SQL adapter maps its existing
fixed messages onto the same codes and keeps the message as `reason`, so no user-visible text
changes in `bh-v3dyp`.

## 4. Gaps

These call sites have no hq.lifecycle.v0 route, planned or live. Until a route exists they
keep the SQL adapter: `hq.route: api` refuses them with `HqUnavailable("no hq-service route
for <method>")`, and never falls back to SQL.

| Gap | Port method | Callers blocked on `api` | Suggested owner and shape |
|---|---|---|---|
| G1 | `HqReads.read_frame_anchor` | heartbeat, hive-lease and registration submits; session renew/evidence path parameters | `bh-hq-9lh`: return the caller's route (principal, epoch, carrier, signer fingerprint) in `GET /eligibility/{frame}` |
| G2 | `HqReads.read_eligibility(prefix=..., hive_lease_epoch=...)` hive-lease row and evidence | `read_hive_lease_record`, `host_lease`, `localloop`, `bh host packup/rm`, `incumbent_primary` | `bh-hq-9lh`: `?prefix=` on `GET /eligibility/{frame}`, or `GET /hive-leases/{prefix}` |
| G3 | `HqReads.read_release_catalog_row` | eligibility release admission, measured beat | `bh-hq-9lh`: include the matched catalog row in the eligibility payload |
| G4 | `HqAdmission.describe_incarnation`, `HqAdmission.preflight` | `bh host release-upgrade` | `bh-hq-3sa`: a plan response from `POST /incarnations` |
| G5 | `HqAdmission.withdraw_manifest` | `bh host rm`, `bh host retire` | `bh-hq-3sa`: `DELETE /manifests/{host_id}` |
| G6 | `HqOperator.publish_authority` | `bh hq authority observe`, `bind-beadyard`, `bh host admit/cordon/drain/park/resume/quarantine/frame-retire/emergency-*`, `release-upgrade` | `bh-hq-mz3`: one generic signed-authority CAS (`{record, signature, expected_revision}`) covers all of these, since the client already builds and signs the state; rebind and release-policy become special cases |
| G7 | `HqOperator.read_evidence` | `observe`, lifecycle verbs | `bh-hq-mz3`: operator read of registration and receipts |
| G8 | `HqOperator.publish_config`, `recover_publication` | every fleet-scope config edit, fleet mode, beadyard binding | a config-publication slice (named in hq-service `docs/DESIGN.md`, not yet a bead) |
| G9 | Placement (`SqlPlacementDirector`) | `bh hq placement`, director failover, doctor | not yet planned; the director runs on the HQ host today, so it can stay SQL |
| G10 | Release catalog (`ReleaseCatalogStore`) | `bh hq release-catalog import/revoke/verify` | not yet planned; operator-host tool |

G1 matters most. Without it, a frame on the hq-service adapter cannot fill the
`/sessions/{principal}/{epoch}` path, so `bh-u0osy` depends on it as much as on `bh-hq-e9z`.

## 5. Notes for the consumers

- **`bh-v3dyp`.** The SQL adapter is a thin class over the existing openers. It keeps every
  transaction boundary, fence and fixed message exactly as it is. `SqlControlPlane` keeps its
  business rules (transition tables, grant building, manifest checks) and calls the port
  instead of `_runtime_authority()`, `_operator()` and `config_store()`. `heartbeat_report`
  calls the port instead of `_session_opener`. No test should need changes.
- **`bh-g76hp`.** Methods marked GAP raise as described in section 4. Each implemented method
  calls exactly one route, with the caller's identity.
- **`bh-j99c8`.** Verification is a private step of the hq-service adapter: every response
  carrying authority is checked against the signature and the local floor, with
  `read_history(since=floor)` feeding the same append-only check `_check_floor` runs today.
- **`bh-u0osy`.** `heartbeat_sender` keeps its loop and error reporting. It gets
  `renew_session` and `publish_evidence` from the port, plus `read_frame_anchor` once G1 is
  closed.

## 6. Landed: the SQL adapter (`bh-v3dyp`)

`src/beadhive/hq_sql_adapter.py` is the default (`hq.route: sql`) adapter. `SqlHqAdapter` is a
thin class over today's openers, reached through `SqlControlPlane` (`_runtime_authority`,
`_operator`, `config_store`), so every transaction, fence and fixed message is unchanged. It maps
the port's value objects onto the SQL tuples and the SQL error classes onto the port taxonomy
(`SqlRuntimeDeadline` to `HqDeadline`, the `*Unavailable`/transport errors to `HqUnavailable`,
the `*Unknown` publication errors to `HqOutcomeUnknown`, everything else definite to
`HqRefused`). It registers itself with `register_sql_adapter` on import, and `select_hq_port`
imports it on demand when `sql` is chosen.

Notes: `SqlRuntimeOperator.publish` gained an `authority_signer=` argument (the port's keyless contract)
beside `operator_key`; `read_history` is not a SQL route (the restore floor is checked inside each
verified read); `register_incarnation` needs the SQL-only provisioning password at construction;
`admit` takes the already-built next authority state, because the grant rules stay in
`SqlControlPlane`. Callers are not yet re-pointed at the port.
