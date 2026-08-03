# Dolt server mode ADR — the fleet runs bd's shared server; embedded is retired

**Status:** accepted · **Date:** 2026-08-03 · **Decision bead:** `bh-ukit.4` ·
**Supersedes:** nothing · **Amends:** no other ADR
**Related:** [multi-host-model-adr.md](multi-host-model-adr.md) (whose LOCAL REPLICA premise this
touches but does not yet change — see Consequence 5),
[bead-backend-abstraction.md](bead-backend-abstraction.md),
`docs/spikes/bh-u562.1-dolt-server-lifecycle.md`,
`docs/spikes/bh-00cq-external-dolt-sql-server.md`

Decided by the operator, 2026-08-03. Records which of bd's storage modes this fleet targets, the
migration and rollback path, and what the decision does and does not settle.

## Decision

**Target mode: bd's shared server** — `shared-server: true` / `BEADS_DOLT_SHARED_SERVER=1`. One
`dolt sql-server` per host at the fixed default port 3308, holding one database per hive under
`~/.beads/shared-server/dolt/<db>/`. **HQ is another database in that same server**, not a
special host or a separate instance.

**Scope: fleet-wide** — all 22 registered hives plus HQ, and the default for newly-onboarded
hives. Not per-hive opt-in.

**Lifecycle stays with bd.** bd spawns and manages the server; bh does not supervise it. This is
mode (a) in the working shorthand. Taking over the process is a separate, later question, filed as
`bh-z41i` ((c)-local) and `bh-3mik` ((c)-remote), both explicitly downstream of this decision
shipping.

## Rejected: embedded (status quo)

Measured on this machine, 2026-08-03:

| | embedded | shared |
|---|---|---|
| 5 readers + 1 writer, median of 3 (`bh-u562.1`) | 3.456 s | **0.720 s** |
| engine-open + query, controlled A/B on identical cloned data (`bh-00cq`) | 187 ms | **55 ms** |
| fleet disk (`.beads/embeddeddolt`, 22 hives) | ~2.8 GB | reclaimed |
| cold 306 MB clone against the production remote (`bh-00cq`) | hung past 240 s | 10 s |

The concurrency row is the one that matters most and was the epic's own flagged unknown until
`bh-u562.1` measured it. Embedded's exclusive file lock is what made `bh plan repair`,
`bh plan verify` and `bd label list-all` each stall past 120 s during earlier work.

Upstream also appears to treat embedded as the legacy path: `bd dolt --help` on the pinned build
states "Beads uses a dolt sql-server for all database operations. The server is auto-started
transparently when needed."

## Rejected: owned mode (bd's per-project default)

Not on performance — owned matches shared closely (0.768 s on the concurrency test) and is the
only mode that supervises itself, restarting transparently in 0.826 s after `kill -9`. It fails on
shape:

1. **Its port is OS-ephemeral and rotates on every restart** (`allocateEphemeralPort`,
   `net.Listen(":0")`, `doltserver.go:340-348`; confirmed by a measured restart yielding a new PID
   *and* a new port, and independently reproduced during review). A rotating port is not an
   address, so no other hive — and no container — can ever attach to it. That rotation *is* the
   collision-proofness, so it cannot be disabled without losing the property.
2. **One server per project directory, unbounded.** `maxDoltServers()` (`doltserver.go:332-334`)
   documents a ceiling of 3 and **has no call site in production code**; the spike ran six
   concurrently with no warning. At this fleet's size that is 22 permanent background
   `dolt sql-server` processes, uncounted.

Owned remains the right default for a single standalone project. It is the wrong shape for a
22-hive fleet with a shared aggregate.

## Migration and rollback path

**The path is bd-native and documented**, in beads' own `docs/architecture/dolt.md` §"Migrating
Between Backends": *"You can migrate data between embedded mode and server mode using `bd backup`.
Both directions preserve full Dolt commit history."*

    # in the embedded project
    bd backup init /path/to/backup-dir
    bd backup sync
    # in a fresh server-mode project
    bd init --server
    bd backup restore --force /path/to/backup-dir
    bd list && bd backup status          # verify

`bd backup` is a Dolt-native database backup — tables, branches, commit history, working-set data.
**`bd export` is explicitly NOT a substitute**: JSONL carries issue records only, no branches, no
history, no working set, no non-issue tables. A second documented path exists via Dolt remotes
(`bd dolt push` / `bd dolt pull`) when both projects share one.

**There is no one-shot mode-switch command for this move.** `bd migrate` does carry EXPERIMENTAL
switches, but only between `server` ↔ `proxied-server` ↔ `shared-server` — none from embedded. The
backup/restore sequence above is the route.

**Rollback is clean, and the earlier contradiction was a category error.** `bh-areg.4`'s brief
recorded beads' docs (reversible, history preserved both ways) against `bh-bmsg` (reverting is NOT
a clean rollback) as an unresolved conflict. Reading `bh-bmsg` directly resolves it: its warning is
about **downgrading the bd binary**, not about changing storage mode. bd HEAD applied six one-way
schema migrations (v53 → v59) on arrival; once a store is at v59, a stable bd that knows only v53
cannot open it — measured in `bh-00cq` as a hard open-time failure after a successful 306 MB clone.

So, stated precisely and to be carried into every doc that touches this:

- **Storage-mode migration is reversible.** embedded → shared → embedded, history preserved.
- **bd-binary schema migration is one-way.** That is the door that does not reopen, and it is
  orthogonal to mode. `bh-wnly` makes the skew a preflight check instead of an open-time failure.

## Consequences

**1. Auto-backup silently turns OFF on migration — a durability regression that must be handled.**
Per `bd backup --help`: *"When backup.enabled is unset, auto-backup turns ON in embedded mode if a
git remote exists, and stays OFF in sql-server / shared-server mode."* The reason is sound — many
clients sharing one server would each register a server-side backup remote under the same name and
full-sync, "a self-amplifying storm." But the effect is that migrating this fleet turns off
automatic backups on every hive that had them, silently, unless `backup.enabled=true` is set
explicitly. **`bh-areg.4` must set it as part of migrating, and `bh-areg.3` should report a hive in
server mode with auto-backup unset.** Nothing had recorded this before this ADR.

**2. Each hive needs a single designated migrator.** `bd migrate` refuses in-place migration on a
remote-backed database (upstream #4259): *"migrating two clones independently forks the schema so
bd dolt pull can no longer merge — the break is silent and unrecoverable."* `--force` (or
`BD_ALLOW_REMOTE_MIGRATE=1`) confirms you are the single migrator, after which the migrated schema
must be published with `bd dolt push`. A 22-hive fleet migration must serialize per hive and never
run concurrently against two clones of the same hive.

**3. The Brewfile HEAD pin is UNCHANGED.** `bh-00cq` Caveat B: server mode fixes *dolt-engine*
currency, not *bd-schema* currency. Pinning a released `beads` still requires a release that is
both dolt-current (≥ 2.2.0) and schema-current with what HEAD has already written. That goal keeps
its own bead and is not advanced by this decision.

**4. Store paths move**, which is what four call sites in bh assumed would never happen:
`.beads/embeddeddolt/<db>/` → `~/.beads/shared-server/dolt/<db>/`. `hq.py:759`,
`hq_restore.py:36-37`, `host_fence.py:64` and `prepush.py` are the affected sites; `bh-areg.1` and
`bh-areg.6` own them. Note the local `.beads/dolt/` path belongs to *owned* mode, not shared.

**5. The multi-host model is touched but NOT yet changed.** `multi-host-model-adr.md` is built on
LOCAL REPLICAS fenced at push. A per-host shared server keeps every store local, so the premise
survives this decision. What does not survive unexamined is `host_fence.transport_repos()`, which
globs beneath `hive_dir` and cannot reach a store at `~/.beads/shared-server/`. **`bh-ukit.2`
decides whether the fence simplifies or breaks; this ADR does not pre-empt it.** The fence is not
in force on this fleet today (one registered host, stale, no pre-push hooks installed, and the
model is inactive until an adopt), so the migration does not step on a live mechanism — but the
fence must be correct before a second host is ever adopted.

**6. A doc/CLI inconsistency worth verifying empirically, not trusting.** In the same pinned build,
`docs/architecture/dolt.md` says `bd init` "creates an embedded-mode project by default" while
`bd dolt --help` says bd "uses a dolt sql-server for all database operations." `bh-areg.4` should
determine what a bare `bd init` actually produces rather than relying on either statement.

## What this decision does not settle

- Whether bh should own the server process — `bh-z41i` ((c)-local), `bh-3mik` ((c)-remote).
- Whether bh should acquire a per-host daemon at all — `bh-u562.5`, now resting only on HTTP
  transport (`bh-u562.3`) and telemetry (`bh-u562.4`) grounds, since `bh-u562.1` settled that bd
  owns dolt lifecycle.
- Whether the epoch fence survives unchanged — `bh-ukit.2`.
- How the test suite is organized against a shared engine — `bh-ukit.5`, implemented by
  `bh-areg.5`.
- Whether one shared engine can retire the hub's wipe-and-rebuild via cross-database queries. An
  open, unverified question recorded on `bh-ukit.4`; dolt's engine supports cross-database queries,
  but whether bd's layer exposes them is untested. If it holds it is a larger argument for
  consolidation than disk or latency, and it deserves its own bead.

## Implementation

`bh-areg` — "Adopt dolt server mode: migrate every existing hive, and make it the first-install
default" — was filed 2026-08-03 ahead of this decision and is the implementation molecule. No
`/bh:replan` is needed. Release scope is **0.8.0 MINOR**: the work adds a migration verb, config
keys, and doctor checks, each independently a `feat:`. The two path corrections (`bh-areg.1`,
`bh-areg.6`) are genuine `fix:` — bh carries an incorrect assumption about a configuration bd
already supports — but that reasoning does not extend to the rest.
