# Dolt issue drafts (dolthub/dolt)

> **Tracked internally, not filed upstream.** ADR Decision 7
> (`docs/design/hive-writer-partitioning-adr.md`): these drafts stay in this tree. Fixes would
> make the fence conditions belts rather than the only brace; none is required. Evidence is
> pinned to Dolt 2.3.5 and the bd build exercised in the cited spike.

## dolt-1: `SET GLOBAL` / `SET PERSIST` are not privilege-checked

*Evidence:* `docs/spikes/bh-wtsrc-receiver-removal-liveness.md` E4
(`test_any_frame_principal_can_set_server_globals_and_persist_config`). Dolt 2.4.0 and 2.4.1
release notes show no change.

*Title:* `SET GLOBAL` / `SET PERSIST` succeed for users without `SUPER` / `SYSTEM_VARIABLES_ADMIN`.

*Repro (2.3.5, sql-server):* `CREATE USER u IDENTIFIED BY 'x'; GRANT SELECT ON db.t TO u;`, then
as `u`: `SET GLOBAL read_only=1; SET GLOBAL max_connections=1; SET PERSIST max_connections=777;`.
All succeed. The last writes `sqlserver.global.max_connections` into the server user's
`config_global.json`.

*Expected (MySQL 8):* `ERROR 1227 ... need SUPER or SYSTEM_VARIABLES_ADMIN`, and
`PERSIST_RO_VARIABLES_ADMIN` for `SET PERSIST`.

*Impact:* any least-privilege account can deny service to the whole server or force
`dolt_force_transaction_commit`.

## dolt-2: trigger bugs found by the bh-sieai guard (R5)

*Evidence:* `docs/spikes/bh-sieai-write-guard-and-nonprimary-writes.md` T1, T3, Recommendation 5.
All reproduced on 2.3.5. One issue each, or one umbrella:

1. `@`-variable `IF` comparisons fail open in triggers (`IF @w <> @me THEN SIGNAL` with
   session variables set in the same trigger accepts the write).
2. Statements after a `CALL` in a trigger body are silently skipped.
3. DML inside a procedure `CALL`ed from a trigger is kept under autocommit but silently dropped
   inside `BEGIN ... COMMIT`.
4. With a trigger that `CALL`s, a multi-row statement runs the trigger body for the first row only.
5. `information_schema` reads inside a `CALL`ed procedure return 0 rows from the second row on.
6. Multi-table `DELETE` is refused on any table with triggers
   (`delete from with explicit target tables does not support triggers`).
7. `dolt_commit_ancestors` fails `max1Row` on merge heads.

Already known upstream (not a seventh new item): the scalar subquery inside
`INSERT ... VALUES` ("unable to find field with index N").

## dolt-3: sql-server ignores `--ref` on a second remote with the same URL

*Evidence:* `docs/spikes/bh-jbb6r-writer-partitioning-e2e.md` E8.

*Observed (2.3.5):* on a sql-server, `CALL DOLT_REMOTE('add', '--ref', 'refs/dolt/frame/q',
'frame-q', <url>)` succeeds and `dolt_remotes` shows the `git_ref`, but `DOLT_FETCH('frame-q')`
sets `remotes/frame-q/main` to the same hash as `origin/main` (it re-reads `refs/dolt/data`), so a
later merge is a silent no-op. Adding the remote as `<url>/` makes the fetch read the private ref.

*Expected:* the new remote's `--ref` is honoured, or `DOLT_REMOTE add` refuses a second remote
that aliases an open one.

*Workaround in bh:* a distinct URL per frame-private remote, plus an assertion that
`remotes/<frame>/main` differs from `origin/main` after fetch.
