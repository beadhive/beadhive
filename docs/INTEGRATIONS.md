# Integrations

`ws` layers on external tools. Today there is one integration — **git-workspace** — and it's
optional (modules: `gitworkspace.py`; routing in `route.py`).

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

## Status / diagnostics

`ws doctor` reports how the integration and the registry line up — see
[DIAGNOSTICS](DIAGNOSTICS.md).
