# Spike `bh-wtsrc` — can server-timed session rows replace the trusted receiver?

**Bead:** `bh-wtsrc` · **Seat:** `dev/liveness` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-pr889` (hive writer partitioning ADR), via `bh-32379` (migration
map). Parent molecule `bh-qlgmm`. Proposal:
[hive-writer-partitioning-proposal.md §5](../design/hive-writer-partitioning-proposal.md#5-liveness-and-conformance-without-a-receiver).
**Executable evidence:**
[`tests/spikes/test_bh_wtsrc_receiver_removal_liveness.py`](../../tests/spikes/test_bh_wtsrc_receiver_removal_liveness.py)
(`integration` + `dolt_server`, 7 tests, about 20 s).

## Question

Can three pieces replace `SqlTrustedReceiver`, its per-principal inbox tables, receipts and
floors in `dolt-server` HQ mode, without weakening the threat model the receiver was built for?

- **Liveness** becomes one per-frame session row. The frame writes it under its own SQL grant.
  The server clock stamps it.
- **Conformance** becomes a separately scheduled evidence row.
- **Eligibility** is computed at read time by `frame_eligibility`.

This spike does **not** ask:

- Who writes hive placement or the hive-lease CAS. The receiver also does that
  (`accept_hive_lease`); it belongs to `bh-cvk70`.
- How to cut over the live factory frame. That belongs to `bh-32379`.
- Whether frames should be mutually untrusted tenants. The ADR's gateway question stands.

## Method

**Read.** These sources define what the receiver defends:

- [frame-dolt-server-hq-mode-adr.md](../design/frame-dolt-server-hq-mode-adr.md): the original
  grants/signatures section (l.89–126) and binding amendment `bh-v0k3i`.
- `src/beadhive/hq_sql_receiver.py`: `accept_heartbeat` (l.963–1197), `accept_hive_lease`
  (l.225), `accept_registration` (l.634).
- `hq_sql_runtime_schema.py`, `hq_framelease_contracts.py`, `heartbeat_report.py` and
  `frame_eligibility.py`.
- The git-mode reader, `host_heartbeat_core.observe` (l.565).

Each receiver check below is tested against the proposal's premise that the frame's signing key
and SQL credential sit on the same host.

**Ran.** One pytest module against an owned Dolt **2.3.5** `sql-server`, the pin the receiver
asserts at `hq_sql_receiver.py:978–980`. Every server runs with its own `DOLT_ROOT_PATH` and
`HOME` under `tmp_path`. The module creates per-frame `frame_<id>_session` and
`frame_<id>_evidence` tables, which are `dolt_ignore`d, single-row, UPDATE-only, and stamped by
operator-owned `BEFORE` triggers. It also creates a committed operator `hq_grant` table and a
SELECT-only `reader` principal that computes eligibility in a single SQL statement.

The tests:

1. Per-frame grants: a 22-statement refusal matrix.
2. Server-time stamping against frame literals, session and global `time_zone`, and `SET
   timestamp`.
3. Trigger-body privilege semantics, including revoking or dropping the trigger's creator.
4. Leaked-password host pinning.
5. Unprivileged `SET GLOBAL` and `SET PERSIST`.
6. History and renewal latency.
7. Scenario 10: two spawned renewer processes (`harness.processes.process_context`), a spawned
   slow probe, and concurrent operator `DOLT_COMMIT`s.

The prior attempt's scratch probes did not isolate `DOLT_ROOT_PATH`. Their findings were re-run
here in committed tests. Two extra scratch probes (isolated root) settled the trigger-privilege
question before the tests were written. `~/.dolt/config_global.json` was byte-identical
(sha256 `db6bf205…d51c`) before and after every run.

**Observed, read-only.** `bh host eligible --hive bh` on this frame, and the live symptoms the
prior attempt recorded from `journalctl` and `bh host`. Nothing live was connected to or
signalled.

**Commands:**

```sh
uv run pytest tests/spikes/test_bh_wtsrc_receiver_removal_liveness.py -s -p no:cacheprovider
uv run pytest tests/spikes/test_bh_wtsrc_receiver_removal_liveness.py -n 6   # 13 consecutive runs green
```

## Evidence

### E1. Per-frame table grants hold on 2.3.5

`frame_a` holds `SELECT, UPDATE` on its own two tables and `SELECT` on `hq_grant`. Every one
of these was refused (`GRANT_REFUSALS`, test
`test_frame_grant_confines_writes_to_its_own_session_and_evidence`):

- UPDATE or SELECT on `frame_b_session` / `frame_b_evidence`: `Access denied … to table`.
- UPDATE, INSERT or DELETE on `hq_grant`: `command denied`.
- INSERT or DELETE on its **own** session table, so it cannot add rows or erase its row.
- ALTER, DROP TABLE, CREATE TABLE, CREATE TRIGGER, DROP TRIGGER.
- Writes to `dolt_ignore`.
- `DOLT_ADD`, `DOLT_COMMIT`, `DOLT_BRANCH`, `DOLT_CHECKOUT`, `DOLT_RESET`.
- GRANT and CREATE USER.

Afterwards, frame b's rows, the grant row count and the stamping trigger were unchanged. This
replaces the ADR's warning that grants "do not restrict a frame to its own rows" (ADR l.100):
with one table per frame, the table grant *is* the row isolation.

### E2. Server time cannot be moved by the frame when an operator trigger stamps UTC

`BEFORE INSERT/UPDATE … SET NEW.renewed_at = UTC_TIMESTAMP(6)`. Results:

- A frame literal of `'2099-01-01'` was overwritten. The stored value was within 5 s of server
  UTC.
- **Negative control (same table, no trigger):** the literal landed (skew > 1 year).
  `NOW(6)` under the frame's `SET time_zone='+14:00'` landed 14 h ahead. This reproduces the
  prior finding that `NOW()`-stamped rows follow session time zone.
- With the trigger, session `time_zone='+14:00'`, `SET timestamp=2000000000` and a frame-set
  `SET GLOBAL time_zone='+14:00'` all left the stamp within 5 s of UTC.
- A no-op renewal (`UPDATE … SET epoch=epoch`) fires the trigger and advances `renewed_at`.
- Evidence `measured_at` is stamped the same way.

Test: `test_operator_trigger_stamps_server_utc_over_frame_literals_and_time_zone`.

### E3. Trigger-body DML runs with the invoking frame's privileges, not the creator's

This was untested before. An operator `op` created an `AFTER UPDATE` trigger on
`frame_a_keyed` whose body updates `status_board WHERE frame_id = NEW.frame_id`.

- `frame_a` updating its own row was refused with `Access denied … to table 'status_board'`.
  The whole statement fails, own row included.
- The result was unchanged after `REVOKE INSERT, UPDATE … FROM op` and after `DROP USER op`.
  The trigger keeps existing and keeps being checked against the invoker.
  `information_schema.triggers.definer` read `root@localhost` even for `op`'s triggers, so the
  definer column is not informative.
- Only after `GRANT UPDATE ON status_board TO frame_a` could the frame point `NEW.frame_id` at
  `'b'` and bump frame b's row. That is no reach beyond the frame's own grant.
- Trigger definitions are rows in versioned `dolt_schemas`, which showed as an uncommitted
  change.

So a trigger is never a privilege-escalation path, and a `NEW.*`-keyed body cannot reach
another frame's rows. The design rule is that liveness triggers only assign `NEW.<col>`. A body
that writes any other table blocks the frame's renewal outright.

This **contradicts** the pre-kickoff research note that trigger-body DML is "NOT checked against
the invoker's write grants". On 2.3.5 it is checked. It is consistent with the prior finding that
CREATE is refused when the body writes a table the creator cannot write. Test:
`test_trigger_body_writes_run_with_the_invoking_frame_privileges`.

### E4. Server globals are not privilege-checked

This is an availability threat shared with today's design. `frame_a`, holding only table grants,
successfully ran:

```sql
SET GLOBAL max_connections = 1;
SET GLOBAL read_only = 1;
SET GLOBAL dolt_force_transaction_commit = 1;
SET GLOBAL dolt_transaction_commit = 1;
SET GLOBAL time_zone = '+14:00';
SET GLOBAL sql_mode = '';
```

Root confirmed all of them. `SET PERSIST max_connections = 777` wrote
`"sqlserver.global.max_connections":"777"` into the **server process's**
`$DOLT_ROOT_PATH/.dolt/config_global.json`. Here that was the isolated root; in production it is
the server user's real config, and it survives restart.

The `bh-v0k3i` inbox principals hold the same kind of table-only grants, so **today's design has
this exposure too**. One frame credential can make the whole HQ server read-only, starve it of
connections, or force every transaction to become a Dolt commit. Test:
`test_any_frame_principal_can_set_server_globals_and_persist_config`. An upstream issue draft is
in the Recommendation.

### E5. A host-pinned account stops a leaked password from another address

`frame_p@'127.0.0.2'` was refused (1045) when connecting from `127.0.0.1` with the right
password, and could renew from `127.0.0.2`. Test:
`test_host_pinned_principal_refuses_a_leaked_password_from_another_address`. This test skips on
platforms without a `127.0.0.2` loopback alias.

### E6. Session rows create no history, and renewal is cheap

After 250 renewals, `HEAD` was unchanged and `dolt_status` showed no `frame_%` table. An
operator `DOLT_COMMIT('-Am')` committed `hq_grant`/`dolt_schemas` but no `frame_%` table.

Renewal latency over loopback on this host (`RENEWAL_LATENCY`):

| Connection | n | p50 | p95 | max |
| --- | --- | --- | --- | --- |
| Persistent | 200 | 3.0 ms | 4.4 ms | 7.7 ms |
| Reconnect each time | 50 | 6.2 ms | 8.6 ms | 12.5 ms |

TLS and WAN latency are not included. Test:
`test_session_rows_leave_no_history_and_renewal_latency_is_small`.

### E7. Scenario 10: a slow probe does not expire the session, and expired evidence alone blocks

Scaled clock: session TTL 3 s, renewal every 0.3 s, evidence TTL 1.5 s, probe 6 s. Two renewer
processes (frames a and b) ran while an operator thread committed `hq_grant` changes every
0.2 s. The reader sampled eligibility every 0.1 s while the probe ran.

- **Decoupled** (`SCENARIO_10_DECOUPLED`): 58 of 58 samples had `session_fresh`. 54 samples
  were blocked by `evidence_unexpired` **alone**; 4 early samples were eligible before the
  evidence aged out. Once the probe published, the frame was eligible again. Frame b, renewing
  but never measured, was live and blocked only by its evidence predicates.
- **Coupled counterfactual** (`SCENARIO_10_COUPLED`): the renewal waits for the measurement, as
  a `HeartbeatLease` with embedded conformance does (`hq_framelease_contracts.py:64`;
  `heartbeat_report.generate` measures before it can send). 40 of 58 samples were blocked by
  `session_fresh` alone: a healthy frame expired by its own probe.
- 0 renewal errors and 0 operator-commit errors under concurrent `DOLT_COMMIT`. The live failure
  "accepted observer receipt changed during read" has no analogue: there is no receipt to
  re-read.

Test: `test_scenario_10_slow_probe_keeps_session_fresh_and_expired_evidence_alone_blocks`.
Stable in 13 consecutive runs under `-n 6`, after one assertion was loosened (frame b's initial
evidence can also age out under load).

### E8. The live receiver is the availability problem this replaces

These are read-only observations.

- The prior attempt recorded the accepted heartbeat `seq` stuck at 59 (08:17–08:42Z), 60
  (08:42–09:18Z) and 61 (09:18–09:47Z), while conformant beats were re-sent about every 3.5 min.
  One beat failed at 07:58:51Z with "accepted observer receipt changed during read". `BEAT_AGE`
  was 427 s against a 300 s TTL.
- During this spike, `bh host eligible --hive bh` at 10:11:41Z reported
  `authenticated_fresh_heartbeat fail` with every other frame predicate passing. This seat's own
  `bh work claim` waited more than 20 min for an eligibility window.

### E9. Git HQ mode already has no receiver

`host_heartbeat_core.observe` (l.565–577) verifies signed parentless heartbeat commits at read
time and keeps first-seen/high-water state in the observer's own directory. The receiver is a
`dolt-server`-only construct, so git mode needs no removal. It still carries the coupled
heartbeat (E7 counterfactual), and its freshness rests on the signed, frame-clock `renewTime`
plus observer first-seen time.

### Threat model: what the receiver defends, and what replaces it

Sources: ADR grants/signatures section (l.89–126), amendment `bh-v0k3i`, and
`hq_sql_receiver.py`.

- **Covered** means DB grants plus read-time checks give the same or a stronger guarantee.
- **Not covered** means the guarantee is lost or never existed.
- **Co-residence** is the result when the frame host is compromised, so its signing key and SQL
  credential are both taken.

| # | Threat | Receiver defence today | Replacement | Status | Co-residence |
| --- | --- | --- | --- | --- | --- |
| T1 | Frame forges liveness for another frame | Ed25519 signature against the grant's key; principal-to-inbox routing (`accept_heartbeat`, l.986–1036) | Only `frame_b`'s principal can write `frame_b_session` (E1) | **Covered** | Equal: a compromised A cannot touch B in either design |
| T2 | Unadmitted, retired or wrong-incarnation frame publishes | Grant/incarnation match; retired refused (l.1009, l.1012) | The principal exists only once the operator provisions it. Read-time `grant.state='active' ∧ session.epoch = grant.epoch`. Retire = `DROP USER` plus grant state | **Covered** | Equal |
| T3 | Replay of an old valid heartbeat | Monotonic `seq` floors and first-seen receipts (l.1076–1081) | Nothing to replay: the server stamps every renewal (E2) | **Covered** (threat eliminated) | Equal |
| T4 | Frame-chosen, future-dated or skewed timestamps | Receiver clock; −30 s / TTL window on `renewTime` (l.1038–1046) | Operator trigger `UTC_TIMESTAMP(6)` beats literals, session/global `time_zone` and `SET timestamp` (E2). The reader compares with `UTC_TIMESTAMP` | **Covered**, stronger: no skew window at all | Equal |
| T5 | Cross-frame overwrite, erasure or flooding of shared rows (ADR residual risk, l.115) | Per-principal inbox tables; protected tables observer-only | Per-frame single-row tables, UPDATE only. No INSERT/DELETE even on its own table (E1). Size is bounded by the columns | **Covered**, stronger: the inbox allowed unbounded INSERTs | Equal |
| T6 | Tampering with authority, registry, receipts or config | No frame DML on protected tables | SELECT-only `hq_grant`; no other grants (E1) | **Covered** | Equal |
| T7 | Schema, ignore-policy or version-control abuse | No frame DDL or `DOLT_*` | Refused (E1) | **Covered** | Equal |
| T8 | Subverting the stamping trigger, or using a trigger as a write path | n/a | No TRIGGER privilege; DROP TRIGGER refused (E1). Trigger DML is invoker-checked even after the creator is revoked or dropped (E3) | **Covered** | Equal |
| T9 | Self-attested conformance (frame reports `conformant` without measuring) | None: the signature proves who sent it, not that the checks ran | None: the evidence row is self-attested. Release digest and profile are compared with the grant at read time | **Not covered**, in either design | Equal |
| T10 | Stale conformance presented as current | Freshness of the whole heartbeat | `measured_at` server-stamped (E2). Expiry = `measured_at` + **operator** `evidence_ttl`, computed by the reader, so the frame cannot extend it (E7) | **Covered**, decoupled | Equal |
| T11 | SQL password leaked **without** the frame host (broker, backup, logs) | A password alone cannot forge liveness without the signing key (ADR l.163 keeps them distinct) | The password *is* the liveness credential | **Not covered by grants alone.** Narrowed by host-pinned accounts (E5) plus mandatory TLS | n/a: under co-residence both designs fall equally |
| T12 | Lost acknowledgement or non-idempotent retry | `hq_live_results`, `ReceiverUnknown`, exact readback | The renewal is an idempotent UPDATE; a retry is harmless | **Covered** (threat eliminated) | Equal |
| T13 | Verifier restart loses replay state | Durable floors (ADR l.112) | No replay state exists | **Covered** (threat eliminated) | Equal |
| T14 | Eligibility read racing an authority change (TOCTOU) | One composite snapshot plus a fresh reread (`read_frame_composite`) | One SELECT joins grant, session and evidence; `require_intake` / `GuardedClaimSession` already reread at claim (`frame_eligibility.py:433–459`) | **Covered in principle.** The `AS OF HEAD` committed grant joined with working-set rows in one statement still needs an implementation proof | Equal |
| T15 | Audit: immutable first-seen record of each accepted observation | `hq_live_receipts` with signed envelopes, but `hq_live_*` is `dolt_ignore`d, so it is live-only and lost on restore | Session rows are overwritten; no per-beat record | **Not covered** (see Recommendation 4) | Equal |
| T16 | Server-wide disruption via `SET GLOBAL` / `SET PERSIST` | **None**: inbox principals can do it too | None | **Not covered**, in either design (E4) | Equal |
| T17 | HQ liveness hostage to one extra component | The receiver is an extra single point of failure (E8) | Direct UPDATE, ~3 ms (E6); the HQ server remains a single point of failure | **Improved** | n/a |
| T18 | Hive-lease CAS preconditions (fresh conformant receipt, incumbent evictability) | `accept_hive_lease` (l.225–630) | Becomes read-time predicates over session and evidence rows; the CAS writer is `bh-cvk70`'s question | **Out of scope**, handed to `bh-cvk70` | — |
| T19 | Registration binding (signed registration matches the committed manifest) | `accept_registration` (l.634) | An operator-side verification at enrollment, before the grant is written | **Out of scope**, handed to `bh-cvk70` / `bh-32379` | — |

Under co-residence the receiver adds nothing in any row: a compromised frame signs whatever it
would otherwise write. The only row where the receiver is stronger is T11, a credential leaked
*apart from* its host. That is a real difference, and it has a cheap mitigation.

## Verdict — **GO**

Liveness can be a per-frame, trigger-stamped session row under the frame's own grant. Conformance
can be a separate evidence row whose expiry the reader computes. Eligibility can be one read-time
statement.

On Dolt 2.3.5 this holds every receiver guarantee that matters under the co-residence premise
(T1–T8, T10, T12–T14). It is strictly stronger on skew (T4) and flooding (T5). It fixes scenario
10 (E7).

The enablers are four established facts:

- table grants isolate per-frame tables (E1);
- an operator `SET NEW` trigger pins server UTC (E2);
- trigger DML is invoker-checked (E3);
- ignored rows leave no history (E6).

Two guarantees the receiver never had stay open (T9, T16). One narrow weakening, T11, needs
host-pinned accounts. Audit (T15) needs a deliberate replacement.

## Recommendation

1. **Implementation molecule (after `bh-pr889`).**
   - Operator DDL per frame incarnation: `frame_<principal>_<epoch>_session` and
     `_evidence`, single-row with a `CHECK (id = 1)`, UPDATE-only, `dolt_ignore`d before
     creation, and two `SET NEW.<ts> = UTC_TIMESTAMP(6)` triggers each. Trigger bodies must
     never write another table (E3).
   - A renewal loop independent of measurement.
   - A conformance job on its own timer that writes evidence.
   - `plane.read_eligibility` reads one statement. Its inputs feed today's predicates:
     `authenticated_fresh_heartbeat` ← `session_fresh`; `conformance_pass` / `release_matches`
     ← evidence vs grant. Keep the claim-time reread.
   - Delete `SqlTrustedReceiver`, `LIVE_SCHEMA` inboxes, `hq_live_receipts` / `floors` /
     `public_observations` / `results`, and `tests/harness/sql_receiver_worker.py`. This
     follows proposal §6 and holds only once `bh-cvk70` has re-homed the hive-lease CAS (T18).
2. **Account hardening (T11).** Create frame accounts as `'<principal>'@'<frame address>'`
   (E5) and require TLS (`require_secure_transport`), so the SQL password only works from its
   host. Optionally sign evidence rows (Ed25519, verified at read time) for audit; this is not
   needed for safety.
3. **Global-variable exposure (T16). Do this whether or not the receiver goes.** Record it in
   the HQ threat model. Mitigations for deployment:
   - run the HQ server with a read-only `DOLT_ROOT_PATH` config, so `SET PERSIST` cannot
     survive a restart;
   - have an operator watchdog assert critical globals (`read_only`, `max_connections`,
     `dolt_force_transaction_commit`, `dolt_transaction_commit`, `time_zone`);
   - never co-host hive databases on the HQ server, since a global
     `dolt_force_transaction_commit=1` would defeat FK-based epoch retirement (`bh-vje85`).

   **Upstream issue draft (dolthub/dolt):**

   > *Title:* `SET GLOBAL` / `SET PERSIST` succeed for users without `SUPER` /
   > `SYSTEM_VARIABLES_ADMIN`.
   >
   > *Version:* 2.3.5, sql-server.
   >
   > *Repro:* `CREATE USER u IDENTIFIED BY 'x'; GRANT SELECT ON db.t TO u;` then, as `u`:
   > `SET GLOBAL read_only=1; SET GLOBAL max_connections=1; SET PERSIST max_connections=777;`.
   > All succeed. The last writes `sqlserver.global.max_connections` into the server user's
   > `config_global.json`.
   >
   > *Expected (MySQL 8):* `ERROR 1227 … need SUPER or SYSTEM_VARIABLES_ADMIN`, and
   > `PERSIST_RO_VARIABLES_ADMIN` for `SET PERSIST`.
   >
   > *Impact:* any least-privilege account can deny service to the whole server.
4. **Audit replacement (T15).** What is lost is a per-beat signed record that was itself
   live-only (`hq_live_*` is ignored) and so lost on restore. Retain what matters at the
   decision point instead: when `GuardedClaimSession` admits a claim, record the decision's
   predicates, `session.renewed_at`, `evidence.measured_at` and the evidence digest in the
   claim record. `claim_authority` already records `epoch`/`host_id`. For trend history, an
   operator-side sampler can append session and evidence snapshots to a bounded, exported
   table. Frames must not write the audit store.
5. **Git HQ mode: scope execution frames to `dolt-server` HQ.** Git mode keeps today's
   read-time signed-heartbeat verifier unchanged (E9). It already has no receiver, and it gets
   no decoupled evidence carrier. The costs:
   - two liveness models stay in the tree: signed `HeartbeatLease` and `observe` for git,
     session/evidence rows for SQL;
   - git-mode frames keep the coupled-heartbeat weakness, so their TTL must exceed probe time
     (today 210 s against 300 s);
   - the ADR's "identical eligibility semantics across adapters" weakens to "identical
     predicates, different evidence carriers", and `bh-32379` must say so.

   The alternative is a separate signed evidence ref verified at read time in git mode. It
   costs a new carrier, verifier and first-seen state, and freshness still rests on the frame
   clock. Pursue it only if multi-frame execution on git HQ becomes a requirement.
6. **Open checks for the implementation, not blockers.**
   - Prove the `AS OF HEAD` grant joined with working-set rows in one statement (T14).
   - Prove that committed `dolt_schemas` triggers on ignored tables survive restore and fresh
     clones. A trigger whose table is absent fails "table not found" (pre-kickoff research).
     Provisioning must recreate the tables, or keep live tables in a database that is never
     cloned.
   - Measure renewal latency over TLS from the factory frame.
