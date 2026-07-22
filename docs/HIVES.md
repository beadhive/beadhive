# Hives — onboarding & identity

A **hive** is a repo's beads database. This covers turning a repo into a hive and how `bh`
derives its identity (modules: `hive.py`, `identity.py`; prefix logic in `registry.py`).

## Identity from the path

`bh` derives a repo's `(group, org, repo)` from its location under the git-workspace root
(`$GIT_WORKSPACE`, default `~/workspace`): `<group>/<org>/.../<repo>`. The first segment is a
repo-group's **path** — not necessarily the provider TYPE (github/gitlab/gitea); a group's
`path` and `provider` can differ (see [INTEGRATIONS.md](INTEGRATIONS.md#git-workspace)). This
is the fast path used by `bh bd create` (the triplet) and `bh hive init` (registration).
Outside that layout, path-derived features degrade gracefully
(`identity.py:workspace_identity` returns `None`). The stored `managed_repos[].provider` field
name is unchanged for backward compatibility — it holds the group path.

## `bh hive init`

Run **from inside the target repo**:

```sh
bh hive init [--furnish] [--claude] [--plugin orca] [--kind K] [--prefix P] [--yes] [--dry-run]
```

Flow (`hive.py`):

1. Derive `group/org/repo` from the path (`group` = the repo-group path, not necessarily
   the provider type — see [INTEGRATIONS.md](INTEGRATIONS.md#git-workspace)).
2. **Classify** the repo (`registry.classify`) → its *kind*.
3. Resolve/derive the **prefix** (`registry.derive_prefix`), or use `--prefix`.
4. **Required-org check** — if the org's policy is `required`, the prefix must start with
   `<code>-`; otherwise it's blocked (a registration invariant, always enforced).
5. **Materialize beads** — zero-footprint (default): `bd bootstrap` when origin already
   carries `refs/dolt/data`, else `bd init --setup-exclude …` with bd's stray `.gitignore`
   block relocated into `.git/info/exclude`. Furnished: plain `bd init --prefix <p>
   --skip-agents --skip-hooks --init-if-missing --non-interactive`.
6. Register `{provider, org, repo, prefix, kind, upstream?, furnish}` in `config.yaml`.
7. Optionally install agent extras (`--claude`, `--agents`, `--skills` — each implies
   `--furnish`).
8. **Footprint** — settle the declared footprint (below). Zero-footprint: ensure `.beads/`
   stays stealth-excluded, commit nothing. Furnished: drop the stealth exclusion and commit
   the onboarding artifacts; a green furnished init/onboard ends with a **clean working
   tree** (and a clean `bh hive survey` row).

`--dry-run` prints the plan and changes nothing.

### Declared footprint: zero by default, furnished on opt-in

Onboarding makes in-repo changes **only when declared**. The default is **zero-footprint**:
`.beads/` lives locally behind `.git/info/exclude`, nothing is tracked, nothing is
committed — bead state rides `refs/dolt/data` on the remote, not the working tree.

**Furnishing** (`--furnish`, or implied by `--claude`/`--agents`/`--skills`) is a conscious,
**ownership-gated** opt-in that puts AGF furniture into the repo's history. It is refused
without confirmed push access, and **external hives (forks / distinct-upstream repos) may
never be furnished**. The declaration is recorded on the registry entry (`furnish:
none|full`) and is sticky across re-onboards; entries without the key infer `none` for
forks and `full` otherwise (the pre-furnish behavior — zero migration).

- **Tracked (furnished hives only):** `.beads/config.yaml`, `.beads/metadata.json`,
  `.beads/issues.jsonl`, `.beads/.gitignore`, `.claude/settings.json`, and the managed
  `CLAUDE.md` / `AGENTS.md` hints. bd's own `.beads/.gitignore` keeps the local-only pieces
  (Dolt db, locks, backups) out of the commit.
- **Host-local only** (`.git/info/exclude`, never the tracked `.gitignore`): `.ws/`,
  `.claude/settings.local.json` (the machine-specific sandbox grant), and — on
  zero-footprint hives — all of `.beads/`.
- Harnesses that only read `AGENTS.md` and have no `bh`-driven furnishing (e.g. Codex — see
  [AGF.md — Per-harness support matrix](AGF.md#per-harness-support-matrix)) can't see a
  zero-footprint hive's AGF setup; declare `--agents`/`--furnish` for those repos. OpenCode also
  reads `AGENTS.md` natively, but as of `--opencode` furnishing it additionally gets its own
  furnished seat defs (translated `.opencode/agents/`, MCP wiring, permissions, skills) the same
  way Claude Code does — `--agents`/`--furnish` is a fallback for OpenCode, not its only path in.

Furnished commits never litter duplicate identically-titled commits: the footprint step
amends an **unpushed** scaffold commit in place, and a later repair pass commits as
`chore(agf): hive scaffolding repair` instead of reusing the original subject
(`chore(agf): hive scaffolding (beads + agent config)`). Re-running `bh hive
init`/`onboard` on an already-diverged furnished hive applies the same repair — hive-state
residue (`.beads/`, `.claude/`, `CLAUDE.md`, `AGENTS.md`) does not trip the `dirty-tree`
gate. Until upstream `bd init --no-commit` lands, a furnished `bd init` still makes its own
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

## Agent extras (independent, opt-in — each implies `--furnish`)

Bundled in the package, merged non-destructively (existing hooks/denies preserved).
`.beads/PRIME.md` is **deprecated** (steering is bh-owned; `bd prime` is no longer hooked):

- **`--claude`** → installs `.claude/settings.json` (a `deny` rule for `bd remember`) and a
  `statusLine` block so the TUI shows the active seat and hive. In **plugin mode** (default,
  `claude.source: plugin`) it also runs
  `claude plugin marketplace add` + `claude plugin install agf` — seat agents and role skills
  arrive via the `agf` Claude Code plugin rather than being copied into the hive. In **copy
  mode** (`claude.source: copy`) it writes `.claude/agents/` and `skills/` directly, which is
  the legacy behaviour suitable for offline or airgapped environments.

Use either, both, or neither. Default `bh hive init` writes no agent files (it passes
`--skip-agents --skip-hooks` to beads) and no tracked files at all (zero-footprint).

### Plugin mode vs copy mode

| | Plugin mode (default) | Copy mode (`claude.source: copy`) |
|---|---|---|
| Agent defs | `agf:<seat>` plugin, user or project scope | `.claude/agents/<seat>.md` in the hive |
| Role skills | bundled inside the `agf` plugin | `skills/` directory in the hive |
| `bh hive ready` skills check | passes when `agf` plugin is installed | passes when `skills/` dir is present |
| Offline / airgapped | requires plugin install at onboard time | works offline after copy |
| Local override | `.claude/agents/<seat>.md` outranks the plugin | n/a |

Configure via `claude:` in `~/.ws/config.yaml` — see [CONFIGURATION.md](CONFIGURATION.md#claude-section).

## `bh hive context` (session hooks — steering with zero repo files)

`bh hive context` is a hidden, read-only verb for **session-start hooks**: inside a registered
hive it prints the AGF steering text (the hint-stanza body plus this hive's
prefix / kind / footprint from the registry); with `--hook-json` it wraps that in the Claude
Code SessionStart `hookSpecificOutput.additionalContext` envelope. Outside a hive — or in an
unregistered repo, or on **any** internal error — it prints nothing and exits 0, because a
hook consumer must never break a session start.

This is how **zero-footprint hives** get in-session AGF steering with no tracked files: a
user-level plugin hook (see the bh Claude plugin) calls `bh hive context --hook-json` and the
registry, not the repo, supplies the context. Furnished hives get the same payload; their
CLAUDE.md/AGENTS.md stanza remains the harness-agnostic fallback.

## `bh hive add` / `bh hive rm`

`bh hive add` registers a triplet in the registry **without a `cwd`** and without running
`bd init`. Use it when the repo is remote or uncloned and you only need the registry entry:

```sh
bh hive add github/acme/infra
bh hive add github/acme/infra --prefix ac-infra --kind org-native
bh hive add github/acme/fork  --kind fork --upstream acme-upstream/infra
```

`bh hive rm` unregisters a hive by id — **registry-only**; it does not touch `.beads`, labels,
or the repo on disk:

```sh
bh hive rm github/acme/infra   # or any hive-match form the registry resolves
```

Both `add` and `rm` are the control-plane equivalents of `hive init`'s side-effect; use
`hive init` (or `hive onboard`) when you have a local checkout that also needs `bd init`.

## `bh hive onboard`

`bh hive onboard` is the **end-to-end** path: it resolves the target directory under
`$GIT_WORKSPACE`, clones if absent, runs the full `hive init` logic (including `bd init`),
and syncs the hub — all in one command:

```sh
# Local folder already cloned — inits in place, syncs hub:
bh hive onboard github/acme/infra

# Remote repo not yet cloned — clones first, then inits + syncs:
bh hive onboard github/acme/infra --clone-url https://github.com/acme/infra.git

# Furnish + install hive furniture in one shot (owner-only):
bh hive onboard github/acme/infra \
  --claude --skills --observaloop --agents
```

`--clone-url` is **guarded**: the clone only happens when the target directory is absent. An
already-local folder is onboarded in place. This prevents cloning over a live checkout.

Options mirror `bh hive init`: `--furnish` (declare tracked furniture), `--claude` (settings,
seat agent defs, and statusLine), `--skills` (role skills), `--observaloop` (observaloop profile),
`--agents` (AGENTS.md hint), `--plugin NAME` (enable a plugin integration for this hive,
repeatable — e.g. `--plugin orca` registers the clone with orca; see
[INTEGRATIONS](INTEGRATIONS.md#orca)), `--force` (re-register), `--kind`, `--prefix`,
`--yes` (required for forks).

## `bh hive ls` / `bh hive ls --available`

`bh hive ls` lists **registered** hives from the registry. `--available` switches to a
**discovery** view — repos tracked by git-workspace (`workspace-lock.toml`) that are **not**
yet registered — the candidates for `bh hive add` or `bh hive onboard`:

```sh
bh hive ls              # registered hives
bh hive ls --available  # discoverable-but-unregistered (zero API calls)
```

The `--available` view is a pure diff: git-workspace's tracked repos minus `managed_repos`.
No live API calls are made; it reads only the lock file and the registry.

## `bh hive survey`

`bh hive survey` is a **read-only fleet table** — one row per on-disk repo (registered and
tracked) — for onboarding triage. Run it before committing to an onboarding batch to see
which repos are easy candidates and which need attention first.

```sh
bh hive survey                     # all on-disk repos
bh hive survey --available         # unregistered candidates only
bh hive survey --sort difficulty   # easiest first; also: disk | age
bh hive survey --json              # machine-readable JSON (one object per repo)
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
  ceremony; `bh hive ready` should pass immediately after init.
- **`MEDIUM`** — no hard signals but fewer than two easy signals. Proceed, but review the
  repo's state before onboarding.
- **`HARD`** — one or more hard signals. Resolve the blocking condition first: push pending
  commits, clean the working tree, or accept that the repo needs attention before it can be
  onboarded.
- **`NOT-A-CANDIDATE`** — registry policy says `excluded`; `bh hive init` refuses this repo.

Typical triage flow: `bh hive survey --available --sort difficulty` → start with `EASY` rows
→ confirm each hive after init with `bh hive ready [-v]` → use `bh doctor` for the
fleet-level aggregate health view.

## `bh hive retire`

`bh hive retire` is the **guarded teardown** command — the symmetric counterpart to
`bh hive onboard`. Run `bh hive survey` first to identify the candidate, then dry-run before
committing.

```sh
bh hive retire <hive> [--dry-run] [--backup] [--confirm] [--purge]
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
  act" contract means a mixed-state hive never ends up partially torn down.
- Failed worktree teardowns prevent clone deletion; a live worktree must not be orphaned by
  moving the clone it references.
- `--dry-run` previews everything and mutates nothing.
- Soft-archive is the default (reversible); `--purge` and `archive prune` are the only
  irreversible deletes and both require explicit flags.

### Plugin notify on retire (WARN-ONLY)

Enabled plugins are notified when a hive retires, but the notify is **WARN-ONLY**: for orca
specifically, `orca project setup-delete --setup <id>` does de-register a repo upstream, but
retire only prints that command (plus `orca project setups --json` for finding `<id>`) as a
reminder rather than running it — `bh` never mutates `orca-data.json` to fake a removal. Run the
de-registration command by hand if you no longer want it tracked. See
[INTEGRATIONS](INTEGRATIONS.md#orca).

## `bh hive archive`

`bh hive archive` inspects and reclaims the soft-archive graveyard that `bh hive retire`
populates.

### `bh hive archive ls`

```sh
bh hive archive ls [--json]
```

Lists every `<group>/<org>/<repo>` clone under `archive.dir`, sorted oldest-first, with
age in days (directory mtime) and disk size. Prints a total at the bottom. `--json` emits
one object per repo with typed `age_days` and `size_bytes` fields.

### `bh hive archive prune`

```sh
bh hive archive prune [--older-than N[d]] [--all] [--dry-run]
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
bh hive classify <group> <org> <repo>             # print the kind (group = repo-group path)
bh hive prefix   <group> <org> <repo> [kind]      # print the derived prefix
bh hive ready    [-v]                             # hive readiness check (read-only)
```

Registration, the registry schema, and how hives are validated live in [LABELS](LABELS.md).
Spinning up isolated worktrees for a hive (per bead/branch/session) lives in
[WORKTREES](WORKTREES.md). The control-plane role that drives these verbs is documented in
[CONTROL-PLANE.md](CONTROL-PLANE.md).
