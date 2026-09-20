# Dolt 2.3.5 fresh-database reset readiness smoke check

Date: 2026-09-20 UTC

Host: `beadhive-factory`, Linux 6.12.100, x86_64, 24 logical CPUs

Beadhive base commit: `5752040e5ffc345eec92db6f5a7810104c95d074`

## Scope and verdict

The operator explicitly reduced this check from the originally requested 100-or-more paired trials
under concurrent CPU load to a focused functional readiness smoke check. The reduced check used
one Dolt 2.3.5 trial and one same-host, same-session Dolt 2.3.2 control trial. No concurrent CPU
load arm was run.

Both trials passed: each server created a fresh database, immediately completed
`CALL DOLT_RESET('--hard')`, and answered a follow-up query. The observed result was **0/1
failures for 2.3.5** and **0/1 failures for the known-clean 2.3.2 control**.

This establishes only the requested functional readiness signal for beginning the subsequent safe
migration sequence. It **does not satisfy** beads' original statistical fresh-database criterion,
does not measure the load-dependent failure rate, and is not evidence that the defect is absent.
For context, the one-sided 95% exact upper confidence bound for 0 failures in 1 trial is 95%; that
deliberately weak bound is another way to show why this result must not be represented as the
original measurement.

No 2.3.5 failure or hit was observed, so the stop-and-fallback condition was not triggered.

## Binary provenance and checksums

Both binaries came from official `dolthub/dolt` GitHub release assets and were realized only with
`nix build`; neither was installed into a profile or copied over an existing executable.

| Role | Release | Official asset | GitHub-published SHA-256 | Runtime identity |
| --- | --- | --- | --- | --- |
| Probe | [v2.3.5](https://github.com/dolthub/dolt/releases/tag/v2.3.5), target `ad65af6cc937d10fa3c88e2041fed4325968b581` | `dolt-linux-amd64.tar.gz` | `c49d4c3e004cf1581ba0d4a00c5023a26f84eb2ec15d5fe876eed36d5343f463` | `dolt version 2.3.5` |
| Control | [v2.3.2](https://github.com/dolthub/dolt/releases/tag/v2.3.2), target `f0feb352b1d3f0919b88ecd28869e515afb60ee0` | `dolt-linux-amd64.tar.gz` | `7a2949fa2b2b3799ee1e57e6d64519a8d65d675fd832f6469d4e07e5a1c72b14` | `dolt version 2.3.2` |

The fixed-output hashes in `nix/dolt-2.3.5-probe.nix` and
`nix/dolt-2.3.2-control.nix` encode those exact raw-archive digests. GitHub's release metadata
reported the same digest for each named asset. Nix rejected placeholder hashes first and realized
the derivations only after the raw archive hashes were pinned.

## Isolation and method

`scripts/probe-dolt-fresh-reset.py` creates a unique temporary root with Python's
`TemporaryDirectory`, then places the server data directory, Dolt configuration directory, Unix
socket, and server log beneath it. It asks the kernel for an unused loopback port and starts the
server bound to `127.0.0.1`. Each invocation creates a new server and a new database named
`fresh_reset_001`; it never discovers or connects to the shared-server endpoint. The script
terminates its child server and removes the complete temporary root on exit.

The SQL sequence was exactly:

```sql
CREATE DATABASE `fresh_reset_001`;
USE `fresh_reset_001`;
CALL DOLT_RESET('--hard');
SELECT DATABASE();
```

The final query checks that the same new database remains queryable immediately after the hard
reset. It does not perform a migration.

The reproducible build and execution shape was:

```console
$ nix build --no-link --print-out-paths --file nix/dolt-2.3.5-probe.nix
/nix/store/2rvf8k2h81v9x82rhp2fgb21gnd197ak-dolt-2.3.5-probe
$ nix build --no-link --print-out-paths --file nix/dolt-2.3.2-control.nix
/nix/store/h85vdsjr8cd1zj49r8rj9s2vy7br9wp2-dolt-2.3.2-control
```

A Nix-realized Python/PyMySQL environment was used only as the loopback SQL client. The two
recorded probe invocations produced:

```text
version=dolt version 2.3.5
trial=1 database=fresh_reset_001 reset=ok query=ok
summary=0/1 failures
scratch_removed=true

version=dolt version 2.3.2
trial=1 database=fresh_reset_001 reset=ok query=ok
summary=0/1 failures
scratch_removed=true
```

At no point did this work run `nix profile install`, address the live shared Dolt server, open any
of its databases, push Dolt data, or run `dolt gc`.

## Upstream-ready note for dolthub/beads#6329 and #6328

> Focused follow-up on Dolt 2.3.5, run 2026-09-20 on Linux x86_64. The official
> `dolt-linux-amd64.tar.gz` release asset (SHA-256
> `c49d4c3e004cf1581ba0d4a00c5023a26f84eb2ec15d5fe876eed36d5343f463`) reported
> `dolt version 2.3.5`. On a throwaway loopback SQL server and scratch datadir, I created a fresh
> database and immediately ran `CALL DOLT_RESET('--hard')`; the call and a following query both
> succeeded. Observed result: 0/1 failures. Exact v2.3.2, run as a same-host/session control with
> the identical method, was also 0/1. The operator explicitly reduced this from the requested
> 100+ paired/load trials to a functional readiness smoke check. Therefore this is only a signal
> that 2.3.5 is ready for our guarded migration sequence; it does not satisfy the pin note's
> statistical criterion, test the load-dependent rate, or demonstrate absence of the regression.
