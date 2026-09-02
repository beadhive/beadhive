# E5 worktree-lifecycle wave exit

This is the checked release proof for `bh-zbht5`, assembled on its container branch before the
exit bead is submitted. The wave is **MINOR** (`release:feature`): its range contains feature
commits and no breaking change.

## Boundary

| Field | Value |
|---|---|
| Integration base | `739349806ead27c94282219b1befb796ba73b583` |
| Assembled source tip | `60bf76f659f4a7ffd2da132bfa7ae1786d7d40a3` |
| Assembled source tree | `01168349e66ebc66e3b0a16cc4ed22887ef6a4df` |
| Implementation commits | 5 |
| Merge bubbles | 5 |
| Signature verdict | all 10 commits are `%G? = G` |

The direct child set contains these five implementation members, this exit bead, and two closed
kickoff event records. There are no other implementation members.

## Member proof

| Bead | Lifecycle | Release label | Implementation commit | Type |
|---|---|---|---|---|
| `bh-kx1x` | closed, `merged` | `release:feature` | `a6e8de98f9286a5f41bcb04c8a949617c6f4e620` | `feat` |
| `bh-c5go4` | closed, `merged` | `release:feature` | `814907f1516dc7ee9239093789e8ecfdf834e74a` | `fix` |
| `bh-1pspf` | closed, `merged` | `release:feature` | `d25d382da974f66ab71d1330faf7134fd018adf7` | `feat` |
| `bh-dyk7` | closed, `merged` | `release:feature` | `54c3f7c2d5016087bf603d05187998dbcf06c5c7` | `fix` |
| `bh-s3dd` | closed, `merged` | `release:fix` | `b7ae0886e731dc17fb4f3d1702d974e45c7067b4` | `fix` |

`bh-s3dd` keeps its individually correct patch label. The four feature-surface members and the
aggregate epic/exit carry `release:feature`; aggregate precedence is therefore MINOR. The full
source range has two `feat`, three `fix`, and five generated `chore(merge)` commits. It has no
`type(scope)!:` subject and no `BREAKING CHANGE` footer.

## Aggregate gate

The exit is handed off with:

```bash
BH_WORKTREES=/tmp/bh-worktrees \
  bh work submit --group bh-zbht5.1 \
  --as dev/codex-worktree-lifecycle-exit \
  --hive github/beadhive/beadhive
```

That command runs the configured `just check` from a clean checkout and records the tree-keyed
verdict. Its recorded verdict, not an ad-hoc run copied into this document, is the authoritative
aggregate-suite result used by review and merge.
