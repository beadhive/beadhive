# Labels, the registry & validation

The registry (`config.yaml`) is the source of truth for the label taxonomy; `ws labels`
manages and validates it (modules: `registry.py`, `validate.py`).

## The label model

- **Identity triplet** `provider:` / `org:` / `repo:` — on every issue, applied automatically
  by `ws bd create` from the rig's *registered* identity (not naively from the path; forks
  carry their upstream's identity). Labels are how you slice the aggregated [hub](HUB.md),
  since `bd list` has no prefix filter.
- **Dimensions** — orthogonal axes under `dimensions:` in config. Each is **open** or
  **closed** by whether it declares `values:`:
  - no `values:` → open (any value), e.g. `component:`, `tag:`
  - `values: [a, b]` → closed (only those pass validation), e.g. `size: [xs,s,m,l,xl]`
  - `values: []` → closed but **reserved** (nothing valid yet — locks the dimension)

  Closed-dimension checking is generic — it applies to *any* dimension with `values:`, not a
  hard-coded set (`registry.closed_dimensions`).

## `ws labels`

| Command | Does |
|---|---|
| `validate` | lint the current rig (or hub) DB against the registry |
| `sync` | reconcile the registry vs git-workspace: onboarding candidates, prefix collisions, required-org violations |
| `report` | usage counts per dimension (identity triplet + every configured dimension) |
| `allowed` | print the allowed label set (providers, orgs, repos, closed-dim values) |
| `docs` | regenerate `~/.ws/labels.md` from the registry |

Providers shown by `allowed`/`docs` are the **effective** set (config ∪ git-workspace when
enabled — `registry.effective_providers`).

## Validation rules (`ws labels validate`)

Against `bd list --json` for the target DB:

1. **Required-org prefixes** — every rig under a `policy: required` org uses its `<code>-`
   prefix (registry-level; `registry.required_violations`).
2. **Triplet consistency** — an issue's `provider:`/`org:`/`repo:` labels must match the
   registered identity of the rig its prefix belongs to (longest-prefix match wins).
3. **Closed dimensions** — any `phase:`/`size:`/… value outside its declared set is flagged
   (`bad-<dim>:…`).
4. **Unknown prefix** — an issue whose prefix isn't registered is flagged.

If `bd`/the DB is unreachable, per-issue checks are **skipped with a note** (not silently
treated as clean); registry-level checks still run.

## Enforcement

Enforcement is fixed behavior, not configurable (there is no `enforcement:` block):

| Surface | Behavior |
|---|---|
| `ws labels validate` | **the linter** — defaults to **enforce** (non-zero exit on any violation); `--advisory` reports and always exits 0 |
| `ws rig init` | **always** blocks a required-org prefix that doesn't match `<code>-` |
| `ws bd create` | **always** refuses to create in a rig that has label violations |

Rationale: invariants (required-org prefix at registration; a clean rig before adding to it)
are always enforced; only the *linter* is a reporting-vs-failing toggle.

## Registry operations (internal)

`registry.py` also provides the building blocks used by [RIGS](RIGS.md) and
[INTEGRATIONS](INTEGRATIONS.md): `classify`, `derive_prefix`, `register` (comment-preserving
upsert into `managed_repos`), `repos_sync`, `effective_providers`, and rig resolution
(`resolve_rig`, `rig_dir`, `all_rig_targets`) for [routing](PASSTHROUGH.md).
