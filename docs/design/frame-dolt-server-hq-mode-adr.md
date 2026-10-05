# Frame membership HQ over Dolt SQL

**Status:** PROPOSED — draft for operator review; no deployment or mode change authorized  
**Date:** 2026-09-30  
**Decision bead:** `bh-cem7l`  
**Parent:** `bh-yzhcg` — Bead Frame fleet membership  
**Supersedes:** nothing; existing accepted ADRs remain in force

**Implementation note:** The [binding implementation amendment](#binding-implementation-amendment-bh-v0k3i)
below is the current local implementation contract. The earlier five-table logical schema,
shared hive-lease DML grants, and frame writer descriptions in this proposal are historical
design evidence, not provisioning instructions. The separate config/runtime schemas, principal
inbox grants, and trusted receiver described in the amendment replace those proposed shapes.
The binding still requires final qualification and a separate deployment handoff.

**Proposed amendment (2026-10-05, pending operator confirmation):**
[hive-writer-partitioning-adr.md](hive-writer-partitioning-adr.md) retires the trusted receiver
in `bh-v0k3i`. Placement moves to a director credential, and liveness to server-stamped
per-incarnation session and evidence rows. "Identical eligibility semantics" becomes
"identical predicates, different evidence carriers". Its `bh-v0k3i` table lists what stands.

## Context and proposed decision

Frame membership needs an authoritative configuration and admission store plus frequent signed
heartbeats. Offer two explicitly selected HQ adapters behind the same membership port:

- **Git HQ** remains the default for a fleet without a configured central SQL HQ endpoint.
  Versioned inventory/configuration and signed lease/report artifacts are synchronized through
  the existing HQ repository transport.
- **Dolt-server HQ** uses a dedicated central membership database. Configuration is versioned;
  leases and reports are live SQL working-set data excluded from Dolt commits through
  `dolt_ignore`. Clients do not clone a writable local fallback when this endpoint is down.

This proposal concerns **frame membership storage**, not moving hive issue databases, replacing
HQ's `hq-` beads, or turning the host daemon into the exclusive owner of HQ. Membership tables
have their own database and schema migration owner. Existing local Beads engine choices,
issue leases, host/hive epoch fences, and aggregation remain separate responsibilities.
The shared domain validator and eligibility evaluator operate above the transport adapters.
A later implementation must define the port and its conformance suite before claiming support.

## Logical schema and version boundary

Names below describe the proposed SQL schema; column types, indexes and migrations remain an
implementation contract. Payloads follow `frame.beadhive.ai/v1alpha1` rather than defining a
second JSON shape in this ADR. Every table has a stable primary key and schema version.

| Table | Key and minimum contents | Versioned | Writer |
| --- | --- | --- | --- |
| `frames` | `frame_id`; admitted host incarnation, lifecycle state, release ID/digest, typed capabilities, instance reference, approved signing principals/public keys | yes | operator/director |
| `frame_classes` | class ID; required release and capability constraints | yes | operator/director |
| `hives` | canonical hive ID; registration and routing requirements | yes | operator/director |
| `attachments` | frame/hive IDs; attachment status and constraints | yes | operator/director |
| `policy` | policy ID/revision; freshness, skew, trust and admission rules | yes | operator/director |
| `frame_leases` | frame/incarnation/sequence; canonical signed FrameLease envelope, signature, key fingerprint | no | frame publisher |
| `frame_reports` | frame/incarnation/report ID; canonical signed report envelope, signature, key fingerprint | no | frame publisher |

`frame_id` identifies inventory across replacements; `host_id` identifies an incarnation.
Lifecycle states are `pending`, `active`, `draining`, `drained`, `parked`, `quarantined`, and
`retired`. New frame enrollment is pending; a frame cannot admit itself by publishing a lease.
Legacy manifest decoding may default missing state to active under U1's compatibility rule;
it does not permit a newly enrolled frame to bypass admission. `stale` and `eligible` are
computed observations, never authoritative columns writable by a frame.

Install exact ignore rules for `frame_leases` and `frame_reports` **before populating either
table**; validate they have never been committed. Version and audit the ignore policy itself.
Commit configuration using an explicit table allowlist, not a blanket staging command. A
configuration publish must reject staged lease/report contents or missing ignore rules.
Ordinary SQL transaction commit makes a live write durable; it is distinct from a Dolt
version-control commit. No heartbeat performs the latter.

Dolt's documented [ignore rules](https://www.dolthub.com/docs/sql-reference/version-control/dolt-system-tables/)
exclude matching tables from staging. They are not an access-control mechanism or a promise
that ignored contents appear on a remote/replica. Keep lease tables initially untracked;
prove behavior on the pinned server before introducing configuration checkout/merge operations.
Ignored working data must not be removed by administrative clean/reset/checkout routines.

## Configuration publication and consistent reads

One authorized configuration writer serializes changes to admission, attachments and policy.
Validate the whole configuration, publish a Dolt commit, and identify the authoritative commit
revision explicitly. Readers evaluate a committed configuration snapshot plus verified live
observations; they must not use an operator's partially edited working configuration.

The HQ port returns configuration revision, observation availability, and verified envelope
identities. A publisher acknowledges a lease/report only after the SQL transaction commits;
bounded retries use its frame/incarnation/sequence or report ID to identify duplicates. There
is no acknowledgement for an in-memory enqueue. Frames do not manage branches, commit config,
change `dolt_ignore`, or invoke administrative version-control procedures.

How a pinned Dolt client reads committed configuration and live ignored data consistently,
including revision rereads during concurrent admission changes, is a required implementation
proof. Final dispatch rechecks authoritative admission and the verified current incarnation;
a successful connection or cached healthy daemon is not authority to start work.

## Grants, signatures and the direct-SQL limitation

Create one SQL principal per frame incarnation. Deliver only that principal's secret to that
frame. Grant SELECT on the five configuration tables, and INSERT/UPDATE on `frame_leases`
and `frame_reports`. Do not grant DELETE, schema changes, privilege management, writes to
configuration/system tables, branch mutation, or version-control administrative operations.
Use explicit table grants rather than database-wide privileges. Readers/verifiers receive
SELECT on live tables separately; operators/directors write configuration. Backup and schema
migration users are distinct from frame and operator accounts.

Dolt documents [table-level grants](https://www.dolthub.com/docs/sql-reference/server/access-management/).
These grants **do not restrict a frame to its own rows**. Therefore SQL authentication alone
cannot make a lease authoritative. Each lease/report must carry the frame's SSH signature,
verified against the authoritative `allowed_signers` policy, using the same canonical bytes,
SSH signing namespace, principal binding and contract validation as Git mode. SQL credentials
never replace frame signing keys. Use shared verification code; transport-specific trust
rules would make eligibility differ across modes.

The signature binds fleet identity, frame ID, host incarnation, contract version, observation
sequence and timestamps, and the full payload. Reject wrong signer/frame mappings, revoked
keys, unknown fields, expired or excessively future-dated envelopes, replayed sequences,
unadmitted incarnations, and capability/release claims that contradict admitted inventory.
Use the signed observation timestamp and policy bounds; receipt time alone cannot refresh a
replayed signed lease. Persist verifier replay state outside frame-writable tables, or require
fresh observations after verifier restart rather than silently losing replay protection.

**Residual availability risk:** a credentialed frame can overwrite another frame's row or
flood the shared tables. Signatures prevent forged eligibility; they do not prevent erasure,
replay insertion, resource exhaustion or denial of service. Append-style observation keys and
bounded retention reduce accidental collisions, but UPDATE grants still allow malicious
replacement. Treat live rows as untrusted input, enforce size/rate/resource budgets, and mark
invalid/absent observations ineligible. Never recover availability by trusting an invalid row.

**Operator choice before adoption:** approve this restricted-trust direct-SQL mode with its
availability limitation, or require an authenticated write gateway that enforces ownership and
quotas before supporting mutually untrusted frames. A SQL account name is not row isolation.
This draft recommends a gateway for vendor-hosted/untrusted frames. No production security
claim is made until adversarial cross-frame grant and procedure-access tests pass.

## Identical eligibility semantics; different availability

Both adapters use the same evaluator. Scheduling requires an admitted active frame, the
current authorized incarnation and signer, a fresh valid signed lease, an allowed attachment,
matching release and required capabilities/trust/harness, and remaining capacity. Draining
frames accept no new sessions; other non-active states are ineligible. Missing or unavailable
facts, stale policy/configuration, invalid signatures and ambiguous incarnation identity all
fail closed. Exact TTL/skew/capacity rules are defined once by the membership contract.
Existing issue leases and host/hive fences still apply after frame selection.

| Guarantee | Git HQ | Dolt-server HQ |
| --- | --- | --- |
| Configuration history | repository commits and authorized publication | authorized Dolt commits in the membership database |
| Lease authenticity | shared SSH verifier over signed artifacts | identical verifier over signed SQL envelopes |
| Observation freshness | bounded by synchronization plus lease/policy TTL | bounded by authoritative reads plus the same TTL |
| Transport failure | local cached data only within explicit freshness bounds; otherwise ineligible | endpoint/query failure is unavailable/ineligible; no writable shadow store |
| Write ownership | repository publication controls, plus verification | table grants plus verification; shared-row availability risk as described above |
| Automatic HA | not implied by repository replication | not implied by a central server or Dolt remotes |

A configured SQL-mode failure never silently switches to Git mode. An explicit operator
migration freezes configuration writes, verifies a revision and trusted-key mapping, changes
the configured adapter, then waits for new signed observations before enabling scheduling.
Do not reinterpret a restored heartbeat as proof that its frame is still alive.

## TLS and fnox credential delivery

Use authenticated TLS with server hostname and CA verification for remote SQL access. Refuse
plaintext remote connections or certificate verification bypasses; a network tunnel does not
remove the client authentication requirement. Bind the server to a restricted network and
limit client connection pools, request deadlines and payload sizes. Deployment must prove the
pinned Dolt server's TLS settings and client verification behavior with negative controls.

The enrollment document carries a non-secret endpoint, database and broker reference.
The frame secret broker (`fnox`) delivers its SQL credential at runtime; no password/private
key enters versioned config, frame reports, CLI output, URLs, telemetry, image layers or ADR
examples. Its SSH signing key remains distinct from the SQL password. Credential rotation
must be proven with supported pinned-server operations; revoke the old SQL principal and
signing authorization on retirement or compromise. Revocation/admission changes must affect
new dispatches immediately at their authoritative reread. Do not assume generic MySQL account
rotation syntax works on Dolt without evidence.

## Failure, backup and HA expectations

- Bound SQL outage/query waits and report unavailable; preserve running sessions under their
  existing lease policy, but schedule no new work without an authoritative eligibility check.
- Conflicting config revisions, clock skew, unknown schema, missing ignore rules and key
  rotation ambiguity are explicit failures. Lost responses retry idempotently; they do not
  mint newer observation times or duplicate authoritative admission.
- A compromised frame cannot produce another frame's valid signature. It can impair shared
  table availability; isolate/revoke it and require fresh observations after recovery.
- Git/Dolt remotes protect committed configuration history, not proof of current liveness.
  Do not claim they back up ignored tables, users or server configuration.
- Follow Dolt's [server backup guidance](https://www.dolthub.com/docs/sql-reference/server/backups/):
  use consistent snapshots or a quiesced supported backup procedure, and include privilege
  state and server configuration. Encrypt and restrict backups, preserve an independent copy
  of committed config, and regularly exercise restore. Frame private keys remain broker-owned.
- Backup/restore coverage of ignored working tables is unproven for the selected mechanism.
  Reports needing durable retention require a tested export/archive policy. Leases may be
  discarded on restore: verify config and credentials, invalidate old observations, then
  require new signed leases before scheduling.
- Start with one authoritative SQL writer. A standby is not an independent admission writer.
  Failover requires fencing the old writer and verifying config revision, credential state
  and verifier replay policy. No split-brain or automatic live-table replication guarantee
  follows from Dolt commit replication. Operator-approved RPO/RTO and tested failure drills
  are adoption prerequisites; this draft does not invent numeric service guarantees.

## Reconciliation with existing work

| Evidence / bead | Relationship to this proposal |
| --- | --- |
| `bh-ukit` / accepted [Dolt engine ADR](dolt-server-mode-adr.md) | Chooses bd's per-host shared engine for hive/HQ bead databases. This dedicated remote membership store does not supersede that choice or transfer process lifecycle from bd. |
| `bh-t2eww` — central per-hive daemon spike | Investigates update visibility, event journals and cursor recovery. SQL co-location is not an event bus; this proposal neither adopts per-hive daemon lifecycle nor promises an event stream. |
| `bh-h506s` — shared endpoint cleanup | Operational migration to `127.0.0.1:3310` remains separate. Resolve actual local engine endpoints from existing config; neither its 3310 override nor older docs' 3308 default is the remote HQ address. |
| `bh-b1scz` — S3 Dolt remotes from HQ | HQ may catalog each hive's non-secret desired remote, but hive data stays per-hive with unique remote paths. SQL membership storage does not migrate those databases or prove S3 endpoint compatibility. |
| `bh-pc2a.30` — SSH-only HQ transport | Git-mode transport mismatch remains its existing scope. TLS SQL access removes that specific transport dependency only for membership in SQL mode, not for existing HQ Git/Dolt publication or hive synchronization. |
| [Unified host daemon ADR](unified-host-daemon-adr.md) / [HQ](../HQ.md) / [Dolt](../DOLT.md) | Direct CLI access survives daemon loss; HQ authority remains distinct from derived hub aggregation and local engine supervision. |

Read bead intent as of 2026-09-30; open spikes and operational tasks are not adopted decisions.
This draft neither closes nor changes their scope.

## Later HQ API / Worker.Connect

An HQ API can front the same membership port, authenticate the connecting frame, enforce row
ownership and quotas, then store and verify the same signed envelopes. A gateway
`Worker.Connect` must map authenticated identity to admitted frame/incarnation, not derive
admission from a healthy socket. Connection liveness can trigger a bounded observation path;
it cannot replace signed freshness, alter lifecycle state, renew unrelated issue leases or
bypass final eligibility checks. SSE/stream cursors and reset semantics need their own proven
contract; no stream guarantee is inferred from SQL polling.

## Review choices and implementation evidence still required

1. Ratify the dedicated membership database boundary and committed-configuration read model.
2. Choose direct SQL's availability trust boundary versus a mandatory write gateway, especially
   for vendor-hosted frames. Ratify the signing namespace/bytes via the shared v1alpha1 contract.
3. Pin supported Dolt/client versions and prove grants, procedure denial, TLS refusal,
   rotation, ignore rules, concurrent config/lease reads, restart and malicious row replacement.
4. Approve retention, backup/restore coverage, RPO/RTO, fencing and replay-state recovery policy.
5. Run identical Git/SQL eligibility fixtures (fresh, stale, unsigned, revoked, pending,
   draining, wrong incarnation/release/capabilities, unavailable config) and failure drills.

The preceding text records the original overnight proposal. The binding implementation
below refines its table and grant design. It does not deploy or migrate a production server.

## Binding implementation amendment (`bh-v0k3i`)

An explicit HOST `hq.sql.enabled` switch selects SQL; prepared reader/trust metadata alone
never changes backend. HOST references identify each connection, trusted CA and fnox key.
The fixed noninteractive fnox contract resolves only the role needed for an operation;
config-only construction/load/publish never resolves runtime, observer or operator secrets.
The config service is a dedicated database with only `hq_config_meta`,
`hq_config_documents` and `hq_config_publications`. It returns ordered canonical raw
documents at one captured committed Dolt HEAD, a finite cache validity, and durable HOST
generation/sequence/HEAD restore floors. The trusted publisher uses the caller's original
expected HEAD, explicit three-table staging and `DOLT_COMMIT` exact-hash readback.
`PublicationUnknown` retains its immutable UUID and original parent. The read-only
`recover_publication(UUID, expected_revision=original_parent)` confirms an exact committed
witness on the current branch or reports no confirmed outcome; absence never authorizes a
new UUID or refreshed-parent retry. Initial publication into an intentionally empty schema
is a separate `bh-4shu3` seed contract.

Runtime authority and per-principal registry live in a separate protected database.
Each frame incarnation gets a distinct authenticated SQL principal and inbox table;
frames have no observer, operator, protected receipt/floor DML, schema or version-control
capability. The separately deployed trusted receiver validates domain-separated complete
Ed25519 envelopes against the current operator grant before writing immutable private
first-seen receipts, monotonic floors, public signed observations and global hive lease
CAS/results. Frames may read only the public result and lease projections needed for
bounded acknowledgment and eligibility. Shared-table ON DUP trigger isolation failed on
the pinned server and is not used. The operator signs versioned authority plus the exact
canonical hive policy projection; a config commit without matching protected projection
denies frame intake. Config and runtime histories are not claimed to commit atomically.
The composite read uses one physical snapshot, exact cross-reference and a fresh outside-
snapshot reread at the eligibility point. Later claim/dispatch checks reread authority.
In the isolated pinned-server fixture, killing the separate receiver before SQL COMMIT
rolls back its receipt, floor and result together; killing it just after COMMIT leaves
the exact accepted UUID/digest recoverable. An orderly Dolt restart retains ignored
protected receipts and hive leases. These tests do not establish physical power-loss
durability or an external backup/restore procedure; that recovery contract is part of
the server-local deployment handoff.

The application pins PyMySQL distribution `1.1.2`, requires server-advertised `CLIENT_SSL`
before authentication, a private trusted-CA/hostname TLS context at least TLS 1.2, no
LOCAL INFILE, no caller socket/Unix transport and no reconnect/replay. A bounded isolated
Python resolver subprocess supplies IPv4 addresses while preserving the original hostname
for TLS SNI and SAN verification. IPv6-only endpoints are unsupported. Each frame proposal,
heartbeat, registration, config publication and operator transition carries its original
monotonic deadline through broker, resolver, connection, packet I/O, floor waits and readback;
unknown acknowledgments retain exact request identities. A raw SSH tunnel to a plaintext
MySQL greeting does not satisfy this native TLS contract. Production native TLS and
server-local provisioning remain deployment handoff prerequisites, not performed here.
