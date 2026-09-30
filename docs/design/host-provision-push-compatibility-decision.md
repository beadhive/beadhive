# Host provision publication: compatibility decision pending

Bead: **bh-0a889 — bh host provision --push publishes the new host manifest to HQ**.
Implementation checkpoint: `956826542294611784f890aae503e7d8c77da8db` on
`wt/bead/issue/bh-0a889`. Status remains `in_progress`; the branch has **not been submitted**.

## Evidence

The implementation adds optional `--push` after all existing `host.provision` parameters and
accepts answers-file `hq.push: true`. Real local Git tests cover isolated manifest publication,
idempotent repeat publication, two hosts racing through a non-fast-forward retry, and preservation
of unrelated local commits, staged edits, unstaged edits, and untracked files. The focused suite
passed **101 tests**. Commit hooks passed the convention checks.

The current operation catalog version is **2.0.0** in
[src/beadhive/kernel/operations/__init__.py](../../src/beadhive/kernel/operations/__init__.py).
The optional parameter extends both the canonical parameter list and CLI projection list from
**5 to 6**. The wire compatibility gate treats any existing operation shape change as breaking,
including this append. The candidate wire **2.2.0** directory and installed contract artifacts
are local review material; they have not been published or adopted.

The clean-checkout submission recorded architecture-contracts red in run
`run-52408ae6ea38202073a469c0d3d9d74b`, with:

```text
urn:beadhive:wire-catalog:operations:1 $.operations[name='host.provision'].parameters:
  list length changed from 5 to 6
urn:beadhive:wire-catalog:operations:1 $.operations[name='host.provision'].surfaces.cli.parameters:
  list length changed from 5 to 6
```

See [wire compatibility policy](../schemas/wire/README.md#add-a-cli-verb) and
[wire compatibility implementation](../../scripts/check_wire_schema_compat.py).
The policy requires a reviewed compatibility decision and a new major for changes to published
operation shapes. The supported `publish_wire_release.py --major` route requires the live catalog
major to match the new wire release major, which would require **catalog 3.0.0** here.
However, the installed official contract currently uses **RELEASE_VERSION 2.0.0** and its
append-only catalog baseline forbids changing the `catalog_version` value. See
[wire release generator](../../scripts/publish_wire_release.py) and
[official contract release](../../src/beadhive/contract_release.py).

Submission was canceled with SIGINT after the known policy failure, ending with **exit 130**.
The interrupted integration run `run-266ad93b19b8db990ba623c646b8a3b6` records reason `interrupted`
and verdict `none`. Its child processes exited and the host validation slot was released.
The completed bd-cli, packages, and demo keys were green; the remaining full gate was not completed.
No review gate, approval, merge, or release resulted from this attempt.

## Proposed morning planning decision

Review whether adding this optional CLI parameter should require the existing major migration
policy. If that policy is retained, plan one coordinated **catalog, wire, and installed official
contract v3** migration: preserve every published v2 directory and baseline, define v3 release
metadata and compatibility baselines, regenerate the v3 catalog/bundles/fixtures, and then resume
U4 against that agreed contract. Review consumer pinning and compatibility claims as part of the
migration. This proposal does not implement a migration or change a compatibility guard.

U4 remains deferred while the decision is pending. Its generated inventory/catalog artifacts may
conflict with U5's configuration bundle; regenerate combined artifacts during later integration.

Separately, **hq-6zj2** records a local generator print-path failure: after writing operational
evidence under the common Git directory, `test_closure_operational_report.py:375` calls
`EVIDENCE_PATH.relative_to(ROOT)` and raises `ValueError` because the evidence path is outside the
bead worktree. That issue is distinct from the compatibility blocker above. No fix for that
unrelated tool issue is included here.
