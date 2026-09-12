# bh-bptze.12 worktree-fixes adoption

## Provenance and approval boundary

This reconciliation starts from capability baseline
`bb1fcef254a79a03866bd0df62ed1d537713833f` (tree
`9cace758b19583ca863318adb4e7ac4bc97a51ce`) and merges approved standalone source tip
`c1e9711e35c7d087ea6b10191c5496a97e936d2f` (tree
`ec7d3377b383d5d7e24fb5f0f70503ad8694182b`) as its ordered second parent. The source has the
sole parent `739349806ead27c94282219b1befb796ba73b583`, the protected main revision.

`git verify-commit` accepted the baseline and source with the same ED25519 signer key
`SHA256:246Vja2yiNhU8XVlz0203XXVk2miVvlsHNVWgBteDgA`. Human review gate `bh-3z2aa` was resolved
as approved by `review/codex-worktree-leftovers-audit`. The exact source submit manifest is
`run-6b9451b75ed04a4808bfeccd560d77fe`: source SHA `c1e9711e...`, tree `ec7d3377...`,
`just check`, 6,955 passed, 11 skipped, zero failures. The capability baseline tree is
independently covered by green submit run `run-ff552d2e0e5309030c98819a60ad544b`.

Protected refs recorded before the merge were main `739349806ead27c94282219b1befb796ba73b583`,
root workstream `287061f089764dac29b5f584ebcdbb5f25f86c26`, and capability container
`bb1fcef254a79a03866bd0df62ed1d537713833f`. The standalone source bead remains in progress,
approved, unmerged, and unfinished; this adoption does not land it to main.

## North-star and reconciliation

The existing capability boundary remains authoritative:

- `worktree_verify.py` owns ordered, best-effort init execution behind the stable `worktree.py`
  facade and its `run` / `missing_binary` patch seams. A tagged synthetic 127 means the executable
  is absent; an untagged real child exit 127 remains an ordinary command failure.
- Molecule ownership consumers use `bd.children`, which adds `--limit 0`, filters the raw dotted-id
  prefix result by the real parent edge, and preserves each caller's prior options:
  `--include-infra --all` for loop/next membership, `--all` for review, and the default selection
  for schedule and collapsed-batch readiness.
- Exactly two raw dotted-id reads remain, both historical event streams rather than ownership
  queries: `work_metrics.flow_events` and the per-child event read in
  `LocalLoop.load_molecule`. Each site records that decision inline.
- CLI/MCP payloads, exit behavior, compatibility imports, and monkeypatch seams remain unchanged.

The mandatory `--no-ff --no-commit` merge had no textual conflict. One capability reconciliation
restored the source's docstring-only edit to `work.py`, leaving both `work.py` and `worktree.py`
facade bytes identical to the first parent; accepted runtime behavior stays in the owning
`work_dispatch.py` module. The canonical generated config dependency ledger was regenerated only
for shifted/new test call-site line evidence. No unrelated source behavior was adopted.

## Validation cadence and evidence

Balanced cadence applies because the change repairs ownership decisions across work and worktree
compatibility surfaces without changing public contracts. Evidence before the signed merge is:

- exact behavior plus the complete structural-facade suite: 14 passed;
- broader impacted work/worktree compatibility selection: 576 passed, with one harness failure
  because the initial high-capacity `TMPDIR` was inside `~/.beadhive`; the isolation guard
  correctly rejected that location;
- the exact failed isolation node plus suite-hermeticity and fence-cleanup proofs under a unique,
  Git-ignored workspace cache outside `/tmp` and `~/.beadhive`: 10 passed;
- config dependency ledger, schema artifacts, capability map, structural closeout, and wire-schema
  evidence: 67 passed;
- Ruff and formatting: all checks passed, 656 files already formatted;
- closure registry: 19 present and four absent;
- import boundaries: 242 files, 2,340 edges, six owned legacy cycles / 278 cyclic edges; and
- wire compatibility: initial release validated with no baseline at `main`.

Exactly one authoritative full `just check` remains submit-owned and will attest the signed
reconciliation tree before review.
