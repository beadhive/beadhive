# v0.16.2 candidate certification

This handoff certifies the assembled `bh-g7pq2` workstream before the release bump. It does not
claim that v0.16.2 exists. Publication remains pending explicit operator approval after the
molecule lands on `main` and the landed tree is rechecked.

## Historical pre-handoff candidate

| Field | Value |
| --- | --- |
| Pre-handoff commit | `6823b4b70f5d11585638d3dfe5e83e3e543c10d9` |
| Pre-handoff tree | `2e51fdf19a70125a3f34f5f5a2ebdcd80274f682` |
| Parent release | `v0.16.1` (`11969f85212c9795f5a7e96d9f6d50e8221fe1fb`) |
| Full-gate receipt | `run-76c2620005bbf23b41726e933e0f1c5b` |
| Full-gate verdict | Green for `just check-all` at `2026-09-14T20:33:26+00:00` |

All six hardening children were independently reviewed and merged into this workstream before
certification began. The candidate adds no public CLI, schema, daemon-route, or authentication
contract.

## Validation

The focused matrix ran with both `PANTS_BIN` and `PYTHONPATH` absent. Its 272 passing tests cover
launcher discovery and exit 127, focused pytest imports, fleet-scoped push-main guidance,
version-sensitive digest refresh, signed exact-target tags and rollback, and the attested publish
workflow.

`just attest` validated the exact commit above from a detached clean checkout with those same
environment variables absent:

- mise resolved scie-pants 0.13.2 without a `PANTS_BIN` override;
- Pants 2.32.1 built `src/beadhive:bh` and passed all 58 qualified tests;
- the native non-integration phase passed 9,038 tests, skipped 12, and emitted one known warning;
- the real-`bd` integration phase passed 66 tests and skipped 2;
- both `demo-local-loop` and `demo-live-ingress` completed successfully; and
- every generated evidence check ran in check mode and reported current content.

## Distribution proof

An unchanged checkout built exactly one sdist and one wheel at the still-published project version
0.16.1. `twine check` passed both files:

| File | SHA-256 |
| --- | --- |
| `beadhive-0.16.1-py3-none-any.whl` | `acb919005f3aebce27329336354993b5ec2e74dcb527fbab31c4859054ddf9cb` |
| `beadhive-0.16.1.tar.gz` | `130b93cb571ce65a2dfd5a4df6583ff57d204119e93a704d1ac343b36057b7ed` |

The wheel was installed with its `otel` extra into an isolated Python 3.13 environment. The import
resolved from that environment's `site-packages`, `bh --version` reported 0.16.1, and the real
`bh-mcp` console entrypoint started successfully and exited cleanly on EOF.

These hashes prove the pre-bump candidate build only. The v0.16.2 bump changes version-bearing
files, so the release workflow must build and hash fresh distributions from the signed v0.16.2
tag; these pre-bump hashes must not be reused as published-file expectations.

## Read-only preview

Commitizen's dry run predicts exactly `0.16.2` with a patch increment. Its incremental changelog
contains only the Pants launcher, focused pytest import, release transaction, generated-proof,
scope-correct gate guidance, signed-tag, and PEP 740 publish hardening delivered by this molecule.

The exact-candidate `bh release preview --next` reported:

- tree `2e51fdf19a70` already passed `just check-all` under the receipt above;
- `v0.16.2` is not present on `origin`; and
- `beadhive` 0.16.2 is not present on PyPI.

The preview was run through a temporary `GIT_WORKSPACE` registration pointing at this candidate.
The default registered clone still points at released `main`, so a default workstream invocation
cannot compute the candidate's next version. The attempted developer escalation could not be filed
because this host has no registered HQ store; the dispatcher should preserve this limitation as
follow-up rather than treating the misleading default preview as evidence.

No bump, local release tag, remote push, workflow dispatch, or publication command ran during this
certification.

## Operator handoff

After the molecule lands, synchronize `main`, capture its actual identity for the release session,
and prove that it contains the reviewed certification handoff. Do not compare the landed tree with
the historical pre-handoff tree: the handoff commit itself necessarily changed that tree.

```bash
git switch main
git pull --ff-only origin main
landed_sha="$(git rev-parse HEAD)"
landed_tree="$(git rev-parse HEAD^{tree})"
printf 'landed main: %s\nlanded tree: %s\n' "$landed_sha" "$landed_tree"
git merge-base --is-ancestor 48d68fb2a98aa361f60218326c3043298f0ab03c "$landed_sha"
```

Establish and re-read a fresh `just check-all` verdict for that exact landed tree. Reconfirm that
the checkout did not move before crossing the local, reversible bump boundary, then inspect the
bump commit's new exact-tree gate:

```bash
BH_EXEC='uv run bh' just attest
uv run bh release preflight "$landed_sha" --gate 'just check-all'
test "$(git rev-parse HEAD)" = "$landed_sha"
test "$(git rev-parse HEAD^{tree})" = "$landed_tree"
BH_EXEC='uv run bh' just bump 0.16.2
uv run bh release await --gate 'just check-all'
git verify-tag v0.16.2
test "$(git rev-parse v0.16.2^{commit})" = "$(git rev-parse HEAD)"
BH_EXEC='uv run bh' just release-preview
```

`just release` is the one-way atomic push and still requires explicit operator approval. After that
approval and push, verify the remote refs, workflow, and both PyPI attestations:

```bash
BH_EXEC='uv run bh' just release
release_sha="$(git rev-parse v0.16.2^{commit})"
test "$(git ls-remote origin refs/heads/main | cut -f1)" = "$release_sha"
test "$(git ls-remote origin refs/tags/v0.16.2^{} | cut -f1)" = "$release_sha"
git verify-tag v0.16.2
run_id="$(gh run list --workflow release.yml --commit "$release_sha" --event push \
  --json databaseId --jq '.[0].databaseId')"
gh run watch "$run_id" --exit-status
uvx pypi-attestations verify pypi \
  --repository https://github.com/beadhive/beadhive \
  pypi:beadhive-0.16.2-py3-none-any.whl
uvx pypi-attestations verify pypi \
  --repository https://github.com/beadhive/beadhive \
  pypi:beadhive-0.16.2.tar.gz
```

The two final commands require PyPI's Integrity API to validate PEP 740 attestations bound to the
`https://github.com/beadhive/beadhive` workflow identity. Success is post-publish evidence; it is
not claimed by this workstream handoff.
