# beadhive-bd-cli

The `bd` CLI argv routes behind [`beadhive-core`](../beadhive-core/README.md)'s ports (bh-o3xuf,
split from bh-fqsp2). A **library package** under
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md): no
`plugin.json`, and it never imports the root `beadhive` distribution. It depends on
`beadhive-core` (the ports and typed errors it implements) and `beadhive-beads-client` (the
`UNSET` sentinel molecule filing reads), through their public `__all__` surfaces only.

## What lives here

Every `bd` argv route the API-first cutover (bh-sy36q) left in the root shell:

| Module | Routes | When the shell selects it |
|---|---|---|
| `lifecycle` | `CliIssues` (`work.issue.get` / `update`) | CLI-compatibility: no capable Beads session and `work.beads.route` allows `bd` |
| `lifecycle` | `CliLeases` (`work.lease.acquire` / `release`), `CliStateReads` (`work.state.get`) | Always: Beads 1.3 has no HTTP route |
| `review` | `CliGateOperations` (`work.gate.lookup` / `resolve`), `CliStateOperations` (`work.state.update`) | Always |
| `planning` | `CliMoleculeFiler` (`plan.batch-apply.atomic`) | CLI-compatibility |
| `planning` | `CliPlanningGates` (`plan.gate.create` / `plan.kickoff.update`) | Always |
| `reads` | `show`, `state`, `child_rows`, `children`, `ready`, `ready_rows` | CLI-compatibility reads of the migrated cohorts (dispatch state, schedule, claim-next, `bh work ready`), and the shell's own `bd.show` / `bd.children` / `bd.child_rows` / `bd.state` |
| `coordination` | `gate_*`, `merge_slot_*`, `heartbeat`, `reclaim` | Always: every `beadhive_core.COORDINATION_OPERATIONS` operation |
| `routes` | Named issue, gate, dependency, swarm, intake, contributor, checkpoint, worktree and presentation routes | Always when root composition still requires a CLI-only operation |

Because the no-API operations live here too (the CLI-only-operations decision recorded on
bh-fqsp2), the package is imported at runtime on essentially every claim, heartbeat, gate and
merge-slot operation — not only when a hive opts into `api+cli-fallback`. "Dormant" refers to its
test selection, not its runtime import frequency.

## The transport seam

A route only shapes argv and interprets output. The process is the caller's `BdTransport`
(`run(args, cwd, actor=, capture=, text_input=)` and `json(args, cwd, strict=)`):

- The root shell resolves this package **lazily, by name** (`beadhive.bd_cli`, the same
  `importlib` pattern `beadhive.beads_routing` uses for `beadhive_core`) and passes its own
  `beadhive.bd` module as the transport, so every route keeps running through the configured
  `Engine` (`-C <hive>` scoping, `--actor`, strict-read narration).
- `SubprocessBd` is the plain `subprocess` transport, used by this package's real-`bd` tests.

`err_line` (bd's significant failure line) and `names_bead` (the anchored gate-description
matcher, bh-1vvdp) are the one implementation of each; the shell's `bd.err_line` /
`bd.names_bead` forward here.

## Tests

`tests/` holds the moved FakeBd tests (`test_ports.py`, `test_reads.py`, `test_coordination.py`,
`test_routes.py`)
and the real-`bd` tests (`test_ports_real_bd.py`, `test_coordination_int.py`, marked
`integration`, self-skipping without `bd` on `PATH`). They run under the shared `packages` attest
key via `just packages-check` (a dedicated key is bh-vq34o):

```sh
uv run --locked --all-packages pytest packages/beadhive-bd-cli/tests
```

## Validation ownership

Changes under `packages/beadhive-bd-cli/**` select the package suite and the registered root
consumers, including the root boundary guard. Native gates use this root-composition mapping;
the optional Pants profile separately enforces its proven-test manifests.
