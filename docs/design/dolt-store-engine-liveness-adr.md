# Store-engine liveness — health reporting + the down-behavior choice

**Status:** accepted · **Date:** 2026-08-03 · **Decision bead:** `bh-areg.3` ·
**Supersedes:** nothing · **Amends:** no other ADR
**Related:** [dolt-server-mode-adr.md](dolt-server-mode-adr.md) (the parent decision — mode (a),
bd's shared server), `docs/spikes/bh-u562.1-dolt-server-lifecycle.md` (the measured evidence this
decision is built on, findings 2 and 9 especially), [DOLT.md](../DOLT.md) (a *different*
dolt server — bh's own docker-compose-managed one; do not conflate the two, see that doc's own
scope note)

## Problem

Embedded mode has no liveness question: the dolt engine runs in-process, so if `bd` runs, the
engine runs. Mode (a) — bd's shared `dolt sql-server`, the fleet's target per
[dolt-server-mode-adr.md](dolt-server-mode-adr.md) — makes the engine a separate process for
the first time. That process can be down, wedged, or on the wrong port, and before this bead
nothing in `bh` had any vocabulary for it: `bh doctor` printed the configured `dolt.backend`
string and never probed; `bh setup check` probed for BINARIES, not running services; `bh hive
ready` had no concept of it at all.

Two things had to be decided, not just built:

1. **How does `bh` find out the engine is down?** (the probe mechanism)
2. **What happens when it is?**, per verb class — hard-fail, auto-start, or fall back to
   embedded are not equivalent, and bd may already have picked one for us.

## Decision 1 — the probe is an ENDPOINT connection check, never a bd-reported PID

`bh-u562.1` finding 9 measured `bd dolt status --json` reporting `"pid": 0, "running": false`
for a **live** external server that was answering real 16K-line `bd list` queries at the time —
bd's own process-level reporting is unreliable outside the modes bd itself spawned. This
directly bears on `safety._bd_dolt_mode()` (`safety.py:300`), which reads the same JSON's
`"mode"` key: `bh-areg.1`'s own `store_locator.py` module docstring already recorded that this
key is present for only two of bd's four modes and wrong for a third (measured against a real
bd binary) — this bead did not need to re-derive that; it inherits `store_locator.dolt_mode()`
(a filesystem read of `.beads/metadata.json`, never a live probe) for PERSISTED mode, and adds a
genuinely new, separate mechanism for LIVE reachability.

**`dolt_health.py`'s `probe_endpoint(host, port)`** opens a real TCP connection and reads the
first byte back, checking it looks like a MySQL-protocol handshake (protocol version 10, byte
`0x0a` — what every MySQL-wire-protocol server, dolt's sql-server included, sends first,
unauthenticated). This behaves identically under mode (a), (c)-local, and (c)-remote — a PID
probe does not, and in particular gives a **false negative for a healthy external server** (the
exact bh-u562.1 finding 9 failure mode) and a **false positive for the "wrong port" case** (any
listening service, not specifically a dolt server, would look "up" to a bare TCP connect —
which the handshake-byte check catches).

Immediate scope is mode (a) only (per the parent ADR): the endpoint is bd's own fixed shared
default — `127.0.0.1:3308` — overridden by bd's own `BEADS_DOLT_SERVER_HOST` /
`BEADS_DOLT_SERVER_PORT` env vars when set, exactly bd's own resolution order. A hive whose
persisted `dolt_mode` is `"server"` is *assumed* to be mode (a) — correct for every hive this
fleet's migration (`bh-areg.4`) can produce today, since owned/external adoption ((c)-local /
(c)-remote, `bh-z41i` / `bh-3mik`) are both explicitly downstream of mode (a) shipping. Revisit
this assumption the moment either lands — a per-hive endpoint (not a single fleet-wide default)
will be needed then.

## Decision 2 — the down-behavior, per verb class, CHOSEN not inherited by accident

**Verb class A — bh's own read-only reporting (`bh doctor`, `bh setup check`, `bh hive
ready`):** report DOWN clearly and move on. This is what this bead builds: a probe with a real
answer, never a crash, never a silent pass. Advisory in every surface it touches (never turns a
missing/required check into a hard failure) — `setup.dolt_fix_advisory`'s "informs without
blocking" shape, copied deliberately (see that function for the precedent this bead follows).

**Verb class B — every other `bh` verb that shells out to `bd` for real work** (`bh work
claim`/`submit`/…, `bh bd` passthrough, hq sync, …): **hard-fail, inherited from bd, unchanged.**
`bh-u562.1` finding 2 measured this directly for shared mode: killing the server, then a `bd`
read verb, fails in **0.32s** with a legible error (`Dolt server unreachable at 127.0.0.1:3308:
connect: connection refused`) plus the exact remedy (`bd dolt start`). bd already chose
hard-fail for mode (a); `bh` does not intercept, wrap, or replace that error — it is the correct
UX already, and `bh-u562.1`'s GO verdict on owned mode (no daemon needed) extends to shared mode
having **no `bh`-owned daemon in the dolt lifecycle path at all**. There is nothing for `bh` to
build here beyond making sure it does not accidentally do one of the two things below.

### Rejected: auto-start

Convenient, but means every `bh` CLI invocation could silently spawn a background daemon.
`bh` does not own the dolt server's lifecycle under mode (a) — bd does (parent ADR). Auto-start
from `bh`'s side would duplicate a decision bd already makes for itself (bd's own auto-start
logic exists and applies per its own `resolveAutoStart` rules) and would be a second, competing
place to get that lifecycle policy wrong. **Not implemented, not planned** for mode (a).

### Rejected: fall back to embedded

The most tempting option and the most dangerous. If a `bh` verb silently created (or resurrected)
an embedded store the moment the shared server was unreachable, **two engines would then be
pointed at what an operator believes is ONE store** — the shared server (once it comes back) and
whatever the fallback wrote to `.beads/embeddeddolt/` in the meantime — with no way to tell
which one wrote last, and no merge path between them (they are different storage engines, not
two replicas of the same one). This is exactly the two-engines-one-store risk this bead's
acceptance bar names explicitly. **`bh` never does this, for any verb, under mode (a).** No code
path in `bh` creates or writes to an embedded store as a fallback when a configured server is
down; a down server is reported (class A) or hard-fails through bd's own error (class B), full
stop.

## Decision 3 — no new `DoltConfig` key

bd already owns the mode/endpoint declaration: `shared-server: true` /
`BEADS_DOLT_SHARED_SERVER=1` turns mode (a) on; `BEADS_DOLT_SERVER_HOST` / `_PORT` override the
endpoint; `.beads/metadata.json`'s `dolt_mode` field persists what was chosen. A `bh`-side mirror
of any of these would be a second place to declare the same fact, with no mechanism keeping the
two in sync — the exact drift class `store_locator.py` was built to stop repeating (`bh-kobw`,
`bh-u562.1`). `config_schema.DoltConfig` (`config_schema.py:422`) gets **zero new fields** from
this bead; every fact `dolt_health.py` needs is either read from bd's own persisted metadata
(`store_locator.dolt_mode`) or from bd's own env vars (`BEADS_DOLT_SHARED_SERVER`,
`BEADS_DOLT_SERVER_HOST`/`_PORT`) — detection, never configuration.

## Decision 4 — engine/metadata mode mismatch is surfaced by `bh doctor`

bd's own `main.go:warnSharedServerEmbeddedMismatch` warns — but only on a live `bd` invocation
an operator happens to be watching — when shared-server mode is active for the current process
but `.beads/metadata.json` still pins `dolt_mode: "embedded"` (bd's env wins for that one
invocation; bd never rewrites the committed metadata to match, so the drift persists silently
otherwise). `dolt_health.mismatch_reason()` mirrors that exact condition
(`store_locator.dolt_mode(hive) == "embedded"` and `BEADS_DOLT_SHARED_SERVER` active) and `bh
doctor`'s new "Store Engine" section surfaces it per hive — independent of whether an operator
happens to be watching a `bd` invocation at the moment it fires.

## Consequences

- **No new noise for embedded-only fleets.** `bh doctor`'s "Store Engine" section, `bh setup
  check`'s advisory, and `bh hive ready`'s "dolt server" line are all silent/`na` when no
  registered hive is server-mode and nothing has drifted — matching the acceptance bar that an
  unmigrated hive (today's default) sees byte-identical output. `bh config show`'s existing
  `# Dolt` section (the `dolt.backend` container-runtime pre-step string — a *different*
  subsystem, see [DOLT.md](../DOLT.md)) is untouched.
- **One probe per host, not per hive.** Mode (a) is one shared `dolt sql-server` per host,
  serving every migrated hive's own database — `bh doctor`'s fleet-wide section probes the
  shared endpoint at most once regardless of how many hives are server-mode.
- **`bh` never becomes a supervisor.** This bead adds reporting only; it starts, restarts, or
  falls back to nothing. The moment (c)-local (`bh-z41i`) or (c)-remote (`bh-3mik`) land, this
  document's endpoint-resolution assumption (single fleet-wide mode-(a) default) needs revisiting
  — the probe mechanism itself (connect + handshake-byte check) does not.
