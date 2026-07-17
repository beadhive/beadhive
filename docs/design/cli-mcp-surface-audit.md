# CLI + MCP surface audit — `bh` (Beadhive)

> Status: **reference.** A canonical snapshot of `bh`'s two hand-authored surfaces (the Typer CLI
> tree and the FastMCP server) and a catalog of every naming/flag/param inconsistency found
> across them. This is the *problem statement*; the *decision* it drove is
> [`cli-mcp-naming-conventions-adr.md`](cli-mcp-naming-conventions-adr.md), which ratifies the
> target end-state. Measured against source (`src/beadhive/`), which is already migrated to
> `hive`; the installed binary may lag.

## Part 1 — Visualization

### 1a. Dual-exposure architecture

```text
        ┌─────────────────────────┐        ┌─────────────────────────┐
        │   CLI surface (Typer)   │        │  MCP surface (FastMCP)  │
        │  cli.py / work.py /     │        │        mcp.py           │
        │  plan.py — @app.command │        │  @tool (name=__name__)  │
        │  @otel.trace_verb       │        │  @resource("beadhive://…")│
        └───────────┬─────────────┘        └───────────┬─────────────┘
                    │  both call the SAME                │
                    ▼                                     ▼
        ┌───────────────────────────────────────────────────────────┐
        │  CORE FUNCTION LAYER (single source of truth for BEHAVIOR) │
        │  hive.add · hive.onboard · hive.available · plan.file_     │
        │  molecule · molecule.validate_spec · work.refine_branch ·  │
        │  config.set_value · bd.create · triage.* · survey.* ·      │
        │  worktree.status_rows · doctor.doctor_payload              │
        └───────────────────────────────────────────────────────────┘
     x  NO shared registry / codegen ties the two NAME sets together — drift is structural.
```

### 1b. CLI command tree (source truth = `src/beadhive/cli.py`)

> Note: the **installed** `bh` binary may still show `rig`; source is already migrated to
> `hive`. Source is the truth for this audit.

```text
bh  [--all/-a] [--rig/-r <hive>*] [--version/-V]        (*root flag is passthrough-routing)
├─ WORKSPACE panel
│  ├─ role [name]
│  ├─ sync                         --json
│  ├─ report <hive> <title>        --type/-t --as --description/-m
│  ├─ report-target                --json
│  ├─ escalate <title>             --tool --as
│  ├─ hive …                       init·add·rm·retire·onboard·ls·migrate·ready·context(H)·
│  │                               survey·classify·prefix·enable·disable·archive{ls,prune}
│  ├─ hq …                         init·intake(passthru)·bd(passthru)
│  ├─ labels …                     validate·sync·report·allowed·docs        <- lone PLURAL group
│  ├─ worktree (alias wt, hidden)  add·list·path·init·rm·status·prune
│  ├─ work …                       (integration plane, see below)
│  └─ plan …                       (planning plane, see below)
├─ PASSTHROUGH panel
│  ├─ bd …                         (opaque -> bd)
│  └─ git …                        (opaque -> git / git workspace)
└─ ADMIN panel
   ├─ doctor · backup [dest] · setup{check,show} · dolt{up,provision,down,logs,ps,sql}
   ├─ otel{up,down,logs,ps,enable,disable,endpoint <url>} · observaloop{status,down}
   ├─ plugin{git-workspace{groups}, orca{sync,fix-settings}}
   ├─ config{path,show,init,get,set,unset}
   └─ mcp{serve,install}
   hidden: statusline · hub (DEPRECATED->hq)

bh work  (work.py) — brief·ready*·issue*·list*·intake·accept·reject·reroute·promote·assign·
         claim·check·schedule·submit·approve·start·finish·merge·resume·abandon·show·review·refine
         (*read verbs forward trailing flags to bd; start=claim<epic>, finish=merge --molecule)
         shared target flag on ~all: --hive/-r
bh plan  (plan.py) — file·adopt·check·verify·approve·show·status·repair
```

### 1c. MCP tools (source = `mcp.py`; tool name == `fn.__name__`)

| MCP tool (source) | Live plugin (stale) | Params | Core / CLI dual |
|---|---|---|---|
| `plan_check` | `plan_check` | `spec` | `molecule.validate_spec` <- `plan check` |
| `plan_file` | `plan_file` | `spec, hive, dry_run` | `plan.file_molecule` <- `plan file` |
| `work_refine` | `work_refine` | `bead, squash_plan, autosquash, since, hive, dry_run` | `work.refine_branch` <- `work refine` |
| `bd_create` | `bd_create` | `issues[], hive` | `bd.create` (batch) |
| `config_set` | `config_set` | `key, value, type` | `config.set_value` <- `config set` |
| `hive_add` | **`rig_add`** | `provider, org, repo, prefix, kind, upstream` | `hive.add` <- `hive add` |
| `hive_onboard` | **`rig_onboard`** | `provider, org, repo, clone_url, furnish, claude, skills, observaloop` | `hive.onboard` <- `hive onboard` |
| `hives_available` | **`rigs_available`** | *(none)* | `hive.available` <- `hive ls --available` (a **flag**) |
| `hives_status` | **`rigs_status`** | *(none)* | assembled; **no CLI command at all** |

### 1d. MCP resources (URI = hand-typed string)

`beadhive://` · probe/health · config · config/{key} · doctor · **hives/available** ·
**hives/status** · **hives/survey** · labels/validation · worktrees · work/ready · work/intake ·
work/intake/dupes · work/issue/{id} · work/show/{id} · work/schedule/{epic} · **plans** ·
plan/{ref} · hq/intake

Dual-exposed (tool **and** resource, same core call): `hives_available` ↔
`beadhive://hives/available`, `hives_status` ↔ `beadhive://hives/status`.

## Part 2 — Inconsistency catalog

| # | Category | Finding | Where | Sev |
|---|---|---|---|---|
| 1 | Rename residue | Live MCP serves `rig_*`/`rigs_*`; source is `hive_*`/`hives_*` (installed build predates cutover — needs rebuild/republish) | live plugin vs `mcp.py` | HIGH |
| 2 | Rename residue | `ws` leaks: `probe_health` returns `"service":"ws"`; docstrings say `ws mcp serve`, `~/.ws/config.yaml`, `ws work ready --json`; `tests/test_ws.py` | `mcp.py:358`, DESIGN.md, tests | MED |
| 3 | Singular/plural | MCP `hive_add`/`hive_onboard` (sing.) vs `hives_status`/`hives_available` (plur.); resources `hives/*` vs `work/*`,`config`,`doctor`; `plans` vs `plan/{ref}` | `mcp.py` | HIGH |
| 4 | Singular/plural | CLI groups all singular **except `labels`** | `cli.py:60` | MED |
| 5 | Overloaded flag | `-r/--hive` = root passthrough-routing **and** per-command target (same letters, different scope) | `cli.py:_root` vs `work/plan/worktree` | MED |
| 6 | Overloaded flag | `-a/--all` = root cross-hive routing vs `archive prune --all` (all archived repos) | `cli.py:161` vs `949` | LOW |
| 7 | Declaration drift | root declares `("-r","--hive")` short-first; per-command `("--hive","-r")` long-first | `cli.py:164` vs constants | LOW |
| 8 | Declaration drift | boolean style mixes paired `--furnish/--no-furnish` with bare `--force`/`--dry-run`; `-f` short only on some | `hive` vs `worktree`/`work` | LOW |
| 9 | Param drift | `--json` binds to `as_json` in some verbs, `json_out` in others | across groups | LOW |
| 10 | Telemetry gap | `@otel.trace_verb` on every `work`/`plan` verb **except `work refine`** | `work.py:1776` | MED |
| 11 | Non-1:1 map | `hives_available` ↔ CLI flag (not a subcommand); `hives_status` has **no CLI** — no naive `group_verb` rule can hold | `mcp.py` | HIGH |
| 12 | Docs stale | `docs/CLI.md` "Full surface" omits `plan/setup/otel/observaloop/plugin/mcp`, lists wrong `work` verbs; no generated reference | `docs/CLI.md` | MED |

**Constraints on any rename** (guardrails that break in lockstep): `tests/test_mcp.py` asserts
the tool set *exactly*; tool-name + resource-URI string assertions across `test_mcp_*`,
`test_otel_instrument.py`, `test_mcp_tool_span.py` (attr key `bh.mcp.tool`); repo precedent
[`rig-to-hive-rename.md`](rig-to-hive-rename.md) = **hard cutover, no alias window, tests swept
in lockstep**.

## Resolution

Each catalog item is resolved by the alignment beads under epic **bh-2l1m**, per the target
surface ratified in [`cli-mcp-naming-conventions-adr.md`](cli-mcp-naming-conventions-adr.md)
(Parts 5a–5e). The convention lint `tests/test_naming_conventions.py` is the durable done-gate
that keeps the surface from drifting back.
