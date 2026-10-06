# Spike `bh-uhx2r` — option B forward RPC, and whether a forced global lands a stale write

**Bead:** `bh-uhx2r` (M13) · **Seat:** `dev/forward-rpc` · **Type:** spike (test-only code,
no product code)
**Parent:** `bh-16347` (hive writer partitioning B, 0.23.0)
**Feeds decision on:** [hive-writer-partitioning-adr.md](../design/hive-writer-partitioning-adr.md)
§3 (option A vs option B), condition 16, Follow-up decisions 1 and 5. C/O8 (admitting the three
new executors) waits on this verdict. M12 (`option A`) does not.
**Executable evidence:**
[`tests/spikes/test_bh_uhx2r_forward_rpc_int.py`](../../tests/spikes/test_bh_uhx2r_forward_rpc_int.py)
(`integration`, `dolt_server`; six tests, about 4.5 min at `-n 2`). It runs on the bh-jbb6r
composed prototype ([`tests/harness/composed_fence.py`](../../tests/harness/composed_fence.py)).
**Status:** **proposed**, awaiting the operator decisions listed at the end of the
Recommendation. The verdict is noted on the ADR (§3, "M13 result").

## Question

The ADR records two open points under §3, and the operator's Follow-up decision 1 asks for both
to be measured:

1. **Does a forced global let a stale write land?** If a forwarder runs
   `SET GLOBAL dolt_force_transaction_commit = 1`, or sets another global that the condition 16
   watchdog names, on the primary's hive `dolt sql-server`, can a write stamped at a retired
   epoch reach remote `main` end to end? It would have to get past FK epoch retirement, the
   write guard and the managed path. The answer sets how urgent option B is.
2. **Can option B carry forwarded writes?** Can a per-hive bh RPC service, run by the hive's
   primary in the bh process that already holds the write, carry forwarded claim, create and
   close, so that executors hold no Dolt login at all? Its trusted network surface must be
   shown to be smaller than a Dolt login's, and it must add no fleet-wide single point of
   failure.

The comparison has to cover attack surface and failure modes, and say whether B replaces A, and
when.

**Not asked:**

- whether the fence itself holds on default paths (`bh-vje85`, `bh-jbb6r`);
- the bd claim semantics of forwarding (`bh-sieai` §8);
- state/work pairing on frame loss (`bh-55vvh`).

Those results are reused, not re-tested. Availability abuse through globals (`read_only`,
`max_connections`) is `bh-wtsrc` E4. It is restated here only where it separates A from B.

## Method

1. **Read:**
   - the ADR: §2, §3, §5, conditions 9 and 16, Decisions 6 and 7, Follow-up decisions 1 and 5;
   - `bh-wtsrc` E3 (trigger DML is invoker-checked), E4 (globals are not privilege-checked) and
     the T16 row;
   - `bh-vje85` E5–E11, the stale-writer path table and the force-path map;
   - `bh-sieai` §8 (forward path);
   - `bh-jbb6r`: the composed harness and its invariant checker.
2. **Ran** one pytest module on the composed prototype:
   - bd `1.3.0 (f45b249ce)` and Dolt `2.3.5`, the current pin;
   - frames `q` (bd embedded, the adopter), `p` (bd shared-server, the primary, on its own
     owned `dolt sql-server`) and `f` (the forwarding host);
   - one `git+file://` hive remote, with HQ placement on an owned Dolt server.

   Every server is test-owned. Each has its own port, data directory, `HOME` and
   `DOLT_ROOT_PATH` under `tmp_path`. **No live HQ or hive server was contacted, and no live
   global was read or changed.** The `SET GLOBAL` and `SET PERSIST` probes land only in the
   owned servers. The tests:

   | Test | Measures |
   |---|---|
   | E1 | A stale forwarded write on a demoted primary, under three global settings × five honest publish paths (15 runs) |
   | E2 | A forwarder login granted `ALL` on the hive database: does a stale write land with *no* global? |
   | E3 | A forwarder login granted database-wide DML: can it spoof the guard identity? |
   | E4 | Capability matrix for three grant shapes (12 probes each), plus bd create/claim/close under each |
   | E5 | A forwarded transaction in flight across the demoted primary's divert-and-reset |
   | E6 | An option B prototype: a small RPC service on the primary (closed verb set, per-frame bearer token standing in for an mTLS client certificate) driving create, a 4 × 4-way claim race, close, refusals and demotion |

**Reproduce:**

```sh
BH_UHX2R_EVIDENCE=/tmp/uhx2r.jsonl \
  uv run pytest tests/spikes/test_bh_uhx2r_forward_rpc_int.py -n 2 -p no:cacheprovider
```

The run quoted below passed six of six (260 s wall clock at `-n 2`). E2 and E3 were re-run
after E3's push was attributed in the move log (two of two passed).

## Evidence

**E1. A forced global lands no stale write on any honest path.** The 15 runs shared one setup.
`p` was primary. A forwarded `bd create` executed on `p`, and its guard mark was stamped at
`p`'s epoch. `q` then adopted the next epoch. Using bd's default (root) login, the forwarder
then set the globals. A fresh session confirmed they had taken effect:
`inherited = [1, 0]` for `force` and `[1, 1]` for `force+txn_commit`.

| Path on the demoted primary | no global | `dolt_force_transaction_commit=1` | `…force=1` + `dolt_transaction_commit=1` |
|---|---|---|---|
| `bd dolt pull`, then `bd dolt push` | pull: constraint violation; push: non-ff | same | pull: `the table(s) bh_write_mark have constraint violations`; push: non-ff |
| `bd sync` | constraint violation | same | same, as `dolt pull failed: … constraint violations` |
| fetch, `bd vc merge origin/main`, `bd dolt push` | `Please commit your changes before you merge` (the adopt sentinel, `bh-vje85` E8); non-ff | same | same |
| SQL session, no `--force`: commit, fetch, `DOLT_MERGE`, `DOLT_COMMIT`, `DOLT_PUSH` | `DOLT_MERGE`: `…constraint violations, transaction rolled back` | `DOLT_MERGE`: **ok** (`conflicts found`). Then `DOLT_COMMIT`: **`error: the table(s) bh_write_mark have constraint violations`**; push: non-ff | `DOLT_MERGE` and `DOLT_COMMIT` both refuse on the violation |
| bh managed push (`cf.managed_push`) | `diverted` | `diverted` | `diverted` |

Results:

- **Landed: 0 of 15.** The invariant checker was clean at the end.
- The global does exactly one thing. It moves the refusal of a violated merge from the
  merge's transaction commit to `DOLT_COMMIT`. `DOLT_COMMIT` checks constraint violations on its
  own, and the global does not lift that check. Only `DOLT_COMMIT --force`, or deleting the
  violation rows, gets past it.
- `dolt_transaction_commit=1` makes things stricter, not weaker: the merge itself then refuses.
- **`dolt_allow_commit_conflicts` cannot be set globally** on 2.3.5:
  `Variable 'dolt_allow_commit_conflicts' is a SESSION variable and can't be used with SET GLOBAL`.
  So the condition 16 watchdog cannot observe it as a global. A session can still set it, and
  only for itself.

**E2. A login with the right grants lands a stale write with no global at all, and
`fence_audit` does not see it.** The forwarder account was granted `ALL ON fx.*`. With that
grant bd's own post-write `DOLT_COMMIT` succeeds; with database-wide DML alone it does not
(E4).
The setup was the same as E1, and the primary ran only its routine fetch. In one session, the
forwarder ran:

- `DOLT_COMMIT('-Am')`;
- session `SET @@dolt_force_transaction_commit = 1`;
- `DOLT_MERGE('origin/main')`;
- a re-stamp: `UPDATE bh_write_mark SET epoch = @live`;
- `DELETE FROM dolt_constraint_violations_bh_write_mark`;
- `DOLT_COMMIT('-Am', 'routine')`.

Every statement returned `ok`. The primary's next **honest** managed push then fast-forwarded
the stale write onto remote `main`: `landed: true`.

- The history invariants catch it: I3 (`late_epoch_commit`), and I1 through the fixture's move
  log.
- The product detector checks `fence_audit` (`stale_marks`, `epoch_regressed`,
  `placement_ahead`) are **clean**, because the re-stamp left no stale mark. Without the
  re-stamp, `stale_marks` does flag it (exploratory run).
- The same login was refused `DOLT_FETCH` and `DOLT_PUSH --force` (`command denied`). It does
  not need either: the primary fetches and pushes for it.

**E3. Database-wide DML spoofs the guard identity, and nothing detects the second writer.**
The forwarder account was granted `SELECT, INSERT, UPDATE, DELETE ON fx.*`. Steps:

1. After the demoted primary's managed rejoin (divert, then reset to the remote head), a
   forwarded `bd create` is refused:
   `Error 1644 (45000): bh-guard: this replica is not the bh_writer for main`.
2. The forwarder runs `UPDATE bh_local_ident SET frame = 'q'`, which returns `ok`.
   `bh_local_ident` is the node's `dolt_ignore`d identity row. It lives in the hive database,
   so a database-wide grant covers it.
3. The next forwarded `bd create` succeeds. Its mark is stamped at the **live** epoch
   (`remote_marks: [3, 3]`).
4. The primary's managed push lands it.

Every history invariant and `fence_audit` were clean. Only I1 flagged it, and only because the
fixture's move log records which frame pushed. Production lacks that information, because bd
commits as `beads`/`root` (`bh-jbb6r` E4).

The result is a second claim-granting writer that nothing can detect. Claims are then granted
in two places, which voids the "claims granted in one place" property that forwarding exists
to provide (`bh-sieai` E8).

**E4. Capability matrix per grant shape.** "Narrow" is table-scoped:

- DML on each bd base table and on `dolt_ignore`, which bd seeds when it opens a database
  (without it, `bd create` fails with `Access denied … to table 'dolt_ignore'`);
- `SELECT` on `bh_writer`, `bh_epoch_live` and `bh_local_ident`;
- `SELECT, INSERT` on `bh_write_mark`, which the guard trigger's inline insert needs because
  trigger DML is invoker-checked (`bh-wtsrc` E3).

| Probe | `ALL ON db.*` | DML `ON db.*` | narrow (table-scoped) |
|---|---|---|---|
| `SET GLOBAL dolt_force_transaction_commit = 1` | ok | ok | **ok** |
| `SET PERSIST max_connections = 777` | ok | ok | **ok** |
| `UPDATE issues …` (any row) | ok | ok | **ok** |
| `UPDATE bh_local_ident …` (E3) | ok | ok | refused |
| `UPDATE bh_write_mark …` (E2 re-stamp) | ok | ok | refused |
| `DELETE FROM dolt_constraint_violations_bh_write_mark` | ok | ok | refused (`Access denied … to table`) |
| `CALL DOLT_COMMIT` | ok | refused | refused |
| `CALL DOLT_MERGE` | ok | refused | refused |
| `CALL DOLT_RESET('--hard')` | **ok** | refused | refused |
| `CALL DOLT_FETCH` / `DOLT_PUSH('--force', …)` | refused | refused | refused |
| bd `create` / `update --claim` / `close` | rc 0 | rc 0, commit deferred | rc 0, commit deferred |

"Commit deferred" means that without `DOLT_COMMIT` rights, bd prints
`post-tx dolt commit failed … (data already committed; change rides the next dolt commit)`.
The write is in the primary's working set, and the primary's managed push commits it. That push
already runs `bd dolt commit` first, because server-mode bd leaves the mark unstaged anyway
(`bh-vje85` E7). The exit code is 0. So the narrow shape closes E2 and E3 and keeps bd working.

What no grant can remove:

- server-wide `SET GLOBAL` and `SET PERSIST`, which are availability abuse (`bh-wtsrc` E4,
  T16);
- unrestricted DML on **every** bead row of the hive. A forwarder can rewrite, close or
  reassign any bead, not just the ones it claims.

`ALL ON db.*` also hands a forwarder `DOLT_RESET --hard`, which can discard the primary's
unpublished work.

**E5. An in-flight forwarded transaction is acknowledged and then silently dropped by the
divert reset.**

1. A forwarder opened a transaction on `p` and inserted a row; the guard passed, because `p`
   was still writer.
2. `q` adopted, and `p` ran its managed rejoin: fetch, divert, then
   `DOLT_RESET('--hard', 'origin/main')`.
3. The forwarder's `COMMIT` returned **ok**.

The row was gone from `p`, there were 0 violations, and it never landed. The result was the
same with the forced global. In the control, with no reset in between, the row stayed and
landed.

This is not a landing path: the fence holds. It is a lost acknowledgement. bh's claim path
already reads back (`claim_won`), but a forwarded `create` or `close` that is acknowledged in
that window is lost. The ack is still lost under B, but there it is avoidable: the primary owns
the service and can quiesce it before it resets (Recommendation 3).

**E6. Option B works: the prototype carries create, claim and close, and the executor holds no
Dolt login.** The prototype is about 70 lines (`_RpcHandler` and `RpcService` in the test
module):

- an HTTP service on the primary's host;
- a per-frame bearer token, standing in for an mTLS client certificate;
- a closed verb set (`create`, `claim`, `close`), with the bd argv built by the service from
  validated fields (bead id `^fx-[a-z0-9.]{1,32}$`, actor `^[a-z0-9][a-z0-9._-]{0,31}$`, title
  ≤ 200 characters);
- bd run on the primary's own workspace and login, with `BEADS_ACTOR=<frame>/<actor>`.

Measured:

- **Claims:** 4 rounds × 4 concurrent `claim` calls produced exactly one winner per round
  (`[1, 1, 1, 1]`). The winner's read-back row names `f/<actor>`, so the claim is granted in one
  place, as on the forward path.
- **Refusals:**

  | Request | Response |
  |---|---|
  | no token | `401` |
  | a `sql` verb carrying `SET GLOBAL read_only = 1` | `403 verb not offered` |
  | `close` with id `fx-1; --force` | `400` |

  None of them reached bd or Dolt.
- **Demotion:** after `q` adopts and `p` diverts, an RPC `create` returns `rc 1` with the
  guard's refusal. B fails closed through the same in-data guard and fence; it does not replace
  them.
- **Latency:** `create` p50 was 0.476 s over RPC against 0.485 s for direct forwarding. bd's
  process start dominates both. B adds no measurable cost at this scale.
- **Credentials:** the executor side is `urllib` plus a token. It never opens a SQL connection,
  so there is no Dolt account to grant, pin or leak.

**Attack surface and failure modes, A against B.**

| | Option A: Dolt login (narrow grants, E4) | Option B: per-hive bh RPC |
|---|---|---|
| Credential on the executor | a SQL account on the primary's hive server (host-pinned, TLS) | a client cert or token for one service, scoped per frame |
| Language exposed | full SQL grammar, all Dolt procedures (privilege-checked), session variables | a closed verb set with typed, validated fields |
| Server-wide `SET GLOBAL` / `SET PERSIST` (T16) | **always possible** (E1, E4): `read_only`, `max_connections`, persisted config | not expressible (E6 `403`) |
| Stale write landing | not via a global (E1). Via grants (E2) and identity spoof (E3) **only if grants are database-wide**: narrow closes both | not expressible; the fence is unchanged behind it |
| Row scope | any row of any bead table | per verb; can be narrowed to "own claims" in the service |
| Detection if abused | E2 is invisible to `fence_audit` after a re-stamp; E3 is invisible to everything | requests are logged per frame by the primary's bh process |
| New code on the trust path | none (bd and Dolt) | the service: request parsing, auth, argv construction. A per-hive bug affects one hive |
| Fleet-wide single point of failure | none | none. It runs in the primary's own bh process; if the primary is down its hive is down under A or B (ADR §3) |
| New failure mode | none | the service down while the primary's Dolt is up: that hive's forwarders stop until it restarts. One hive |
| bd heartbeat and lease renewal from forwarders | native (bd talks to the server) | must be forwarded as verbs too, or renewed on the primary (M12/O9 open item) |
| Quiesce before divert (E5) | the primary must kill foreign sessions | the primary drains its own service |

## Verdict — **NO-GO** (B does not replace A in 0.23.0; B is feasible and deferred)

- **The forced global does not let a stale write land.** In 0 of 15 honest-path runs did a
  stale write land under `dolt_force_transaction_commit=1`, alone or with
  `dolt_transaction_commit=1` (E1). `DOLT_COMMIT`'s own constraint-violation check, which the
  global does not lift, is what holds. So the urgency that motivated B is not there.
- **The real exposure is the grant shape, not the global.** A forwarder login with
  database-wide grants can land a stale write without any global (E2) or become an undetectable
  second writer (E3). The narrow, table-scoped shape closes both and keeps bd's verbs working
  (E4). That is a change to option A (M12, condition 16), not a reason to replace it.
- **B is feasible and strictly smaller.** It carries the verbs, keeps the single claim point,
  adds no fleet-wide single point of failure and costs no latency (E6). It also removes what A
  cannot: server-wide globals and unrestricted row DML. Those are availability and integrity
  risks from a misbehaving forwarder, not fence breaches. They do not justify reworking M12
  before the three new executors, all of which are operator-built frames.

No replan is triggered. C/O8 may proceed on option A once M12 carries Recommendations 1–3.

## Recommendation

1. **Amend M12 and condition 16 with the narrow grant shape (operator decision).** Forwarder
   accounts get table-scoped grants only:
   - DML on bd's tables and `dolt_ignore`;
   - `SELECT` on `bh_writer`, `bh_epoch_live` and `bh_local_ident`;
   - `SELECT, INSERT` on `bh_write_mark`.

   They must **never** get a database-wide grant and never `DOLT_COMMIT` rights; the primary's
   managed push commits for them. Add a conformance check that fails when any non-operator
   account holds a database-level privilege, or any privilege on a `bh_*` table beyond these.
   M1's install should regrant when bd adds a table, so it should re-derive the list from
   `SHOW FULL TABLES`.
2. **Correct the condition 16 watchdog list.** `dolt_allow_commit_conflicts` is session-only on
   2.3.5 (E1), so watching it as a global is a no-op. Keep `dolt_force_transaction_commit`,
   `dolt_transaction_commit` and `read_only`, and add `max_connections` (`bh-wtsrc` E4). State
   plainly that the watchdog covers availability. Per E1, a forced global is not an integrity
   breach of the fence.
3. **Quiesce forwarders before a divert reset (M12).** Before `DOLT_RESET('--hard')` on a
   demoted primary, kill the forwarder sessions on its hive server. Forwarders must read back
   every write, not only claims, and treat a missing row as "not done" (E5).
4. **Close two `fence_audit` blind spots (M1, M10).**
   - Add a history check equivalent to I3: no non-adopt commit stamped at epoch *e* outside the
     ancestry of every later adopt. E2 is invisible to the three current checks.
   - Treat E3 (a non-writer frame writing at the live epoch) as **undetectable from data**. The
     narrow grant is the only defence, which makes Recommendation 1 binding rather than
     hardening.
5. **Keep B on file as a dormant design, not a 0.23.0 item (operator decision on filing).**
   Revisit it to replace A when any of these becomes true:
   - an executor is admitted that the operator does not build and control;
   - per-bead or per-claim authorization is wanted for forwarders;
   - server-wide globals become an observed availability problem;
   - a Dolt pin bump still shows `SET GLOBAL` unprivileged after the Φ3 soak.

   Shape, from E6:
   - an mTLS listener in the primary's bh process for each hive it holds;
   - a closed verb set covering create, claim, close and bd heartbeat/renew, plus a drain verb
     for the divert;
   - argv built only from validated fields;
   - per-frame request logs;
   - forwarders fail closed when the service is unreachable.

   It needs no new fleet component and changes no fence or guard behaviour.
6. **Pin the evidence.** Re-run this module with the bh-sieai/vje85 canaries on every Dolt or
   bd pin bump (condition 9, F3). A bump that lets `DOLT_COMMIT` honour the forced global would
   turn E1 into a landing path and reopen this verdict.

**Needs an operator decision:**

- accept NO-GO (B deferred);
- adopt Recommendations 1–4 into M12, M1 and M10 and amend condition 16's text;
- whether to file B (Recommendation 5) now as a dormant bead or leave it to a future replan.
