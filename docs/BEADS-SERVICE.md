# Beads service (`bd serve`)

Beadhive's API-first commands (starting with `bh work approve` / `bounce`, bh-bwnys) read and
write beads over the Beads v1.3 HTTP API instead of shelling out to `bd` for every call. That API
is served by `bd serve`. This page covers how Beadhive runs it, what it costs, and what to check
before running many of them against one shared Dolt server.

The binding decision is
[bh-ie41e.5](spikes/bh-ie41e.5-bd-serve-adoption-decision.md): one supervised service per hive,
bound to exactly that hive, loopback only, and **never started per request or per agent**. The
Beadhive host runtime owns it; commands never start, stop, or address `bd serve` themselves.

## How it runs

- **One service per hive, always on.** A hive's service runs until it is stopped. There is no
  lazy start on first use and no idle sleep or suspend.
- **Commands never start it.** A command that needs the API resolves the hive's running service.
  If none is running (or it is stale, not ready, or bound to a different workspace) the command
  fails closed with `start it with bh host beads start --hive <hive>`.
- **Per hive, never fleet-wide by default.** `start`, `stop`, `run`, `enable`, and `disable` act
  on one hive (`--hive`, defaulting to the hive you are in). Only `status` takes `--all`.
  Turning services on across the fleet is a per-hive choice.
- **Server-mode hives only.** Beads 1.3 does not serve its API from an embedded-Dolt store; those
  hives are refused with a clear message. Beadhive does not support embedded Dolt on this path.

## Commands

| Command | What it does |
|---|---|
| `bh host beads start` | Start a detached `bd serve` for the hive. Idempotent: a second start reports the running one. |
| `bh host beads stop` | `SIGTERM`, then `SIGKILL` after the stop budget, and remove the record. If a foreground `run` owns the service it signals that supervisor; if the host daemon owns it, it refuses and points to `disable`. |
| `bh host beads status [--all]` | Report `absent`, `stale`, `mismatch`, `unready`, or `running` (context-verified) per hive. Exits non-zero unless every reported hive is running. |
| `bh host beads run` | Foreground supervisor: keeps the service alive with bounded backoff (0.5 s doubling, capped at 30 s, gives up after 5 consecutive failures; the count resets after 60 s healthy). The shape a systemd unit or an operator terminal runs. |
| `bh host beads enable` / `disable` | Record or remove a durable intent. While the authenticated host daemon runs, it checks intents every 2 s and starts, adopts, or stops the service as its child with the same backoff. With no enabled hive the daemon supervises nothing. |

All accept `--json`. Who owns the service on Factory hosts (systemd unit versus host-daemon
child) is not decided here; it belongs to `bh-infra-920k.2`. Both owners write the same record, so
consumers do not care which one is running.

## Files

Everything lives under `$BH_HOME/run/beads-serve/<sanitized hive key>/`:

| File | Contents |
|---|---|
| `endpoint.json` | Address, port, token-file path, project id, database, `bd` executable and version, pid, supervisor, start time. Mode `0600`, written atomically. **Never contains the token.** |
| `token` | Bearer token, mode `0600`, in a `0700` directory. Minted once; not rotated yet. |
| `serve.lock`, `serve.log` | Start/stop serialization and the service's log. |
| `supervise.json` | The daemon intent written by `enable`. |

Consumers call `host_beads.resolve_session(main, capabilities, entry=...)`. It returns an
unopened, context-verified `BeadsSession` for the running service and raises
`ServiceUnavailable` (or `HiveNotServable` for embedded Dolt) otherwise. Passing the `entry` from
`worktree.locate` skips a registry lookup worth about 0.44 s.

## Cost

Measured 2026-09-26 on one server-mode hive (Beads 1.3.0, bd's shared Dolt server), one service
idle for about 70 s after start, no requests:

| Metric | Idle `bd serve` |
|---|---|
| Resident memory (RSS) | 96.7 MiB, flat over 60 s |
| Proportional share (PSS) | 63 MiB |
| Private (unshared) memory | about 25 MiB |
| Shared clean memory | about 70 MiB, the `bd` binary's pages, shared by every `bd` process of the same build |
| CPU | effectively zero (0.2 % at 60 s; 0 CPU-seconds over 68 s) |
| Threads | 18 |
| Upstream database connections | 1 |

Latency: a cold start becomes ready in 2.5–2.7 s, once per hive. Resolving and connecting to a
running service takes about 110 ms warm (235 ms on first call). The per-command spawn this
replaces cost 1.2–1.8 s on every call.

### Many hives on one host

Each additional idle service costs roughly its private memory, about 25 MiB; the ~70 MiB of
binary pages are paid once. Twenty idle hives are therefore about 0.5–0.6 GB in practice. Summing
RSS per process would suggest about 2 GB, which double-counts the shared pages. Idle CPU is
negligible, so running 20 or more always-on services does not need suspension.

The rollout gate in the [bd serve benchmark](spikes/bh-ie41e.4-bd-serve-benchmark.md) is at most
**100 MiB idle RSS** and at most **two steady-state database connections** per idle service. One
idle sample is within both, but 96.7 MiB RSS is close to the ceiling. Busy RSS, growth across
restarts, and fleet-scale behavior are **not measured yet**; measure them before a fleet rollout.

## Database connections under load

On a server-mode host every hive's store is a database in `bd`'s shared `dolt sql-server` (see
[Dolt](DOLT.md#the-store-engine)). `bd serve` does not connect to Dolt directly; it connects
through `bd`'s database proxy (`bd db-proxy-child`), which fronts the shared server on
`127.0.0.1:3308`. So every hive's service ultimately draws on **one** Dolt server.

An idle service holds one connection. Under load each service may open up to its own pool limit.
Beads 1.3 logs a pool of **20 open / 16 idle** database connections per service, plus 16
in-flight requests and 64 client connections. The worst case against the shared server is
therefore about:

```text
hives with a running service × 20 open connections
+ every other bd / bh client connecting at the same time
```

Twenty busy hives can reach roughly 400 connections from the services alone. Before enabling
services across many hives on one host, check the shared server's limit and current use:

```sql
-- against the shared Dolt server (127.0.0.1:3308)
SHOW VARIABLES LIKE 'max_connections';
SHOW STATUS LIKE 'Threads_connected';
SHOW STATUS LIKE 'Max_used_connections';
```

`~/.beads/shared-server/dolt-server-config.yaml` sets no `listener.max_connections` today, so
Dolt's built-in default applies; confirm the effective value with the query above rather than
assuming it. If the worst case can exceed it, raise `listener.max_connections` in that config (bd
owns the server, so coordinate with its restart) or cap how many hives run a service on the host.
Watch `Max_used_connections` during a busy period to see real headroom.

## Known limits

- Readiness is checked at start; afterwards the supervisor checks liveness only, so a database
  outage does not restart the service (consistent with the spike's `db_unavailable` handling).
- The token is not rotated; there is no rotation command yet.
- A detached service later adopted by the daemon keeps supervisor `none` in its record.
- `bh host beads status` takes about 4 s, mostly CLI startup and the registry lookup.
