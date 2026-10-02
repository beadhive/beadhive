# Foreground dispatch runtime

`host.dispatch.backend: process` selects the foreground picker. The existing
`container` and `launchd` names select the same runtime. Systemd remains the default.

An external supervisor invokes:

```sh
bh host dispatch run --hive github/acme/widgets
```

Container init, Kubernetes, frame-agent, or launchd owns restart policy and invokes
this command directly. `enable` and `disable` explain this ownership rather than
installing a service or creating a detached child. Status does not invent an OS
service or claim a process is running when the external supervisor has not been
queried.

The picker retains the existing hive lease check and `max_epics_in_flight` cap.
Before reading candidates and immediately before each spawn, process mode reloads
current effective configuration and calls U3's `local_intake_decision` for the exact
hive. Enrolled frames need verified admission and current authoritative lease
ownership; legacy hosts retain their existing primary-lease policy. An unreadable
configuration or authority denies new intake. The spawned loop rechecks intake at
the claim API write, including a config or authority change after the picker's
last check. Both process and systemd dispatch renew their shared lease keeper using
a current config read; expired or unavailable central configuration denies renewal
even when a loop started under an earlier valid revision. SIGTERM stops intake and
wakes the poll sleep. Each live `bh work loop`
child receives SIGTERM directly, allowing LocalLoop's cancellation ladder to save
its branch, release its claims, and reap its seat process groups. The drain window
is 60 seconds. External supervisors must allow more than 60 seconds before forcing
termination. A completed zero-exit drain exits normally; a nonzero child exit
reports an unverified checkpoint after reaping. An expired drain fences remaining
loop groups and raises an error, because safe checkpoint completion is unverified.
Unrecoverable startup or runtime errors also propagate for supervisor restart.
