# Toolchain declaration — knowledge-only metadata (`toolchain:`) (bh-d0kb)

> Status: **shipped (knowledge-only).** Follow-up from the bh-17n4 triage. **Revised
> decision (2026-07-17, operator):** the declaration is KNOWLEDGE-ONLY — it never drives
> behavior. An earlier v1 draft derived init rules and a validate default from the
> declaration; that derivation is deliberately **not** shipped. Rationale: "we don't know
> what could be behind even sane defaults" — a template's assumption about what `npm ci` or
> `just setup` means in a given repo must never be acted on implicitly.

## Problem

Provisioning today is **sniffed, not declared**. The shipped `worktrees.init` defaults probe
for convention files (justfile, pyproject.toml, .mise.toml) and guess canonical recipe
names, so a repo that happens to contain a `justfile` gets `just setup` behavior it never
asked for, and agents have no structured way to learn what a repo's toolchain actually
exposes.

A repo/hive should be able to **declare** its toolchain(s) — and agents should be able to
**discover** the entrypoints those toolchains expose and **suggest** config to the operator.
Nothing more: declaration never implies behavior.

## The contract: declaration never implies behavior

1. **Declaration + registry are metadata.** `worktrees.toolchain` (a name or a list;
   per-hive `managed_repos[*].toolchain` wins) resolves against the shipped template
   registry in `src/beadhive/toolchain.py`. `worktrees.toolchains: {name: template}`
   overlays the registry per name (**replace, not merge** — an override owns its whole
   template; unknown declared names simply have no template).
2. **Nothing runs, and nothing is defaulted, because of a declaration.**
   `worktree._rules` reads explicit config only (`worktrees.init` + the hive's
   `worktree_init`); `config.validate_cmd` resolves explicit config only
   (`work.validate.<phase>` > `work.validate_cmd` > the hard `just check` default).
   Neither consults the toolchain registry.
3. **Suggest, don't set.** Each template carries `suggested_init` (rules an agent may
   propose for `worktrees.init`, with the bh-7k1p `verify` line pre-drawn) and
   `suggested_validate_cmd` (a candidate for `work.validate_cmd`). These are what an agent
   **proposes to the operator**, who sets explicit config; bh never writes or assumes these
   values.
4. **Discovery + exec are the operative surface** (below): list the registry, list a
   toolchain's entrypoints in the hive's main clone, invoke one explicitly.

## The surface

CLI (per the [cli-mcp-naming-conventions ADR](cli-mcp-naming-conventions-adr.md):
per-command `--hive` long-only; root `--hive` stays passthrough routing):

- `bh toolchain list [--hive H] [--json]` — declared toolchains + the effective registry.
- `bh toolchain show <name> [--hive H] [--json]` — runs the template's `entrypoints_cmd`
  in the hive's **main clone** and prints the raw listing + the template's suggestions.
- `bh toolchain exec [--hive H] -- <argv...>` — invoke an entrypoint through bh's `run()`
  seam in the hive's main clone (`--` passthrough per the `bh bd` precedent). Refuses an
  empty argv; the entrypoint's exit code passes through.

MCP (convention 5e; resources share the CLI's payload producers so the shapes never drift):

- `beadhive://toolchain/list` — same payload as `bh toolchain list --json`.
- `beadhive://toolchain/show/{name}` — same payload as `bh toolchain show <name> --json`.
- tool `toolchain_exec(argv, hive="")` — backs `bh toolchain exec`; returns
  `{exit_code, stdout, stderr}`.

## Shipped templates

Each template: `entrypoints_cmd` (the read-only discovery command `show` runs) +
`suggested_init` / `suggested_validate_cmd` (propose-only).

| name | entrypoints_cmd | suggested init | suggested validate_cmd |
|---|---|---|---|
| `just` | `just --list` | probe-guarded `just setup` (`if_exists: justfile`) | `just check` |
| `uv`   | python3 one-liner reading `[project.scripts]` from pyproject (tomllib, py3.11+) | `uv sync` (`verify: true`) | `uv run pytest` |
| `npm`  | `npm run` | `npm ci` (`verify: true`) | `npm test` |
| `make` | best-effort rule-database dump (`make -pRrq :` filtered to target-looking lines — make has no portable listing verb) | probe-guarded `make setup` (`if_exists: Makefile`) | `make check` |

Notes on the pragmatic picks: `just` and `npm` ship first-class listing verbs. `make` does
not — the filtered database dump may include file targets and is documented best-effort.
`uv` entrypoints live in pyproject's `[project.scripts]`; the python3 one-liner reads them
without needing `uv` itself, and stays a plain command string so hive overrides remain YAML
data and tests fake the single `run()` seam.

The suggested init rules keep the bh-17n4 severity principle: a declaration says "this repo
uses just", not "this repo has a `setup` recipe", so the suggested provisioning entries
probe first and no-op quietly when the canonical recipe is absent.

## Design questions, resolved

- **Single vs multiple:** a list; a bare name is sugar for a one-element list. Order is
  presentation-only now (nothing is derived from it).
- **Detection vs explicit:** explicit config is the only source of truth. *Later (design
  intent):* onboard MAY detect candidates and suggest a declaration — operator-confirmed,
  written as ordinary config, never inferred at runtime.
- **Registry shape:** shipped dict in code + per-name replace overrides in config
  (`worktrees.toolchains`), so a hive can amend a built-in or add its own (e.g. `gradle`).
- **Relationship to `worktrees.init` / `validate_cmd`:** none at runtime. The declaration
  is the input to a **suggestion flow**: an agent reads `beadhive://toolchain/list` +
  `show/{name}`, proposes explicit `worktrees.init` / `work.validate_cmd` values, and the
  operator sets them. This supersedes the v1 "declaration generates default rules" design.
- **Interplay with verify flags (bh-7k1p):** the suggested rules pre-draw the `verify`
  line per toolchain (dependency sync flagged, seat provisioning not) — that knowledge
  rides along in the suggestion, and takes effect only if the operator adopts it into
  explicit config.

## Compatibility

Declared or not, behavior is **byte-identical** to before this feature everywhere outside
the new `bh toolchain` group and MCP toolchain surface: `_rules` and `validate_cmd`
resolve exactly as they always did. No migration, no deprecation.
