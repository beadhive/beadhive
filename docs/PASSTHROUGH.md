# Passthrough & rig routing

`ws bd` and `ws git` forward to `bd`/`git`, optionally across rigs (modules: `bd.py`,
`git.py`, `route.py`).

## `ws bd`

Forwards to `bd` in the current directory, with one enhancement: `ws bd create` auto-applies
the `provider:/org:/repo:` triplet derived from the path (ports the old `bdc`). Outside a
managed path it degrades to plain `bd create`. Before creating, it refuses if the rig has
label violations ([LABELS](LABELS.md#enforcement)).

```sh
ws bd ready
ws bd create "Fix login" -p 1      # → bd create … -l provider:…,org:…,repo:…
```

## `ws git`

Forwards to `git`, including `git workspace …` (git-workspace's own subcommands). One special
case: git hijacks `--help` for subcommands, so `ws git workspace --help` is rerouted to the
`git-workspace` binary (which has the real help).

```sh
ws git status
ws git workspace list
ws git workspace --help            # → git-workspace --help
```

## Rig routing (`-a` / `-r`)

Run the passthrough across rigs instead of the current directory. Flags are **global** —
they go on `ws`, before the subcommand:

```sh
ws -a bd dolt push                 # every registered rig
ws -a git status
ws -r ag-infra git log --oneline   # one rig
ws -r ag-infra bd ready
```

- `-a/--all` → every entry in `managed_repos` (registered rigs; the ws domain).
- `-r/--rig <id>` → one rig (resolution below).
- no flag → the current directory (today's plain passthrough; works without git-workspace).

For *all cloned repos* (broader than registered rigs), use git-workspace's own runner:
`ws git workspace run -- <cmd>`.

### Mechanics (`route.py`)

- The root callback captures the flags; `route.targets(cfg, mode, target)` resolves them to
  `[(label, cwd)]`.
- `route.fan_out(targets, runner)` runs each, printing a `=== <rig>  <path> ===` header for
  multi-target runs, **continuing past failures**, and ending with an
  `N ok / M failed / K skipped` summary (exit non-zero if any failed). A single
  current-directory run propagates the child's exact exit code.
- `ws -r/-a bd create` applies each target rig's own triplet (cwd-aware).

### Gating & guards

- `-a`/`-r` require **`git_workspace.enabled`** ([INTEGRATIONS.md](INTEGRATIONS.md));
  otherwise they fail fast with `this feature requires git_workspace enabled`.
- They're honored only by `bd`/`git`; using them elsewhere, with `ws git workspace …`, or
  after the subcommand is rejected (see [CLI](CLI.md#global-routing-flags)).

### Resolving `-r <id>` (`rig_match`)

Set under `git_workspace` in config; default `flexible`:

- **flexible** — try in order: prefix (`ag-infra`) → triplet (`github/agentguides/infra`) →
  `org/repo` (`agentguides/infra`) → bare repo (`infra`, only if unique).
- **prefix** — only the beads prefix.
- **triplet** — only the full `provider/org/repo`.

Resolution maps to `managed_repos` and the rig's checkout dir under `$GIT_WORKSPACE`
(`registry.resolve_rig` / `rig_dir`).
