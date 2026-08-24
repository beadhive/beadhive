# RepoWise dead-code and security signal audit

Status: complete

Evidence date: 2026-08-24 UTC

Live source commit: `c5e5b5757ab2de28248d7dc2e3f80ec57a03e018`
(`chore(merge): bead bh-1jhk4.1`)

This is the adjudication ledger for `bh-1jhk4.2`. It is an evidence audit, not a deletion
quota. No production symbol or file was deleted. A finding is only a candidate until live
references, runtime registration, compatibility obligations, and focused behavior tests agree.

## Provenance and method

RepoWise 0.45.0 was forced from the inherited `df8d2b73...` index through the 49-file delta to
the live commit above:

```text
repowise update /tmp/bh-worktrees/github/beadhive/beadhive/bh-1jhk4-2 \
  --since df8d2b73a03614a17e020bb4728ba434527a8d0c \
  --index-only --no-workspace --no-cost-tracking --progress json -v
# outcome=regenerated, duration=55.2s, Dead code findings: 42
```

Both `.repowise/state.json` and the knowledge graph then named `c5e5b575...`. The SQLite
`repositories.head_commit` field remained `df8d2b73...` even after the successful forced update;
that is a RepoWise 0.45.0 provenance-field defect, not hidden by this report. The regenerated
graph changed to 12,094 nodes / 36,239 edges and the command below reproduced 42 persisted
findings. Its presentation layer additionally derived one `zombie_package` signal for `docker`,
so the emitted inventory contains 43 rows and this ledger adjudicates all 43.

```text
repowise dead-code /tmp/bh-worktrees/github/beadhive/beadhive/bh-1jhk4-2 \
  --format json --include-internals --no-workspace
```

The verdict vocabulary is intentionally closed:

- **false positive** — a live direct call, callback, default, registry entry, or assignment the
  static graph failed to connect;
- **compatibility surface** — an operator/runtime entry point outside Python's import graph;
- **owned elsewhere** — real cleanup already has a bead and must not be duplicated here;
- **proven removable** — no live, test, reflection, registry, or compatibility reference was
  found. These are retained because this bead audits evidence rather than silently acquiring a
  deletion scope.

For every private symbol, the dynamic-use check included `git grep` for the name and inspection
of callback/default/registry construction plus `getattr`, `globals`, `locals`, and `importlib`
paths in its module. “None” in the overlap column means no existing bead claiming that symbol was
found; it does not mean the source lacks history.

## Validation command catalogue

| Code | Command / executable evidence |
|---|---|
| `V-index` | The two RepoWise commands above; confirms the refreshed inventory. |
| `V-script` | `uv run pytest -q tests/test_backfill_commit_linkage.py tests/test_prune_cadence_assets.py tests/test_hive_opencode.py` plus the documented `git grep` references named below. |
| `V-cli` | `uv run pytest -q tests/test_cli.py -k version` and the Typer callback binding in `src/beadhive/cli.py`. |
| `V-doctor` | `uv run pytest -q tests/test_doctor.py` and name/reflection `git grep` described above. |
| `V-routing` | `uv run pytest -q tests/test_model_routing.py`. |
| `V-observa` | `uv run pytest -q tests/test_observaloop.py`. |
| `V-onboard` | `uv run pytest -q tests/test_onboard_dag.py tests/test_onboard_external.py tests/test_onboard_plugin.py`. |
| `V-release` | `uv run pytest -q tests/test_release_order.py`. |
| `V-child` | `uv run pytest -q tests/test_bounded_child.py`. |
| `V-schedule` | `uv run pytest -q tests/test_schedule.py`. |
| `V-storage` | `uv run pytest -q tests/test_storage_migrate.py`. |
| `V-image` | `uv run pytest -q tests/test_image_build.py tests/test_compose_volumes.py`. |
| `V-sql` | `uv run pytest -q tests/test_doctor.py tests/test_dolt_health.py tests/test_hub_bulk.py`. |
| `V-process` | `uv run pytest -q tests/test_localloop.py tests/test_dispatch_hive_run.py tests/test_toolchain.py`. |
| `V-security` | `uv run pytest -q tests/test_credentials.py tests/test_config_routing.py tests/test_work.py`. |
| `V-full` | `just check`; final correctness gate for this ledger. |

## Dead-code decisions

Every row is one emitted candidate. The evidence column also records the dynamic-use check.

| ID | Candidate | Live-reference and dynamic-use evidence | Existing-bead overlap | Verdict | Validate |
|---|---|---|---|---|---|
| D01 | `scripts/backfill_commit_linkage.py` (file) | Executable with three documented invocations; loaded directly by `tests/test_backfill_commit_linkage.py`, not imported as a package. | None | compatibility surface | `V-script` |
| D02 | `scripts/bench_read_path.py` (file) | Manual benchmark invoked by `just bench-read-path` and cited by two read-path design documents. | None | compatibility surface | `V-script` |
| D03 | `scripts/bh-worktree-prune` (file) | Shell entry point used by the shipped systemd service and operator/cron instructions in `docs/WORKTREES.md`. | `bh-zbht5` owns later lifecycle vocabulary, not this installed wrapper. | compatibility surface | `V-script` |
| D04 | `scripts/demo_local_loop.py` (file) | Runnable hermetic demo invoked by `just demo-local-loop` and linked from `docs/WORK.md`; scripts are roots, not import targets. | `bh-c6dk` owns runtime-tier behavior; deleting its proof surface here would overlap. | compatibility surface | `V-script` |
| D05 | `scripts/measure_doctor_sources.py` (file) | Reproducible manual profiler, invoked and interpreted by `docs/design/read-path-source-measurement.md`. | None | compatibility surface | `V-script` |
| D06 | `scripts/profile_fleet_health.py` (file) | Reproducibility harness cited with its exact invocation in `docs/METADATA-CACHE.md`. | None | compatibility surface | `V-script` |
| D07 | `scripts/profile_metadata_rollup.py` (file) | Manual attribution harness cited by `docs/BH_DATA_PIPELINE.md`; its module docstring carries the invocation. | None | compatibility surface | `V-script` |
| D08 | `src/beadhive/assets/opencode-plugins/bh-steer.js` (file) | Runtime asset loaded by constructed path in `hive._install_bd_steer_opencode`; install tests assert the copied plugin. | None | false positive | `V-script` |
| D09 | `scripts/measure_doctor_sources.py:_traced` | Assigned directly as `subprocess.run = _traced` inside `main`; no name lookup or reflection is needed. | None | false positive | `V-script` |
| D10 | `src/beadhive/cli.py:_version` | Passed by reference as Typer's eager `--version` callback in `_root`; CLI behavior exercises it. | None | false positive | `V-cli` |
| D11 | `src/beadhive/doctor.py:_section_node_id` | Only its definition survives; `doctor()` calls `_render_node_id(data["node_id"])` directly. No section registry/reflection path exists. | None | proven removable | `V-doctor` |
| D12 | `src/beadhive/doctor.py:_section_beads_role` | Only its definition survives; collection/rendering use `_data_beads_role` and `_render_beads_role` directly. No dynamic lookup exists. | None | proven removable | `V-doctor` |
| D13 | `src/beadhive/doctor.py:_section_dispatch` | Only its definition survives; `_collect` and `doctor` use the split data/render functions directly. No dynamic lookup exists. | None | proven removable | `V-doctor` |
| D14 | `src/beadhive/doctor.py:_section_harness_plugin` | Only its definition survives; payload and text paths use `_data_harness_plugin` / `_render_harness_plugin`. | None | proven removable | `V-doctor` |
| D15 | `src/beadhive/doctor.py:_data_disk_usage` | Direct `_timed(..., _data_disk_usage, ...)` call in `_collect`; RepoWise missed a function passed as an argument. | None | false positive | `V-doctor` |
| D16 | `src/beadhive/model_routing.py:_openai_models` | Dataclass field default `fetch: GatewayModelFetcher = _openai_models`; instances call `self.fetch`. No reflection involved. | None | false positive | `V-routing` |
| D17 | `src/beadhive/observaloop.py:_Unavailable` | Name appears only at the class definition. `_resolve_command` now returns `None`; no raise, catch, registry, or reflection path remains. | None | proven removable | `V-observa` |
| D18 | `src/beadhive/onboard.py:_chk_valid_triplet` | Registered as the `resolve` step's `Check.predicate` in `build_steps`; the DAG invokes predicates indirectly. | None | false positive | `V-onboard` |
| D19 | `src/beadhive/onboard.py:_chk_clone_url_present` | Registered in the `clone` step's check list with an applicability predicate. | None | false positive | `V-onboard` |
| D20 | `src/beadhive/onboard.py:_chk_clone_url_reachable` | Registered in the `clone` step's check list; optional network policy is encoded by the `Check`. | None | false positive | `V-onboard` |
| D21 | `src/beadhive/onboard.py:_chk_parent_writable` | Registered in the `clone` step's check list and executed through the check runner. | None | false positive | `V-onboard` |
| D22 | `src/beadhive/onboard.py:_chk_under_git_workspace` | Registered in the `identity` step with `repo_present` applicability. | None | false positive | `V-onboard` |
| D23 | `src/beadhive/onboard.py:_chk_not_excluded` | Registered in the enabled `classify` step's check list. | None | false positive | `V-onboard` |
| D24 | `src/beadhive/onboard.py:_chk_external_no_furnish` | Registered as a non-overridable `bd-init` precondition. | None | false positive | `V-onboard` |
| D25 | `src/beadhive/onboard.py:_chk_furnish_needs_ownership` | Registered as the second `bd-init` precondition and behavior-tested for ownership failure. | None | false positive | `V-onboard` |
| D26 | `src/beadhive/onboard.py:_chk_prefix_policy` | Registered in the `prefix` step's check list. | None | false positive | `V-onboard` |
| D27 | `src/beadhive/onboard.py:_chk_prefix_change_needs_yes` | Registered in the `prefix` step as the destructive-change confirmation gate. | None | false positive | `V-onboard` |
| D28 | `src/beadhive/onboard.py:_chk_dirty_tree` | Registered in `worktree-clean` with `unclean_applies`; focused DAG tests execute the gate. | None | false positive | `V-onboard` |
| D29 | `src/beadhive/onboard.py:_chk_on_default_branch` | Registered beside dirty-tree in `worktree-clean`. | None | false positive | `V-onboard` |
| D30 | `src/beadhive/onboard.py:_noop` | Shared action callback for four assessment-only `Step` objects. | None | false positive | `V-onboard` |
| D31 | `src/beadhive/onboard.py:_act_clone` | Action callback of the mutating `clone` step. | None | false positive | `V-onboard` |
| D32 | `src/beadhive/onboard.py:_act_register` | Action callback of the `register` step. | None | false positive | `V-onboard` |
| D33 | `src/beadhive/onboard.py:_do_agents` | Wrapped by `_installer("agents", _do_agents)` and installed as the `agents` step action. | None | false positive | `V-onboard` |
| D34 | `src/beadhive/onboard.py:_do_opencode` | Wrapped by `_installer("opencode", _do_opencode)`; it installs D08 dynamically. | None | false positive | `V-onboard` |
| D35 | `src/beadhive/onboard.py:_do_observaloop` | Wrapped by `_installer("observaloop", _do_observaloop)`; the wrapper records completion. | None | false positive | `V-onboard` |
| D36 | `src/beadhive/onboard.py:_act_footprint` | Action callback of the final `footprint` step. | None | false positive | `V-onboard` |
| D37 | `src/beadhive/onboard.py:_act_hq_parent` | Action callback of the warn-only `hq-parent` step. | None | false positive | `V-onboard` |
| D38 | `src/beadhive/onboard.py:_act_hub_sync` | Action callback of `hub-sync`; its step is enabled from `Ctx.hub_sync`. | None | false positive | `V-onboard` |
| D39 | `src/beadhive/release_order.py:_stable_versioning` | Value in `_STRATEGIES[DEFAULT_STRATEGY]`; `order_beads` resolves and invokes the scorer dynamically. | None | false positive | `V-release` |
| D40 | `src/beadhive/run.py:_die_with_parent` | Passed as `subprocess.run(..., preexec_fn=_die_with_parent)` on Linux; focused tests capture the exact callback. | None | false positive | `V-child` |
| D41 | `src/beadhive/schedule.py:_model_rank` | Passed directly as `max(..., key=_model_rank)` by the deprecated compatibility helper. | None | false positive | `V-schedule` |
| D42 | `src/beadhive/storage_migrate.py:_backup_root` | Only its definition survives after callers moved to `backup.migrate_set_dir`; no test patch, dynamic lookup, or compatibility export remains. | None | proven removable | `V-storage` |
| D43 | `docker` (`zombie_package`, presentation-derived) | Docker build context, not an importable package; bake/Compose recipes and image contract tests consume its files. | None | compatibility surface | `V-image` |

The 21 onboard rows are deliberately individual: the scanner reported individual symbols, and
the audit must not turn “the module has a registry” into an unverified blanket exemption. Each
named callable appears as a concrete `Step`, `Check`, or `_installer` argument in `build_steps`.

## Security, SQL, and process decisions

`repowise update` persisted 24 security rows: nine lexical `exec_call` rows, five `fstring_sql`
rows, two test-secret rows, two auth-name rows, and six weak-hash-name rows. The working-tree
`repowise security scan --format json` command correctly reports
that scanning is performed by update; this table adjudicates the persisted result.

| ID | RepoWise signal | Live evidence and decision | Verdict | Validate |
|---|---|---|---|---|
| S01 | `doctor.py:394` `fstring_sql` | Database identifiers round-trip through `sanitize_database_name`; epic IDs must fully match `_EPIC_ID_RE`. Unsafe/unanswered items are omitted from the bulk map and `_orphan_container_branches` falls back to per-hive `bd show`. | false positive | `V-sql` |
| S02 | `dolt_health.py:975` `fstring_sql` | Database identifiers are retained only when equal to `sanitize_database_name`; missing/failed results are partial and callers fall back to per-hive schema probes. | false positive | `V-sql` |
| S03 | `dolt_health.py:1038` `fstring_sql` | Same identifier round-trip; a missing prefix remains unanswered and `_data_prefix_mismatches` falls back to `bd config get issue_prefix`. | false positive | `V-sql` |
| S04 | `hub_bulk.py:263` `fstring_sql` | Tables come only from the fixed `CONTENT_TABLES`; columns come from `DESCRIBE` of the same bd-managed schema; the database must be a freshly observed server database selected by `co_located_database`. Copy failure leaves the hive registered for `bd repo sync`. | compatibility surface | `V-sql` |
| S05 | `tests/test_hub_bulk_int.py:124` `fstring_sql` | Integration-test query over fixture-owned table/column identifiers, never a product input path. | false positive | `V-sql` |
| S06 | `docs/design/toolchain-declaration.md:58` `exec_call` | Prose naming an argv API, not executable code. | false positive | `V-process` |
| S07 | `docs/spikes/bh-a7so.3-codex-provider.md:67` `exec_call` | Historical design quotation, not executable code. | false positive | `V-process` |
| S08 | `docs/spikes/bh-a7so.7-graceful-interrupt.md:392` `exec_call` | Interface documentation, not executable code. | false positive | `V-process` |
| S09 | `cli.py:657` `exec_call` | Typer help text containing the word “exec”, not a call site. | false positive | `V-process` |
| S10 | `dispatch_hive_run.py:202` `exec_call` | Builds a token list and calls `asyncio.create_subprocess_exec(*argv, ...)`; no shell or interpolation. Tests capture the exact forwarded argv. | false positive | `V-process` |
| S11 | `localloop.py:402` `exec_call` | Converts a `Sequence[str]` to tokens and calls `create_subprocess_exec(*tokens, ...)` with `start_new_session=True`; cancellation tests prove process-group behavior. | false positive | `V-process` |
| S12 | `mcp.py:1063` `exec_call` | Explicit agent tool accepting `list[str]`; delegates to `toolchain.exec_entrypoint`, which forwards the list to the shared `run()` seam without `shell=True` and rejects empty argv. | compatibility surface | `V-process` |
| S13 | `triage_store.py:102` `exec_call` | Comment containing “at exec”, not a call site. | false positive | `V-process` |
| S14 | `tests/test_role.py:200` `exec_call` | Test function name describing an exec boundary, not an invocation. | false positive | `V-process` |
| S15 | `tests/test_config_routing.py:113` `hardcoded_secret` | Deliberately fake value passed to a model expected to reject `api_key`; the test proves credentials cannot enter tier config. | false positive | `V-security` |
| S16 | `tests/test_credentials.py:60` `hardcoded_secret` | Deliberately fake canary whose absence from every report field is the test assertion. | false positive | `V-security` |
| S17 | `dep_cli.py:233` `security_sensitive_symbol` | Public `bh dep auth` command name; it reports credential provenance and never a value. | false positive | `V-security` |
| S18 | `deps.py:125` `security_sensitive_symbol` | Typed `Auth` declaration describing environment-variable names and probes; values are read only by `credentials`. | false positive | `V-security` |
| S19-S24 | `tests/test_work.py` `weak_hash` on `sha1` / `sha2` (six lines) | Ordinal local names for the first and second full 40-character Git commit IDs. There is no SHA-1 algorithm invocation or password hashing. | false positive | `V-security` |

The process finding is therefore not “subprocesses are absent.” They are intentional and tested.
The security property is that command boundaries remain argv-based: `create_subprocess_exec` and
`subprocess.run(list)` receive tokens, while no `create_subprocess_shell` or `shell=True` path is
present in these call chains.

## Existing-work boundary

`bh-bhsqp` was read directly before deciding scope. Its F6 owns the unreachable
`FileNotFoundError` branch after `worktree.run_init(..., check=False)` and restoration of the
missing-binary message through `run.missing_binary`; its F8 owns remaining dotted-parent reads.
Neither issue is one of RepoWise's 42 persisted symbol/file findings. They are real, classified
**owned elsewhere**, and intentionally not folded into D01-D43 or changed here.

The six **proven removable** rows are similarly retained. A cleanup bead can remove them with
focused behavior tests and `just check`; this audit does not convert absence of evidence into
permission to delete. All other emitted candidates have an evidenced runtime or compatibility
consumer and must remain unless that consumer is intentionally retired first.
