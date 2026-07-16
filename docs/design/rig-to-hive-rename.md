# Rename ADR — `rig` → `hive`

> Status: **implemented.** This records the decision to rename Beadhive's user-facing term for a
> managed repo from **rig** to **hive**, the complete inventory of surfaces that said "rig", and
> the migration plan as it was actually executed. The rename landed as a hard cutover in epic
> **bh-41rh**, pre-0.3.0.

## Decision

The user-facing noun for a managed repo changes from **rig** to **hive**:

| Axis | Before | After |
|---|---|---|
| Concept noun | rig (a repo under `bh` management) | **hive** |
| CLI command tree | `bh rig <verb>` | **`bh hive <verb>`** (hard cutover — no `bh rig` alias) |
| Routing flags | `-r/--rig <id>` on passthrough/work verbs | `--hive` (kept the `-r` short flag; `--rig` removed, not aliased) |
| MCP tools | `rig_add`, `rig_onboard`, `rigs_available`, `rigs_status` | `hive_*` / `hives_*` (old names removed outright — no dual registration) |
| MCP resources | `beadhive://rigs/{available,status,survey}` | `beadhive://hives/…` (old URIs removed, not aliased) |

Rationale: "hive" is the brand-native term — Beadhive manages hives of beads; "rig" was
inherited factory vocabulary that predates the Beadhive identity (see
[limn-naming-strategy-adr.md](limn-naming-strategy-adr.md)) and now collides with nothing in
the product language. The original plan called for a phased, alias-carrying migration; at
replan that was superseded by a **full-depth hard cutover** — user-facing surface and internal
identifiers renamed together in one pass, not staged.

## Inventory — every surface that said "rig"

Measured on this tree (rev of bh-7yhl); counts are `\brig\b` matches that scoped the sweep, not
an edit checklist. All of the following were renamed as part of epic bh-41rh.

### CLI (breaking surface — the reason this rode its own epic)

- Command tree `bh rig …` → `bh hive …`, subcommands unchanged: `init add rm retire onboard ls
  migrate ready context survey classify prefix enable disable` (cli.py, 87 mentions incl. help
  text).
- Routing flags `-r/--rig` → `-r/--hive` on the `bd`/`git` passthroughs and `bh work --hive`
  (cross-hive). No `--rig` alias was kept.
- Help/error prose across verbs renamed ("not an AGF hive", "hive 'X' ready", survey/doctor
  output).

### MCP plane

- Tools: `rig_add` → `hive_add`, `rig_onboard` → `hive_onboard`, `rigs_available` →
  `hives_available`, `rigs_status` → `hives_status` (+ `_resource` twins), `rigs_survey_resource`
  → `hives_survey_resource` (mcp.py, 43 mentions). Old names were removed outright, not
  dual-registered.
- Resource URIs: `beadhive://rigs/available|status|survey` →
  `beadhive://hives/available|status|survey`.

### Python internals (renamed in full — no partial follow)

- Modules: `rig.py` → `hive.py`, `rig_ready.py` → `hive_ready.py`, `rig_migrate.py` →
  `hive_migrate.py`.
- Registry vocabulary: `rig_dir_for` → `hive_dir_for`, `rig_match` → `hive_match`, "rig id"
  triplets → "hive id" triplets across registry.py, work.py, worktree.py, onboard.py, config.py
  — internal identifiers, comments, and log strings were renamed alongside the user-facing
  surface, not deferred as a later cleanup.
- Config accessor strings: per-hive config regions, `work.*` docs; the persisted config-key
  surface is covered below under persisted state.

### Persisted / cross-session state (compat-critical)

- Head Office registry `~/.ws/config.yaml`: the `managed_repos` entry schema does **not** say
  rig (keys are provider/org/repo/prefix/kind/upstream/furnish) — no data migration was needed;
  only surrounding docs/comments said "rig", and those were swept.
- There is **no** `component:rig` label — `component` is an open dimension on beads, not a
  closed enum carrying a `rig` value on historical beads. (The original inventory claimed a
  `component:rig` label existed on historical beads and that history should be left untouched;
  that claim was wrong and is corrected here — there was nothing to leave untouched or add a
  `component:hive` counterpart to.)
- The keys that actually changed: `otel.rig` → `otel.hive` and `git_workspace.rig_match` →
  `git_workspace.hive_match`, both in the operator's persisted `~/.beadhive/config.yaml`.
  Renamed outright — no dual-key read support — with exactly one cheap, targeted
  migrate-on-load shim (`config.migrate_hive_keys_if_needed`) that upgrades an operator's own
  existing config keys in place on first CLI invocation post-cutover. It is not a general
  migration framework: it covers only these two keys, no-ops when the config file is absent or
  already migrated, and is best-effort (never blocks the CLI on a hiccup).
- HQ guard config section `rig-config` → `hive-config` (guard.py's `HQ_HIVE_CONFIG`), including
  the owning-seat mapping text (`policy->supervisor, fleet->director, hive-config->custodian`).
- OTEL attribute `bh.rig` → `bh.hive` (span/metric attribute name), including the Grafana
  dashboard JSON that referenced it — renamed outright, not dual-emitted; dashboards were
  updated in the same pass so nothing was left pointing at the old attribute name.
- Statusline output: the rendered seat/rig wording became seat/hive wording.
- No environment variables carried a `rig` token, and worktree paths (e.g.
  `~/.beadhive/wt/<org>/<repo>/<bead>`) carry no `rig` token either — neither needed touching.

### Docs, skills, plugins (sweep surface)

- Docs: 379 mentions across ~19 files were swept; the largest file was renamed to
  `docs/HIVES.md` (no redirect stub — hard cutover, not a compat surface).
- Bundled assets: `AGF-hint.md` ("onboarded as a `bh` hive" — updated from the original "rig"
  wording), statusline seat/hive display.
- **Cross-repo**: the bh Claude plugin (beadhive/claude-plugin — skills `bh:setup`,
  `bh:developer`, `bh:dispatcher`, hooks calling `bh hive …`), the workspace-repo plugin
  skills, and `docs/AGF.md`/`PRIME`-successor steering text. These rode as standalone
  lockstep beads dependent on the surface bead in this epic, rather than being inlined here.
- Tests: 82 test files renamed/updated (fixture names, CLI invocations, output assertions) —
  see `tests/test_hive_*.py`.

## Migration / deprecation plan (as executed)

The staged 0.4.0-dual / alias-window / 0.5.0-removal plan originally recorded here was
**superseded at replan** and executed instead as a single hard cutover:

1. **One hard cutover, full depth.** `bh hive` replaced `bh rig` everywhere in the same change:
   no `bh rig` CLI alias, no dual MCP tool/resource names, no dual OTEL attribute emission. The
   old names simply cease to exist — there was no deprecation window to carry or later remove.
2. **Shipped with 0.3.0.** The rename rode the same, still-unreleased breaking window as the
   zero-footprint onboarding change, rather than waiting for a dedicated 0.4.0. 0.3.0 therefore
   carries both breaking changes together; there was no released version that ever had the
   dual/aliased surface described in the original plan.
3. **Persisted state renamed outright.** `otel.rig` → `otel.hive` and
   `git_workspace.rig_match` → `git_workspace.hive_match` were renamed in the persisted config
   schema, with exactly one cheap migrate-on-load shim scoped to the operator's own existing
   config keys (see the persisted-state inventory above) — not a general or long-lived
   migration mechanism.
4. **Cross-repo plugin changes rode as standalone lockstep beads**, dependent on the surface
   rename bead in this epic, rather than being deferred to a follow-up epic.
5. **Out of scope for the rename** (unchanged from the original plan): the `managed_repos`
   registry schema (already term-neutral), bead history relabeling (moot — there was no
   `component:rig` label to relabel), and the `refs/dolt/data` storage layout.

## Implementation record

The rename was implemented in full as part of epic **bh-41rh**, landing pre-0.3.0. There is no
outstanding follow-up epic or alias-removal step: the cutover above is the complete change.
