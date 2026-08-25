# First operator UI release

The first Beadhive operator UI is a deliberately narrow local profile. It is
**loopback-only, unauthenticated, read-only**, and intended for one operator on one machine.
It is not a remote-access, multi-user, or privileged-control release.

## Start the installed commands

Install `bh` and the `beadhive-operator-ui` package, configure the hives the normal `bh` CLI
will read, then start these foreground processes in separate terminals:

```sh
# Terminal 1: Beadhive core daemon
BH_OPERATOR_UI_ORIGIN=http://127.0.0.1:3000 bh host daemon serve --host 127.0.0.1 --port 8420

# Terminal 2: static operator UI host
BH_OPERATOR_UI_ORIGIN=http://127.0.0.1:3000 beadhive-operator-ui --host 127.0.0.1 --port 3000 --daemon-url http://127.0.0.1:8420
```

Open <http://127.0.0.1:3000>. Keep `BH_OPERATOR_UI_ORIGIN` byte-identical in both terminals.
The daemon accepts that one browser origin, while the UI host binds that exact literal-loopback
origin. Stop either foreground process with `Ctrl-C`.

The browser reads the daemon directly. It first loads the flat FactorySnapshot v1 factory
response, derives each authoritative hive route from the full `provider/org/repo` identity,
loads a hive snapshot, and only then applies `operator-event` SSE frames after the snapshot
cursor. Run activity is also read directly from `/api/v1`. Browser requests omit cookies and do
not send an `Authorization` header in this phase-one profile.

## Safety boundary

The phase-one daemon exposes only the read-only operator `GET` routes, their SSE stream,
OpenAPI, and minimal health response on literal `127.0.0.1`. It validates `Host` and either
rejects `Origin` or requires the one configured local UI origin.

The profile does **not** expose:

- MCP over HTTP;
- activity `POST` or any other write route;
- terminal attach tokens or terminal WebSockets;
- command, supervisor-control, or mutation UI; or
- any non-loopback listener.

Do not place a proxy in front of this profile, forward its ports, bind it to a LAN/tailnet
address, or treat a different spelling of loopback as equivalent. Authentication and
authorization remain prerequisites for phase two and for every remote or multi-user use.
That follow-on work is tracked in `bh-xw03t`.

## What recovery looks like

The UI renders the authoritative snapshot before live deltas. A checked cursor conflict,
`reset`, sequence gap, or producer-epoch change discards stale live state and performs one fresh
snapshot/subscription. A temporary disconnect replays retained events. If the daemon is down,
the UI says so rather than starting a hidden daemon or falling back to an embedded source.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| UI reports **Local daemon unavailable** | Confirm terminal 1 is still running on `127.0.0.1:8420`, then use the UI retry action. CLI and stdio MCP continue independently. |
| Daemon rejects the browser with `403` | `BH_OPERATOR_UI_ORIGIN` must be exactly `http://127.0.0.1:3000` in both processes. No wildcard origin is accepted. |
| Request fails with a Host error | Use the literal `127.0.0.1` URLs and the ports shown above. Hostnames, `127.1`, other `127/8` addresses, integer/octal spellings, credentials, paths, queries, fragments, HTTPS, and IPv6 are refused. |
| Either process says the address is in use | Stop the old foreground process or choose a new pair of ports, updating `BH_OPERATOR_UI_ORIGIN` and `--daemon-url` consistently. |
| UI resnapshots after reconnect | This is expected after a daemon restart, reset, expired replay cursor, sequence gap, or producer-epoch change. It must not retry stale state. |
| UI cannot see a hive or activity detail | Read the displayed coverage state. The public source reports complete, partial, or unavailable coverage honestly; it does not invent provider detail. |

The legacy Node relay and `beadhive-canvas` remain a diagnostic oracle and rollback path in the
beadhive-ui repository. They are not part of this product server, must not be run as a second
production relay, and expose no release claim for this phase-one profile.

## Manual release proof

From a clean core checkout with a clean sibling beadhive-ui checkout:

```sh
just check-operator-release /path/to/beadhive-ui
```

The core recipe delegates to the UI-owned browser proof. It is intentionally absent from
`just check` and `just check-all`: the UI repository owns its install/build prerequisites,
Chromium, product bundle, browser adapters, process groups, and proof assertions. See the
[dated evidence report](proof/operator-loopback-ui-release-2026-08-25.md).
