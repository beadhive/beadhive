# Beadhive private-path inventory

This is the migration inventory for Beadhive-owned private state.  The only
canonical roots introduced by `bh-22nb6.1` are:

| Root | Resolver | Ownership |
| --- | --- | --- |
| `<git-common-dir>/bh/` | `private_paths.git_private_root()` | shared control metadata |
| `<hive>/.bh/` | `private_paths.repo_private_root()` | artifacts and caches shared across every worktree |
| `<worktree>/.bh/` | `private_paths.worktree_private_root()` | explicitly worktree-local overlays only |

Resolution is read-only.  Writers choose `ensure_git_private_root()` or
`ensure_repo_private_root()` at their write seam, or
`ensure_worktree_private_root()` for the explicit local-overlay exception.  A
bare repository, missing checkout, or malformed Git metadata is a
non-destructive `None` result; it must not cause a directory to be created.

| Current path | Classification | Canonical migration target / reason |
| --- | --- | --- |
| `.git/bh-validation-ledger.json` | semantic canonical migration | decompose rows into authoritative git-private `validation/runs/<run-id>/manifest.json` records and, only for qualifying completed-green runs, the reconstructable git-private `validation/verdicts/<tree>/<command-hash>.json` index; there is no replacement flat ledger |
| `.git/bh-release-bump-gate.json` | canonical migration target | git-private `release/bump-gate.json` lifecycle and ownership record |
| `.git/bh-release-bump-gate.log` | canonical migration target | shared repo-private `release/bump-gates/<tree>/gate.log` diagnostic artifact; the control record references it and artifact loss does not alter lifecycle or verdict |
| `.git/bh/validation/active/<verify-worktree-id>.json` | compatibility / derived index | git-private `validation/active/<run-id>.json`; the 0.15.1 worktree-keyed marker is a migration input for reconstructable liveness cleanup, not validation-run truth |
| `.git/bh-build/` | canonical migration target | git-private `build/` |
| own git-dir `bh-claim.json` (`.git/worktrees/<worktree>/` for linked worktrees) | canonical migration target | git-private `worktrees/<worktree-id>/claim.json`; the stable worktree id selects the record, with no extra `claims/` segment |
| `.bh/testreport/<tree>/` | semantic canonical migration | legacy per-tree reports become artifacts in a fresh shared repo-private `validation/runs/<run-id>/reports/` directory; the tree is recorded in the run manifest rather than retained as the artifact-directory identity |
| `.bh/otel.env` | canonical migration target | worktree-local `observability/otel.env` overlay |
| `.bh-verify.json` in a throwaway verification checkout | compatibility / derived index | legacy read-only orphan input for git-private `validation/active/<run-id>.json`; never write a lifecycle marker into a verification checkout |
| `.git/refs/`, `HEAD`, `config`, `hooks/`, `objects/`, `logs/`, `worktrees/` | Git-native exception | Git administration, never a Beadhive private-root migration target |
| `.git/info/exclude`, `.git/info/attributes` | Git-native exception | Git's local ignore / attribute administration; Beadhive may use them but does not relocate them |
| `.beads/` (including metadata and embedded-store paths) | other-tool-owned exclusion | `bd`/Beads-owned state; storage migration has its own explicit workflow |
| `.repowise/`, `.claude/`, `.orca/`, `.baml/` | other-tool-owned exclusion | respective tool-owned local state |

`private_paths.LEGACY_PRIVATE_PATHS` is the code-level mapping for the Beadhive
paths above.  Its values carry one or more typed canonical targets plus the
migration semantics, so a one-to-many migration cannot be mistaken for a file
rename.  Future migration beads extend that map rather than adding a fresh
private-path spelling at call sites.
