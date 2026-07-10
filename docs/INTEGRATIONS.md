# Integrations

`ws` layers on external tools. The foundational one is **git-workspace**; **orca** is the first
**plugin** (a generic seam the onboard / retire / rig-ready lifecycle loops over — see
`plugins.py`). All integrations are optional (modules: `gitworkspace.py`, `orca.py`, `plugins.py`;
routing in `route.py`).

## git-workspace

[orf/git-workspace](https://github.com/orf/git-workspace) clones a fleet of repos into a
`<provider>/<org>/<repo>` layout under `$GIT_WORKSPACE` and tracks them in
`$GIT_WORKSPACE/workspace*.toml`. `ws` already derives rig identity from that layout; enabling
the integration also lets it read git-workspace's config.

### Enabling

```yaml
# ~/.ws/config.yaml
git_workspace:
  enabled: true
  # path: ~/workspace/workspace.toml   # optional; default: glob $GIT_WORKSPACE/workspace*.toml
  # rig_match: flexible                 # how `ws -r <id>` resolves (see PASSTHROUGH.md)
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
- **Rig routing** `-a`/`-r` for `ws bd` / `ws git` → see [PASSTHROUGH](PASSTHROUGH.md).
- **Remote-cache hub** for uncloned rigs → see [HUB](HUB.md).
- **`ws git workspace …`** central passthrough, with the `--help` reroute → see
  [PASSTHROUGH](PASSTHROUGH.md).

### Scope & gating

- **Rigs vs all repos.** `-a` targets **registered rigs** (`managed_repos`). To act on *every*
  cloned repo (rig or not), use git-workspace's own runner: `ws git workspace run -- <cmd>`.
- **Gating.** `-a`/`-r` and provider auto-load require `git_workspace.enabled`; routing fails
  fast otherwise (`this feature requires git_workspace enabled`). Everything else — plain
  `ws bd`/`ws git`, `rig init`, `labels`, `sync`/`hub` over cloned rigs, `dolt`, `doctor`,
  `backup` — works whether or not the integration is on.

### Lifecycle roadmap (design intent, not yet built)

The hub + minimal-clone cache is the foundation for keeping most rigs remote until needed:

1. **Import** git-workspace providers → register rigs (first-time setup).
2. **Add remote-only** rigs and browse their issue graphs via the hub (no code clone).
3. **Clone down to work** — configure git-workspace from a rig's info + `git workspace update`
   to materialize the checkout and wire beads for live work.
4. **Release** — when done, verify branches are clean and beads is pushed, then remove the
   repo from git-workspace to reclaim disk (the rig stays registered + viewable via cache).

Also deferred: `ws config import-orgs` (write stub org entries); high-level verbs coordinating
a git branch + its beads issues together.

## Orca

orca is a separate repo-registry tool that keeps a list of known repos in a JSON store. `ws` can
register its git-workspace clones with orca so orca's own tooling sees them. orca is the **first bh
plugin**: the generic `bh plugin` seam (`plugins.py`) drives it through the onboard / retire /
rig-ready lifecycle, so nothing about orca is hardcoded into those flows.

### Enabling

```yaml
# ~/.ws/config.yaml
orca:
  enabled: true
  # data_path: ~/.config/orca/orca-data.json   # default: ~/.config/orca/orca-data.json
```

Per-rig overrides live on the `managed_repos` entry (`orca: {enabled: true}`) and are set with the
generic feature-flag verbs: `bh rig enable orca <rig>` / `bh rig disable orca <rig>`.

### What it reads

orca's state file is **`orca-data.json`** (default `~/.config/orca/orca-data.json`, overridable via
`orca.data_path`). It holds three collections — `repos`, `projects`, and `projectHostSetups` —
but **`ws` only ever reads and writes `repos`**:

- `repos` — a list of registered repos; each entry carries a `path`. `ws` lists them via
  `orca repo list --json` when the orca CLI is on `PATH`, else by reading `orca-data.json` directly.

`ws` **never** reads `projects` / `projectHostSetups`, and never touches any orchestration database.

### What it unlocks

- **Repo registration on onboard** — `bh rig onboard … --plugin orca` (or with orca enabled in
  config) registers the freshly onboarded clone with orca via `orca repo add`.
- **`bh plugin orca sync`** — walks the real on-disk clones exactly three levels under
  `$GIT_WORKSPACE` (`provider/org/repo` dirs containing `.git`) and registers any not yet known to
  orca. Idempotent: a second run adds nothing. `--dry-run` previews without writing.
- **`bh rig ready`** — shows an `orca` readiness line (registered / not registered) when enabled.

### Scope & gating

- **repos only.** `ws` confines itself to orca's `repos` list — `projects` / `projectHostSetups`
  and any orchestration DB are out of scope, by design.
- **Gating.** orca requires the **git-workspace** integration: `orca_enabled` is false whenever
  `git_workspace.enabled` is off, regardless of the orca flag (it registers git-workspace clones).
- **Retire is WARN-ONLY.** orca has no de-registration verb, so retiring a rig only prints a
  manual-removal reminder — `ws` never mutates `orca-data.json` to fake a removal.
- **Best-effort.** A missing orca CLI, an unreadable data file, or a failing `orca` subprocess
  degrades to a warning; it never aborts onboarding, retire, or rig-ready.

## Status / diagnostics

`ws doctor` reports how the integration and the registry line up — see
[DIAGNOSTICS](DIAGNOSTICS.md).
