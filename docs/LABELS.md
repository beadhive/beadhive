# Labels, the registry & validation

The registry (`config.yaml`) is the source of truth for the label taxonomy; `bh label`
manages and validates it (modules: `registry.py`, `validate.py`).

## The label model

- **Identity triplet** `provider:` / `org:` / `repo:` — on every issue, applied automatically
  by `bh bd create` from the hive's *registered* identity (not naively from the path; forks
  carry their upstream's identity). Labels are how you slice the aggregated [hub](HUB.md),
  since `bd list` has no prefix filter.
- **Dimensions** — orthogonal axes under `dimensions:` in config. Each is **open** or
  **closed** by whether it declares `values:`:
  - no `values:` → open (any value), e.g. `component:`, `tag:`
  - `values: [a, b]` → closed (only those pass validation), e.g. `size: [xs,s,m,l,xl]`
  - `values: []` → closed but **reserved** (nothing valid yet — locks the dimension)

  Closed-dimension checking is generic — it applies to *any* dimension with `values:`, not a
  hard-coded set (`registry.closed_dimensions`).

## Code-owned dimensions

Two closed dimensions are owned **in code**, not in a hive's `dimensions:` block, so their
vocabulary is uniform fleet-wide and a hive cannot narrow or extend it:

| Dimension | Values | Owner | Meaning |
|---|---|---|---|
| `release:` | `breaking` · `feature` · `fix` | `registry.RELEASE_VALUES` | the change's semantic-version impact, consumed by release-order planning (`release_order.py`) |
| `intake:` / `outbound:` / `publish:` / `origin:` | see `state.STATE_DIMENSIONS` | `beadhive/state.py` | report-channel queue state and intake provenance ([REPORT-CHANNEL.md](REPORT-CHANNEL.md)) |

Both are merged into the effective set by `registry.closed_dimensions`, so beads carrying them
validate clean under `bh label validate` and an off-vocabulary value (`release:cosmetic`) is
rejected like any other closed-dimension violation.

### `release:` and `wave:` — release-order planning

`release:` pairs with **`wave:`**, an *open* label that groups additive features into a release
wave — deliberately distinct from the worktree-collapse label `batch:<group>`, which groups beads
into one shared worktree rather than one version bump.

| Label | Kind | Set at | Read by |
|---|---|---|---|
| `release:<breaking\|feature\|fix>` | closed, code-owned | plan time (molecule spec) or by hand | `release_order.release_impact` |
| `wave:<name>` | open | plan time | `release_order.wave_name` |

An off-vocabulary `release:` value is treated as *unset* by the scorer (the validator is what
rejects it), and an unlabeled bead simply orders after the classified ones. See
[CLI.md](CLI.md#bh-release) for `bh release order` and the `release:` config section in
[CONFIGURATION.md](CONFIGURATION.md).

## `bh label`

| Command | Does |
|---|---|
| `validate` | lint the current hive (or hub) DB against the registry |
| `sync` | reconcile the registry vs git-workspace: onboarding candidates, prefix collisions, required-org violations |
| `report` | usage counts per dimension (identity triplet + every configured dimension) |
| `allowed` | print the allowed label set (providers, orgs, repos, closed-dim values) |
| `docs` | regenerate `~/.beadhive/labels.md` from the registry |

Providers shown by `allowed`/`docs` are the **effective** set (config ∪ git-workspace when
enabled — `registry.effective_providers`).

## Validation rules (`bh label validate`)

Against `bd list --json` for the target DB:

1. **Required-org prefixes** — every hive under a `policy: required` org uses its `<code>-`
   prefix (registry-level; `registry.required_violations`).
2. **Triplet consistency** — an issue's `provider:`/`org:`/`repo:` labels must match the
   registered identity of the hive its prefix belongs to (longest-prefix match wins).
3. **Closed dimensions** — any `phase:`/`size:`/… value outside its declared set is flagged
   (`bad-<dim>:…`).
4. **Unknown prefix** — an issue whose prefix isn't registered is flagged.

If `bd`/the DB is unreachable, per-issue checks are **skipped with a note** (not silently
treated as clean); registry-level checks still run.

## Enforcement

Enforcement is fixed behavior, not configurable (there is no `enforcement:` block):

| Surface | Behavior |
|---|---|
| `bh label validate` | **the linter** — defaults to **enforce** (non-zero exit on any violation); `--advisory` reports and always exits 0 |
| `bh hive init` | **always** blocks a required-org prefix that doesn't match `<code>-` |
| `bh bd create` | **always** refuses to create in a hive that has label violations |

Rationale: invariants (required-org prefix at registration; a clean hive before adding to it)
are always enforced; only the *linter* is a reporting-vs-failing toggle.

## Registry operations (internal)

`registry.py` also provides the building blocks used by [HIVES](HIVES.md) and
[INTEGRATIONS](INTEGRATIONS.md): `classify`, `derive_prefix`, `register` (comment-preserving
upsert into `managed_repos`), `repos_sync`, `effective_providers`, and hive resolution
(`resolve_hive`, `hive_dir`, `all_hive_targets`) for [routing](PASSTHROUGH.md).
