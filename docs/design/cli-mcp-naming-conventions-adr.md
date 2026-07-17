# CLI + MCP naming conventions ADR — `bh` (Beadhive)

> Status: **decided.** This is the decision record that ratifies the naming, flag, and
> parameter conventions for `bh`'s two hand-authored surfaces — the Typer CLI tree and the
> FastMCP server — and the target end-state they resolve to. It is the convention spine every
> alignment bead applies and the convention lint (`tests/test_naming_conventions.py`) asserts.
> The companion audit that motivated it lives in
> [`cli-mcp-surface-audit.md`](cli-mcp-surface-audit.md). Rename precedent (hard cutover, no
> alias window) is [`rig-to-hive-rename.md`](rig-to-hive-rename.md).

## Context

`bh` exposes its capabilities on **two hand-authored surfaces** over a shared core-function
layer: a **Typer** CLI tree (`bh <group> <verb>`, assembled in `cli.py` and extended by
`work.py` / `plan.py`) and a **FastMCP** server (`mcp.py`) exposing a curated subset as
**tools** and **resources**. Both wrap the same core functions, so behavior has one source of
truth — but the *names* do not: each MCP tool name is literally `fn.__name__`, each resource URI
is a hand-typed string, each CLI verb/flag is a separate Typer declaration. Nothing derives one
from another, so naming/flag/param drift is **structural, not accidental**. This ADR fixes the
rules; the lint keeps them from drifting.

## The 8 conventions (each an enforceable rule)

1. **Singular everywhere for per-entity operations.** No pluralized command / tool / resource
   names. Fan-out across all entities is a **`--all` flag**, never a pluralized name.
2. **Collections via a `list` verb with rendering/status modes.** Fleet views collapse into a
   singular `list` verb (+ mode flags), never a plural name. `hive ls` becomes `hive list`
   (+ `--available`); MCP exposes them as `hive_list` (mode params), not `hives_*`. Resources
   singularize: `beadhive://hive/*`, `beadhive://plan/list` + `plan/{ref}`.
3. **`--json` (bool) is the machine-output convention** — never `--format json` — bound to a
   single canonical parameter name **`as_json`** everywhere.
4. **`--hive` is declared long-only, long-first, with no short flag.** `-h` is help; the old
   `-r` short is dropped as unintuitive. The root passthrough-routing flag and the per-command
   target flag are documented as distinct scopes (see the applicability matrix, 5d-i).
5. **Every `work`/`plan` verb is `@otel.trace_verb`-wrapped** — no exceptions.
6. **MCP tool name = `group_verb`** derived from the CLI verb for the 1:1 cases; a documented
   exceptions list carries the genuinely non-1:1 tools.
7. **Resource URI scheme** = `beadhive://<group-singular>/<view>[/{param}]`.
8. **No `ws` / `rig` residue** anywhere in the surface — names, docstrings, probe payloads, and
   test filenames alike.

## 5a. Panel scheme (6 panels, reflecting the plane model)

CLI groups are assigned to **6 rich-help panels**, ordered by lifecycle. Every visible group
carries a `rich_help_panel`; there is no un-paneled visible group.

| Panel | Groups |
|---|---|
| **Planning plane** | `plan` |
| **Integration plane** | `work`, `worktree` (alias `wt`) |
| **Hive** | `hive`, `label` *(was `labels`)* |
| **Fleet / HQ** | `hq`, `sync`, `role`, `report`, `report-target`, `escalate` |
| **Admin / infra** | `doctor`, `backup`, `setup`, `config`, `mcp`, `plugin` |
| **Passthrough** | `bd`, `git` |

**Regroup decisions:** `observaloop` is registered under **`plugin`** (a telemetry-routing
integration; it joins `git-workspace`/`orca` in the plugin registry). **`otel` and `dolt` drop
off the exposed surface** — marked `hidden=True` (deprecation-track), like `hub`/`statusline`;
they keep working for anyone who knows the path but no longer clutter the panels.

## 5b. Shared verb vocabulary (the same verb means the same thing everywhere)

Canonical CRUD/lifecycle verbs, reused across every group that needs them:

| Verb | Meaning | Applied to |
|---|---|---|
| `add` | register/create one entity | `hive add`, `worktree add` |
| `rm` | remove/unregister one entity | `hive rm`, `worktree rm` (never `remove`/`delete`) |
| `list` | show many (render/filter modes via flags) | `hive list` (was `hive ls`), `archive list` (was `archive ls`), `worktree list`, `work list` |
| `show` | detail one entity | `work show`, `plan show`, `config show` |
| `status` | state view (one or, with `--all`, fleet) | `hive status` (new — absorbs fleet health), `worktree status`, `observaloop status` |
| `init` | scaffold | `hive init`, `worktree init`, `config init` |

Rule: **no pluralized command names** — "many" is `list` (+ modes) or `--all`.

## 5c. Target CLI tree (renames marked)

```text
bh  [--all/-a] [--hive <hive>] [--version/-V]     (root flags: passthrough routing; --hive has NO short)
├─ Planning plane
│  └─ plan     file·adopt·check·verify·approve·show·status·repair
├─ Integration plane
│  ├─ work     brief·ready·issue·list·intake·accept·reject·reroute·promote·assign·claim·
│  │           check·schedule·submit·approve·start·finish·merge·resume·abandon·show·review·refine
│  └─ worktree add·list·path·init·rm·status·prune            (alias wt)
├─ Hive
│  ├─ hive     init·add·rm·retire·onboard·list*·status†·migrate·ready·survey·classify·
│  │           prefix·enable·disable·archive{list,prune}
│  │             *  hive ls        -> hive list [--available]
│  │             †  NEW hive status  (fleet health: collisions/violations; --hive narrows)
│  └─ label    validate·sync·report·allowed·docs             (was: labels)
├─ Fleet / HQ
│  └─ hq·sync·role·report·report-target·escalate
├─ Admin / infra
│  └─ doctor·backup·setup·config·mcp
│     └─ plugin  git-workspace{groups}·orca{sync,fix-settings}·observaloop{status,down}  <- moved in
└─ Passthrough
   └─ bd·git

hidden (deprecation-track, off all panels): statusline · hub(->hq) · otel · dolt
```

## 5d. Canonical flag / parameter table (applies to every command)

| Flag | Short | Type | Canonical param | Rule |
|---|---|---|---|---|
| `--hive` | *(none)* | str | `hive` | target **one** hive (default: cwd's). **No short flag** (`-h`=help; `-r` dropped). Only on **hive-scoped** commands — see 5d-i |
| `--all` | `-a` | bool | `all` | broadcast across **every** hive. **Passthrough-only** (+ explicit aggregate reads) — see 5d-i. Never on per-entity mutations |
| `--json` | *(none)* | bool | **`as_json`** | machine output; one param name everywhere (never `--format json`) |
| `--dry-run` | *(none)* | bool | `dry_run` | preview, zero mutation |
| `--force` | `-f` | bool | `force` | bypass guards; `-f` short **everywhere** force exists |
| `--yes` | `-y` | bool | `yes` | non-interactive confirm |
| `--as` | *(none)* | str | `as_` | actor/seat identity |
| `--type` | `-t` | str | `type_` | bd-style |
| `--priority` | `-p` | str | `priority` | bd-style |
| feature toggle | *(none)* | bool | — | bare `--flag`; add paired `--no-flag` **only** for default-on toggles (e.g. `--furnish/--no-furnish`) |

Specific rules this table forces: `--hive` has **no `-r` short** (one edit to the `_HIVE`
constant + the inline options); **`archive prune --all` becomes `--all-ages`** (frees `-a/--all`
for fleet broadcast); `--json` param unified to `as_json`; `@otel.trace_verb` added to
`work refine`; `-f` short given to `--force` everywhere it exists.

### 5d-i. Where `--hive` and `--all` apply

| Flag | Applies to | Does NOT apply to |
|---|---|---|
| `--hive` | every **hive-scoped** command: all `work *`, `plan *`, `worktree *`; hive-scoped `hive` verbs (`ready`, `status`, `migrate`); passthrough `bd`/`git` (routing) | fleet/global commands (`doctor`, `sync`, `config`, `setup`, `mcp`, `backup`, `role`, `hq`); `hive add/rm/onboard/retire` (hive is a positional **arg** there) |
| `--all` | **passthrough** `bd`/`git` (the original design); explicit **aggregate reads** only (which mostly already have `bh hq …` equivalents) | every per-entity **mutation** (`work submit/merge/approve/assign/claim/accept/reject`, `plan file/approve`, `hive add/rm`, …) — broadcasting these is incoherent |

Rule of thumb: **`--hive` = "which one" (broad); `--all` = "all of them at once" (passthrough /
aggregate-read only).** They are mutually exclusive on any given invocation.

### 5d-ii. Default hive resolution (cwd-aware) — so `--hive` is rarely needed

Target: **inside any managed hive you never type `--hive`.** One shared resolver,
`registry.current_hive(cfg)`, implements the two-way cwd resolution:

1. **`identity.workspace_identity`** — the `$GIT_WORKSPACE` git-remote triplet for a real hive
   clone;
2. **shadow-root reverse-map** (`_entry_for_path`) — for an agent inside an OS-temp managed
   worktree, whose path is not under `$GIT_WORKSPACE`;
3. **synthesize a minimal triplet** — when the resolved repo isn't registered.

Every hive-scoped default (`hive == ""`) routes through it: `work`, `plan`, and `worktree`
alike. `--hive` is then required only when cwd is **outside** the workspace or you target a
**different** hive; the resolver's error path ("cwd belongs to no hive") is the single,
consistent failure mode. This is a DRY win — one resolver instead of the two divergent ones
(`worktree._resolve_entry` vs `registry.hive_dir_for` returning a bare `Path.cwd()`).

## 5e. Derived MCP surface (tool = `group_verb`; URI = `beadhive://<group>/<view>`)

| CLI | MCP tool | Resource | Change |
|---|---|---|---|
| `hive add` | `hive_add` | — | build serves `hive_add`, not `rig_add` |
| `hive onboard` | `hive_onboard` | — | build serves `hive_onboard`, not `rig_onboard` |
| `hive list --available` | `hive_list` | `beadhive://hive/list` | **`hives_available` -> `hive_list`**; `hives/available` -> `hive/list` |
| `hive status` | `hive_status` | `beadhive://hive/status` | **`hives_status` -> `hive_status`** (now backed by a real `bh hive status` CLI verb) |
| `hive survey` | — | `beadhive://hive/survey` | `hives/survey` -> `hive/survey` |
| `plan …` | `plan_check`·`plan_file` | `beadhive://plan/list`, `plan/{ref}` | **`plans` -> `plan/list`** |
| (unchanged) | `work_refine`·`bd_create`·`config_set` | `work/*`, `config`, `doctor`, `label/validation` (was `labels/validation`), `worktree/list` (was `worktrees`), `hq/intake` | singularize residual plural URIs |

**Documented non-1:1 exceptions** (can't derive from a `group_verb` rule; listed explicitly):

- `probe/health` — a bare health probe resource, no CLI verb; its payload reports
  `"service":"bh"`.
- `bd_create` — maps to the `bd` passthrough, not a native `bh` verb.

Every other tool maps 1:1 to a real CLI verb (`hive_status` gains `bh hive status`, closing the
last gap where a tool lacked a backing CLI command).

## Consequences

- The alignment beads (`mcp-names`, `ws-residue`, `flags`, `resolver`, `regroup`, `docs`) apply
  5a–5e; the `lint` bead asserts them mechanically so the surface cannot silently drift again.
- Rename posture follows [`rig-to-hive-rename.md`](rig-to-hive-rename.md): **hard cutover, no
  alias window** — tests are swept in lockstep with each rename, no compatibility shims left
  behind.
- A future shared name-registry (spike epic `bh-ykyi`) could derive all three name sets from one
  declaration; until its GO/NO-GO verdict, this ADR + the lint are the durable anti-drift guard.
