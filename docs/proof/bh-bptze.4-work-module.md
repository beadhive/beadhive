# `bh-bptze.4` work capability extraction

## Boundary and baseline

The exact starting revision was `777fe96f4c0a50a1690223d4d779a4f3b5a33589` with tree
`25991ee3293a3c80f603e336b2ab27e8266db266`. It was clean and Good-signed. The selected
26-file legacy work closure passed 973 tests in 91.98 seconds before source edits.

The work candidate retained its recorded 10/14 score (`2,1,1,2,2,1,1`). The selected boundary
owns assignment through abandonment. It rejects a wholesale `work.py` lift, raw-bead transport
consolidation, LocalLoop/runtime movement, generalized CQRS, and changes to externally observable
Beadflow behavior.

## North-star and dependency direction

`beadhive.modules.work` owns immutable command-specific request/result types and the
`WorkLifecycleService` application boundary. Application code points inward to the following
outbound ports:

- `BeadStore` for bead authority and scheduling;
- `WorktreeLifecyclePort` for claim/resume provisioning;
- `ExecutionPort` for check, submit, and merge execution;
- `ValidationEvidenceStore` for review evidence;
- `IdentityProvider` for actor resolution; and
- `WorkNotifier` for completed-result publication.

Concrete compatibility adapters live outside the capability in `beadhive.work_services`. They
are constructed per operation so established `beadhive.work` monkeypatch points stay live. The
worktree adapter calls the existing claim/resume implementations, whose adopted worktree facade
is already composed over `beadhive.modules.worktrees`; this slice adds no Git, filesystem,
process, PID, plugin, or classifier implementation.

CLI lifecycle handlers and the MCP schedule projection enter the application service. Existing
text and JSON rendering remains in the outer facade/presentation adapters. The public
`beadhive.work` imports, one-return injected facade shape, review function module identity,
validation reuse, review gates, and merge implementation remain compatibility contracts.

## Live-work reconciliation

`bh-o6lsj — work merge: reconcile an already-landed bounced bead` owns the ordering inside
`work_merge.impl__merge_bead` and its two focused `tests/test_work.py` cases at live tip
`567ca343295abc658f808bbfd2522828307f01e5`. This extraction does not modify that implementation
or duplicate its reconciliation policy. The typed merge adapter calls the live legacy
implementation, so the external fix remains adoptable without a competing symbol.

## Isolation and validation evidence

The module imports no Typer, FastMCP, bead store, legacy work, plugin, filesystem, network, or
process runtime. Pure fake-port tests and an import/effect sentinel form its standalone test
artifact. Callback adapter contracts exercise every typed lifecycle request.

Recorded checkpoints:

- untouched legacy baseline: 973 passed;
- pure service transition: 5 passed;
- pure + adapter + independence transition: 9 passed;
- repaired facade/MCP focused set: 119 passed;
- closure/architecture/focused aggregate: 148 passed;
- exact Typer-sentinel repair selection: 10 passed; and
- registered `just test-module work`: 465 passed in 224.85 seconds; and
- strict post-migration 26-file legacy comparison: 973 passed in 82.79 seconds.

The checked import graph is 262 files and 2,415 edges with the same six owned legacy cycles and
278 cyclic edges as before the extraction. The closure registry is 21 present and two absent.
Ruff checked and format-checked 684 files, Markdown lint checked 180 files, and license and wire
schema checks passed.
The authoritative full `just check` remains submit-owned and is not replaced by these focused
checks.
