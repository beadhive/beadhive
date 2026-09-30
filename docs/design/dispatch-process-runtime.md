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
It checks its injected eligibility predicate both at intake and immediately before
each spawn. SIGTERM stops intake and wakes the poll sleep. Each live `bh work loop`
child receives SIGTERM directly, allowing LocalLoop's cancellation ladder to save
its branch, release its claims, and reap its seat process groups. The drain window
is 60 seconds. External supervisors must allow more than 60 seconds before forcing
termination. A completed drain exits normally; an expired drain fences remaining
loop groups and raises an error, because safe checkpoint completion is unverified.
Unrecoverable startup or runtime errors also propagate for supervisor restart.

## Integration checkpoint

U3 (`bh-38h7p`) has not yet supplied the authoritative verified-heartbeat eligibility
adapter. Process dispatch therefore fails closed with `eligibility_unavailable`.
The injection tests prove the scheduling seam and actual SIGTERM delivery; they
are not evidence that production frames can yet claim work. Replace this adapter
with U3's common eligibility function and validate eligible/ineligible real frame
records before submitting U7 (`bh-bfb2y`). The existing `bh work claim` guard must
also enforce U3 so eligibility changes inside a spawned loop cannot admit work.
