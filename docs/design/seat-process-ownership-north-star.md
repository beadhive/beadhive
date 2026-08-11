# Seat process ownership and observation — North Star

> **Status:** proposed. Three decisions are the operator's and are called out in
> [Open decisions](#open-decisions). Everything in [Already settled](#already-settled) is cited, not
> re-decided.
>
> **Date:** 2026-08-11 · **Bead:** filed alongside `bh-c6dk.13`/`.14`/`.15`
> **Related:** baml-harness [ADR 0010][adr10] (two supervisors, one record schema — *awaiting this
> sign-off*), baml-harness [ADR 0011][adr11] (cancellation cannot live in BAML), agent-hitch
> [ADR 0004][adr04] (the managed run record), `work-runtime-tiers-adr.md` Amendment 2,
> `loop-ownership-and-execution-memory-adr.md`

The question this answers: **now that seats are packed binaries the dispatch loop spawns directly,
rather than Claude Code sub-agents someone else manages — who owns those processes, what stops them
being orphaned, how do we notice one that has gone quiet, and what do we see while it runs?**

## Already settled

Cited so the North Star is a synthesis, not a re-litigation.

1. **The harness cannot own the process.** `baml.sys.exec`'s `ProcessOptions.stdin` is a static
   string fixed before launch, and `exec` returns output for an *already-finished* process. No
   edit to `seat_argv` can produce a bidirectional channel ([ADR 0011][adr11], from our own spike
   `bh-a7so.7` §13). Something outside BAML supervises the child. That is the premise, not a gap.
2. **Two supervisors, scoped by run lifetime, over one shared record schema** — [ADR 0010][adr10]'s
   recommendation C. `hitch run` supervises human-launched detached runs; the beadhive `local` tier
   supervises factory dispatch; neither reimplements the record.
3. **State is derived, never stored** ([ADR 0004][adr04]). Computed at read: `terminal.state` if
   terminated, else a liveness probe of `pid` **and** `pid_start`, else `crashed`. A store that
   cannot *assert* "running" cannot disagree with another store about whether something is running.
   The OS is the tiebreaker.
4. **herdr is a NO for this** (`bh-d75gv`, source read at v0.8.0). It is a terminal multiplexer with
   an embedded VT emulator, not a supervisor: every pane is a pty running a shell and the agent is a
   grandchild of that shell. Scored **0/7** against the local tier's measured criteria. It survives
   only as a candidate transport for remote seat access (`bh-lx6e.1`).
5. **BAML-packed seats are kill-only.** They declare `cancel: ["kill"]`; CANCEL rungs 1 and 2 ride a
   stream channel `baml.sys.exec` cannot provide. This is permanent, not pending.

## The failure modes this exists to prevent

All measured, none hypothetical:

- **Orphaning is the expensive one.** `proc.terminate()` leaves the `claude` grandchild reparented
  to init, where it **runs the entire task to completion**, keeps committing to the worktree for
  minutes after the supervisor believed it cancelled, and spends a full run's tokens into a pipe
  nobody holds (`bh-a7so.2` §3). This is why rung 3 signals the process **group**, and why the
  reader stays alive while it does.
- **A kill with no trace.** `kill -9` against a packed seat leaves no record anywhere, because
  baml-harness has no run record at all ([ADR 0004][adr04] measured it).
- **A record that lies.** After `kill -9`, claude-code's own native record drops `pid` and `status`
  while `state` remains `"working"` — forever. Any design storing a mutable status field inherits
  this.

## What exists today

| Concern | Mechanism | Adequate? |
| --- | --- | --- |
| Spawn + reap | own process group per seat, group reaped behind rung 3 | yes |
| Cancellation | three-rung ladder; kill-only against BAML seats | yes, once `bh-c6dk.14` lands |
| Crash of the loop itself | restart re-derives from `bd ready` + open gates + `bd reclaim`; the in-flight map is deliberately volatile | yes, by design |
| Loop stalled | `bh doctor` flags a running loop with no pass in `stale_after_seconds` (900) | yes |
| **Seat stalled** | **nothing but `max_run_seconds`, a blunt wall-time cap** | **no — see gap 3** |
| Run record | none on the beadhive side | no — see gap 2 |
| Telemetry | OTEL spans exist; seat `usage`/`cost_usd`/footprint not yet wired to a sink | partial |

## Are we rolling our own? No — and this names what we adopt instead

The instinct not to build a process framework is right. We are not building one. The record schema
is **agent-hitch [ADR 0004][adr04]'s**, already accepted next door and cited verbatim by
[ADR 0010][adr10] — that is the in-family framework we adopt, and the reason [ADR 0010][adr10]
rejects "beadhive owns everything" is precisely that it would mean reimplementing discipline that is
"already written and accepted next door, and any of which is easy to get subtly wrong."

The two external candidates were assessed against that, not against nothing.

## Gas Town and Gas City

`gastownhall/gastown` (MIT) is the multi-agent workspace manager from the org that ships beads.
`gastownhall/gascity` (MIT) is its **ongoing generic fork** — an orchestration-builder SDK that
extracts Gas Town's reusable infrastructure into a toolkit: runtime providers, work routing,
formulas, orders, health patrol, and a declarative city configuration.

**Adoptable as a pattern, not as a dependency.** Gas City is **Go**, and its runtime provider layer
lives in **`internal/runtime/`** — the Go convention that explicitly forbids external import. We are
Python. There is no import path, only a design to learn from. That is worth stating plainly so
nobody plans around a library we cannot link.

**Take: the runtime-provider seam.** Gas City abstracts the process substrate behind one interface
with tmux, subprocess, exec, ACP, Kubernetes, hybrid and herdr implementations, swappable by
configuration. We already have a narrower version of this — `bh host dispatch`'s supervision-backend
seam selects systemd / launchd / container by config key. Gas City validates that seam and suggests
where ours should widen: the *seat* substrate, not just the *loop* substrate.

**Take: reconcile desired state to running state.** Gas City runs a "controller/supervisor loop that
reconciles desired state to running state" — a Kubernetes-shaped reconciler rather than imperative
spawn-and-remember. Our loop already behaves this way and calls it something else: the in-flight map
is deliberately volatile and restart re-derives everything from `bd ready` + open gates +
`bd reclaim`. That is reconciliation. It should be stated as the governing principle rather than
left as an implementation detail, because it is what makes orphan recovery a property rather than a
feature.

**Take: health derived from the tracker, not the process.** Gas Town's watchdogs classify a *GUPP
violation* — hooked work with no progress over an extended period — by reading beads, and surface it
with `gt feed --problems`. Liveness and progress are different questions and the tracker answers the
second. Beadhive is well placed to copy this: it already derives retry counts by counting event
beads and stores nothing.

**Take: a patrol cadence separate from the work loop**, and the OTEL event vocabulary. Gas Town
splits per-rig lifecycle (Witness) from a cross-rig patrol (Deacon), and emits session lifecycle,
agent state changes, `bd` calls with duration, and spawn/remove, with counters like
`gastown.session.starts.total`. Our equivalent half-exists: `bh doctor`'s dispatch section is a
patrol with no scheduler, and we already emit a `dispatch_pass` stream.

**Do not take: tmux as the process substrate.** Gas Town's sessions are tmux and its zombie
detection is "dead tmux session"; Gas City keeps tmux as its default and required fallback. That is
the same pty/shell-grandchild shape we scored herdr **0/7** for, and it forfeits the process-group
control the orphan finding makes non-negotiable.

**A corroboration worth noting.** Gas City lists **herdr as one runtime provider among peers**,
alongside tmux. That is consistent rather than contradictory: herdr fits a session/pty-shaped model,
which is theirs by default and deliberately not ours. Our 0/7 was not a misreading of herdr — it is
that we are not a pty-shaped system, by choice, because a grandchild-of-a-shell cannot be reaped as
a group.

## Open decisions

**1. Ratify [ADR 0010][adr10].** It is `Proposed — needs operator sign-off` and says so explicitly:
"The verdict is the operator's to ratify: it commits two repos to one schema." Nothing below can be
built coherently until this is yes or no. *Recommendation: ratify.* Two supervisors is the honest
model — a factory run dies with its epic; a `hitch up -d` run outlives the shell that launched it,
and one store cannot hold two retention policies it cannot tell apart.

**2. How the beadhive side obtains the record — vendor, import, or reimplement.** [ADR 0010][adr10]
fixes the *schema* and explicitly defers the *distribution mechanism* to `bh-c6dk.5`, because it
turns on whether beadhive accepts a Python dependency on `agent_hitch`. *Recommendation: vendor the
schema with a `PROVENANCE.json`, mirroring what baml-harness already does for agent-hitch's
resolved-profile fixtures* — that pattern is proven between these repos, and it keeps the runtime
dependency-free, which [ADR 0009][adr09] deliberately bought.

**3. Seat stall detection.** Today a wedged seat is invisible until `max_run_seconds` fires and then
burns the whole cap. A liveness probe cannot help: a wedged `claude` is alive. *Recommendation: two
signals, cheap first.* (a) **Silence** — time since last byte on the seat's stdout, which the loop
already drains; a seat producing nothing for N× `poll_interval` is a stall signal that costs nothing
to compute. (b) **No progress** — Gastown's GUPP, derived from beads: a claimed bead whose worktree
has no new commits and whose event beads have not moved. Neither should auto-kill on first firing;
both should surface, and `max_run_seconds` remains the backstop.

## Not doing

- **Not adopting herdr** for supervision (`bh-d75gv`, 0/7). Revisit only for remote seat access.
- **Not adopting a pty/tmux substrate**, for the same reason.
- **Not rolling our own run record.** The schema is [ADR 0004][adr04]'s, cited rather than copied;
  if it forks, records written by one supervisor become unreadable by the other's tooling and the
  operator loses the one cross-cutting view — "what is this machine running right now" — that
  motivated recording anything.
- **Not storing derived state.** No mutable `status` field, ever. See the failure mode above.

[adr04]: https://github.com/briancripe/agent-hitch/blob/main/docs/design/0004-managed-run-record.md
[adr09]: https://github.com/beadhive/baml-harness/blob/main/docs/design/0009-demote-live-resolve-to-a-convenience-path.md
[adr10]: https://github.com/beadhive/baml-harness/blob/main/docs/design/0010-which-supervisor-owns-the-seat-process.md
[adr11]: https://github.com/beadhive/baml-harness/blob/main/docs/design/0011-cancellation-cannot-live-in-baml.md
