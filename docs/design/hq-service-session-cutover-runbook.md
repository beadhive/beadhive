# Runbook — frame session renew and evidence through hq-service (beadhive side)

> **Status:** operator procedure for the beadhive side of the hq-service **Cutover** (`bh-q0kiz`,
> epic `bh-jpjd2`). It is a procedure, not a record that a cutover happened. In particular the
> **real staged run of the end-to-end test has not been recorded**: see
> [section 2](#2-required-before-the-cutover-a-real-staged-run-open-operator-step).

hq-service's own cutover (`deploy/README.md`, "Cutover", read at hq-service `origin/main`
`205fd5b`) is a numbered **Order** list of six steps. Steps 1-4 are copy, migrate and dry run, and
backup plus apply on production; they are service-side and have no beadhive action. This runbook
covers the two steps that touch frames:

| Service-side step | What the service README says | Beadhive side (this runbook) |
|---|---|---|
| 5 | Deploy the pio.7+ build; run `hq-service cutover` again right before the restart. From the restart liveness decays until each frame renews; either switch the frames' renew timers and conformance job to the new endpoints at or just before the restart, or announce a stale window. | [Section 4](#4-service-step-5-switch-the-frame-within-one-ttl-of-the-restart): turn the flags on and prove one renew and one evidence post. |
| 6 | Frames now renew through `POST /sessions/{principal}/{epoch}/renew` and `/evidence`. Stop the per-frame `beadhive-session-renew` timers and the conformance job's direct writes. | [Section 5](#5-service-step-6-stop-the-direct-writes): the timers are **not** stopped on the beadhive side. The same timers keep running and their writes are redirected by `hq.session_via_port`. What stops is the direct SQL write. |

The numbering matches the service's: "step 5" and "step 6" here are the service's Order items 5
and 6. The one wording difference is step 6: the service says "stop the timers", the beadhive
timers (`beadhive-session-renew`, `beadhive-heartbeat-conformance`) stay installed and enabled,
and a flag changes what they do.

Related: [HQ lifecycle port](hq-lifecycle-port.md) (the port and its gaps),
[HQ configuration authority migration](dolt-hq-config-migration-runbook.md) (the `fleet.json`
republish, see [section 9](#9-rollback)), and the `hq.*` rows of
[CONFIGURATION](../CONFIGURATION.md).

## 1. The flags

All three are HOST-only: set them in the host's own config (never in a published fleet
document). Each has an environment override that wins for one process.

| Setting | Env override | Default | Meaning |
|---|---|---|---|
| `hq.route` | `BH_HQ_ROUTE` | `sql` | Which adapter this host's HQ calls use: `sql` or `api` (hq-service through the Gateway `hq` plane named by `hq.api`). No fallback between them; an invalid value is refused. **Host-wide:** it moves every HQ call on the host, not only session renew, to hq-service. |
| `hq.session_via_port` | `BH_HQ_SESSION_VIA_PORT` (`1`/`true`/`yes`/`on`; any other set value is off) | off | Session renew (`heartbeat_sender renew`) and the conformance evidence post go through the port the route selects instead of the direct SQL session writes. An unreadable host config reads as off. |
| `hq.session_dual_write` | `BH_HQ_SESSION_DUAL_WRITE` (`1`/`true`/`yes`/`on`) | off | Only with `hq.route: api` and `hq.session_via_port` on: after the port accepted a renew or evidence post, also run the direct SQL write. **Keep it off, see the first caution below.** |

Two things the flags alone do not give you:

- **`hq.api`** (URL or unix socket, mTLS client certificate and key, CA) must already be valid
  for `hq.route: api`.
- **The signing key.** The service renew is a signed beat. It is signed with the host's recorded
  frame signing key, `host.yaml` `signing_key` (an absolute path), the same key
  `heartbeat_sender beat` signs with. With the key missing or unusable the renew raises
  `HqUnavailable` and sends nothing.

### Cautions that apply to the whole cutover

1. **Leave `hq.session_dual_write` off until `bh-ubpoc` settles** whether the SQL runtime and
   hq-service share their liveness floor tables (`hq_live_floors`, `hq_live_receipts`,
   `hq_live_public_observations`). The client's renew `seq` is rebased to the wall clock
   (about 1.78e9 now); if the SQL sender writes to the same floor, its small sequence numbers
   could be refused. Nothing enables the flag automatically and this cutover does not need it.
2. **The direct SQL writes stop immediately, and that is not undone here.** With `hq.route: api`
   and dual-write off, the port path is port-only: the frame's SQL `_session` and `_evidence`
   rows stop updating the moment `hq.session_via_port` is turned on. Stopping the direct writes
   *for good* (dropping the per-frame SQL accounts and tables, the service README's "Retiring the
   Python layout") is a **follow-up and not part of this cutover**. See
   [section 6](#6-what-sql-side-readers-see).
3. **Design gap G1 (`bh-b2mq7`).** hq-service serves no frame-grant route, so a fresh beat still
   reads this host's SQL frame authority for its grant-bound fields. An `hq.route: api` host
   with **no SQL frame runtime cannot build a beat**: renew fails with `HqUnavailable`, nothing
   is sent, and there is no fallback. Such a host cannot be cut over until G1 is closed.
4. **No cross-process lock yet (`bh-ktd2x`).** See [section 7](#7-expected-noise-seq_not_advanced).

## 2. Required before the cutover: a real staged run (open operator step)

The end-to-end test is `tests/test_hq_session_staged_e2e.py`, marker `staged_hq_service`, opt-in
`BH_STAGED_HQ_SERVICE=1`; the command is in
[TEST_PROCESS_POLICY](../TEST_PROCESS_POLICY.md) ("Staged hq-service session test"). In every
gate lane it runs against an in-process fake. **No real staged pass has been recorded**: this is
an open step, and the result is to be recorded on `bh-zephr`. Do not treat the cutover as
verified until it has been done.

Variables (names only; values and key paths are never printed by the test):

- Required: `BH_STAGED_HQ_SERVICE`, `BH_STAGED_HQ_URL`, `BH_STAGED_HQ_SIGNING_KEY` (a path),
  `BH_STAGED_HQ_PRINCIPAL`, `BH_STAGED_HQ_FRAME`, `BH_STAGED_HQ_HOLDER`,
  `BH_STAGED_HQ_INSTANCE`, `BH_STAGED_HQ_EPOCH`.
- Optional: `BH_STAGED_HQ_CLIENT_CERT`, `BH_STAGED_HQ_CLIENT_KEY`, `BH_STAGED_HQ_CA_CERT`,
  `BH_STAGED_HQ_AUDIENCE`, `BH_STAGED_HQ_CONFIG_REVISION`.

Cautions:

- **Staged principal only, never production.** The run posts real renews and evidence.
- Run staged tests **one at a time, at least 2 seconds apart**, and never re-run within the same
  wall-clock second: a fresh seq state rebases above `int(now)`, which must exceed the floor an
  earlier run left, or you get a spurious `seq_not_advanced`.
- First run: `beat_schema_invalid` or `identity_mismatch` can appear if the grant rejects the
  corpus-template beat fields; set `BH_STAGED_HQ_AUDIENCE` and `BH_STAGED_HQ_CONFIG_REVISION`.
  `incarnation_unprovisioned` means the incarnation is not registered with the service (for an
  incarnation that predates the service build, that is service step 4's `hq-service cutover`).
- What a staged pass proves: the client retry and real renew and evidence acceptance. The
  `no_current_beat` fault is injected at the client transport and the interleaving is one fixed
  order, so it does **not** prove the service produces `no_current_beat` or survives overlapping
  renews (the overlap is fake-only).

## 3. Preconditions

### 3.1 The installed bh must contain both flags (every frame)

The timers run the **installed** bh (`python -m beadhive.heartbeat_sender`), not a checkout.
`hq.session_via_port` (`bh-osom7`, commit `def9b89d`) and `hq.session_dual_write` (`bh-a3369`,
commit `43af1eae`) are **not in `v0.38.0` or any earlier tag** (checked with `git tag` and
`git merge-base --is-ancestor`). Install, on every frame, the first release that includes
commits `def9b89d` and `43af1eae`; which release that is was not known when this was written.

On a build without the flag, the setting is ignored, the direct SQL writes continue, and
verification step 1 (section 8) **passes falsely**: the SQL path also prints `{"renewed": ...}`
and exits 0. After the service restart that frame then goes stale with no error.

Check the build on each frame before flipping anything:

```sh
bh config schema | grep -c 'hq.session_via_port\|hq.session_dual_write'
```

It must print `2`. On a build without the flags it prints `0` (run against an installed
`0.36.0`; a build with the flags printed `2`). Do **not** rely on `bh --version`: it reports the
last tag, so a build from after `v0.38.0` also reads `0.38.0`. `bh config get hq.session_via_port`
prints `hq.session_via_port is not set` and exits 1 while the flag is unset, on either build, so
it does not tell you whether the flag is recognised (it was not run with the flag set here).
To confirm the port path is really in use once flipped, see section 8 step 3 (the service's
`liveness.session.age_s` must keep falling back near 0 every 60 s; that read depends on a Gateway
route that is unverified).

### 3.2 Service-side (service steps 1-4)

Before touching a frame, on the service side: the production backup is taken, `migrate` and
`cutover` have been applied, every incarnation has a row, and `GET /readyz` answers 200 (it is
on the service's Unix socket only; the Gateway does not expose it, so probe it from the
service host). While any incarnation has no liveness row, `/readyz` answers 503
`cutover_incomplete`, and renew and evidence for that incarnation are refused
`incarnation_unprovisioned`. The deploy happens after step 4, so `/readyz` is expected to be 503
right after the new build starts until the **step 5 `hq-service cutover` rerun** has run (it
clears with no restart); 200 is the state to reach after that rerun, not one to have before it.

## 4. Service step 5: switch the frame within one TTL of the restart

Order of operations on each frame (do one frame first and verify before the rest):

1. Confirm the installed bh has both flags ([section 3.1](#31-the-installed-bh-must-contain-both-flags-every-frame)),
   `hq.api` and `host.yaml` `signing_key` are in place, and that the host has a SQL frame
   runtime (G1).
2. Set `hq.route: api` and `hq.session_via_port: true` in the host config. Leave
   `hq.session_dual_write` unset. For a one-off trial use the environment overrides
   (`BH_HQ_ROUTE=api BH_HQ_SESSION_VIA_PORT=1`) on a single command instead.
   **`hq.route: api` is host-wide**: every HQ call on this host moves to hq-service with no
   fallback, not just session renew. Before flipping, inventory the host's other HQ call paths
   (dispatch, admission, lifecycle and config reads, anything else that calls HQ) and confirm
   hq-service serves each of them; this runbook did not inventory them.
3. **Renew once before anything else** and read the output:

   ```sh
   python -m beadhive.heartbeat_sender renew
   ```

   Success prints `{"renewed": ...}` on stdout and exits 0. The renew timer
   (`beadhive-session-renew`, `dev.beadhive.session-renew` on launchd) runs this verb every
   60 s (`RENEW_INTERVAL_SECONDS`).
4. Then run conformance once (or let its timer, every 300 s, `CONFORMANCE_INTERVAL_SECONDS`,
   do it):

   ```sh
   python -m beadhive.heartbeat_sender conformance
   ```

   The evidence post follows the refresh. The ordering rules are in the code, not options:
   - Evidence is never posted before an accepted renew. A conformance process with no accepted
     renew for this incarnation in its own process renews first (`accepted` and `replayed` both
     count), so each conformance run costs one extra beat. If that renew fails, the evidence is
     not posted and the run reports `session evidence refused`.
   - The evidence posted is the exact report the accepted beat digested, so its digest matches
     the beat the service holds even if the conformance cache changed in between.
   - On `no_current_beat` it renews once more and re-posts once more: no loop, no wait. A second
     failure is reported.
5. Systemd/launchd units keep running unchanged. If the flags are set by `Environment=` lines in
   a unit instead of the host config, `heartbeat_sender units --install` preserves
   operator-set values (use `--replace` only to drop them).

### Sequence relative to the service restart

Both edges around the restart are bounded by the 300 s session TTL (evidence TTL 900 s; both
default, read from the operator's policy). Source: the service's `deploy/README.md` "Cutover"
step 5, at `origin/main` `205fd5b`.

1. Service steps 1-4 are done and the old build is still serving.
2. Flip the first frame (steps 2-3 above). **The first renew goes to whichever service build is
   serving at that moment.** Flipped before the restart it reaches the old build, which does not
   judge liveness from it (see below); flipped after, it reaches the new build, the intended
   target.
3. Service step 5: run `hq-service cutover` once more immediately before the restart, deploy the
   new build, restart. From the restart the service ignores Python's session tables and judges
   only its own `hq_live_*` rows.
4. Let `/readyz` clear (the step 5 rerun, section 3.2), then verify (section 8).

Two exposures, by which side of the restart a frame is flipped:

- **Flipped too late** (after the restart): the carried stamp ages from the restart. Until the
  frame's first accepted renew, candidate admission refuses `session_stale` once the age reaches
  300 s. This is the service README's own stale window.
- **Flipped too early** (while the old build still serves): the frame's SQL `_session` and
  `_evidence` rows stop updating **at once** (port-only path), and the old build judges liveness
  from exactly those tables, so the frame goes stale on the old build after up to one TTL (300 s
  for the session). Its renews meanwhile reach hq-service, which the old build does not read.
  The step 5 cutover rerun carries only what the old build's tables held at that moment.

**Rule: keep the gap between flipping a frame and the restart under 300 s**, and do not leave a
frame unflipped for more than 300 s after the restart. In practice: flip each frame inside the
last 300 s before the restart (or immediately after it), run the verification renew, and do one
frame first and verify before the rest.

### What the operator sees on failure

Both sender messages are generic, exit 1, and do **not** print the refusal code:

- `session renewal refused; verify grant, configuration and runtime` (renew verb).
- `session evidence refused; verify grant, configuration and runtime` (conformance verb).
- `renew --loop` instead prints `session renewal failed (<ExceptionClass>); retrying` and goes
  on at the next interval.

The sender never prints the refusal code, and **a production frame has no way to read it**:
no verb shows it. The code is in the service's error envelope (`{"error":{"code",...}}`), so the
source is the service side (its logs, or the envelope if you can see the response); this runbook
did not verify what the service logs. The table below says what each code means once you have
it. Registry: the corpus copy `tests/fixtures/hq_service_contract/session_v1/refusal-codes.json`
(the service's `conformance/session/v1/refusal-codes.json`) and `conformance/http/v1/codes.json`;
the adapter's `RENEW_REFUSALS` and `EVIDENCE_REFUSALS` carry the same names. All are
non-retryable. HTTP status is 409 for a rule refusal, with these exceptions:

| Status | Codes |
|---|---|
| 400 | `path_invalid`, `request_malformed` |
| 401 | `upstream_authentication_failed` (the call was not authenticated upstream: check the frame's client certificate and the Gateway; not in the table below) |
| 413 | `request_size_invalid` |
| 415 | `content_type_invalid` |

| Code | What the operator does |
|---|---|
| `incarnation_unprovisioned` | The service has no liveness row. Run the service-side `hq-service cutover` (step 4/5) or provision the incarnation. |
| `unprovisioned_principal`, `no_current_grant`, `grant_retired`, `candidate_expired` | The service has no current granted incarnation for this principal, or it was retired or expired. Fix the grant and admission; not a frame setting. |
| `identity_mismatch`, `beat_signature_invalid`, `beat_schema_invalid` | The beat does not match the protected grant or is not signed by the granted key. Check `signing_key`, host identity, and that the grant names the public half. |
| `seq_not_advanced` | The service's floor is at or above the sent seq. Normally self-heals (see section 7); persistent means another writer shares the floor. |
| `beat_not_fresh` | The beat's first observation is expired or future-skewed: check the host clock. |
| `lease_duration_out_of_range`, `lease_duration_below_intervals`, `request_*`, `content_type_invalid`, `path_invalid` | Client or contract mismatch; a version skew. Do not retry blindly. |
| `epoch_stale`, `epoch_future`, `path_principal_mismatch` | The frame's recorded (principal, epoch) differs from the service's committed one: re-read the frame's route. |
| `authority_failed`, `floor_unprovisioned` | Authority or the protected floor is not ready on the service; service side. |
| `no_current_beat` (evidence) | No beat accepted for the principal. The sender renews and re-posts once; only a second failure is reported. |
| `evidence_digest_unmatched`, `evidence_invalid` (evidence) | The report is not the one the current beat digested, or is malformed. Not expected in steady state. |

Retryable outage codes (`503`, CAS conflicts) are not refusals: they are `HqUnavailable`,
`HqDeadline` or `HqOutcomeUnknown`, reported the same way as a failed renewal, never retried in
place for evidence, and never fall back to SQL.

## 5. Service step 6: stop the direct writes

On the beadhive side there is nothing further to stop: the moment `hq.session_via_port` is on
and dual-write is off, `heartbeat_sender` makes no direct session or evidence SQL write. Leave
the renew and conformance timers enabled; stopping them would stop the only renew path. The
per-frame SQL accounts and the `_session`/`_evidence` tables stay in place and untouched
(they are what [section 9](#9-rollback) returns to, and only before the new service build
serves). Their retirement is the service README's
"Retiring the Python layout", a separate follow-up.

## 6. What SQL-side readers see

With the direct writes off, the frame's SQL `_session` and `_evidence` rows are no longer
updated. Anything that still reads those tables sees the stamps from the last direct renew and
judges them stale once they
age past the policy (session 300 s, evidence 900 s by default, read from the operator's
`hq_liveness_policy`). Expect those readers to report the frame stale a few minutes after the
flip, even though the service reports it fresh. That is the expected consequence of the
port-only path, not a fault.

The readers found in `src/` (grep for `session_table`, `evidence_table`, `read_eligibility`,
`LivenessKind.SESSION`):

- **Eligibility facts on SQL-route hosts:** `hq_sql_runtime.py` `_session_observation` calls
  `hq_sql_session.read_eligibility`, reached through `hq_sql_adapter.py` `read_eligibility` and
  `frame_eligibility.py` (~line 465). Its consumers are the dispatch and admission eligibility
  decisions, so a stale SQL row can make a frame ineligible there.
- **Director failover observer:** `director_failover.py` `observe_sessions`, via
  `failover_observer.py` `session_staleness` (the storage port's `LivenessKind.SESSION` ages
  over the session rows), started from `host_daemon.py`. On a host that runs that loop a stale
  SQL session row reads as a stale frame for failover. Whether that loop is enabled on your
  frames was not checked.
- Not readers of the row stamps: `hq_placement_ops.py` `check` (grants on the same table
  names), `failover_policy.py` `bounds_from` (reads the liveness policy, not frame rows), and
  the writers in `heartbeat_report.py` and `hq_sql_session.py`.

`hives-ready` is **not** a reader of the frame session or evidence tables.

What to check: inventory those readers before flipping a frame, and switch or tolerate them; if
one cannot be tolerated, hold the frame back from service step 6. Do not
turn on `hq.session_dual_write` as the workaround until `bh-ubpoc` is settled (first caution).

## 7. Expected noise: seq_not_advanced

Without a cross-process lock (`bh-ktd2x`) a timer renew and a conformance run (which renews
first) overlapping in time can cost one tick to `seq_not_advanced`. It is expected and
bounded: the state is then marked for resync, the next fresh beat is rebased above the wall
clock, and the next tick succeeds. Operators see, at most, one `session renewal refused` (or
`session evidence refused` if the renew inside conformance lost) and one tick later it is
healthy; in steady state there is no `evidence_digest_unmatched`. A repeating `seq_not_advanced`
is not this: suspect another writer on the same floor.

The renew `seq` is kept by the client, per frame, holder and epoch, in
`$BH_HOME/heartbeat/hq-renew-seq-<hash>.json` (mode 0600, no key material). Lost or unreadable
state or `seq_not_advanced` rebases the next fresh seq to `int(now) + 1` or above. An unanswered
beat is resent byte for byte while younger than half its lease, so a transport retry is a
`replayed` beat.

## 8. Verification: no stale window

On each frame, across at least two conformance intervals (so at least 10 minutes):

1. `python -m beadhive.heartbeat_sender renew` prints `{"renewed": ...}` and exits 0. **Exit 0
   alone is not proof**: the verb also exits 0 after printing `session renewal skipped: no
   dolt-server frame runtime` or `... incarnation not switched`, and a conformance run prints
   `conformance run already in progress; skipped` and exits 0 without posting evidence.
2. No `session renewal refused` / `session evidence refused` lines in the unit journals
   (systemd: `journalctl --user -u beadhive-session-renew -u beadhive-heartbeat-conformance`;
   launchd: `$BH_HOME/heartbeat/renew.err.log` and `$BH_HOME/heartbeat/conformance.err.log`, the
   `StandardErrorPath` per verb in `heartbeat_sender.py`) over the same window.
3. hq-service shows the frame fresh through the whole window. The read is the service's
   eligibility route, `GET /eligibility/{frame}` (`hq.lifecycle.v0`), which returns
   `liveness.session` and `liveness.evidence` with `verdict`, `age_s` and `ttl_s`. This is also
   the check that the port path is really in use: on a build without the flag the SQL writes
   continue and the service row stops moving after the restart. Expect
   `session` fresh with `age_s` always below the TTL (renews arrive every 60 s) and `evidence`
   fresh after each conformance interval (300 s against a 900 s TTL). It returns `null` liveness
   when there is no active grant or no row. *Not verified here:* whether the Gateway exposes
   this route to an operator credential in your deployment, and the exact path prefix through the
   Gateway; check the Gateway route table. A second, indirect check is candidate admission: it
   requires a fresh session and refuses `session_stale` otherwise.
4. Read the service's `GET /readyz` from the service host: 200, no `cutover_incomplete`.

A stale window shows as: `session` age reaching the TTL (300 s), `session_stale` on admission,
or repeated refusals in step 2.

## 9. Rollback

**After the new service build serves, unsetting `hq.session_via_port` alone is NOT a safe
rollback.** From the restart hq-service is the admission authority and judges only its own
liveness row; once the frame stops renewing there, that row ages and admission refuses
`session_stale` within about 300 s (evidence: 900 s), even though the SQL rows are fresh again.
After the restart, rolling back means returning to the old service build as well (below).

**Before the restart** (the old build still serves) the rollback is the simple one: unset
`hq.session_via_port` (and `BH_HQ_SESSION_VIA_PORT`); `hq.route` may stay `api` for other calls,
since the session path is direct SQL whenever the flag is off. The next renew tick writes the SQL
session row again, so the SQL-side readers, and the old build, recover within one tick.

- **Local seq state:** the `hq-renew-seq-<hash>.json` file is harmless and left in place.
- **hq-service state:** the service floor and receipts remain. The service's liveness row simply
  stops being renewed and ages stale, which matters only if something still reads it. A later
  switch back to the port path must send a seq above the service floor, and the client rebases
  there automatically (to `int(now) + 1` on lost state or after one `seq_not_advanced`).
- **After the restart, roll back the service too.** The service README's step 4 rollback
  (restoring dumped `hq_live_*` values) is valid only before the new build serves. After it
  serves, rolling back means returning to the old service build **and** the SQL session tables,
  and unsetting the flag (in that combination: old build serving, flag unset, SQL rows renewed
  again), which requires the tables and accounts from "Retiring the Python layout" to still be
  intact: do not retire them until the cutover has held.
- **`fleet.json` republish (`bh-vrqne`, `bh-se4se`):** the three session flags are HOST-only and
  never touch `fleet.yaml` or `fleet.json`, so unsetting them is independent of that republish
  and does not undo it. The `fleet.json` republish is **inferred** to remain one-way until every frame
  runs a version that knows it (that is an inference, **not run**; `bh-se4se`); see the
  [`fleet.json` projection section](dolt-hq-config-migration-runbook.md#fleetjson-projection-bh-vrqne)
  of the migration runbook.

## 10. Not verified by this runbook

- A real staged pass of the end-to-end test (section 2).
- Whether the Gateway exposes `GET /eligibility/{frame}` to operators (section 8).
- The refusal code behind a refused renew: the sender prints no code and what the service logs was
  not checked (section 4).
- Which release first contains `def9b89d` and `43af1eae`: unknown until it is released (section
  3.1); the `bh config schema` check was run only against an installed `0.36.0` and a worktree
  build, and `bh config get hq.session_via_port` was not run with the flag set.
- Whether the other HQ call paths on a host work under `hq.route: api` (section 4 step 2).
- Whether the director failover loop is enabled on the frames (section 6).
- The `fleet.json` one-way statement (inferred, `bh-se4se`), the service's log content, and the
  Gateway route behind the `GET /eligibility/{frame}` read.
- Behaviour of overlapping renews on a real service (fake-only; `bh-ktd2x`).
- Whether dual-write is safe (`bh-ubpoc`).
