# Dolt server hardening: globals watchdog and read-only root (bh-ijxif)

ADR `docs/design/hive-writer-partitioning-adr.md` §5 "Server globals (T16)", conditions 15 and
16. Evidence: `docs/spikes/bh-wtsrc-receiver-removal-liveness.md` E4.

## The exposure

On Dolt 2.3.5 any login can `SET GLOBAL` and `SET PERSIST` any variable, whatever its grants.
A global `dolt_force_transaction_commit=1` may defeat FK epoch retirement for every writer of a
hive; `SET PERSIST` survives a restart. Neither a grant nor `system_variables` prevents it, and
a session-level `SET` is always possible. The measures below **narrow and detect**; they do not
prevent. A forwarder is trusted not to issue a session-level `SET`.

## Placement rule

**Hive databases are never co-hosted on the HQ server.** The HQ server hosts HQ data only; each
primary's hive server hosts that hive's databases only. Co-hosting would put a hive's FK epoch
fence behind a global that every HQ login can flip.

## Read-only `DOLT_ROOT_PATH` (config templates)

`SET PERSIST` writes `$DOLT_ROOT_PATH/.dolt/config_global.json` through a temp file in the same
directory. Make that directory and file unwritable by the server's user and the write is refused
(measured on Dolt 2.3.5: `open .../config_global.json-<n>: permission denied`; the in-memory
value does not change either). Dolt still needs `.dolt/eventsData` writable to start, so that one
subdirectory stays writable.

Templates in `deploy/dolt/`:

| File | Use |
|---|---|
| `hq-server.yaml.example` | HQ server config (HQ data only, LAN + TLS) |
| `hive-server.yaml.example` | a primary's hive server config |
| `beadhive-dolt-hq.service.example`, `beadhive-dolt-hive.service.example` | units with the read-only root |
| `beadhive-dolt-*-watchdog.{service,timer}.example` | the operator-run watchdog, every minute |
| `watched-globals.json` | the watched list (default shape) |

Provision (as the operator, not the service user), once per server:

```sh
install -d -m 0555 -o root -g root /etc/beadhive/dolt-hive/root/.dolt   # then make it 0555 last
install -m 0444 -o root -g root config_global.json /etc/beadhive/dolt-hive/root/.dolt/
install -d -m 0755 -o dolt -g dolt /etc/beadhive/dolt-hive/root/.dolt/eventsData
```

`config_global.json` carries the Dolt identity (`user.name`, `user.email`) and
`versioncheck.disabled` / `metrics.disabled`. Do all provisioning before locking the directory,
and change settings only by re-provisioning as the operator.

## The watchdog

`scripts/dolt_globals_watchdog.py` (stdlib only, shells out to the `dolt` client). It is
read-only against the server's variables: it alerts and exits non-zero, it **never** resets a
value. The password comes from `DOLT_CLI_PASSWORD`. The client needs its own writable
`DOLT_ROOT_PATH`; never point it at the server's read-only root.

```sh
dolt_globals_watchdog.py check --host H --port P --user U [--tls]       # exit 0 / 2 drift / 3 unverifiable
dolt_globals_watchdog.py persist --host H --port P --user U [--tls]     # SET PERSIST must be REFUSED
sudo -u dolt dolt_globals_watchdog.py root-check --root /etc/beadhive/dolt-hive/root
```

### The watched list is data

The default list is the one in the bead: `dolt_force_transaction_commit`,
`dolt_allow_commit_conflicts`, `dolt_transaction_commit` (each expected `0`) and `read_only`
(expected `0`, for a writable primary or HQ server).

- `--config FILE` replaces the whole list (`{"expect": {name: value}}`, see
  `deploy/dolt/watched-globals.json`).
- `--expect NAME=VALUE` adds or overrides one entry; `--drop NAME` removes one;
  `--no-defaults` starts empty.
- A deliberately read-only replica sets `read_only=1`.

A variable that cannot be read as a global is reported `UNVERIFIABLE` (exit 3), never skipped
silently. Booleans compare as 0/1 (`ON`/`OFF`/`true`/`false` normalize).

### The persist check

The `persist` subcommand re-issues `SET PERSIST max_connections = <current value>`. With a
read-only root it is refused (pass). If it is accepted it alerts loudly (exit 2): the root is
writable, and the value written equals the current one, so the probe itself changes nothing
that matters. The restart half ("a `SET PERSIST` does not survive a restart") is exercised on a
scratch server by `tests/test_dolt_globals_watchdog_int.py`, which starts a server with a
read-only root, attempts a persist, restarts it, and also shows the contrast: with a writable
root the persisted value does survive the restart.

## Upstream drafts

The bd and Dolt issue drafts are in `docs/upstream/` (`bd-writer-fencing-issues.md`, `dolt-writer-fencing-issues.md`), marked "tracked internally, not
filed upstream" (ADR Decision 7).

## Reuse

B/M12 reuses the watchdog for forwarding primaries by pointing it at a primary's hive server
with its own `--config`. Deployment to the live HQ server and the factory's hive server is C/O0,
not part of this change.
