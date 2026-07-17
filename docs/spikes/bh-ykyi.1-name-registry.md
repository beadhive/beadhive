# Spike `bh-ykyi.1` — Can ONE declaration drive CLI verb + MCP tool name + resource URI?

**Bead:** `bh-ykyi.1` · **Seat:** `dev/registry` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-ykyi.2` (registry GO → replan into a codegen molecule; NO-GO → ADR
plus accept the convention lint `bh-2l1m.9` as the durable anti-drift guard)

> Canonical spike-artifact template. A spike bead (`type: task`, label `tag:spike`) is done
> when this file exists with all five sections filled and **no product code**.

## Question

`bh` exposes its capabilities on **three hand-authored name surfaces** over a shared core:
the **CLI verb string** (`bh <group> <verb>`, Typer), the **MCP tool name** (literally
`fn.__name__` in `mcp.py`), and the **resource URI** (a hand-typed `beadhive://…` string).
Nothing derives one from another, so name drift is structural (audit
`review-the-entire-cli-ancient-pond.md`, Part 1/§1a: *"✗ NO shared registry / codegen ties the
two NAME sets together"*).

**GO/NO-GO:** Can **one declaration** (a shared name registry) drive all three surfaces
cleanly — given the genuinely non-1:1 cases (a tool with no backing CLI verb; a tool backed by
a CLI *flag* not a subcommand; resources with path params; dual-exposed tool+resource views) —
such that a registry is worth building as the structural anti-drift mechanism?

This is **NOT** asking whether the names *should* be made consistent (Molecule A already does
that), nor whether the behavior has one source of truth (it does — both surfaces call the same
core fns). It asks only whether **the names can be single-sourced** at acceptable cost, versus
the already-scoped alternative: a convention **lint** (`bh-2l1m.9`) that asserts the derivation
at CI time instead of generating it.

## Method

Read-only inspection of the real surface, then a paper prototype measured against it:

1. Enumerated every MCP registration in `src/beadhive/mcp.py` — 9 `@tool`s and 18
   `@resource`s (27 handlers total) — and mapped each to its backing CLI verb/flag by reading
   `src/beadhive/cli.py` (`hive ls --available` flag at `cli.py:711-724`; top-level `doctor` at
   `cli.py:1508`; no `hive status` verb exists) and `work.py`/`plan.py` verb lists (audit §1b).
2. Sketched a registry schema `Entry(group, verb, surfaces[], …overrides)` and the three
   derivation rules the target ADR ratifies (audit Part 3 §6/§7, Part 5e): CLI = `<group>
   <verb>`, tool = `<group>_<verb>`, URI = `beadhive://<group>/<view>[/{param}]`.
3. Classified each of the 27 handlers as **clean-derive** (all present names fall out of one
   `(group, verb)` declaration) vs **override** (at least one surface needs an explicit field),
   both on the **current** surface and on the **post-Part-5e** target surface.
4. Weighed the residual overrides against the lint alternative (`bh-2l1m.9`) and estimated
   implementation cost against the actual `cli.py` (1558 lines) / `mcp.py` (900 lines) shape.

## Evidence

### 1. The derivation rule is clean for the 1:1 core — but only ~half the *current* surface

Classifying all 27 MCP handlers against a single `(group, verb)` declaration:

**Tools (9):** clean = `plan_check`, `plan_file`, `work_refine`, `config_set`, `hive_add`,
`hive_onboard` (**6**); override = `bd_create`, `hives_available`, `hives_status` (**3**).

**Resources (18):** clean = `probe/health`, `doctor`, `work/ready`, `work/intake`,
`work/issue/{id}`, `work/show/{id}`, `work/schedule/{epic}`, `hq/intake` (**8**); override =
`config`, `config/{key}`, `hives/available`, `hives/status`, `hives/survey`,
`labels/validation`, `worktrees`, `work/intake/dupes`, `plans`, `plan/{ref}` (**10**).

**Current surface: 14/27 clean-derive (~52%), 13/27 override (~48%).**

But most of the current overrides are **mid-migration plural drift** (`hives_*`, `plans`,
`labels`, `worktrees` — audit catalog #3/#4), not structural. Molecule A's renames
(`mcp-names`, `flags`, `regroup`) singularize them regardless of any registry.

### 2. Against the *post-Part-5e target* surface, clean-derive rises to ~78% — the residual is irreducible

Re-scoring after Part 5e's renames (`hives_available`→`hive_list`, `hives_status`→`hive_status`
with a **new `bh hive status` verb**, `plans`→`plan/list`, `worktrees`→`worktree/list`,
`labels/validation`→`label/validation`, `hives/survey`→`hive/survey`), the plural-drift
overrides collapse into clean derivations. What **remains** as genuine, non-migratable
overrides:

| Residual override | Why one `(group,verb)` can't drive all three | Non-1:1 class |
|---|---|---|
| `hive_list` ↔ CLI `hive list **--available**` | the tool exposes the *available* view; the CLI reaches it via a **flag**, which appears in **none** of the three names — the flag→view collapse is invisible to the declaration | tool backed by a CLI **flag**, not a subcommand |
| `bd_create` | maps to the `bd` **passthrough**, not a native `bh <group> <verb>`; `bh` owns no `create` verb to derive from (audit Part 5e: *"maps to the bd passthrough, not a native bh verb"*) | tool with **no backing `bh` CLI verb** |
| `beadhive://probe/health` | resource-only diagnostic — no tool, no CLI verb; one declaration drives exactly **one** surface, so there is no cross-surface leverage to single-source | **resource-only** singleton |
| `beadhive://work/intake/dupes` | a finer **sub-slice** of `work intake` with no `work intake dupes` subcommand; the extra URI nesting has no CLI/tool partner | **resource-only** sub-view |
| `beadhive://config` vs `config/{key}` | one URI shape splits **two** CLI read verbs — `config show` (no param) vs `config get <key>` (param) — onto param-presence; the URI encodes **neither verb** | one URI ↔ **two CLI verbs**, param-discriminated |
| noun/verb skew: `…/validation` ↔ `label validate`; `…/status`, `…/schedule` (nouns) | resource **views are nouns**, several CLI verbs are **verbs**; `validation ≠ validate` needs an explicit view field even when singularized | **read-projection** naming skew |

Post-fix estimate: **~21/27 clean-derive (~78%), ~6/27 irreducible override (~22%)**. The
override floor does **not** approach zero — it is the genuine non-1:1 structure the bead names.

### 3. Adding `bh hive status` closes ONE non-1:1 gap, not the class

The audit (Part 5e) claims *"none remain that lack a CLI verb — `hive_status` gains `bh hive
status`."* Verified: that is true **for the specific "tool with no CLI verb" sub-case**
(`hives_status`, currently backed by nothing — confirmed no `@hive_app.command("status")` in
`cli.py`). But it does **not** close the non-1:1 *class*: `hive_list`↔`--available` (flag-backed),
`bd_create` (passthrough), `probe/health` and `work/intake/dupes` (resource-only), and the
`config`/`config/{key}` verb-collapse all survive. So the honest answer to the bead's pointed
sub-question is **no — residual non-1:1 cases remain** after `hive status` is added.

### 4. Dual-exposure is the one place a registry genuinely helps — and the lint gets it too

`hives_available`/`hives_status` are each registered **twice** — once as `@tool`, once as
`@resource` with a **byte-duplicated body** (`mcp.py:517-535` and `646-701` are copy-paste
pairs). One declaration with `surfaces=[tool, resource]` would emit both from a single source —
a real DRY win, and the strongest point *for* a registry. But this is **2 of 27** handlers, and
the duplication it removes is body duplication (behavior), which a helper fn already solves
without any registry; the *name* duplication a lint catches directly.

### 5. A registry does not remove the hand-authoring for the hard cases — it relocates it

For every override in §2 the declaration must carry an explicit field
(`cli_flag="--available"`, `cli="bd create"`, `surfaces=[resource]`, `view="validation"`, …).
That override field **is** the second, hand-reconciled declaration — now co-located, but still
authored and still driftable against the real Typer/FastMCP wiring. For ~22% of the surface the
registry's "single source of truth" degrades into "two sources of truth in one file." It
**reduces** drift risk for the clean 78% and **relocates** it for the hard 22%.

### 6. Cost is high and fights the frameworks; the lint gets the same guard for ~1 test file

A registry that only emits **names** guards exactly what the lint (`bh-2l1m.9`) already guards —
no reason to prefer it. A registry that emits the **wiring** must generate Typer commands
(rich per-param types, `rich_help_panel`, paired `--flag/--no-flag`, `@otel.trace_verb`
wrappers — `cli.py` is 1558 lines of exactly this) and FastMCP tool/resource registrations
(typed signatures, `ctx: Context` async notifies, per-URI annotations — `mcp.py` is 900 lines).
Codegen would fight both decorator ergonomics for the ~22% that need overrides anyway. By
contrast the lint is a single `tests/test_naming_conventions.py` asserting `tool ==
derive(cli)` for the 1:1 cases with a **documented exceptions allowlist** — which is precisely
the override list a registry would need regardless. The lint gets ~100% of the drift-prevention
value at a fraction of the cost and requires no invasive refactor of two hand-tuned surfaces.

## Verdict — **NO-GO**

A single-declaration name registry derives cleanly for only ~52% of the **current** surface and,
even against the fully-renamed **Part 5e target**, tops out at ~78% clean with an **irreducible
~22% that require explicit per-entry overrides**. Adding `bh hive status` closes one non-1:1
sub-case but not the class: flag-backed views (`hive_list`↔`--available`), passthrough-backed
tools (`bd_create`), resource-only projections (`probe/health`, `work/intake/dupes`), and
verb-collapsed reads (`config` vs `config/{key}`) all persist. For those the registry does not
eliminate the second hand-authored name — it **relocates** it into an override field, so drift
risk is reduced for the easy half and merely moved for the hard half. That partial benefit does
not justify a codegen/wiring layer over two 900–1558-line decorator-based surfaces, when the
already-scoped convention lint (`bh-2l1m.9`) neutralizes the same drift for the 1:1 cases and
handles the non-1:1 cases with the **same** documented-exceptions allowlist a registry would
need — at the cost of one test file and no refactor.

**Concrete blocker:** the non-1:1 cases are structural and irreducible; single-sourcing them
requires per-entry overrides that reintroduce the very hand-authored second declaration the
registry was meant to eliminate — so the registry relocates rather than removes the drift, at a
cost the lint undercuts.

## Recommendation

**NO-GO on the shared name registry as a codegen/single-source mechanism.** For `bh-ykyi.2`:

1. **Adopt the convention lint `bh-2l1m.9` as the durable anti-drift guard** (Molecule A's
   `lint` bead). Assert `tool_name == derive(group, verb)` and `uri ==
   beadhive://<group>/<view>` for the 1:1 cases; carry the §2 residual as an explicit,
   commented **exceptions allowlist** in the test — the allowlist *is* the documentation of the
   genuine non-1:1 surface.
2. **Record this NO-GO in the naming-conventions ADR** (Part 3 §6/§7): state the derivation
   rule as the convention authors follow by hand, enforced by the lint, with the six residual
   exceptions named. This gives the "single source of truth" *intent* without the codegen cost.
3. **Salvage the one real win cheaply:** collapse the two dual-exposed tool/resource pairs
   (`hive_list`, `hive_status`) onto a shared body helper so the tool and resource can never
   diverge in **behavior** — no registry needed for that; it is a local refactor already in
   Molecule A's scope.
4. **Revisit only if** the surface grows a large tranche of *new* clean 1:1 groups where
   codegen would amortize — today's 27-handler surface, ~22% of it irreducibly non-1:1, does
   not clear that bar.
