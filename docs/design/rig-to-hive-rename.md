# Rename ADR — `rig` → `hive`

> Status: **decided (design); implementation deferred to a follow-up epic.** This records the
> decision to rename Beadhive's user-facing term for a managed repo from **rig** to **hive**,
> the complete inventory of surfaces that say "rig", and the migration/deprecation plan.
> **The implementation molecule is NOT filed yet — file it via `/bh:replan` referencing this
> ADR and its bead (bh-7yhl.3).** No product code changes ride this document.

## Decision

The user-facing noun for a managed repo changes from **rig** to **hive**:

| Axis | Today | Decided |
|---|---|---|
| Concept noun | rig (a repo under `bh` management) | **hive** |
| CLI command tree | `bh rig <verb>` | **`bh hive <verb>`** (deprecated `bh rig` alias for one minor) |
| Routing flags | `-r/--rig <id>` on passthrough/work verbs | `--hive` (keep `-r` short flag; alias `--rig`) |
| MCP tools | `rig_add`, `rig_onboard`, `rigs_available`, `rigs_status` | `hive_*` / `hives_*` (old names dual-registered, deprecated) |
| MCP resources | `beadhive://rigs/{available,status,survey}` | `beadhive://hives/…` (old URIs aliased) |

Rationale: "hive" is the brand-native term — Beadhive manages hives of beads; "rig" was
inherited factory vocabulary that predates the Beadhive identity (see
[limn-naming-strategy-adr.md](limn-naming-strategy-adr.md)) and now collides with nothing in
the product language. The rename is user-surface-first: **internal identifiers only follow
where cheap**, per the migration plan below.

## Inventory — every surface that says "rig"

Measured on this tree (rev of bh-7yhl); counts are `\brig\b` matches to scope the sweep, not
an edit checklist.

### CLI (breaking surface — the reason this is its own epic)

- Command tree `bh rig …` with subcommands: `init add rm retire onboard ls migrate ready
  context survey classify prefix enable disable` (cli.py, 87 mentions incl. help text).
- Routing flags `-r/--rig` on the `bd`/`git` passthroughs and `bh work --rig` (cross-rig).
- Help/error prose across verbs ("not an AGF rig", "rig 'X' ready", survey/doctor output).

### MCP plane

- Tools: `rig_add`, `rig_onboard`, `rigs_available`, `rigs_status` (+ `_resource` twins),
  `rigs_survey_resource` (mcp.py, 43 mentions).
- Resource URIs: `beadhive://rigs/available|status|survey`.

### Python internals (rename only where cheap)

- Modules: `rig.py`, `rig_ready.py`, `rig_migrate.py`.
- Registry vocabulary: `rig_dir_for`, `rig_match`, "rig id" triplets (registry.py 25,
  work.py 91, worktree.py 90, onboard.py 66, config.py 42 — mostly internal identifiers,
  comments, and log strings).
- Config accessor strings: per-rig config regions, `work.*` docs; the literal config key
  surface is small (`"rig"` appears once as a key string in config.py).

### Persisted / cross-session state (compat-critical)

- Head Office registry `~/.ws/config.yaml`: `managed_repos` entry SCHEMA does **not** say
  rig (keys are provider/org/repo/prefix/kind/upstream/furnish) — no data migration needed;
  only surrounding docs/comments say "rig".
- Beads labels: `component:rig` label values on historical beads — leave history untouched;
  add `component:hive` to the closed set going forward.
- OTEL attributes / telemetry: 22 rig mentions in otel.py (span/metric attribute names) —
  renaming breaks dashboards; ship both attributes for one minor.

### Docs, skills, plugins (sweep surface)

- Docs: 379 mentions across ~19 files (RIGS.md is the largest at 71 and should be renamed
  HIVES.md with a redirect stub).
- Bundled assets: `AGF-hint.md` ("onboarded as a `bh` rig"), statusline seat/rig display.
- **Cross-repo**: the bh Claude plugin (beadhive/claude-plugin — skills `bh:setup`,
  `bh:developer`, `bh:dispatcher`, hooks calling `bh rig …`), the workspace-repo plugin
  skills, and `docs/AGF.md`/`PRIME`-successor steering text. These need coordinated beads in
  their own repos (cross-rig planning caveat applies).
- Tests: 82 test files mention rig (fixture names, CLI invocations, output assertions).

## Migration / deprecation plan

Target: **0.4.0** (the rename is breaking at the CLI/MCP surface; 0.3.0 is already taken by
the zero-footprint onboarding release this epic lands).

1. **0.4.0 — dual surface.** Introduce `bh hive` as the canonical tree; keep `bh rig` as a
   full alias that prints a one-line deprecation warning to stderr. MCP: register `hive_*`
   tools/URIs alongside the old names (old ones marked deprecated in their docstrings). OTEL:
   emit both attribute names. Docs/skills sweep lands here; `component:hive` added to the
   label dimension. Internal renames (modules, helpers) happen here too — they're invisible
   to users and cheapest done with the sweep.
2. **Alias window.** One minor release minimum. `bh doctor` gains a note when the deprecated
   alias is exercised (telemetry counts alias usage via the existing passthrough counters).
3. **0.5.0 — removal.** Drop the `bh rig` alias, old MCP names, and dual OTEL attributes.
4. **Out of scope for the rename:** the registry schema (already term-neutral), bead history
   relabeling, and the `refs/dolt/data` storage layout.

## Follow-up filing instruction

The implementation is a **follow-up epic**: run `/bh:replan` with this ADR + bead bh-7yhl.3
as the trigger evidence and decompose along the inventory above (suggested slices: CLI tree +
alias · MCP dual-registration · internals/modules · docs/skills sweep · cross-repo plugin
beads · telemetry dual-emit). Do not start the rename piecemeal outside that molecule.
