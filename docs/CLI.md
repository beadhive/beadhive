# CLI

The command surface (module: `cli.py`, a Typer app). `ws` (or any group) with no args prints
help.

## Help panels

`ws --help` groups commands by purpose:

| Panel | Commands | Theme |
|---|---|---|
| **Workspace & rigs** | `sync`, `hub`, `rig`, `labels` | operate on rigs in the workspace |
| **Passthrough — honor `-a/--all` and `-r/--rig`** | `bd`, `git` | forward to a tool, per rig |
| **Admin (ws itself)** | `doctor`, `backup`, `dolt`, `config` | manage `ws`/its infra |

Panels are set via Typer's `rich_help_panel`; order is Workspace → Passthrough → Admin.

## Global routing flags

`-a/--all` and `-r/--rig <id>` are **root** options, placed **before** the subcommand:

```sh
ws -a git status              # run in every registered rig
ws -r ag-infra bd dolt push   # run in one rig
```

They're captured by the root callback into `ctx.obj` and consumed only by the **passthrough**
commands (`bd`, `git`). Guards (all enforced):

- using them on any other command (`ws -a doctor`) → error;
- using them with `ws git workspace …` (which runs centrally) → error;
- placing them *after* the subcommand (`ws git -a …`) → a hint to move them before.

Full semantics: [PASSTHROUGH](PASSTHROUGH.md). Routing requires the git-workspace
integration enabled ([INTEGRATIONS.md](INTEGRATIONS.md)).

## Passthrough command pattern

`bd`, `git`, and `hub` are passthroughs: Typer `context_settings` use
`allow_extra_args + ignore_unknown_options` and `add_help_option=False`, so all args after the
subcommand are forwarded verbatim to the underlying tool. (`ws bd --help` shows beads' help;
`ws git workspace --help` is rerouted to the `git-workspace` binary.)

## Full surface

```text
ws sync                       build/refresh the hub (HUB.md)
ws hub <bd cmd>               query the cross-rig hub (HUB.md)
ws rig init [opts]            onboard the current repo (RIGS.md)
ws rig classify|prefix …      registry helpers (RIGS.md)
ws labels validate|sync|report|allowed|docs   registry ops (LABELS.md)
ws worktree add|list|path|rm|prune   ws-managed worktrees (WORKTREES.md)
ws work brief|claim|check|submit|resume|abandon   bead lifecycle driver (WORK.md)
ws bd <args> | ws git <args>  passthrough (+ global -a/-r) (PASSTHROUGH.md)
ws doctor                     status + diagnostics (DIAGNOSTICS.md)
ws dolt up|down|provision|logs|ps|sql   optional Dolt server (DOLT.md)
ws backup [dest]              JSONL export mirror
ws config init|path           config management (CONFIGURATION.md)
```

## Exit codes

Single-target runs propagate the child command's exit code. Multiplexed (`-a`) runs continue
past failures, print an `N ok / M failed / K skipped` summary, and exit non-zero if any
target failed. `ws labels validate` exits non-zero on violations unless `--advisory`.
