# Rigs — onboarding & identity

A **rig** is a repo's beads database. This covers turning a repo into a rig and how `ws`
derives its identity (modules: `rig.py`, `identity.py`; prefix logic in `registry.py`).

## Identity from the path

`ws` derives a repo's `(provider, org, repo)` from its location under the git-workspace root
(`$GIT_WORKSPACE`, default `~/workspace`): `<provider>/<org>/.../<repo>`. This is the fast
path used by `ws bd create` (the triplet) and `ws rig init` (registration). Outside that
layout, path-derived features degrade gracefully (`identity.py:workspace_identity` returns
`None`).

## `ws rig init`

Run **from inside the target repo**:

```sh
ws rig init [--prime] [--claude] [--kind K] [--prefix P] [--yes] [--dry-run]
```

Flow (`rig.py`):

1. Derive `provider/org/repo` from the path.
2. **Classify** the repo (`registry.classify`) → its *kind*.
3. Resolve/derive the **prefix** (`registry.derive_prefix`), or use `--prefix`.
4. **Required-org check** — if the org's policy is `required`, the prefix must start with
   `<code>-`; otherwise it's blocked (a registration invariant, always enforced).
5. `bd init --prefix <p> --skip-agents --skip-hooks --non-interactive`.
6. Register `{provider, org, repo, prefix, kind, upstream?}` in `config.yaml`.
7. Optionally install agent extras (`--prime`, `--claude`).

`--dry-run` prints the plan and changes nothing.

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
  and a `deny` rule for `bd remember`), the seat agent defs in `.claude/agents/`, and a
  `statusLine` block so the TUI shows the active seat and rig.

Use either, both, or neither. Default `ws rig init` writes no agent files (it passes
`--skip-agents --skip-hooks` to beads).

## `ws rig add` / `ws rig rm`

`ws rig add` registers a triplet in the registry **without a `cwd`** and without running
`bd init`. Use it when the repo is remote or uncloned and you only need the registry entry:

```sh
ws rig add github/acme/infra
ws rig add github/acme/infra --prefix ac-infra --kind org-native
ws rig add github/acme/fork  --kind fork --upstream acme-upstream/infra
```

`ws rig rm` unregisters a rig by id — **registry-only**; it does not touch `.beads`, labels,
or the repo on disk:

```sh
ws rig rm github/acme/infra   # or any rig-match form the registry resolves
```

Both `add` and `rm` are the control-plane equivalents of `rig init`'s side-effect; use
`rig init` (or `rig onboard`) when you have a local checkout that also needs `bd init`.

## `ws rig onboard`

`ws rig onboard` is the **end-to-end** path: it resolves the target directory under
`$GIT_WORKSPACE`, clones if absent, runs the full `rig init` logic (including `bd init`),
and syncs the hub — all in one command:

```sh
# Local folder already cloned — inits in place, syncs hub:
ws rig onboard github/acme/infra

# Remote repo not yet cloned — clones first, then inits + syncs:
ws rig onboard github/acme/infra --clone-url https://github.com/acme/infra.git

# Install AGF furniture in one shot:
ws rig onboard github/acme/infra \
  --prime --claude --skills --observaloop --agents
```

`--clone-url` is **guarded**: the clone only happens when the target directory is absent. An
already-local folder is onboarded in place. This prevents cloning over a live checkout.

Options mirror `ws rig init`: `--prime` (PRIME.md), `--claude` (settings, seat agent defs,
and statusLine), `--skills` (role skills), `--observaloop` (observaloop profile),
`--agents` (AGENTS.md hint), `--force` (re-register), `--kind`, `--prefix`,
`--yes` (required for forks).

## `ws rig ls` / `ws rig ls --available`

`ws rig ls` lists **registered** rigs from the registry. `--available` switches to a
**discovery** view — repos tracked by git-workspace (`workspace-lock.toml`) that are **not**
yet registered — the candidates for `ws rig add` or `ws rig onboard`:

```sh
ws rig ls              # registered rigs
ws rig ls --available  # discoverable-but-unregistered (zero API calls)
```

The `--available` view is a pure diff: git-workspace's tracked repos minus `managed_repos`.
No live API calls are made; it reads only the lock file and the registry.

## `ws rig survey`

`ws rig survey` is a **read-only fleet table** — one row per on-disk repo (registered and
tracked) — for onboarding triage. Run it before committing to an onboarding batch to see
which repos are easy candidates and which need attention first.

```sh
ws rig survey                     # all on-disk repos
ws rig survey --available         # unregistered candidates only
ws rig survey --sort difficulty   # easiest first; also: disk | age
ws rig survey --json              # machine-readable JSON (one object per repo)
```

### Columns

| Column | Meaning |
|---|---|
| `REPO` | `provider/org/repo` triplet |
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
  ceremony; `ws rig ready` should pass immediately after init.
- **`MEDIUM`** — no hard signals but fewer than two easy signals. Proceed, but review the
  repo's state before onboarding.
- **`HARD`** — one or more hard signals. Resolve the blocking condition first: push pending
  commits, clean the working tree, or accept that the repo needs attention before it can be
  onboarded.
- **`NOT-A-CANDIDATE`** — registry policy says `excluded`; `ws rig init` refuses this repo.

Typical triage flow: `ws rig survey --available --sort difficulty` → start with `EASY` rows
→ confirm each rig after init with `ws rig ready [-v]` → use `ws doctor` for the
fleet-level aggregate health view.

## Helpers

```sh
ws rig classify <provider> <org> <repo>          # print the kind
ws rig prefix   <provider> <org> <repo> [kind]   # print the derived prefix
ws rig ready    [-v]                             # AGF readiness check (read-only)
```

Registration, the registry schema, and how rigs are validated live in [LABELS](LABELS.md).
Spinning up isolated worktrees for a rig (per bead/branch/session) lives in
[WORKTREES](WORKTREES.md). The control-plane role that drives these verbs is documented in
[CONTROL-PLANE.md](CONTROL-PLANE.md).
