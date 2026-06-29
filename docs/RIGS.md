# Rigs — onboarding & identity

A **rig** is a repo's beads database. This covers turning a repo into a rig and how `ws`
derives its identity (modules: `rig.py`, `identity.py`; prefix logic in `registry.py`).

## Identity from the path

`ws` derives a repo's `(provider, org, repo)` from its location under the git-workspace root
(`$GIT_WORKSPACE`, default `~/workspace`): `<provider>/<org>/.../<repo>`. This is the fast
path used by `ws bd create` (the triplet) and `ws rig init` (registration). Outside that
layout, path-derived features degrade gracefully (`identity.py:workspace_identity` returns
`None`).

## `ws rig init`

Run **from inside the target repo**:

```sh
ws rig init [--prime] [--claude] [--kind K] [--prefix P] [--yes] [--dry-run]
```

Flow (`rig.py`):

1. Derive `provider/org/repo` from the path.
2. **Classify** the repo (`registry.classify`) → its *kind*.
3. Resolve/derive the **prefix** (`registry.derive_prefix`), or use `--prefix`.
4. **Required-org check** — if the org's policy is `required`, the prefix must start with
   `<code>-`; otherwise it's blocked (a registration invariant, always enforced).
5. `bd init --prefix <p> --skip-agents --skip-hooks --non-interactive`.
6. Register `{provider, org, repo, prefix, kind, upstream?}` in `config.yaml`.
7. Optionally install agent extras (`--prime`, `--claude`).

`--dry-run` prints the plan and changes nothing.

## Kinds (classification)

| Kind | Detected when | Prefix | beads |
|---|---|---|---|
| **org-native** | path org has `policy: required` | `<code>-<repo>` (enforced) | on |
| **personal** | personal account, kept long-term | `<code>-<repo>` (suggested) | on |
| **prototype** | personal account, org undecided (default) | bare `<repo>` | on |
| **fork** | `gh repo view` reports `isFork` | upstream identity | **off unless `--yes`** |

`registry.classify` checks, in order: excluded (refuse) → required-org → fork (via `gh`) →
personal-or-prototype. Forks are skipped unless `--kind fork --yes`; when opted in, their
identity reflects the **upstream** so they don't pollute org/personal rollups.

## Prefix derivation

`registry.derive_prefix` (mirrors the original `prefix` policy):

- `org-native` / `personal` → `<code>-<repo>`
- `prototype` → bare `<repo>`
- `fork` → `fork-<repo>`
- no kind → bare `<repo>` if globally unique, else `<code>-<repo>`

`<code>` comes from the org's registry entry, falling back to `sanitize(org)[:2]`. Names are
sanitized to `^[a-z0-9-]+$`. A prefix over 8 chars or one already in use produces a warning
(override with `--prefix`). The registry enforces global uniqueness.

Why provider isn't in the prefix and why it's stable: see [DESIGN](DESIGN.md#prefixes).

## Agent extras (independent, opt-in)

Both bundled in the package, merged non-destructively (existing hooks/denies preserved):

- **`--prime`** → installs `.beads/PRIME.md` (a trimmed beads issue-workflow doc).
- **`--claude`** → installs `.claude/settings.json`: a `SessionStart` hook running `bd prime`
  and a `deny` rule for `bd remember` (beads-as-issues-only).

Use either, both, or neither. Default `ws rig init` writes no agent files (it passes
`--skip-agents --skip-hooks` to beads).

## Helpers

```sh
ws rig classify <provider> <org> <repo>     # print the kind
ws rig prefix   <provider> <org> <repo> [kind]   # print the derived prefix
```

Registration, the registry schema, and how rigs are validated live in [LABELS](LABELS.md).
Spinning up isolated worktrees for a rig (per bead/branch/session) lives in
[WORKTREES](WORKTREES.md).
