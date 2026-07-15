# Rigs — onboarding & identity

A **rig** is a repo's beads database. This covers turning a repo into a rig and how `bh`
derives its identity (modules: `rig.py`, `identity.py`; prefix logic in `registry.py`).

## Identity from the path

`bh` derives a repo's `(group, org, repo)` from its location under the git-workspace root
(`$GIT_WORKSPACE`, default `~/workspace`): `<group>/<org>/.../<repo>`. The first segment is a
repo-group's **path** — not necessarily the provider TYPE (github/gitlab/gitea); a group's
`path` and `provider` can differ (see [INTEGRATIONS.md](INTEGRATIONS.md#git-workspace)). This
is the fast path used by `bh bd create` (the triplet) and `bh rig init` (registration).
Outside that layout, path-derived features degrade gracefully
(`identity.py:workspace_identity` returns `None`). The stored `managed_repos[].provider` field
name is unchanged for backward compatibility — it holds the group path.

## `bh rig init`

Run **from inside the target repo**:

```sh
bh rig init [--prime] [--claude] [--plugin orca] [--kind K] [--prefix P] [--yes] [--dry-run]
```

Flow (`rig.py`):

1. Derive `group/org/repo` from the path (`group` = the repo-group path, not necessarily
   the provider type — see [INTEGRATIONS.md](INTEGRATIONS.md#git-workspace)).
2. **Classify** the repo (`registry.classify`) → its *kind*.
3. Resolve/derive the **prefix** (`registry.derive_prefix`), or use `--prefix`.
4. **Required-org check** — if the org's policy is `required`, the prefix must start with
   `<code>-`; otherwise it's blocked (a registration invariant, always enforced).
5. `bd init --prefix <p> --skip-agents --skip-hooks --non-interactive`.
6. Register `{provider, org, repo, prefix, kind, upstream?}` in `config.yaml`.
7. Optionally install agent extras (`--prime`, `--claude`).
8. **Scaffold commit** — restore the tracked-rig convention (below): drop any `.beads/`
   stealth exclusion `bd init` added, then commit the onboarding artifacts. A green
   init/onboard ends with a **clean working tree** (and a clean `bh rig survey` row).

`--dry-run` prints the plan and changes nothing.

### Rig-side scaffolding is tracked, not stealth

Rigs **track** their scaffolding in git — the convention every established rig follows:

- **Tracked:** `.beads/PRIME.md`, `.beads/config.yaml`, `.beads/metadata.json`,
  `.beads/issues.jsonl`, `.beads/.gitignore`, `.claude/settings.json`, and the managed
  `CLAUDE.md` / `AGENTS.md` hints. bd's own `.beads/.gitignore` keeps the local-only pieces
  (Dolt db, locks, backups) out of the commit. The hub relies on this: `bh sync` hydrates
  from each rig's `.beads/issues.jsonl`.
- **Host-local only** (`.git/info/exclude`, never the tracked `.gitignore`): `.ws/`,
  `.claude/settings.local.json` (the machine-specific sandbox grant).
- **Forks are the exception**: `.beads/` stays stealth-excluded and nothing rig-side is
  committed, so beads never pollutes an upstream PR.

`bd init` sometimes stealth-excludes `.beads/` wholesale; the final *scaffold* step repairs
that and commits the artifacts (`chore(agf): rig scaffolding (beads + agent config)`).
Re-running `bh rig init`/`onboard` on an already-diverged rig applies the same repair —
rig-state residue (`.beads/`, `.claude/`, `CLAUDE.md`) does not trip the `dirty-tree` gate.
Until upstream `bd init --no-commit` lands, `bd init` still makes its own
scaffolding commit; bh sweeps everything else into the scaffold commit.

## Kinds (classification)

| Kind | Detected when | Prefix | beads |
|---|---|---|---|
| **org-native** | path org has `policy: required` | `<code>-<repo>` (enforced) | on |
| **personal** | personal account, kept long-term | `<code>-<repo>` (suggested) | on |
| **prototype** | personal account, org undecided (default) | bare `<repo>` | on |
| **fork** | `gh repo view` reports `isFork` | upstream identity | **off unless `--yes`** |

`registry.classify` checks, in order: excluded (refuse) → required-org → fork (via `gh`) →
personal-or-prototype. Forks are skipped unless `--kind fork --yes`; when opted in, their
identity reflects the **upstream** so they don't pollute org/personal rollups.

## Prefix derivation

`registry.derive_prefix` (mirrors the original `prefix` policy):

- `org-native` / `personal` → `<code>-<repo>`
- `prototype` → bare `<repo>`
- `fork` → `fork-<repo>`
- no kind → bare `<repo>` if globally unique, else `<code>-<repo>`

`<code>` comes from the org's registry entry, falling back to `sanitize(org)[:2]`. Names are
sanitized to `^[a-z0-9-]+$`. A prefix over 8 chars or one already in use produces a warning
(override with `--prefix`). The registry enforces global uniqueness.

Why provider isn't in the prefix and why it's stable: see [DESIGN](DESIGN.md#prefixes).

## Agent extras (independent, opt-in)

Both bundled in the package, merged non-destructively (existing hooks/denies preserved):

- **`--prime`** → installs `.beads/PRIME.md` (a trimmed beads issue-workflow doc).
- **`--claude`** → installs `.claude/settings.json` (a `SessionStart` hook running `bd prime`
  and a `deny` rule for `bd remember`) and a `statusLine` block so the TUI shows the active
  seat and rig. In **plugin mode** (default, `claude.source: plugin`) it also runs
  `claude plugin marketplace add` + `claude plugin install agf` — seat agents and role skills
  arrive via the `agf` Claude Code plugin rather than being copied into the rig. In **copy
  mode** (`claude.source: copy`) it writes `.claude/agents/` and `skills/` directly, which is
  the legacy behaviour suitable for offline or airgapped environments.

Use either, both, or neither. Default `bh rig init` writes no agent files (it passes
`--skip-agents --skip-hooks` to beads).

### Plugin mode vs copy mode

| | Plugin mode (default) | Copy mode (`claude.source: copy`) |
|---|---|---|
| Agent defs | `agf:<seat>` plugin, user or project scope | `.claude/agents/<seat>.md` in the rig |
| Role skills | bundled inside the `agf` plugin | `skills/` directory in the rig |
| `bh rig ready` skills check | passes when `agf` plugin is installed | passes when `skills/` dir is present |
| Offline / airgapped | requires plugin install at onboard time | works offline after copy |
| Local override | `.claude/agents/<seat>.md` outranks the plugin | n/a |

Configure via `claude:` in `~/.ws/config.yaml` — see [CONFIGURATION.md](CONFIGURATION.md#claude-section).

## `bh rig add` / `bh rig rm`

`bh rig add` registers a triplet in the registry **without a `cwd`** and without running
`bd init`. Use it when the repo is remote or uncloned and you only need the registry entry:

```sh
bh rig add github/acme/infra
bh rig add github/acme/infra --prefix ac-infra --kind org-native
bh rig add github/acme/fork  --kind fork --upstream acme-upstream/infra
```

`bh rig rm` unregisters a rig by id — **registry-only**; it does not touch `.beads`, labels,
or the repo on disk:

```sh
bh rig rm github/acme/infra   # or any rig-match form the registry resolves
```

Both `add` and `rm` are the control-plane equivalents of `rig init`'s side-effect; use
`rig init` (or `rig onboard`) when you have a local checkout that also needs `bd init`.

## `bh rig onboard`

`bh rig onboard` is the **end-to-end** path: it resolves the target directory under
`$GIT_WORKSPACE`, clones if absent, runs the full `rig init` logic (including `bd init`),
and syncs the hub — all in one command:

```sh
# Local folder already cloned — inits in place, syncs hub:
bh rig onboard github/acme/infra

# Remote repo not yet cloned — clones first, then inits + syncs:
bh rig onboard github/acme/infra --clone-url https://github.com/acme/infra.git

# Install rig furniture in one shot:
bh rig onboard github/acme/infra \
  --prime --claude --skills --observaloop --agents
```

`--clone-url` is **guarded**: the clone only happens when the target directory is absent. An
already-local folder is onboarded in place. This prevents cloning over a live checkout.

Options mirror `bh rig init`: `--prime` (PRIME.md), `--claude` (settings, seat agent defs,
and statusLine), `--skills` (role skills), `--observaloop` (observaloop profile),
`--agents` (AGENTS.md hint), `--plugin NAME` (enable a plugin integration for this rig,
repeatable — e.g. `--plugin orca` registers the clone with orca; see
[INTEGRATIONS](INTEGRATIONS.md#orca)), `--force` (re-register), `--kind`, `--prefix`,
`--yes` (required for forks).

## `bh rig ls` / `bh rig ls --available`

`bh rig ls` lists **registered** rigs from the registry. `--available` switches to a
**discovery** view — repos tracked by git-workspace (`workspace-lock.toml`) that are **not**
yet registered — the candidates for `bh rig add` or `bh rig onboard`:

```sh
bh rig ls              # registered rigs
bh rig ls --available  # discoverable-but-unregistered (zero API calls)
```

The `--available` view is a pure diff: git-workspace's tracked repos minus `managed_repos`.
No live API calls are made; it reads only the lock file and the registry.

## `bh rig survey`

`bh rig survey` is a **read-only fleet table** — one row per on-disk repo (registered and
tracked) — for onboarding triage. Run it before committing to an onboarding batch to see
which repos are easy candidates and which need attention first.

```sh
bh rig survey                     # all on-disk repos
bh rig survey --available         # unregistered candidates only
bh rig survey --sort difficulty   # easiest first; also: disk | age
bh rig survey --json              # machine-readable JSON (one object per repo)
```

### Columns

| Column | Meaning |
|---|---|
| `REPO` | `<group>/<org>/<repo>` triplet (`group` = the repo-group path) |
| `REG` | `yes` if already registered, `no` if a candidate |
| `CLASS` | registry classification: `org-native`, `personal`, `prototype`, `fork`, `excluded` |
| `COMMITS` | total commit count reachable from HEAD |
| `LAST-COMMIT` | date of most-recent commit (YYYY-MM-DD) |
| `AHEAD/BEHIND` | `+A/-B` totals across all local branches vs their upstreams |
| `DIRTY` | count of local branches with uncommitted changes |
| `DISK` | total disk usage (working tree + `.git`) |
| `DIFFICULTY` | `EASY` / `MEDIUM` / `HARD` / `NOT-A-CANDIDATE` — see below |

### DIFFICULTY semantics

DIFFICULTY combines three signal groups: registry exclusion, maturity (commit count and
last-commit recency), and cleanliness (the repo's `Category` from `safety.scan()`).

| Signal | Direction |
|---|---|
| `registry.classify` returns `excluded` | `not-a-candidate` (immediate short-circuit) |
| Commits `≥ 50` (mature) | easy signal |
| Commits `< 5` (immature) | hard signal |
| Last commit `≤ 90` days ago (recently active) | easy signal |
| Last commit `≥ 365` days ago (stale/abandoned) | hard signal |
| Category `READY` | easy signal |
| Category `WIP_AND_AHEAD`, `WIP_DIRTY`, `NO_ORIGIN_DIRTY`, `NO_ORIGIN_EMPTY`, `NOT_A_REPO` | hard signal |

Verdict rules:

- **`EASY`** — no hard signals and two or more easy signals. Safe to onboard with minimal
  ceremony; `bh rig ready` should pass immediately after init.
- **`MEDIUM`** — no hard signals but fewer than two easy signals. Proceed, but review the
  repo's state before onboarding.
- **`HARD`** — one or more hard signals. Resolve the blocking condition first: push pending
  commits, clean the working tree, or accept that the repo needs attention before it can be
  onboarded.
- **`NOT-A-CANDIDATE`** — registry policy says `excluded`; `bh rig init` refuses this repo.

Typical triage flow: `bh rig survey --available --sort difficulty` → start with `EASY` rows
→ confirm each rig after init with `bh rig ready [-v]` → use `bh doctor` for the
fleet-level aggregate health view.

## `bh rig retire`

`bh rig retire` is the **guarded teardown** command — the symmetric counterpart to
`bh rig onboard`. Run `bh rig survey` first to identify the candidate, then dry-run before
committing.

```sh
bh rig retire <rig> [--dry-run] [--backup] [--confirm] [--purge]
```

### Orchestration order

1. **Assess** — `assess_retire` does a read-only all-branch scan. Returns one of three
   verdicts:
   - `SAFE` — every branch pushed, tree clean, no stashes, origin present.
   - `NEEDS_BACKUP` — work exists that would be lost: unpushed commits, branches with no
     upstream tracking ref, no-origin repos with commits, dirty working tree, stash entries,
     or detached HEAD commits.
   - `BLOCKED` — structural problem (`NOT_A_REPO`, or repo empty with no commits and no
     origin); cannot assess retirement safety.
2. **Backup or consent gate** — `SAFE` proceeds. `NEEDS_BACKUP` requires `--backup` or
   `--confirm`. `BLOCKED` requires `--confirm`. After `--backup`, retire RE-ASSESSES; if
   the repo is not provably `SAFE` afterward, it refuses again (add `--confirm` to accept
   any remainder).
3. **Worktree teardown** — a probe pass (dry-run internally) discovers all dirty worktrees
   before any clean one is removed. Dirty worktrees need `--backup` or `--confirm`. Failed
   teardowns (git errors on clean worktrees) also gate the clone move.
4. **Archive + unregister** — the clone moves to `archive.dir`
   (default `$GIT_WORKSPACE/.archived`). Unregister is performed last, only once the
   move succeeds. `--purge` hard-deletes the clone instead of archiving.

### Flag reference

| Flag | Effect |
|---|---|
| `--dry-run` | Print the full plan; mutate nothing (default-safe) |
| `--backup` | Durably push `wip/retire-<date>` branches / publish no-origin repos, then retire |
| `--confirm` | Proceed past the safety gate, explicitly accepting any remaining data loss |
| `--purge` | Hard-delete the clone instead of soft-archiving it (still gated) |

### The guardrail contract

**A repo never loses data without the operator's consent.**

- `assess_retire` scans ALL local branches, not just HEAD: unpushed commits, no-upstream
  refs, no-origin repos, dirty working trees, stashes, and detached HEAD WIP.
- `NEEDS_BACKUP` refuses unconditionally unless `--backup` (work reaches a remote) or
  `--confirm` (explicit acceptance of loss).
- After `--backup`, the orchestrator independently RE-ASSESSES. Retiring proceeds only if
  the repo is now `SAFE`; otherwise it refuses again.
- Dirty worktrees are probed before any clean worktree is removed — the "assess fully, then
  act" contract means a mixed-state rig never ends up partially torn down.
- Failed worktree teardowns prevent clone deletion; a live worktree must not be orphaned by
  moving the clone it references.
- `--dry-run` previews everything and mutates nothing.
- Soft-archive is the default (reversible); `--purge` and `archive prune` are the only
  irreversible deletes and both require explicit flags.

### Plugin notify on retire (WARN-ONLY)

Enabled plugins are notified when a rig retires, but the notify is **WARN-ONLY**: for orca
specifically, `orca project setup-delete --setup <id>` does de-register a repo upstream, but
retire only prints that command (plus `orca project setups --json` for finding `<id>`) as a
reminder rather than running it — `bh` never mutates `orca-data.json` to fake a removal. Run the
de-registration command by hand if you no longer want it tracked. See
[INTEGRATIONS](INTEGRATIONS.md#orca).

## `bh rig archive`

`bh rig archive` inspects and reclaims the soft-archive graveyard that `bh rig retire`
populates.

### `bh rig archive ls`

```sh
bh rig archive ls [--json]
```

Lists every `<group>/<org>/<repo>` clone under `archive.dir`, sorted oldest-first, with
age in days (directory mtime) and disk size. Prints a total at the bottom. `--json` emits
one object per repo with typed `age_days` and `size_bytes` fields.

### `bh rig archive prune`

```sh
bh rig archive prune [--older-than N[d]] [--all] [--dry-run]
```

Docker-`system-prune`-style reclamation. Removes archived repos whose age exceeds the
threshold and reports bytes reclaimed.

| Flag | Effect |
|---|---|
| `--older-than N[d]` | Remove repos archived more than N days ago (`30` or `30d`); default: `archive.window_days` config key (30) |
| `--all` | Remove every archived repo regardless of age |
| `--dry-run` | Preview what would be removed and bytes reclaimed; mutate nothing |

Path-escape guard: every candidate is resolved and confirmed to be strictly inside
`archive.dir` before any `shutil.rmtree` call — a misconfigured or symlinked `archive.dir`
cannot cause collateral damage outside the graveyard.

### Archive config keys

| Key | Default | Effect |
|---|---|---|
| `archive.dir` | `$GIT_WORKSPACE/.archived` | Root directory for soft-archived clones |
| `archive.window_days` | `30` | Default `--older-than` threshold for `archive prune` |

```sh
bh config set archive.dir /mnt/cold/bh-archive
bh config set archive.window_days 60
```

## Helpers

```sh
bh rig classify <group> <org> <repo>             # print the kind (group = repo-group path)
bh rig prefix   <group> <org> <repo> [kind]      # print the derived prefix
bh rig ready    [-v]                             # rig readiness check (read-only)
```

Registration, the registry schema, and how rigs are validated live in [LABELS](LABELS.md).
Spinning up isolated worktrees for a rig (per bead/branch/session) lives in
[WORKTREES](WORKTREES.md). The control-plane role that drives these verbs is documented in
[CONTROL-PLANE.md](CONTROL-PLANE.md).
