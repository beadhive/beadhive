# Spike `bh-iads.1` — Can the CLI itself be the single source for bh's MCP TOOL surface?

**Bead:** `bh-iads.1` · **Seat:** `dev/cli-src` · **Type:** research-only (no product code)
**Feeds decision on:** whether to adopt CLI-as-source (à la `click-mcp`) derivation for the MCP
**tool** half — vs. keeping the hand-authored `mcp.py` tool set guarded by the convention lint
(`tests/test_naming_conventions.py`, bh-2l1m.9). Companion to the registry NO-GO
[`bh-ykyi.1-name-registry.md`](bh-ykyi.1-name-registry.md).

## Question

`bh` derives none of its MCP **tool** surface from its CLI: `mcp.py` hand-authors 9 curated
`@tool`s whose names are literally `fn.__name__`, and the CLI is a separate Typer tree
(`cli.py`/`work.py`/`plan.py`). Typer is built on Click, and `click-mcp` turns a Click group's
whole command tree into MCP tools with dot-notation names — so in principle the CLI *could* be
the single source for the tool names/schemas.

**GO/NO-GO:** Should bh adopt CLI-as-source derivation for the MCP **tool** half — deriving tool
names + input schemas directly from bh's Typer/Click command tree instead of the curated
`mcp.py` tool authoring?

This is **NOT** asking about the 18 **resource** URIs (click-mcp is tools-only; resources are in
scope only as a coverage-gap question), nor whether the names *should* be consistent (the ADR +
lint already settle that). It asks only whether the tool surface can be *generated from the CLI*
at acceptable cost and fidelity.

## Method

Ran the derivation against bh's **real** app on this branch (aligned surface, `0f743c5`); no
paper estimates. Environment: `uv sync --extra otel` → typer 0.26.8, click 8.4.2, fastmcp 3.4.2;
`uv pip install click-mcp` → click-mcp 0.6.1 (venv-only, not committed to lock/pyproject).

1. **Attach click-mcp to bh's actual app.** `typer.main.get_command(cli.app)` yields the
   underlying Click object; fed it to `click_mcp.scanner.scan_click_command`
   (`scratchpad/proto.py`, throwaway).
2. **Fallback walker.** click-mcp failed to bind (see Evidence #1), so — per the bead's
   sanctioned fallback — wrote a ~35-line command-tree walker (`scratchpad/walk.py`, throwaway)
   applying click-mcp's **exact** naming rule (dot-join the CLI path, `.`/`-` → `_`) to bh's real
   produced Click tree, enumerating the full tool surface CLI-as-source would expose.
3. **Measured the delta** against the curated 9 tools; catalogued the EXTRA surface (passthroughs,
   admin verbs, hidden groups, destructive lifecycle verbs).
4. **Read click-mcp's source** (`scanner.py`, `server.py`, `decorator.py`) for its
   curation/exclude story, its resource story, and its execution model (how a derived tool runs).
5. **Assessed interaction** with bh's structured-I/O tool contract, the 18 resource URIs, and the
   async `resources/updated` / `ctx: Context` / `@otel.trace_verb` machinery in `mcp.py`.

## Evidence

### 1. click-mcp does not bind to bh's Typer app at all — Typer vendors its own Click fork

`scan_click_command(typer.main.get_command(cli.app))` raised immediately:

```text
Typer->Click bridge: typer.core.TyperGroup
AttributeError: 'TyperGroup' object has no attribute 'to_info_dict'
```

Root cause, measured on the live object:

```text
TyperGroup MRO: typer.core.TyperGroup → typer._click.core.Command → abc.ABC → object
issubclass(type(g), click.Group):   False
issubclass(type(g), click.Command): False
click.Command.to_info_dict in __dict__: True   (present on the REAL click)
```

Typer 0.26.8's `TyperGroup` subclasses Typer's **vendored** Click fork
(`typer._click.core.Command`), **not** the installed top-level `click.Group`/`click.Command` —
and that vendored build lacks `to_info_dict`, which `scanner.py:94` calls. click-mcp targets
`click.Group` (`decorator.py` type hints, `scanner.py` internals), so it is structurally
incompatible with a modern Typer app. **click-mcp cannot drive bh's CLI as-is; any adoption
means forking it or hand-maintaining a walker against Typer/Click internals both projects can
change out from under us.**

### 2. The full derived tool surface is 109 tools — 12.1× the curated 9

The fallback walker (click-mcp's own naming rule, bh's real tree) enumerated **109 leaf-command
tools**. It reproduces **8 of the 9** curated tools by name:

```text
reproduced (8/9): config_set, hive_add, hive_list, hive_onboard, hive_status,
                  plan_check, plan_file, work_refine
NOT reproduced (1): bd_create   # maps to the `bd` passthrough, not a native bh verb
```

`bd_create` is unreachable because `bh` owns no `create` verb — it is the passthrough exception
the lint already documents (`TOOL_MAP_EXCEPTIONS = {"bd_create"}`). So CLI-as-source reproduces
the curated names for the 8 tools that *are* backed by a native verb, and cannot express the 1
that isn't.

### 3. The other 101 derived tools are EXTRA — the curated-minimal policy is inverted

Blast-radius ratio: **109 derived vs 9 curated = 12.1× the intended tool surface.** The 101
extra tools include everything the curated set deliberately excludes:

- **Arbitrary passthroughs** exposed as MCP tools: `bd`, `git` — i.e. an MCP client could run
  any `bd`/`git` subcommand (an unbounded, unsafe tool).
- **Destructive lifecycle verbs:** `work_merge`, `work_finish`, `work_abandon`, `hive_rm`,
  `hive_retire`, `backup`.
- **Hidden CLI commands** (Typer `hidden=True`) that click-mcp does **not** skip:
  `hive_context`, `hub`, `statusline` — plus the whole hidden `dolt_*` (6) and `otel_*` (8)
  deprecation-track groups.
- **Alias duplication:** the hidden `wt` alias re-derives all 7 `worktree` verbs a second time
  (`wt_add`, `wt_list`, …), so the same capability appears twice.
- **Admin/infra + read scalars** the curated set intentionally omits: `config_get`/`config_show`/
  `config_path`/`config_init`/`config_unset`, `doctor`, `setup_*`, `plugin_*`, `mcp_serve`,
  `mcp_install`, and the full `work`/`plan`/`label`/`hive` verb sets.

Every one of these is a curated-minimal-policy violation the moment the CLI is the source.

### 4. Curation/filtering: click-mcp has NO usable allowlist — you must re-add the curated list

click-mcp exports only `['click_mcp', '__version__']` — no per-command opt-in/exclude decorator.
`include_all_commands` is all-or-nothing. Its `_should_skip_command` consults
`get_mcp_metadata(name)` for `include: False`, but the setter (`register_mcp_metadata`) is
unexported, is keyed by the **bare** command name (collision-prone across groups), and has no
public decorator. To honor curated-minimal you would therefore have to **fork click-mcp** or
**post-filter the derived list down to exactly the 9 curated names** — which is the *same*
hand-authored allowlist the lint already encodes (`TOOL_MAP_EXCEPTIONS` + the derivation
assertion). CLI-as-source does not remove the curation authoring; it relocates it into a
subtractive allowlist over a 109-tool firehose.

### 5. Resource URIs: zero coverage — a second mechanism is still mandatory

click-mcp's server (`server.py`) registers only `list_tools` + `call_tool` — it is **tools-only**
and has no concept of MCP resources. bh exposes **18** resource URIs
(`beadhive://probe/health`, `config`, `config/{key}`, `doctor`, `hive/{list,status,survey}`,
`label/validation`, `worktree/list`, `work/{ready,intake,intake/dupes,issue/{id},show/{id},
schedule/{epic}}`, `plan/{list,{ref}}`, `hq/intake`). CLI-as-source covers **0/18**; the entire
resource half — the coordinator's most re-read dashboards, template URIs with path params, and
the dual-exposed views — still needs the hand-authored `mcp.py` mechanism. Adopting it would
leave bh maintaining *two* tool-registration paths (derived tools + hand-authored resources)
instead of one unified surface.

### 6. Execution model breaks the structured-I/O contract that justifies the curated tools

click-mcp runs a derived tool by **re-invoking the CLI in-process** and scraping stdout:
`server.py:_run_click_command` calls `self.cli_group.main(args=…, standalone_mode=False)` under
`redirect_stdout`, and derives params from CLI **options/arguments as strings**. But the curated
tools exist *precisely* to avoid that (`mcp.py` docstring: "the ones whose value over the CLI is
structured I/O … so an MCP client never marshals YAML temp files or scrapes CLI strings"):

- `plan_check(spec: dict)` / `plan_file(spec: dict)` / `bd_create(issues: list[dict])` /
  `work_refine(squash_plan: dict)` take **structured JSON** in and return **structured** dicts
  (`{valid, problems}`, `{epic_id, …}`). The CLI verbs read a **YAML file path** / positional
  args and print text. A derived `plan_check` would have no usable `spec` param and would return
  scraped stdout — losing the whole reason those four tools were curated.

### 7. async notify / ctx / otel: the derived path bypasses bh's MCP machinery

- **`resources/updated` notifications:** bh's mutating tools are `async` and take `ctx: Context`
  to emit `ResourceUpdatedNotification` after a state change (`_notify_updated`), so subscribed
  clients re-read invalidated resources. click-mcp's execution is sync, has no `Context`, and no
  notification concept — every MCP-driven mutation would go silent.
- **`ctx: Context` params:** curated tools thread `ctx` for notifies; a CLI-derived schema has no
  `ctx` and no way to introduce one.
- **`@otel.trace_verb`:** because a derived tool *runs the CLI verb*, the verb's
  `@otel.trace_verb` span **would** fire — but the MCP-layer envelope (`_measured_tool`'s
  `gen_ai.execute_tool` span + `record_mcp_invocation` counter/latency, `bh.mcp.tool` attrs)
  is bypassed. You'd get CLI-verb telemetry, not MCP-tool telemetry, plus the `ToolError`
  contract collapses (server.py maps every failure to a generic `ValueError`, and CLI bodies that
  `typer.Exit`/`sys.exit` surface as opaque errors).

### 8. Weighed against the already-landed lint

The convention lint (`tests/test_naming_conventions.py`, bh-2l1m.9) already asserts
`tool_name == derive(group, verb)` for the 1:1 cases and carries `bd_create` as a documented
exception — it *proves* the CLI-as-source correspondence at CI time for the 8 derivable tools
**without** importing the 101-tool firehose, the fork, the subtractive allowlist, the resource
second-mechanism, or the structured-I/O/async/otel regressions. CLI-as-source would add all of
that to guarantee a correspondence the lint already guarantees for one test file and no refactor.

## Verdict — **NO-GO**

CLI-as-source derivation reproduces only **8 of 9** curated tools (the 9th, `bd_create`, is a
passthrough with no native verb) while dragging in **101 extra tools — 12.1× the curated
surface** — including arbitrary `bd`/`git` passthroughs, destructive lifecycle verbs
(`work_merge`/`work_finish`/`work_abandon`/`hive_rm`), and hidden deprecation-track groups. The
prototype established four hard blockers: (1) click-mcp **does not bind to Typer** — TyperGroup is
built on Typer's vendored `typer._click` fork, not `click.Group`, and lacks `to_info_dict`, so
adoption means forking it or hand-maintaining a walker against volatile internals; (2) it offers
**no usable curation** allowlist, so honoring curated-minimal means re-adding exactly the
hand-authored 9-name list the lint already encodes — over a 109-tool firehose; (3) it is
**tools-only**, covering **0 of 18** resource URIs, so the hand-authored `mcp.py` mechanism
survives regardless, leaving *two* registration paths; and (4) its **CLI-scrape execution model
breaks the structured-I/O contract** (`spec: dict` → YAML-file/stdout) and bypasses bh's async
`resources/updated`, `ctx: Context`, and MCP-layer otel envelope — the very properties for which
those four tools were curated.

**Concrete blocker:** CLI-as-source inverts the curated-minimal policy (12.1× blast radius with
no native filter) and cannot express bh's structured-I/O + async-notify tool contract, while the
already-landed convention lint proves the CLI↔tool correspondence for the derivable cases at a
fraction of the cost.

## Recommendation

**NO-GO on CLI-as-source derivation for the MCP tool half.** Keep the curated `mcp.py` tools as
the source of truth. Next steps:

1. **Rely on the convention lint** (`tests/test_naming_conventions.py`, bh-2l1m.9) as the durable
   anti-drift guard — it already asserts `tool == derive(group, verb)` for the 8 derivable tools
   and documents `bd_create` as the passthrough exception. No new mechanism needed.
2. **Record this NO-GO** alongside the registry NO-GO in the naming-conventions ADR: the CLI is
   the *convention* the tool names follow (lint-enforced), not a *generator* of them — curation,
   structured I/O, async notifies, resources, and telemetry require hand authoring the tool half.
3. **Do not adopt or vendor click-mcp** — it neither binds to Typer nor filters to a curated set,
   and would add a fork + a subtractive allowlist + a second resource mechanism for negative net
   value.
4. **Revisit only if** bh's tool surface grows a large tranche of *new*, structurally 1:1,
   string-in/string-out CLI verbs where CLI scraping would be acceptable and a real allowlist
   lands upstream in click-mcp — today's 9-tool, structured-I/O, resource-heavy surface does not
   clear that bar.
