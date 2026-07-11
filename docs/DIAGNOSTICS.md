# Diagnostics — `bh doctor`

`bh doctor` prints a status view of the whole workspace and warns about config drift and
stray folders (module: `doctor.py`). It's **informational** — always exits 0.

```sh
bh doctor
```

## What it shows

- **Config** — resolved `config.yaml` path, the workspace root, and whether the
  [git-workspace integration](INTEGRATIONS.md) is on (and which `workspace*.toml` were read).
- **Providers** — the effective set, tagged by source (`config` / `git-workspace` / `both`).
- **Orgs** — name · code (`explicit` vs `auto`) · policy · source · `[excluded]`.
- **Rigs** — `managed_repos` with prefixes.
- **Inventory** (counts) — rigs registered, git repos on disk, onboarding candidates,
  excluded, untracked git repos, non-repo folders, unrecognized top-level dirs.
- **Fleet Health** — dirty repos (uncommitted working-tree changes), repos with unpushed
  branches, repos with no `origin` remote, stale clones (last commit older than 365 days),
  and total reclaimable disk bytes (no-origin or stale repos, counted once each).
- **Warnings** — orgs missing from `config.yaml`, required-org prefix violations, prefix
  collisions, git repos git-workspace isn't tracking, folders with no git repo, unrecognized
  top-level folders, and rigs missing a local `.beads/`. Excluded orgs are skipped to keep the
  signal clean.

## How it works

It diffs three sources:

- a **filesystem scan** under recognized provider dirs (`<provider>/<org>/<repo>`),
- **`git workspace list`** (what git-workspace tracks),
- the **registry** (`managed_repos`).

It degrades gracefully: without the git-workspace integration enabled, the provider/org
sections are empty; without the `git-workspace` binary, the "untracked" detection is skipped.
The filesystem scan, registry checks, and warnings still run. See
[Without git-workspace](INTEGRATIONS.md#scope--gating).

## See also

`bh rig survey` provides a per-repo table with DIFFICULTY ratings for onboarding triage —
complementary to `bh doctor`'s aggregate Fleet Health counts. Run
`bh rig survey --available` to triage the unregistered candidates surfaced by `bh doctor`'s
Inventory section; `bh rig survey --sort difficulty` ranks them easiest-first.
See [RIGS.md — bh rig survey](RIGS.md#bh-rig-survey) for the full column reference and
DIFFICULTY semantics.

## Deferred

- `bh doctor --strict` — non-zero exit on warnings, for CI.
