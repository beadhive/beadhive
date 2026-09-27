# ADR: Package classes — library vs. plugin — and the beadhive-plugins/beadhive-worktrees split

- **Status:** Accepted
- **Date:** 2026-09-27
- **Decision owner:** Beadhive maintainers
- **Related work:** `bh-qo63b` (this molecule), `bh-qdezo.1` (superseded replan),
  `bh-mr9tk` (worktree manager contract), `bh-bwnys`, `bh-sy36q` (API-first parallel
  replacement)
- **Supersedes:** `bh-qdezo.1`'s replan content (2026-09-27), which this ADR now records as
  the committed decision.
- **Amends:** [`docs/MODULES.md`](../MODULES.md), [`docs/PLUGIN-AUTHORING.md`](../PLUGIN-AUTHORING.md),
  and [`beads-api-first-parallel-replacement-adr.md`](beads-api-first-parallel-replacement-adr.md)
  — each carries a dated note pointing back here.

## Context

The `packages/*` boundary landed by the build-plugins-and-scripts-modularization proposal
(`docs/design/build-plugins-and-scripts-modularization-proposal.md`) defines exactly one
package shape: a **plugin package** such as `beadhive-pants`. It depends on `beadhive`, is
named as data in the built-in plugin catalog, and is resolved lazily by root only after
manifest selection. `docs/MODULES.md` principle 9 ("no premature distribution split") and its
"Landed `packages/*` boundary" section, and `docs/PLUGIN-AUTHORING.md`'s "Build plugins in
`packages/*`" section, both describe only this direction.

Two pieces of code need a package boundary that runs the **other** way before any code moves:

- The typed capability-slot and lifecycle contracts in `kernel/plugins/contracts.py`,
  `binding.py` (`bind_application_port`), and `kernel/lifecycle/contracts.py` are
  stdlib-only, but they live in the root `beadhive` distribution. That traps them: a package
  must depend on all of `beadhive` to see them, and `beadhive-core` (the future API-first
  replacement core from `bh-bwnys`/`bh-sy36q`) cannot see them at all, because `beadhive-core`
  must never import root.
- The typed worktree seam in `src/beadhive/modules/worktrees` (573 lines) is also
  stdlib-only, but lives in root alongside about 6,000 lines of legacy worktree code that
  carries the domain's only `bd` argv coupling. `bh-mr9tk`'s worktree manager contract and the
  native Git manager need a home `beadhive-core` can depend on without depending on root or on
  argv-era `bd` coupling.

Forcing both into the existing plugin-package shape is wrong: a plugin package depends on
`beadhive`, and neither of these two extractions can depend on `beadhive` — they need to be
depended *on*, including eventually by `beadhive-core`, which itself must never import root.
The existing rules don't have a name for that shape. This ADR names it, fixes the enforcement
rule for both shapes, and fixes the scope of the two concrete packages, before any code moves.

## Decision

### 1. Two package classes

**Plugin packages** (for example `beadhive-pants`) depend on `beadhive`, are named as data in
the built-in catalog, and are resolved lazily after plugin manifest selection; root never
imports them statically. This is the shape `docs/design/build-plugins-and-scripts-modularization-proposal.md`
already defined and `beadhive-pants` already implements. Nothing about that shape changes.

**Library packages** (`beadhive-plugins`, `beadhive-worktrees`, and `beadhive-beads-client`)
never import `beadhive`, carry no plugin manifest, and may be imported statically by root and
by other packages through their public `__all__` surface. This is a new class.

| | Plugin package | Library package |
| --- | --- | --- |
| Example | `beadhive-pants` | `beadhive-plugins`, `beadhive-worktrees`, `beadhive-beads-client` |
| May import `beadhive`? | Yes — through the existing allowlist (`beadhive.kernel.*.contracts`, `beadhive.modules.*.contracts`, `beadhive.testing`) | No — never, under any name |
| Carries a plugin manifest? | Yes (`plugin.json`, named in the built-in catalog) | No |
| May `beadhive` import it? | Only lazily, by name, after manifest selection — never a static import | Yes — statically, at module load time, like any other dependency |
| May another package import it? | No (plugin packages are leaves consumed only by root) | Yes — through its public `__all__` surface |
| Import direction, in one line | package → `beadhive` (lazy the other way) | `beadhive` (and other packages) → package (never the other way) |

The two classes are duals of each other along the same axis (who may import whom, and
statically or lazily), not two independent rule sets. A package is exactly one class; nothing
is both.

### 2. Enforcement

`scripts/check_package_imports.py` (invoked by `just architecture-structural-check`) is
extended to classify every package under `packages/*` by a manifest-presence check —
carries `plugin.json` under its `src/<import_name>/` tree, or does not — and enforce the
matching rule set:

- **Plugin package:** may import only `beadhive.kernel.*.contracts`, `beadhive.modules.*.contracts`,
  and `beadhive.testing` from root; may not import root's `application`/`adapters` internals or
  any other package. Unchanged from today.
- **Library package:** must import nothing from the `beadhive` distribution at all — no
  contracts, no `beadhive.testing`, nothing. It may import other library packages through
  their public `__all__` surface. A library package importing `beadhive` under any path is a
  checker failure, not a lint warning.
- **Root (`src/beadhive`):** may statically import a library package's public `__all__`
  surface. It may only import a plugin package's module lazily, after manifest selection,
  exactly as today.

This is one checker with two classification branches, not two checkers. A package that
carries a `plugin.json` and also imports nothing from `beadhive` is contradictory (a plugin
package with no manifest consumer would degrade to a library, and a library with a manifest
would invite root to statically bind a "plugin" ID); the checker rejects that combination
rather than silently picking a class.

### 3. Why the lazy-import rule applies only to plugin packages

`docs/design/build-plugins-and-scripts-modularization-proposal.md` section 2 states the
original rationale for plugin packages, in its numbered rules:

> 1. **Separate import name.** A package imports `beadhive` only through an allowlist of
>    public surfaces... 2. **Nothing in `src/beadhive` imports a package statically.**
>    First-party plugins are named as data in the built-in catalog, and bootstrap resolves
>    that name lazily.

That rule exists because a plugin package is optional, swappable, third-party-shaped
composition: root doesn't know at import time which plugin (if any) will be selected for a
capability, so it can't import one statically without picking a winner ahead of manifest
resolution and configuration. Laziness is what makes "declared but not installed" and
"declared but not selected" both safe — the exact property the plugin kernel's manifest
selection depends on (`docs/design/plugin-kernel-v1-adr.md`).

A library package inverts that premise. `beadhive-plugins` and `beadhive-worktrees` are not
optional alternatives root picks among at runtime — they are the typed contracts and default
mechanics root (and eventually `beadhive-core`) depends on unconditionally, the same way it
depends on any other library in its dependency graph. There is nothing to select lazily:
there is exactly one `beadhive-plugins` and it is always present. Applying the plugin
package's lazy-import rule to a library package would gain nothing (there is no alternative
provider to defer choosing between) and would cost real things: it would force root's own
capability contracts to be discovered through the plugin-selection machinery they are
supposed to sit *beneath*, and it would block `beadhive-core` from seeing them at all, since
`beadhive-core` cannot participate in root's manifest-selection bootstrap without depending on
root. The lazy rule is therefore scoped to plugin packages specifically, not package code in
general; library packages get the ordinary static-dependency rule instead.

### 4. Scope of `beadhive-plugins`

**Depends on:** nothing (no dependencies, not even another library package).

**Moves in:**

- The capability-slot and provider-binding contracts from `kernel/plugins/contracts.py` and
  `binding.py` (`CapabilityKey`, `ProviderBinding`, `bind_application_port`).
- The plugin manifest schema.
- The lifecycle event contracts from `kernel/lifecycle/contracts.py`, including the
  `WORKTREE` family of events.

**Stays in root:** plugin discovery, the lifecycle dispatcher, and telemetry — these are
composition machinery that binds concrete providers to the contracts above, not part of the
contracts themselves. Root keeps forwarding facades at the old `beadhive.kernel.*.contracts`
import paths so existing plugins and consumers keep working unchanged (per `docs/MODULES.md`
principle 8, compatibility facades are deliberate migration tools).

### 5. Scope of `beadhive-worktrees`

**Depends on:** `beadhive-plugins` only.

**Moves in:**

- Naming policy, `WorktreeSpec` and handle types.
- The `worktree.manager` capability slot, with native Git as the built-in default provider.
- The safety classifier.
- Inventory, status, cleanup, and prune services.
- Inbound ports: `BeadStateLookup`, `ClaimRecords`, `MergeEvidence`, config values, a command
  runner, and `WORKTREE` lifecycle observers for telemetry, Observaloop provisioning, and
  metadata invalidation.

**Adapters, supplied from outside the package:** the shell supplies argv-era `bd` adapters
now; `beadhive-core` supplies `BeadsSession` adapters once the API-first replacement
(`bh-bwnys`, `bh-sy36q`) lands. Root composition binds the selected manager with
`bind_application_port` and injects it — `beadhive-worktrees` does not select or bind its own
provider.

**Non-goals (explicitly out of scope for this extraction):**

- Clean-checkout validation (`worktree_verify.py`) — tracked separately as `bh-7oo93.11`.
- Branch, history, signature, and rewrite mechanics — tracked separately as `bh-7oo93.8`.
- The Herdr manager and its binding adapters — tracked separately as the Option A/B molecules
  filed from `bh-mr9tk.2`.
- Any behavior or CLI change. This is a pure move: the same identity, naming, and safety
  policy root already enforces, relocated behind a package boundary, not revised.

### 6. What stays in root

`src/beadhive` keeps: plugin discovery, the lifecycle dispatcher, telemetry, composition
roots (`bootstrap/*`) that select and bind concrete providers, the legacy ~6k-line worktree
code carrying the domain's `bd` argv coupling (until `bh-7oo93` retires it on its own
schedule), clean-checkout validation, and branch/history/signature/rewrite mechanics. None of
that moves as part of this decision.

### 7. Validation profile

Both `beadhive-plugins` and `beadhive-worktrees` extractions run the **strict** validation
profile: full `check-native` on every bead, not a reduced or package-scoped subset. This is
risk-derived — worktree removal safety guards user work, so a partial-coverage validation
profile is not an acceptable trade here, unlike lower-risk isolated package work.

## Consequences

- `scripts/check_package_imports.py` grows a second, library-package rule branch; a package's
  presence or absence of a `plugin.json` manifest is now load-bearing for which rule applies
  to it, not just discovery metadata.
- `beadhive-core` (once `bh-bwnys`/`bh-sy36q` land) can depend on `beadhive-plugins` and
  `beadhive-worktrees` directly, without depending on root — the reason this ordering was
  chosen: extract the library packages *before* `beadhive-core` exists, so `beadhive-core` is
  built against the packages, not against root with a promise to migrate later.
- Root's compatibility facades at the old `beadhive.kernel.*.contracts` paths mean this is not
  a breaking change for existing first-party plugins or tests; they keep working against the
  forwarding import paths until they migrate.
- The non-goals in section 5 remain open work items tracked by their own beads
  (`bh-7oo93.11`, `bh-7oo93.8`, `bh-mr9tk.2`'s molecules); this ADR does not resolve or
  schedule them, only fences them out of this extraction's scope.

## Superseded decisions

- `bh-qdezo.1`'s replan content (2026-09-27) is superseded by this ADR, which is now the
  committed record of the package-class decision and the two packages' scope.
