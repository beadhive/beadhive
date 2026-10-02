# Host provision publication: approved v2 draft amendment

Bead: **bh-0a889 — bh host provision --push publishes the new host manifest to HQ**.

## Decision — 2026-09-30

The operator confirmed that neither contract has been released or used and explicitly directed:
“for this change just make it directly on v2, don't do another migration”. This supersedes the
previous pending v3 planning proposal. No further migration or release is authorized here.

Apply the optional boolean `push` parameter at the end of `host.provision`, defaulting to
`False`, and accept answers-file `hq.push: true`. Preserve existing behavior unless publication
is explicitly requested. Keep catalog version **2.0.0**, installed release **2.0.0**, and the
current wire draft **2.1.0**. Remove the superseded, U4-only unpublished wire candidate **2.2.0**.
Regenerate the current catalog, installed bundle, inventory and evidence from their owners.
Preserve older wire releases and the installed compatibility baseline.

## Scope of the compatibility amendment

The wire gate normally rejects any existing operation shape edit and any in-place release
edit. The approved draft amendment recognizes only the exact final canonical declaration
`push` / boolean / optional / inherited privilege and the matching final CLI `push` projection
on `host.provision`, with five unchanged preceding parameters. Removing those two additions
must reproduce the complete prior operation. The in-place exception applies only to
`docs/schemas/wire/v2.1.0/operation-catalog-v1.json`; removing the additions must reproduce the
complete prior catalog. Catalog policy, other operations, schema artifacts, parameter removal,
requiredness/type/privilege edits, and unrelated in-place file edits retain their rejection.
This decision does not establish a general exception for optional parameter additions.

The installed append-only catalog policy already permits the appended parameter and therefore
requires no compatibility baseline reset. Publication, adoption, approval, merge and release
remain separate lifecycle actions.

## Behavioral evidence and validation

Real local Git tests cover isolated manifest publication, idempotent repeat publication,
two hosts racing through a non-fast-forward retry, and preservation of unrelated local
commits, staged edits, unstaged edits and untracked files. The earlier focused suite passed
101 tests; final validation and review evidence are recorded by the bead lifecycle gate.

The prior interrupted submission did not create a review gate. Separately, **hq-6zj2** records
`test_closure_operational_report.py` printing a common-Git evidence path using
`relative_to(ROOT)` outside a linked worktree. That unrelated tool issue is not changed here.
