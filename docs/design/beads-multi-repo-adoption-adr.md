# What bh adopts from beads' native multi-repo model ADR (bh-5lpl9.1)

**Status:** accepted · **Date:** 2026-10-16 · **Decision bead:** `bh-5lpl9.1`
**Epic:** `bh-5lpl9` (hive classification: ownership, remotes, store classes, routing)
**Counterpart:** [beads-methodology-divergence-adr.md](beads-methodology-divergence-adr.md)
(`bh-5lpl9.2`, what bh deliberately does *not* adopt)
**Builds on:** [two-store-scope-adr.md](two-store-scope-adr.md) (`bh-lniwf.1`)

## Why this record exists

Beads ships a multi-repo story: hydration of several repositories into one view, an explicit
`--repo` override, a routing ladder keyed on `routing.mode`, and federation of stores through
buckets. Beadhive (bh) grew most of the same mechanisms independently, often under different
names. A reader comparing the two today has to re-derive which beads feature bh already uses,
which one it wraps under another name, and which one it never wants. This record settles the
first question so nobody "fixes" bh toward a pattern it already implements.

The beads pages this derives from are living documents. They were read on the retrieval date
below; re-check them before relying on a detail that may have moved.

| Source | Retrieved |
|---|---|
| <https://beads.gascity.com/multi-agent/multi-repo-migration> | 2026-10-16 |
| <https://beads.gascity.com/multi-agent/routing> | 2026-10-16 |
| <https://beads.gascity.com/multi-agent/federation> | 2026-10-16 |
| <https://beads.gascity.com/multi-agent/bucket-federation> | 2026-10-16 |
| `bd` 1.3.0 (`f45b249ce`) `--help`, `bd repo --help`, `bd config --help` | 2026-10-16 |

The `bd` binary is the tie-breaker when a page and the binary disagree: `bd config` reports
`routing.mode` valid values as `auto, maintainer, contributor, explicit`, and stores the key
in `.beads/config.yaml` (`"location": "config.yaml"`).

## Adopted

### 1. Hydration (`repos.additional`, `source_repo`) is the bh hub

Not an analogy — the same mechanism. `bd repo add <path>` records a read-only additional
repository under `repos.additional`; `bd repo sync` hydrates its issues, stamped with their
`source_repo`, into the primary database. bh's aggregate hub does exactly that.

- **Where:** `src/beadhive/hub.py` — `_sync_hive()` runs `bd repo add <cache>` per hive and
  `sync()` finishes with `bd repo sync`; `hub_target()` / `ensure_hub()` own the aggregate.
  [HUB.md](../HUB.md) documents the operator surface.
- **Changes:** none to the mechanism. [two-store-scope-adr.md](two-store-scope-adr.md) §7
  fixes its *scope*: hydration aggregates **internal** stores only, so a public projection
  never becomes duplicate dispatchable work (`hub.hydration_source`).

### 2. The `--repo` override is `bh report <hive>`

`bd create --repo` is the top rung of beads' routing ladder: an explicit target always wins
over any heuristic. bh has the same rung, spelled as a hive identity instead of a path.

- **Where:** `src/beadhive/cli.py` `report_cmd` (`bh report <hive> "<title>"`), backed by
  `src/beadhive/report.py`; the `-r/--hive` passthrough routing in `src/beadhive/route.py`
  and `registry.resolve_hive()`. [REPORT-CHANNEL.md](../REPORT-CHANNEL.md) and
  [PASSTHROUGH.md](../PASSTHROUGH.md) describe them.
- **Changes:** documentation only — the docs now name `bh report <hive>` as bh's `--repo`.

### 3. `routing.mode explicit` + `routing.default`, set on every hive

An unset key that resolves to a plausible guess is the failure that produced the
`beads.role` incident (`bh-s08me`): bd fell back to a remote-shape heuristic, and nothing
reported that it had. The fix is to declare, not to guess. Every bh-managed hive declares
`routing.mode: explicit` and `routing.default: .` (this repository), so bd's `auto`
heuristic is never the operative rule.

- **Where:** onboarding sets both when absent (`onboard._act_beads_routing`, step
  `beads-routing`), never overwriting an operator's existing value; `bh doctor` lists every
  hive where either key is unset or differs (`doctor._data_beads_routing`, section
  `bd routing`). The fix for a drifted hive is the passthrough
  `bh -r <hive> bd config set routing.mode explicit` (and `routing.default .`). The ONE
  expected-value table is `hive_repair.EXPECTED_ROUTING`.
- **Changes:** new in this bead, behind `work.beads.explicit_routing` (off by default, per
  the fleet's opt-in rule — enabling it fleet-wide is an operator decision). The keys are
  config-file keys (`.beads/config.yaml`), so the doctor read parses the file directly
  rather than paying a `bd config get` per hive.
- **Why `.`:** `routing.default` names where `bd list` / `bd ready` read when routing is
  explicit. `.` is the hive's own store; any other value would send reads to a planning
  store the operator never chose.

### 4. One prefix per store

Already true, and now enforced across stores rather than only across hives. Groups
([BEAD_GROUPS.md](../BEAD_GROUPS.md)) add zero stores and zero prefixes.

- **Where:** `registry.prefix_collisions()` (hive prefixes), and for configured stores
  `ManagedRepoStores` validation in `src/beadhive/modules/config/contracts.py`
  (`BeadhiveConfig._store_references`), which rejects a prefix shared by any two stores in
  the fleet.
- **Changes:** the cross-store check is new (`bh-lniwf.2`). Existing prefixes are untouched.

### 5. Cross-store dependency edges via hydration

Beads' own example writes an edge across repositories (`bd dep add impl-42 plan-10`) and
reads it through the hydrated view. bh adopts that with one caveat beads leaves implicit:
the edge is written in the store that owns the **dependent** bead, because the aggregate is
read-only — a write against the hub would land in a derived database that the next
`bd repo sync` discards.

- **Where:** `bh bd dep add` is a passthrough run in the dependent bead's hive
  (`bd.passthrough`, `src/beadhive/route.py`); the hub refuses writes (`guard.guard_hub`,
  called from `hub.query`, see [HUB.md](../HUB.md)).
- **Changes:** none. An edge from an internal bead to a public projection record is not
  supported (two-store ADR §5): public records cannot satisfy or block operational work.

## Not decided here

- Which beads features bh rejects, and why: the counterpart record
  [beads-methodology-divergence-adr.md](beads-methodology-divergence-adr.md).
- How two stores per repository work: [two-store-scope-adr.md](two-store-scope-adr.md).
