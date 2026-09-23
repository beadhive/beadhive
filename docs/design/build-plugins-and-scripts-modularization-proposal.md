# Proposal: in-repo packages, build-system plugins, and a structured `scripts/`

> Status: **proposal** (2026-09-23). Not filed as beads; nothing here changes code or policy.
> Companion to
> [`repository-organization-second-review-2026-09-23.md`](repository-organization-second-review-2026-09-23.md).

## Summary

The goal is to build new, isolated modules in this repository without two things: a new repo,
and waiting for the monolithic `src/beadhive` package and its Pants targets to be cleaned up.
The first users would be build-system plugins (Pants, Turborepo, Bazel) that make attestation
first-class in the `bh` runtime. The second would be the roughly 16,000 lines in `scripts/`.

Most of the hard parts already exist:

- **The backend contract.** The `ImpactBackend` port in `modules/work/contracts/impact.py`,
  from Attested Green Amendment 1, was designed for Pants, Turborepo, and Bazel. The five
  fail-closed rules, receipts, per-key verdicts, and carry-forward are all core code already.
- **The plugin model.** The plugin kernel (`kernel/plugins`) has manifests, capability
  ownership, CLI presentation, a data-only built-in catalog, and a reserved entry-point group,
  `beadhive.plugins.v1`.

What's missing is small:

- **A package boundary** for code that shouldn't live in `src/beadhive`.
- **A plugin binding for impact backends.** Today Pants is hard-wired: `selective_validation.py`
  constructs `PantsImpactBackend` directly.
- **A home for each kind of script.**

The proposal has four parts:

1. **A uv workspace of in-repo packages under `packages/*`.** Each package is its own
   distribution and import name, with its own tests, one small BUILD file, and a sandbox proof
   from day one. Adding a package needs no edits to shared root files.
2. **Two new plugin-kernel capabilities, `build.impact` and `build.verify`.** Bootstrap
   collects the backends from these instead of hard-coding one. Pants becomes the first
   plugin package, `beadhive-pants`, which ships the backend, its health checks, and the Pants
   runner scripts.
3. **An impact-backend conformance kit** in `beadhive.testing`. This is the shared definition of
   done that lets Turborepo and Bazel plugins be built in parallel worktrees.
4. **Split `scripts/` by kind.** Pants orchestration goes into the Pants plugin. Architecture and
   closure tooling goes into a dev-only package. Release scripts stay with their existing
   promotion beads. Demos and probes stay put. Thin shims keep every current path working.

None of this depends on the `bh-cgfj0` reorganization, and none of it blocks it. It touches no
root module that the reorganization has filed for moving.

## 1. What exists today

| Asset | Where | State |
| --- | --- | --- |
| Backend port | `modules/work/contracts/impact.py` | `ImpactResolver`, `ImpactBackend` (`name`, `version`, `analyze`), and `TreeDiffPort`. Its docstring names Pants, Turborepo, and Bazel. |
| Fail-closed rules and resolver selection | `modules/work/application/impact.py` | `select_resolver` takes an explicit `backends` mapping, so an unknown backend degrades to `native-full`. |
| Composition root | `bootstrap/impact.py` | Binds a resolver from config and the backends it is given. It doesn't pick backends itself. |
| Pants backend | `adapters/impact_pants.py` (290 lines) | Works, but reads `scripts/pants_proven_tests.json`, so product code depends on a file in `scripts/`. |
| Backend wiring | `selective_validation.py` (root) | Imports and constructs `PantsImpactBackend` directly. |
| Plugin kernel | `kernel/plugins/` | Manifest schema, capability `provides`, CLI presentation, fail-closed conflicts. The five built-ins are listed as data. External entry points are reserved and gated off. |
| Legacy plugin registry | `plugins.py` (root) | A compatibility facade over the kernel. |
| Setup-pack epic `bh-n9gkm` | tracker | Plans a pnpm + Turborepo cache pack against the **legacy** `plugins.Plugin` registry. |
| Script-promotion beads | tracker | `bh-1h1n6` (push hardening) and `bh-uiuut` (release safety checks) already promote release scripts into `bh`. |

The problem in numbers:

- **Most tests run outside Pants' sandbox.** `tests/BUILD` has one broad
  `legacy-stateful-tests` generator covering 395 flat root test files. Only 86 of 447 test files
  are sandbox-proven.
- **The Pants source target is one glob.** `src/beadhive/BUILD` has a single `:lib` target over
  `**/*.py`.
- **The main-push gate takes about 6 minutes** (`bh-j5saf`).
- **`scripts/` is 68 flat files, about 16,000 lines.** The justfile references them 80 times.
  54 test files load them by path, and 12 import them.

## 2. The isolation unit: in-repo packages

### Layout

```text
packages/
  beadhive-pants/            # first build plugin
    pyproject.toml           # name = "beadhive-pants"; depends on "beadhive"
    BUILD                    # one python_sources + one python_tests + resources
    justfile                 # package-local recipes: test, lint, check
    src/beadhive_pants/
      __init__.py
      plugin.json            # kernel manifest (plugin_id "pants")
      impact.py              # ImpactBackend (moved from adapters/impact_pants.py)
      verify.py              # ownership / proven-manifest / catalog checks
      runner.py              # proven-vs-native partition (from scripts/pants_ci.py)
      cli.py                 # `bh plugin pants ...` commands
      data/proven_tests.json # moved from scripts/pants_proven_tests.json
    tests/
  beadhive-devtools/         # dev-only; never shipped
    src/beadhive_devtools/{architecture,closures,evidence}/
    tests/
```

### Rules

1. **Separate import name.** A package imports `beadhive` only through an allowlist of public
   surfaces: `beadhive.kernel.*.contracts`, `beadhive.modules.*.contracts`, and
   `beadhive.testing`. The rule is enforced by a small AST check. Packages can't import root
   modules or `application`/`adapters` internals. This makes packages the first real consumers of
   the "explicit public API" rule (Decision 5 of the physical-organization ADR), in a small
   place where getting it wrong is cheap.
2. **Nothing in `src/beadhive` imports a package statically.** First-party plugins are named as
   data in the built-in catalog, and bootstrap resolves that name lazily. This is the same
   pattern `plugin_runtime_catalog.py` already uses for the five built-ins.
3. **Sandbox-proven from birth.** A package's tests use only its own fixtures and the public
   conformance kit, so they run under `pants test packages/<name>::` with no stateful fixture
   plugin. They never join the `legacy-stateful-tests` pool.
4. **One attest key for all packages.** A single `packages` key runs
   `pants test lint packages::`. Pants' content-addressed cache means unchanged packages are
   served from cache, so the key stays cheap as packages multiply. There's no per-package entry
   in the key catalog, the `check_attest_catalog.py` partition, or fleet config.

### Why this avoids the "full weight" of the Pants build

- **Its own targets.** A package never lands in `//src/beadhive:lib` or
  `//tests:legacy-stateful-tests`. From day one it already has the owner-scoped shape Decision 7
  asks for.
- **Invalidation stays inside the package.** The Pants backend maps a change under
  `packages/beadhive-turborepo/` to that package's targets and the `packages` key. It won't
  invalidate `stateful` or `unit`, and verdicts carried for other keys stay valid (Amendment 1).
- **Two caveats to accept knowingly.** Editing a package's `BUILD` matches the global input
  `**/BUILD` (rule 2), so it invalidates every key. Keep BUILD files generator-shaped and rarely
  edited. Adding a new package once changes `uv.lock`, a lockfile, which also invalidates
  everything once.

### Adding a package touches no shared file

Parallel worktrees should never conflict just because each adds a package. So make the shared
wiring glob-based, once:

| Shared file | One-time change | After that |
| --- | --- | --- |
| `pyproject.toml` | `[tool.uv.workspace] members = ["packages/*"]` | New packages are picked up automatically. |
| `pants.toml` | `root_patterns` adds `/packages/*/src` and `/packages/*/tests` | Same. |
| `justfile` | Recipe `pkg name *args` → `just -f packages/{{name}}/justfile {{args}}`, plus `packages-check` | No per-package recipe. |
| Attest catalog | Add the `packages` key once (updating `check_attest_catalog.py` and the `check-all` partition) | Same. |
| Import checker | Add a `packages/` rule: public-surface allowlist, no root-module imports | Same. |

The only file every new package touches is `uv.lock`. It merges mechanically: regenerate it with
`uv lock`.

### Shipping

- **In development,** `uv sync --all-packages` installs the members, and `bh` finds first-party
  plugins through the built-in catalog.
- **The released `bh` binary** depends on first-party plugin packages through the
  `pex_binary(name="bh")` dependency list. Dev-only packages such as `beadhive-devtools` are
  never added.
- **Third-party plugins** later use the reserved `beadhive.plugins.v1` entry-point group once
  `allow_external_entry_points` is implemented beyond manifest reading. That work isn't needed
  for first-party plugins.

## 3. Build-system plugins: making attestation first-class

### Capabilities

Add two capabilities to the plugin kernel. The Attested Green constraints still hold: keys stay
opaque commands, the five fail-closed rules stay in core, and a plugin only answers questions.

| Capability | Port | Used by |
| --- | --- | --- |
| `build.impact` (API 1) | `ImpactBackend`, unchanged | Bootstrap collects every enabled provider into the `backends` mapping for `select_resolver`. `work.attest.impact.backend` picks one by name, and an unknown name still degrades to `native-full`. |
| `build.verify` (API 1) | `BuildVerifier.verify(repo) -> tuple[PluginDiagnostic, ...]` | `bh doctor` and `bh hive ready`: ownership completeness, proven-test manifest drift, key-tag coverage. This is where `check_pants_ownership.py` and `check_pants_proven.py` move. |

A third capability, `build.key-catalog`, would let a plugin propose `work.attest.keys` entries
from its own graph: Pants tags, Turborepo task names, or Bazel tags. Defer it until two backends
exist, so its shape is proven rather than guessed.

Plugin CLI commands come from manifest `presentation.cli`: `bh plugin pants verify`,
`bh plugin pants test affected <base>`, `bh plugin pants test all`, `bh plugin pants native`.
A key's `cmd` can then be `bh plugin pants test all` instead of a `just` recipe that calls
`scripts/pants_ci.py`. It's still an opaque command to the core; it just no longer depends on
`scripts/`.

### Conformance kit: the shared definition of done

Add `beadhive.testing.impact` (under `testing/`, the public conformance root the physical ADR
already defines). It provides:

- a fixture repository builder: a few owners, a dependent chain, a global input, an unowned
  path, and a deletion or rename;
- the five fail-closed rules as parametrized tests against any `ImpactBackend` factory:
  unowned path, global input, version mismatch or timeout, unproven test, and git-metadata
  keys;
- receipt-shape assertions.

Each backend package's tests are then "run the kit against my backend plus my own specifics."
Three backends can be developed in three worktrees without coordinating beyond the kit.

### The three plugins

| Plugin | `build.impact` source | Notes |
| --- | --- | --- |
| `beadhive-pants` | the current `PantsImpactBackend` plus `pants_routes`/`pants_ci` logic | A move, not new logic. It also owns `pants_attest`, `pants_cache`, `pants_launcher`, and the benchmark and shadow-evidence scripts. |
| `beadhive-turborepo` | `turbo run <tasks> --filter=...[<base>] --dry=json`, per Amendment 1's table | Should be the **same plugin** as `bh-n9gkm.8`'s pnpm + Turborepo cache pack, with two capabilities (impact, and later setup). |
| `beadhive-bazel` | `rdeps(//..., set(changed))` intersected with `attr(tags, "attest:<k>", //...)` | Can wait until a hive actually uses Bazel. The kit makes it a bounded task. |

Retarget `bh-n9gkm` to the kernel's manifests and capabilities before it starts. Its description
still describes extending the legacy `plugins.Plugin` registry, which would create a second
plugin model.

## 4. `scripts/` triage

| Kind | Files | Lines | Destination |
| --- | ---: | ---: | --- |
| **A. Pants and test-run orchestration**: `pants_*`, `check_pants_*`, `test_impact_selector`, `hermetic.sh`, `test-watchdog.py` | 13 | 2,577 | `beadhive-pants`. `hermetic.sh` and `test-watchdog.py` are backend-neutral run hygiene, so move them into core validation later, not into the Pants plugin. |
| **B. Test-closure machinery**: `test_closures`, `test_closure_{certification,shadow_*,promotion_policy,operational_report}` | 6 | 3,936 | `beadhive-devtools/closures`. **Freeze its features first** (see below). |
| **C. Repository architecture and policy checks**: `check_import_boundaries`, `capability_*`, `refresh_modularization_closeout`, `config_*` metrics and ledgers, `validate_final_refactor_parity`, `render_transport_composition_evidence`, `check_attest_catalog`, `check_wire_schema_compat` | 10 | 3,980 | `beadhive-devtools/architecture`. |
| **D. Artifact generators**: `generate_*`, `render_*` | 6 | 157 | Leave as they are. They're thin CLIs over `beadhive`; move each with its owning module during the reorganization. |
| **E. Release, push, and supply chain**: `push-main`, `main-push-gate`, `release*`, `osv-*`, `image-*`, `local-build`, `proof-gate` | 15 | 2,037 | Keep with the existing promotion beads `bh-1h1n6` and `bh-uiuut`. Don't open a parallel effort. |
| **F. Demos, probes, benchmarks, profiling, backfills** | 12 | 3,327 | Stay in `scripts/`. These really are scripts. Retire one-off backfills once they've been used. |
| **G. Host ops units**: `bh-worktree-prune*`, timer installer | 4 | 110 | Later, a `bh host` subcommand or `deploy/`. Low priority. |

Only 17 of these 66 files import `beadhive`, so most moves are mechanical.

**Keep old paths working.** Every moved script leaves a shim at its old path, for example
`scripts/pants_ci.py` → `from beadhive_pants.runner import main`. The 80 justfile references,
CI workflows, docs, and the 54 path-loading tests keep working. Remove each shim only after its
references reach zero, the same one-way lifecycle as root facades.

**Why freeze the closure machinery.** `tests/closures.toml` with the certification, shadow, and
promotion scripts is a second test-selection system beside the attest-key `ImpactResolver`.
The justfile already labels closures "advisory." Move the machinery into devtools unchanged, and
add no features. Then decide, preferably inside the reorganization's test audit (`bh-4kmy0`),
whether closure selectors become Pants tags and plugin-proposed keys, or are retired.

**Why devtools helps the reorganization.** The checker improvements recommended in the second
review (facade-body check, exception budget, role table, allocation columns) can then be built
in an isolated, sandbox-proven package with fast tests. They no longer ride the
`legacy-stateful-tests` pool.

## 5. Short-term mitigation for the Pants monolith

These are cheap steps that can run beside the plugin work. None of them changes validation
policy.

1. **Contain the monolith; don't decompose it yet.** Keep `//src/beadhive:lib` and
   `legacy-stateful-tests` as they are, but name them for what they are: migration debt, as
   Decision 7 already says. New code goes into `packages/*` or governed directories with their
   own targets.
2. **Give the governed directories their own targets now.** They import root modules through
   only five ledgered edges, so they're already clean. Add a BUILD file to `modules/<capability>`,
   `kernel/<concern>`, `adapters/<boundary>`, `integrations/herdr`, `bootstrap`, and `testing`,
   and narrow `:lib` to root `*.py`. Do the same for `tests/unit/<area>`.
   - **What this gains:** addressability (`pants test src/beadhive/modules/config::`) and
     owner/role tags, which is Decision 7's first step at little cost.
   - **What it doesn't gain:** selection precision. Pants already generates per-file targets, so
     impact selection won't get meaningfully sharper.
3. **Don't widen the stateful pool.** New tests go in `tests/unit/<area>` or a package, where
   they must pass under the sandbox. Keep adding to the proven manifest opportunistically, as
   `bh-1j3ei.4` started. It moves to `beadhive-pants/data/` with the plugin.
4. **Leave the global-input list alone.** Scoping `**/BUILD` to its own subtree would sharpen
   invalidation, but it amends Attested Green rule 2. Make it a separate decision once packages
   exist and the cost shows up in real receipts.

## 6. Sequencing

Everything below lands directly on `main` in small molecules. Nothing waits on `bh-cgfj0`. Each
row names the one touch point that needs care.

| Step | What | Size | Parallel? | Touch point |
| --- | --- | --- | --- | --- |
| 0 | Workspace scaffold: glob members, `root_patterns`, `pkg` recipe, `packages` key, package import rule, a template package with a trivial sandbox test | S | — | Root `pyproject.toml`, `pants.toml`, `justfile`, attest catalog: one time |
| 1 | `build.impact` and `build.verify` capabilities; bootstrap collects backends from enabled plugins | S–M | after 0 | `selective_validation.py` stops constructing `PantsImpactBackend` (a root file with no filed reorganization owner) |
| 2 | `beadhive.testing.impact` conformance kit, run against the current Pants backend | M | with 1 | `testing/` |
| 3 | `beadhive-pants`: move the backend, verify checks, runner, and proven manifest; add shims | M | after 1 and 2 | `adapters/impact_pants.py` becomes a forwarding facade or is deleted after one caller change |
| 4 | Governed-directory BUILD split (section 5, item 2) | S | any time | `src/beadhive/BUILD`, `tests/BUILD` |
| 5 | `beadhive-devtools`: closure machinery (frozen) and architecture checks, with shims | M | after 0 | **After phase one of `bh-cgfj0` lands on `main`.** That container holds a newer `check_import_boundaries.py` (724 lines against 558 on `main`), and moving the old one first would force a painful merge. |
| 6a | `beadhive-turborepo` (impact), merged with `bh-n9gkm.8`'s cache pack | M | after 2 | Separate worktree; the kit is the contract |
| 6b | `beadhive-bazel` (impact) | M | after 2 | Separate worktree; defer until a hive needs it |
| 7 | `build.key-catalog` capability, once two backends exist | S | after 3 and 6a | Plugin kernel contracts |

Steps 0 through 3 give the smallest useful result: Pants is a plugin, attestation backends are
pluggable at runtime, and new build plugins can start in parallel worktrees.

## 7. Decisions for the operator

1. **Adopt the `packages/*` workspace pattern** as the sanctioned home for new isolated
   modules, alongside the capability packages under `src/beadhive`. Recommended. The rule of
   thumb: code the core product must have belongs in `src/beadhive`. Optional plugins and dev
   tooling belong in `packages/*`.
2. **Distribution:** should first-party plugin packages ship inside the `bh` pex, or as
   separately installable distributions? I recommend inside the pex for now, and entry points
   later for third parties.
3. **Retarget `bh-n9gkm`** onto kernel capabilities, and merge its Turborepo cache pack with the
   Turborepo impact plugin.
4. **Freeze the test-closure machinery** and fold the closures-versus-keys decision into
   `bh-4kmy0`.
5. **Package import allowlist:** is `beadhive.*.contracts` plus `beadhive.testing` the right
   public surface for packages? This also answers part of the second review's D4 (public API
   granularity) in a low-risk setting.

## 8. Risks

- **Two plugin models.** If `bh-n9gkm` proceeds on the legacy registry while build plugins use
  the kernel, the repo ends up with competing extension mechanisms. That's why decision 3
  matters.
- **Uncontrolled shims.** Shims without a retirement ledger become permanent. Track them the
  same way as root facades: introduced commit, consumers, and an expiry trigger.
- **A package quietly depending on internals.** Without the import allowlist check, a package
  can depend on `beadhive` internals and the isolation disappears. Enforce the check in step 0,
  not later.
- **`uv.lock` churn.** It's the one shared file per new package. Keep package additions small,
  and regenerate rather than hand-merge.
- **Scope creep into the Attested Green ADR.** Plugins answer questions. They never interpret
  keys or weaken the five fail-closed rules. Any change there is an ADR amendment, not plugin
  work.

## Evidence

Measured on `main` at `9b3e671a`:

- the `scripts/` categories and line counts, from `wc -l` and a filename classifier;
- `beadhive` imports in scripts, from grep;
- script references from the justfile (80) and from tests (54 by path, 12 by import);
- the test-file split (395 at the `tests/` root, 42 under `tests/unit`, 86 sandbox-proven, from
  `scripts/pants_proven_tests.json`);
- the plugin-kernel and impact-port sources cited above;
- open beads `bh-n9gkm`, `bh-1h1n6`, `bh-uiuut`, `bh-j5saf`, and `bh-k1ynk`; and `bh-1j3ei`,
  closed, which delivered the Pants backend.

Tool versions: uv 0.12.1, just 1.57.0, Pants 2.32.1 (pinned). No builds or tests were run for
this proposal.
