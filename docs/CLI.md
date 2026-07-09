# CLI

The command surface (module: `cli.py`, a Typer app). `bh` (or any group) with no args prints
help.

## Help panels

`bh --help` groups commands by purpose:

| Panel | Commands | Theme |
|---|---|---|
| **Workspace & rigs** | `sync`, `hq`, `rig`, `labels` | operate on rigs in the workspace |
| **Passthrough — honor `-a/--all` and `-r/--rig`** | `bd`, `git` | forward to a tool, per rig |
| **Admin (bh itself)** | `doctor`, `backup`, `dolt`, `config` | manage `bh`/its infra |

Panels are set via Typer's `rich_help_panel`; order is Workspace → Passthrough → Admin.

## Global routing flags

`-a/--all` and `-r/--rig <id>` are **root** options, placed **before** the subcommand:

```sh
bh -a git status              # run in every registered rig
bh -r ag-infra bd dolt push   # run in one rig
```

They're captured by the root callback into `ctx.obj` and consumed only by the **passthrough**
commands (`bd`, `git`). Guards (all enforced):

- using them on any other command (`bh -a doctor`) → error;
- using them with `bh git workspace …` (which runs centrally) → error;
- placing them *after* the subcommand (`bh git -a …`) → a hint to move them before.

Full semantics: [PASSTHROUGH](PASSTHROUGH.md). Routing requires the git-workspace
integration enabled ([INTEGRATIONS.md](INTEGRATIONS.md)).

## Passthrough command pattern

`bd`, `git`, and the `hq bd` / `hq intake` commands are passthroughs: Typer `context_settings`
use `allow_extra_args + ignore_unknown_options` and `add_help_option=False`, so all args after
the subcommand are forwarded verbatim to the underlying tool. (`bh bd --help` shows beads' help;
`bh git workspace --help` is rerouted to the `git-workspace` binary.) `bh hub` is a deprecated
alias for `bh hq` and prints a deprecation note when used.

## Full surface

```text
bh sync                       build/refresh the HQ aggregate (HUB.md)
bh hq init                    stand up the Factory HQ store (HUB.md)
bh hq bd <bd cmd>             query the HQ aggregate (cross-rig view) (HUB.md)
bh hq intake [flags]          director's fleet-wide untriaged-intake inbox (HUB.md)
bh rig init [opts]            onboard the current repo (RIGS.md)
bh rig classify|prefix …      registry helpers (RIGS.md)
bh labels validate|sync|report|allowed|docs   registry ops (LABELS.md)
bh worktree add|list|path|rm|prune   bh-managed worktrees (WORKTREES.md)
bh work brief|claim|check|submit|resume|abandon   bead lifecycle driver (WORK.md)
bh bd <args> | bh git <args>  passthrough (+ global -a/-r) (PASSTHROUGH.md)
bh doctor                     status + diagnostics (DIAGNOSTICS.md)
bh dolt up|down|provision|logs|ps|sql   optional Dolt server (DOLT.md)
bh backup [dest]              JSONL export mirror
bh config init|path           config management (CONFIGURATION.md)
```

## Exit codes

Single-target runs propagate the child command's exit code. Multiplexed (`-a`) runs continue
past failures, print an `N ok / M failed / K skipped` summary, and exit non-zero if any
target failed. `bh labels validate` exits non-zero on violations unless `--advisory`.
