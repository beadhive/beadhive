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
| `.bh/testreport/<tree>/` | semantic canonical migration | latest legacy raw reports become artifacts in a deterministic imported shared repo-private `validation/runs/<run-id>/reports/` directory; bounded retry history becomes `validation/runs/.summary/<tree>/results.json`, preserving the derived per-tree flake view without making tree the run-storage identity |
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

## Canonical trees

The trees below are the complete Beadhive-owned layouts. Angle-bracketed leaves are identities,
not extra storage roots. Git's own refs, hooks, object database, `info/exclude`, worktree admin,
and per-worktree config remain Git-native.

```text
<git-common-dir>/bh/
├── build/
│   └── build-<pid>-<nonce>/                 # local-build scratch worktrees
├── release/
│   └── bump-gate.json                     # live background-gate control
├── validation/
│   ├── active/<run-id>.json               # derived liveness pointers
│   ├── migrations/*.json                  # idempotent import receipts
│   ├── runs/<run-id>/manifest.json        # authoritative execution facts
│   ├── uses/<use-id>.json                 # gate/reuse audit records
│   └── verdicts/<tree>/<command-hash>.json # reconstructable green index
└── worktrees/<worktree-id>/claim.json          # claim holder + fence generation
```

```text
<hive>/.bh/
├── release/bump-gates/<tree>/gate.log
└── validation/runs/
    ├── <run-id>/gate.log
    ├── <run-id>/reports/*
    └── .summary/<tree>/results.json

<worktree>/.bh/
└── observability/otel.env                       # local endpoint/profile overlay
```

## Ownership, retention, backup, and cleanup

| Subtree | Owner and authority | Retention / cleanup | Backup and CI behavior |
| --- | --- | --- | --- |
| `.git/bh/validation/runs`, `uses`, `verdicts` | validation runner; manifests and uses are audit facts, verdicts are derived | manifests remain; active pointers end with the run; raw artifacts follow the bounded policy in `VALIDATION-RECORDS.md` | include in a clone-private operational backup when validation audit history matters; never publish as source |
| `.bh/validation/runs` | validation artifact writer | running, newest red/retry, and verdict-referenced artifacts are protected; uploaded superseded raw data may be pruned | CI may set an absolute `BH_VALIDATION_ARTIFACT_ROOT`, uploads the complete run directory, then records upload before pruning |
| `.git/bh/worktrees` | claim authority | removed with the matching managed worktree; a record with no registered Git incarnation is stale | operational control state, not a portable source backup |
| `.git/bh/build` | `scripts/local-build.sh` | each invocation removes its own scratch; a later invocation sweeps only owners proven dead | build outputs go to tracked-checkout `dist/`; scratch is never a CI artifact or backup input |
| `.git/bh/release` + `.bh/release` | release attest/await | a live local or remote/unmeasurable gate is protected; dead control and ownerless logs are cleanup candidates | gate logs are diagnostics, not verdicts; archive explicitly when an incident requires them |
| worktree `.bh/observability` | worktree provisioner / loader | lasts with the worktree; canonical file wins, legacy `.bh/otel.env` is read-only fallback | contains local routing configuration; exclude from backups and CI artifacts |

`bh doctor` calls `private_paths.inventory_private_state()` and reports legacy paths, abandoned
validation state, orphaned artifacts, stale claims, and dead build/release leftovers. The
inventory is strictly read-only: path resolution and inspection do not create a private root,
and a live owner is omitted. Corrupt, unreadable, special, symlinked, outside-root, remote, or
otherwise unmeasurable state is reported separately as **PROTECTED — not a deletion candidate**.
Validation artifact paths are authorized only at the exact canonical
`.bh/validation/runs/<manifest-run-id>` identity; doctor never follows or prints an arbitrary
`artifacts.directory` supplied by a manifest. A corrupt or mismatched manifest protects the raw
directory with the same run identity, and an unmeasurable release marker protects every gate log.
An externally configured CI artifact root is likewise outside doctor's cleanup authority. Inspect
the named owner and use the subsystem cleanup (`bh worktree prune`, a subsequent local build
sweep, validation retention after CI upload, or the release recovery flow) instead of recursively
deleting a root.

After upgrading and verifying migration, a clean hive has neither Beadhive-owned top-level
`.git/bh-*` entries nor `.bh/testreport` / `.bh/otel.env`. The only Beadhive private roots are
`.git/bh/` and `.bh/`. `tests/test_private_path_policy.py` audits source constants and runtime
path construction—including named constants and statically computed concatenations—so a new
top-level hidden root or `.git/bh-*` sibling fails CI. Exceptions are exact triples of source
module, function context, and static path operand; there is no global `.ssh`, `.config`, `.git`,
or other root-name exemption that a different callsite can reuse. The pinned callsites cover
Git-native administration, Beads/Dolt state, harness/plugin installation, Repowise, and
user/workspace configuration; those tools retain ownership only in the named contexts.
