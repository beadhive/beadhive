# Diagnostics — `bh doctor`

`bh doctor` prints a status view of the whole workspace and warns about config drift and
stray folders (module: `doctor.py`). It's **informational** — always exits 0.

```sh
bh doctor
```

## What it shows

- **Config** — resolved `config.yaml` path, the workspace root, and which
  [git-workspace](INTEGRATIONS.md) `workspace*.toml` sources were found (git-workspace is a
  required dep — no on/off flag any more, just whether sources exist).
- **Providers** — the effective set, tagged by source (`config` / `git-workspace` / `both`).
- **Orgs** — name · code (`explicit` vs `auto`) · policy · source · `[excluded]`.
- **Hives** — `managed_repos` with prefixes.
- **Inventory** (counts) — hives registered, git repos on disk, onboarding candidates,
  excluded, untracked git repos, non-repo folders, unrecognized top-level dirs.
- **Fleet Health** — dirty repos (uncommitted working-tree changes), repos with unpushed
  branches, repos with no `origin` remote, stale clones (last commit older than 365 days),
  and total reclaimable disk bytes (no-origin or stale repos, counted once each).
- **Repo-group auth** — (git-workspace only) a per-group table of the git identity that
  actually applies: effective `user.name`/`user.email`, signing key, any `insteadOf` alias, and
  whether an `includeIf gitdir:` block scopes it. Read-only — `bh` never writes git config. See
  [INTEGRATIONS.md — Per-group auth](INTEGRATIONS.md#per-group-auth).
- **Harness** — (`executor`/`transient` hosts only) whether this host's Claude Code
  `bh@beadhive` plugin — the AGF role skills a seat needs to exist at all — is installed.
  Silent for a `viewer` host or one with no role registered yet, same convention as **Seats**.
- **Warnings** — orgs missing from `config.yaml`, required-org prefix violations, prefix
  collisions, git repos git-workspace isn't tracking, folders with no git repo, unrecognized
  top-level folders, hives missing a local `.beads/`, workspace-lock.toml paths nested deeper
  than `<group>/<org>/<repo>` (which `orca discover_repos` won't find), and repo groups with
  missing or shared auth. Excluded orgs are skipped to keep the signal clean.

## How it works

It diffs three sources:

- a **filesystem scan** under recognized provider dirs (`<provider>/<org>/<repo>`),
- **`git workspace list`** (what git-workspace tracks),
- the **registry** (`managed_repos`).

It degrades gracefully: without any `workspace*.toml` sources, the provider/org sections are
empty; without the `git-workspace` binary itself, the "untracked" detection is skipped. The
filesystem scan, registry checks, and warnings still run. See
[Scope & gating](INTEGRATIONS.md#scope--gating).

## Timings (bh-8nnh7)

`bh doctor --json` always carries a `timings` object: per-section wall-clock cost in
milliseconds from a monotonic clock (`time.monotonic()`), plus a `total`. It's metadata for
attributing doctor's cost, not a substitute for real tracing (see `bh-13spb`) — a dict of
numbers, nothing more. `bh doctor -v`/`--verbose` also prints it under the text report,
sorted worst-first; the default text report is unchanged. The same object backs the
`beadhive://doctor` MCP resource, so all three consumers (text, `--json`, MCP) read one
measurement.

Measured on this host (2026-08-18, 22 hives, warm cache, `bh doctor -v`): total 48.8s, with
`seats` (13.1s), `node_id` (9.0s), `beads_role` (8.6s), `warnings` (6.8s), and
`prefix_mismatches` (5.0s) accounting for the bulk of it — each of those sections calls out
to `bd`/git per hive, so their cost scales with fleet size. `molecules` (3.3s) and `dispatch`
(1.6s) are the next tier down; every remaining section is sub-second. Instrumentation
overhead is negligible: the sum of the `timings` values (48752.5ms) matched an outside
`time.monotonic()` wrapper around the whole call to within noise on both a warm run (48.8s)
and a cold run (62.7s vs the bead's independently-measured 62.6s).

## See also

`bh hive survey` provides a per-repo table with DIFFICULTY ratings for onboarding triage —
complementary to `bh doctor`'s aggregate Fleet Health counts. Run
`bh hive survey --available` to triage the unregistered candidates surfaced by `bh doctor`'s
Inventory section; `bh hive survey --sort difficulty` ranks them easiest-first.
See [HIVES.md — bh hive survey](HIVES.md#bh-hive-survey) for the full column reference and
DIFFICULTY semantics.

## Deferred

- `bh doctor --strict` — non-zero exit on warnings, for CI.
