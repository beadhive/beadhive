# MCP name single-sourcing ADR — `bh` (Beadhive)

> Status: **decided.** This records the decision **not** to single-source `bh`'s MCP name/tool
> surface from a generated mechanism, after two spikes rejected the two candidate approaches. The
> durable anti-drift guard is the convention lint (`tests/test_naming_conventions.py`, epic
> bh-2l1m). Companion documents: the conventions ADR
> [`cli-mcp-naming-conventions-adr.md`](cli-mcp-naming-conventions-adr.md), the surface audit
> [`cli-mcp-surface-audit.md`](cli-mcp-surface-audit.md), and the two spike records
> [`../spikes/bh-ykyi.1-name-registry.md`](../spikes/bh-ykyi.1-name-registry.md) and
> [`../spikes/bh-iads.1-cli-as-source.md`](../spikes/bh-iads.1-cli-as-source.md).

## Context

`bh` exposes two hand-authored surfaces over a shared core-function layer: a Typer CLI tree and a
FastMCP server (9 curated tools + 18 resources in `mcp.py`). Behavior has one source of truth (the
core functions); **names do not** — each MCP tool name is literally `fn.__name__`, each resource
URI is a hand-typed string. The conventions ADR fixed the *rules* and the alignment epic (bh-2l1m)
applied them; this ADR settles the separate question the audit raised: should the names be
**single-sourced by a mechanism** so drift becomes structurally impossible, rather than merely
asserted by a lint? Two mechanisms were spiked; both were rejected.

## Decision

**Keep the MCP tool/resource names hand-authored and curated, guarded by the convention lint.**
Do **not** adopt a codegen registry or CLI-derived tool generation. The lint
(`tests/test_naming_conventions.py`) asserts the CLI↔tool correspondence for the derivable cases
and carries the genuine non-1:1 cases as a documented exceptions allowlist — it delivers the
drift-prevention value at the cost of one test file, with no framework swap or refactor.

## Options considered and rejected

### 1. Bespoke shared name registry — NO-GO (spike bh-ykyi.1)

One declaration driving CLI verb + MCP tool name + resource URI. Measured against the real
surface: ~52% clean-derive on the current surface, ~78% even against the fully-renamed target,
with an **irreducible ~22%** needing explicit per-entry overrides (flag-backed views,
passthrough-backed tools, resource-only projections, verb-collapsed reads). The override field
*is* a second hand-authored declaration co-located — the registry **relocates** drift rather than
removing it, at a cost the lint undercuts.

### 2. CLI-as-source derivation — NO-GO (spike bh-iads.1)

Derive the MCP tool surface directly from the Typer/Click command tree.

- **`click-mcp`** — does not bind to bh's app at all (Typer vendors its own Click fork lacking the
  `to_info_dict` the scanner calls). A naming-rule walker derived **109 tools vs the curated 9**
  (12.1× blast radius: every passthrough, destructive verb, and hidden group), and it has no
  usable allowlist and no resource concept.
- **`pycli-mcp`** (Ofek Lev; actively maintained) — a materially better candidate found during the
  spike: it **binds to Typer**, **respects `hidden=True`** (86 tools, correctly skipping
  `dolt_*`/`otel_*`/`wt`/`hub`/`statusline`/`hive_context`), and ships a **real `include`/`exclude`
  regex filter**. But it still fails bh's bar: it derives `plan_check`'s `spec` as a
  **`{"type":"string"}`** file-path arg, not the **`{"type":"object"}`** structured-dict contract
  the curated `plan_check(spec: dict)` was hand-built for; it needs a rename layer
  (`bh.hive.add` dot-notation ≠ `hive_add`); it runs tools as a **subprocess** returning captured
  stdout (no `ctx: Context`, no `resources/updated` notify); it is **HTTP/Starlette** transport,
  not bh's stdio FastMCP; it covers **0 of 18 resources**; and it needs a `name="bh"` workaround
  for a real path-truncation bug on bh's unnamed root Typer app.

No `FastMCP.from_click`/`from_typer` exists; FastMCP's own `from_fastapi`/`from_openapi` need an
HTTP/OpenAPI source `bh` does not have. Net: no framework path gets bh a working, curated,
structured-I/O-preserving CLI-as-source surface today.

## Consequences

- **`tests/test_naming_conventions.py` is the durable anti-drift guard.** Its exceptions allowlist
  is the canonical documentation of the genuine non-1:1 surface.
- **Salvaged win (already in scope):** the dual-exposed tool/resource pairs (`hive_list`,
  `hive_status`) share a body helper so tool and resource cannot diverge in behavior — the one real
  benefit either mechanism offered, obtained without any registry or codegen.
- **Revisit trigger:** reopen only if the surface grows a large tranche of clean 1:1 groups where
  codegen would amortize, **or** if `bh` ever gains an OpenAPI/FastAPI layer that
  `FastMCP.from_openapi` could bind to. Today's 27-handler, ~22%-irreducibly-non-1:1 surface does
  not clear that bar.
