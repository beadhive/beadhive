# Integrations

`bh` layers on external tools. The foundational one is **git-workspace**; **orca** is the first
**plugin** (a generic seam the onboard / retire / rig-ready lifecycle loops over — see
`plugins.py`). All integrations are optional (modules: `gitworkspace.py`, `orca.py`, `plugins.py`;
routing in `route.py`).

## git-workspace

[orf/git-workspace](https://github.com/orf/git-workspace) clones a fleet of repos into a
`<provider>/<org>/<repo>` layout under `$GIT_WORKSPACE` and tracks them in
`$GIT_WORKSPACE/workspace*.toml`. `bh` already derives rig identity from that layout; enabling
the integration also lets it read git-workspace's config.

### Enabling

```yaml
# ~/.ws/config.yaml
git_workspace:
  enabled: true
  # path: ~/workspace/workspace.toml   # optional; default: glob $GIT_WORKSPACE/workspace*.toml
  # rig_match: flexible                 # how `bh -r <id>` resolves (see PASSTHROUGH.md)
```

### What it reads

From each `[[provider]]` in `workspace*.toml` (parsed with stdlib `tomllib`; the
`workspace-lock.toml` lock is **not** treated as a config source):

- `path` → a recognized `provider:` label,
- `name` → an `org:` label.

From `workspace-lock.toml` it reads each repo's clone **URL**, used by the hub to fetch
uncloned rigs.

### What it unlocks

- **Provider auto-load** — `providers:` can be omitted from `config.yaml`; the effective set is
  the union of config + git-workspace. Org **codes/policies** still come from `config.yaml`
  `orgs:` (absent orgs fall back to `sanitize(name)[:2]` + `personal`).
- **Rig routing** `-a`/`-r` for `bh bd` / `bh git` → see [PASSTHROUGH](PASSTHROUGH.md).
- **Remote-cache hub** for uncloned rigs → see [HUB](HUB.md).
- **`bh git workspace …`** central passthrough, with the `--help` reroute → see
  [PASSTHROUGH](PASSTHROUGH.md).

### Scope & gating

- **Rigs vs all repos.** `-a` targets **registered rigs** (`managed_repos`). To act on *every*
  cloned repo (rig or not), use git-workspace's own runner: `bh git workspace run -- <cmd>`.
- **Gating.** `-a`/`-r` and provider auto-load require `git_workspace.enabled`; routing fails
  fast otherwise (`this feature requires git_workspace enabled`). Everything else — plain
  `bh bd`/`bh git`, `rig init`, `labels`, `sync`/`hub` over cloned rigs, `dolt`, `doctor`,
  `backup` — works whether or not the integration is on.

### Lifecycle roadmap (design intent, not yet built)

The hub + minimal-clone cache is the foundation for keeping most rigs remote until needed:

1. **Import** git-workspace providers → register rigs (first-time setup).
2. **Add remote-only** rigs and browse their issue graphs via the hub (no code clone).
3. **Clone down to work** — configure git-workspace from a rig's info + `git workspace update`
   to materialize the checkout and wire beads for live work.
4. **Release** — when done, verify branches are clean and beads is pushed, then remove the
   repo from git-workspace to reclaim disk (the rig stays registered + viewable via cache).

Also deferred: `bh config import-orgs` (write stub org entries); high-level verbs coordinating
a git branch + its beads issues together.

## Orca

orca is a separate repo-registry tool that keeps a list of known repos in a JSON store. `bh` can
register its git-workspace clones with orca so orca's own tooling sees them. orca is the **first bh
plugin**: the generic `bh plugin` seam (`plugins.py`) drives it through the onboard / retire /
rig-ready lifecycle, so nothing about orca is hardcoded into those flows.

### Enabling

```yaml
# ~/.ws/config.yaml
orca:
  enabled: true
  # data_path: ~/.config/orca/orca-data.json   # default: platform-aware, see below
  # worktrees: true                            # opt in to worktree delegation (see below)
  # worktrees:
  #   enabled: true
  #   fallback: false                          # true = degrade to native git when orca fails
```

Per-rig overrides live on the `managed_repos` entry (`orca: {enabled: true, worktrees: true}`) and
the `enabled` flag is set with the generic feature-flag verbs: `bh rig enable orca <rig>` /
`bh rig disable orca <rig>`. A rig entry's `orca.worktrees` wins over the global `orca.worktrees`
(bare bool or `{enabled, fallback}` mapping); `orca.worktrees.fallback` itself is global-only.

### What it reads

orca's state file is **`orca-data.json`** — default `~/Library/Application Support/orca/
orca-data.json` on macOS, `~/.config/orca/orca-data.json` elsewhere (overridable via
`orca.data_path`). It holds three collections — `repos`, `projects`, and `projectHostSetups` —
and **`bh` only ever reads/writes the `repos` list and the `settings` object directly**:

- `repos` — a list of registered repos; each entry carries a `path`. `bh` lists them via
  `orca repo list --json` when the orca CLI is on `PATH`, else by reading `orca-data.json` directly.
- `settings.autoRenameBranchFromWork` — a **global**, UI-only setting (see
  [Worktree delegation](#worktree-delegation) below); `bh` parses it read-only except through the
  dedicated `fix-settings` verb.

`bh` never reads `projects` / `projectHostSetups` directly, and never touches any orchestration
database. The one deliberate exception is CLI-only: worktree-delegation wiring drives
`orca project setups` / `setup-update` (never the data file's `projects`/`projectHostSetups`
keys) to point a repo's project-setup at bh's shadow worktree dir — see below.

### What it unlocks

- **Repo registration on onboard** — `bh rig onboard … --plugin orca` (or with orca enabled in
  config) registers the freshly onboarded clone with orca via `orca repo add`.
- **`bh plugin orca sync`** — walks the real on-disk clones exactly three levels under
  `$GIT_WORKSPACE` (`provider/org/repo` dirs containing `.git`) and registers any not yet known to
  orca. Idempotent: a second run adds nothing. `--dry-run` previews without writing.
- **`bh rig ready`** — shows an `orca` readiness line (registered / not registered, or the
  worktree-delegation readiness states below) when enabled.

### Worktree delegation

With `orca.worktrees` on for a rig, `bh worktree` hands new-branch **create** and **remove**
(`bh worktree rm` / `prune`) to `orca worktree create` / `orca worktree rm` instead of plain
`git worktree`, so the tree shows up managed in Orca's desktop/mobile UI at bh's own
`wt/bead/<type>/<id>` path + branch convention.

- **Delegation policy — hard fail by default.** If a delegated create/remove fails (orca down, a
  bad result, a path/branch mismatch), `bh` raises rather than silently falling through to native
  git — a silently-broken delegation must never masquerade as success. Set
  `orca.worktrees.fallback: true` to relax this to warn-and-fall-back-to-native instead.
- **Attach and `verify-` trees are never delegated.** Only the *new-branch* create path can be
  taken over by orca; re-attaching an existing branch into a fresh dir always stays native (a
  warning is printed if a delegating plugin is enabled), and the ephemeral `verify-*`
  clean-checkout worktrees used by `bh work check`/`submit` bypass the delegation seam entirely —
  they're not a durable seat.
- **`keep_branch` semantics on remove.** orca's `worktree rm` deletes the tree's checked-out
  branch outright, even without `--force`. `bh worktree rm` (the durable-branch path) detaches
  HEAD first so the branch survives; `bh worktree prune` (already-merged, disposable branches)
  skips the detach so orca's delete matches native prune's own branch cleanup.
- **Readiness states** (`bh rig ready`, once `orca.worktrees` is on): `ok` when the orca runtime
  is reachable (`orca status --json`) and `settings.autoRenameBranchFromWork` is off; `warn`
  otherwise, naming every problem (runtime down — delegation will hard-fail or fall back per the
  `fallback` knob; or auto-rename is on).
- **Onboard/sync worktree-base-path wiring.** When `orca.worktrees` is on, `bh rig onboard` and
  `bh plugin orca sync` best-effort point the rig's orca project-setup `worktree-base-path` at
  `config.worktrees_root()/<provider>/<org>` (orca appends `<repo-displayName>/<leaf>` itself
  under its default `nestWorkspaces: true`, landing delegated trees exactly at bh's own worktree
  dir). This is onboarding bookkeeping, not the hard-failing hooks above — it warns and
  continues on any failure (missing CLI, no matching project-setup, a failing `setup-update`).
- **Auto-Rename Branch From Work.** `settings.autoRenameBranchFromWork` is a **global**, UI-only
  orca setting (default ON) that renames branches after agent startup — left on, it fights bh's
  `wt/bead/...` naming convention. There's no per-repo CLI override, so:
  - onboard/sync print an operator instruction to disable it by hand in Orca's Settings UI
    whenever it's on and worktree delegation is enabled;
  - **`bh plugin orca fix-settings`** flips it to `false` directly in `orca-data.json`, but
    *only* while `orca status` shows the runtime down — a safe write window where the live app
    isn't holding the file open. It refuses (exit 1, same Settings-UI instruction) when the
    runtime is up, and preserves every other key when it writes (atomic temp-file + rename).

### Scope & gating

- **repos + settings only** (plus the CLI-only project-setup exception above). `bh` confines
  itself to orca's `repos` list and the `settings` object — `projects` / `projectHostSetups`
  and any orchestration DB stay out of scope, by design.
- **Gating.** orca requires the **git-workspace** integration: `orca_enabled` is false whenever
  `git_workspace.enabled` is off, regardless of the orca flag (it registers git-workspace clones).
  Worktree delegation (`orca_worktrees_enabled`) is further AND-gated on `orca_enabled`.
- **Retire names the de-registration verb, WARN-only.** `orca project setup-delete --setup <id>`
  does de-register a repo — but retire only *prints* the command (with `orca project setups
  --json` for finding `<id>`) rather than running it, since auto-deleting a project-setup on
  retire risks dropping orca state the operator wanted to keep. `bh` never mutates
  `orca-data.json` to fake a removal.
- **Best-effort.** A missing orca CLI, an unreadable data file, or a failing `orca` subprocess
  degrades to a warning; it never aborts onboarding, retire, or rig-ready. The worktree
  delegation hooks (`create`/`remove`) are the deliberate exception — see above.

## Status / diagnostics

`bh doctor` reports how the integration and the registry line up — see
[DIAGNOSTICS](DIAGNOSTICS.md).
