# CLI

The command surface (module: `cli.py`, a Typer app extended by `work.py` / `plan.py`). `bh` (or
any group) with no args prints help. The naming/flag conventions this surface follows are the
decided ADR: [`design/cli-mcp-naming-conventions-adr.md`](design/cli-mcp-naming-conventions-adr.md).

## Help panels

`bh --help` groups commands into **6 panels** reflecting the plane model, ordered by lifecycle:

| Panel | Groups |
|---|---|
| **Planning plane** | `plan` |
| **Integration plane** | `work`, `worktree` (alias `wt`) |
| **Hive** | `hive`, `label` |
| **Fleet / HQ** | `hq`, `host`, `sync`, `role`, `report`, `report-target`, `escalate` |
| **Admin / infra** | `doctor`, `backup`, `setup`, `config`, `mcp`, `plugin` |
| **Passthrough** | `bd`, `git` |

Panels are set via Typer's `rich_help_panel`. `otel` and `dolt` are `hidden` (deprecation-track):
they still run (`bh otel …`, `bh dolt …`) but appear in no panel and are omitted from `--help`.
`hub` (→ `hq`) and `statusline` are likewise hidden.

## Global routing flags

`-a/--all` and `--hive <id>` are **root** options, placed **before** the subcommand. `--hive` is
long-only (no short — `-h` is help; the old `-r` short is dropped):

```sh
bh -a git status              # run in every registered hive
bh --hive ag-infra bd dolt push   # run in one hive
```

They're captured by the root callback into `ctx.obj` and consumed only by the **passthrough**
commands (`bd`, `git`). Guards (all enforced):

- using them on any other command (`bh -a doctor`) → error;
- using them with `bh git workspace …` (which runs centrally) → error;
- placing them *after* the subcommand (`bh git -a …`) → a hint to move them before.

`-a/--all` and `--hive` are mutually exclusive on any invocation: `--hive` is "which one",
`--all` is "all of them at once" (passthrough / aggregate-read only). Full semantics:
[PASSTHROUGH](PASSTHROUGH.md). Routing requires the git-workspace integration enabled
([INTEGRATIONS.md](INTEGRATIONS.md)).

## Per-command `--hive` and the default hive

Hive-scoped commands (`work *`, `plan *`, `worktree *`, and the hive-scoped `hive` verbs) take
their **own** `--hive <id>` option (also long-only) to target one hive. You rarely need it:
inside any managed hive — a real clone under `$GIT_WORKSPACE` **or** a `bh`-managed worktree — the
hive is resolved from cwd by the shared `registry.current_hive` resolver (identity triplet →
shadow-root reverse-map → synthesized triplet). Pass `--hive` only when cwd is **outside** the
workspace or you're targeting a **different** hive; otherwise the single failure mode is
"cwd belongs to no hive".

## Passthrough command pattern

`bd`, `git`, and the `hub bd` / `hub intake` / `hq bd` / `hq intake` commands are
passthroughs: Typer `context_settings`
use `allow_extra_args + ignore_unknown_options` and `add_help_option=False`, so all args after
the subcommand are forwarded verbatim to the underlying tool. (`bh bd --help` shows beads' help;
`bh git workspace --help` is rerouted to the `git-workspace` binary.) `bh hub` and `bh hq` are
NOT aliases — they read two different stores: the derived cross-hive aggregate and HQ's own
authoritative beads respectively. See [HQ — Hub vs HQ](HQ.md#hub-vs-hq).

## Full surface

```text
bh plan file|adopt|check|verify|approve|show|status|repair   planning plane (PLANNING-PLANE.md)
bh work brief|ready|issue|list|intake|accept|reject|reroute|promote   bead reads + triage (WORK.md)
bh work assign|claim|next|schedule|check|submit|approve|start|finish|merge|resume|abandon|show|review|refine
                              bead lifecycle driver (WORK.md); next is the atomic
                              pick-claim-provision entry point for external schedulers
bh worktree add|list|path|init|rm|status|prune   bh-managed worktrees, alias wt (WORKTREES.md)
bh hive init|add|rm|retire|onboard|list|status|migrate|ready|survey|classify|prefix|enable|disable
                              onboard/inspect hives (HIVES.md); archive list|prune
bh label validate|sync|report|allowed|docs   registry ops (LABELS.md)
bh hq init|bd|intake          Factory HQ store + cross-hive views (HUB.md)
bh host init|identity|list|show|remove|adopt|release|packup
                              this host's manifest + the host lease in Factory HQ
                              (multi-host-model-adr.md); list --lease-hive adds lease state;
                              list also flags stale/likely-dead manifests; remove drops one
                              (gated on live leases + self-removal); identity fills this
                              host's git name/email/signing-key GAPS from bh's own config
                              and never overwrites an existing value (WORK.md)
bh sync                       build/refresh the hub, the cross-hive aggregate (HUB.md)
bh role [name]                launch claude in a seat role
bh report <hive> <title>      file intake into a hive we own (REPORT-CHANNEL.md)
bh report-target              emit bh's own report-channel descriptor
bh escalate <title>           fire-and-forget escalation to HQ
bh bd <args> | bh git <args>  passthrough (+ root -a/--all, --hive) (PASSTHROUGH.md)
bh doctor [--json]            status + diagnostics (DIAGNOSTICS.md); --json emits the same
                              payload the beadhive://doctor MCP resource serves
bh alerts show [--json]       normalized active alerts for agent/operator steering (ALERTS.md)
bh backup export|usage|reclaim   JSONL export mirror; disk-usage + retention across all three
                              backup roots (backup-retention-boundary-adr.md)
bh setup check|show           post-install dependency gate (`check --json` for the
                              schema-versioned per-tool state + remedies)
bh setup guide [--wizard]     export the bundled setup Guide to ~/.beadhive/guides/setup/
                              and hand it to your harness — or walk it here
bh setup toolchain            install the pinned toolchain (bd/dolt/gh/git-workspace) via nix
bh config path|show|init|get|set|unset   config management (CONFIGURATION.md)
bh mcp serve|install          FastMCP server (MCP.md)
bh plugin git-workspace|orca|observaloop …   external-tool integrations (INTEGRATIONS.md)

hidden (still runnable, off all panels): bh otel … · bh dolt … · bh hub · bh statusline
```

### Provider-qualified headless roles

`bh role <seat> --task … --harness claude|codex` treats the harness as an explicit provider
request. A BAML launch resolves only `bh-<seat>-<provider>` and its adjacent
`*.manifest.json`; it validates the BAML-owned artifact digest, bake/provenance, runnable state,
provider-native authority, framing, and live-event mechanism before resolving or claiming a bead
worktree. The provider-unspecified `bh-<seat>` compatibility alias is never considered for an
explicit provider request.

`--baml-required` makes this selection mandatory. A missing/unqualified/mismatched artifact or
unsupported provider is a refusal; direct Hitch is not a fallback. Without the flag, a missing
qualified artifact may use a matching direct Hitch profile, which is reported as a distinct
driver. A present but invalid qualified artifact is always refused rather than bypassed.

This is intentionally stricter than the in-progress producer draft: an artifact manifest that
still labels the provider executable as the execution driver, omits runnable/native-authority or
profile/packs-digest evidence, or reports no live-event mechanism is not yet an integration-ready
BAML artifact. Beadhive refuses it until the sibling baml-harness contract supplies those facts;
it does not weaken validation or silently relabel that run.

For an accepted BAML launch, `bh role` mints one outer attempt id and a separate provider
continuation id. It propagates the run-journal context through the `BH_RUN_*` environment and the
packed-seat correlation arguments without placing task text or credential values in either.

Canonical verb vocabulary is reused everywhere (`add` / `rm` / `list` / `show` / `status` /
`init`); "many" is a `list` verb (+ mode flags) or `--all`, never a pluralized command name.
`--json` (bound to `as_json`) is the machine-output flag on every command that has one, and
`--force` carries a `-f` short wherever it exists.

## Exit codes

Single-target runs propagate the child command's exit code. Multiplexed (`-a`) runs continue
past failures, print an `N ok / M failed / K skipped` summary, and exit non-zero if any
target failed. `bh label validate` exits non-zero on violations unless `--advisory`.
