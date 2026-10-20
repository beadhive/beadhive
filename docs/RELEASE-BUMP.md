# Choosing the version with `just bump`

`just bump` is the local, reversible half of a release (signed release commit, changelog, tag).
`just release` stays the one-way step. The version is chosen one of three mutually exclusive ways:

| Form | Meaning |
|---|---|
| `just bump` | Commitizen computes the version from the commits. |
| `just bump --increment PATCH` | Commitizen computes it under your `cz bump` flags. The same flags go to the prediction (`scripts/next-version.sh --get-next`) and to the real `cz bump --changelog --gpg-sign`, so what is predicted is what is cut. |
| `just bump 0.28.2` | An exact version, passed to cz as its manual version. |

A version together with flags is refused. The resolved version and any operator overrides are
printed before anything changes (`cz overrides (operator): --increment PATCH`).
`just bump-preview [flags]` shows the same prediction read-only.

## Exact-version rule

The version must be an immediate successor of the current version (PEP 440, as the repo's
`version_scheme`). No escape hatch: skipping a range is out of scope.

- From a final `X.Y.Z`: the next patch, minor or major, or the first pre-release (`a0`, `b0`,
  `rc0`) of one of those.
- From a pre-release: its base final release, the next number of the same kind, or the first of a
  later kind (`a` then `b` then `rc`).
- Refused, listing the allowed successors: equal or lower versions, skipped versions, malformed or
  non-canonical versions, a `v` prefix, and a version whose local tag already exists.

## Refused cz flags

Flags that skip or change the single signed release commit, the changelog, the tag or the
release-files check: `--dry-run`, `--files-only`, `--version-files-only`, `--no-verify`,
`--changelog-to-stdout`, `--get-next`, `--tag-format`, `-h/--help` (also by unambiguous prefix),
and any bare positional (a manual version) among the flags.
