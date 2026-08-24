# Structural quality baseline

Status: complete

Evidence date: 2026-08-24 UTC

Live source commit: `c0baa210e6da5cd7d592f3ff2505107ee38ae544`
(`chore(merge): molecule bh-i96uz`, committed 2026-08-23T23:16:18Z)

This is the evidence ledger for `bh-1jhk4`. It fixes the source and test baseline before any
implementation is moved. Measurements here describe the named commit only; they are not targets
and must not be copied forward after the tree changes.

## Tool and selection record

| Item | Recorded value |
|---|---|
| Python | 3.11.15 |
| pytest | 9.1.1 |
| pytest-xdist | 3.8.0, `-n auto` (24 workers on this host) |
| pytest-cov | 7.1.0 |
| coverage.py | 7.15.3; JSON format 3; branch coverage disabled |
| RepoWise | 0.45.0 |
| RepoWise index commit | `c0baa210e6da5cd7d592f3ff2505107ee38ae544` |
| Selection | `pytest -n auto -m 'not integration' --cov=src/beadhive` |
| Result | 5,738 passed, 12 skipped, one asyncio subprocess-cleanup warning |
| Wall time | 121.03 seconds |

The dispatcher baseline index's `.repowise/state.json` records
`last_sync_commit=c0baa210...`, `written_by_version=0.45.0`, store format 2, and health analyzer
version 5. A supported forced incremental refresh from the stale SQLite provenance commit
(`repowise update --since df8d2b73... --index-only --no-workspace --no-cost-tracking`) updated
the repository row and index to the same `c0baa210...` commit in 56.9 seconds.

Coverage remains periodic and non-blocking. Closed bead `bh-rmem` landed the dependency and
`just cov` in `9a3d58f585e36c7c051ff9bb91ff88e9a0fa5af7`; its measured paired runs were 64.6 seconds
without coverage and 74.4 seconds with it (about 15% overhead). Adding that known recurring cost
to every submit and merge would answer a periodic structural question on every change. There is
also no evidence-backed failure threshold yet. `just check` remains the correctness gate;
coverage is regenerated for structural work and compared with this ledger.

## Coverage baseline

Percentages below are coverage.py's unrounded statement measurements. The numerator and
denominator are included so the displayed integer report cannot be mistaken for greater
precision.

| Scope | Covered / statements | coverage.py | RepoWise | RepoWise health | Missing |
|---|---:|---:|---:|---:|---:|
| all `src/beadhive` | 23,855 / 26,610 | 89.6467% | 89.65% | average 7.03 / hotspot 4.58 | 2,755 |
| `work.py` | 1,389 / 1,682 | 82.5803% | 82.58% | 1.96 | 293 |
| `config.py` | 950 / 984 | 96.5447% | 96.54% | 3.21 | 34 |
| `worktree.py` | 1,106 / 1,311 | 84.3631% | 84.36% | 2.79 | 205 |
| `localloop.py` | 723 / 842 | 85.8670% | 85.87% | 1.43 | 119 |
| `doctor.py` | 956 / 1,054 | 90.7021% | 90.70% | 1.68 | 98 |
| `cli.py` | 902 / 1,164 | 77.4914% | 77.49% | 2.51 | 262 |
| `safety.py` | 623 / 706 | 88.2436% | 88.24% | 1.53 | 83 |
| `onboard.py` | 572 / 582 | 98.2818% | 98.28% | 1.98 | 10 |

The machine-readable source for these values was
`/tmp/bh-1jhk4-1-coverage.json`, generated directly from this worktree. It is deliberately not
committed because coverage artifacts are generated evidence and are gitignored by the landed
coverage design.

### RepoWise population and execution constraint

RepoWise 0.45.0 populated coverage from the Cobertura report through its supported command:

```text
repowise coverage add /tmp/bh-1jhk4-1-coverage.xml \
  --path /tmp/bh-worktrees/github/beadhive/beadhive/bh-1jhk4 \
  --format cobertura -v
# Ingested coverage for 123 file(s) (123 exact, 0 resolved).

repowise coverage status \
  --path /tmp/bh-worktrees/github/beadhive/beadhive/bh-1jhk4 --format json
# file_count=123, covered_lines=23855, total_lines=26610,
# line_coverage_pct=89.65, source_format=cobertura,
# ingested_commit_sha=c0baa210e6da5cd7d592f3ff2505107ee38ae544
```

The original apparent hang was an execution-environment constraint, not a parser failure or a
locked database. Fault-handler dumps showed the main thread waiting in asyncio's selector while
the `aiosqlite` worker was completing a queued connection operation; direct synchronous SQLite
queries returned immediately. The managed sandbox denies the event-loop wakeup socket that
RepoWise's async SQLite driver needs. Running the same local RepoWise commands with the agent
environment's local-process escalation completed `coverage status` in 3.8 seconds and ingestion
in 4.3 seconds. Increasing the timeout inside the restricted sandbox would never address that
denied primitive.

After ingestion, `repowise health --format table --no-workspace` persisted coverage-aware health
metrics. The table above records every required hotspot exactly as RepoWise reported it. Branch
coverage and the per-test map remain unavailable because the periodic recipe measures statement
coverage without branch mode or `--contexts=test`; no value is invented for either limitation.

## Related-work reconciliation

The comparison target is this bead's actual delta: one evidence document and
`tests/test_structural_facade_contracts.py`; it changes no production source. “Blocking” below
means a downstream extraction must wait for that slice, not that the evidence-only work here
races it. Statuses were read from the shared bead store on 2026-08-24 UTC.

| Bead | State and disposition | Concrete path/symbol comparison |
|---|---|---|
| `bh-acvq` | open; **superseded** by closed `bh-rmem` plus this baseline's RepoWise ingestion | `bh-rmem` already added `pytest-cov` in `pyproject.toml` and `just cov` in `justfile` at `9a3d58f`; this bead consumes that recipe and populates the existing RepoWise index without adding coverage plumbing. |
| `bh-m8ts` | open; **proven non-overlapping** | Owns `safety.py` (`assess_retire`, `difficulty`, Dolt scanning). This molecule defers safety implementation; the baseline only measures it. |
| `bh-5sizy.3` | open; **proven non-overlapping** | Owns doctor/hive-status source, fact, view, and cache adoption. No `doctor.py`, `hive.py`, `host_lease.py`, `dolt_health.py`, or `metadata.py` source changes occur here. |
| `bh-il87m` | open; **proven non-overlapping** | Owns `doctor._bd_schema_skew_warnings` → `hive_schema.refresh` write-on-read behavior. This program makes no doctor implementation change. |
| `bh-8vx8c` | open; **proven non-overlapping** | Owns `safety._bd_dolt_mode`, `dolt_health.probe_raw_schema_version`, and the doctor false-alarm path. Those modules are measured, not edited. |
| `bh-e8s3i` | in progress; **proven non-overlapping** | Owns correlated run-journal launch context and live ingress around the existing state stream/`AgentRunSummary`; its inspected epic branch had no source delta at tip `4642281`. LocalLoop/ingress refactoring is deferred. |
| `bh-c6dk` | in progress; **proven non-overlapping** | Owns scheduler/runtime tiers behind `work.runtime` and LocalLoop process behavior. This program defers LocalLoop movement and changes no runtime source. |
| `bh-zbht5` | open; **blocking** downstream worktree lifecycle movement | Owns the terminal-disposition/classifier/reaping/prune/reclaim vocabulary. Worktree extraction must wait; this bead only freezes the current facade. |
| `bh-9yrn.2` | open; **blocking** `_classify_entry` movement | Owns precious-overlay integration into `wt_status` and `worktree._classify_entry`. The contract test records today's forwarding seam without changing classification. |
| `bh-bhsqp` | open; **blocking** affected cleanup slices | F6 is the unreachable `FileNotFoundError` branch in `worktree.run_init`; F8 covers dotted-parent reads in work/LocalLoop. This bead tests, but does not alter, `run_init`. |
| `bh-j4gbx` | open; **proven non-overlapping** | Owns the canonical operation catalog and generated CLI/MCP wire surfaces. Broad `cli.py` movement is deferred; facade consumers are merely recorded below. |
| `bh-8kn42` | open; **proven non-overlapping** | Owns consolidation of raw `bd` subprocess sites across CLI/onboard/hub and related modules. This bead neither moves transport code nor edits onboarding. |
| `bh-1h9h` | open; **blocking** the overlapping config foundation slice | Owns schema-derived `config.KNOWN_SECTIONS` and alias policy. Config extraction must preserve it and may not independently “fix” that list. |
| `bh-o6lsj` | in progress; **blocking** mutation-heavy work lifecycle movement | Its live branch changes exactly `src/beadhive/work.py` and `tests/test_work.py` for already-landed bounced-bead reconciliation (tip `567ca34`). Read/intake extraction can remain distinct; merge mutation movement waits. |

## Compatibility matrix

There is no `__all__` boundary today. “Supported” therefore means imported by production
CLI/MCP code or intentionally patched/called by tests. Extraction keeps these names on the old
module as forwarding wrappers and performs collaborator lookup through the old module when the
matrix says it is patchable.

| Facade | Importable names to preserve | Patch points to preserve | Observable contract | Consumers and executable proof |
|---|---|---|---|---|
| `beadhive.work` | Typer `app`; read verbs `brief`, `ready`, `issue`, `list_`, readiness/schedule payload builders; lifecycle verbs `assign`, `claim`, `check`, `submit`, `approve`, `merge`, `resume`; `WorkError`, `RefineResult` | module-local `bd`/`bd._run`, `config`, `registry`, `worktree`, `run`, telemetry collaborators | Read verbs preserve forwarded argv, stdout/stderr bytes, JSON shapes, and exit status. Lifecycle transitions, review gates, validation reuse, and payload keys remain unchanged. | CLI mounts `app`; MCP calls schedule/readiness payloads; `work_show` lazy-imports compatibility names. `test_structural_facade_contracts.py::test_work_issue_facade_executes_the_module_local_bd_patch_point`, plus `test_work_reads.py` and `test_work.py`. |
| `beadhive.config` | paths and layers (`home`, `config_path`, `fleet_path`, `load_host`, `load_fleet`, `load`, `save`, `save_fleet`); mutation payloads (`get_value`, `set_value`, `unset_value`); existing typed getters used throughout the package | `config_path`, `fleet_path`, `load_host`, `load_fleet`, `save`, `save_fleet`, schema/partition guards, round-trip YAML object | Allowed host values override fleet values without mutating either source; forbidden overrides fail by policy. Get/set/unset retain `{ok, problems, value}` or `{ok, problems, old, new}` shapes and byte-preserving writes. | CLI and MCP both expose config reads/writes; nearly every module calls typed getters. `test_structural_facade_contracts.py::test_config_load_facade_executes_layer_patch_points_and_preserves_precedence`, plus `test_config.py`. |
| `beadhive.worktree` | `run_init`, `ensure`, `locate`, `clean_checkout`, `status_rows`, `integration_base`, `base_of`, `commit_rows`, history/signature helpers; test-supported `_classify_entry`, `_pid_start` | module-local `run`, `_run_git`, `subprocess.run` for `_pid_start`, `config`, `registry`, `bd`, `metadata.read_fleet`, `wt_status.classify`, and classifier callbacks (`is_merged`, `bead_and_parent`, `is_landed`) | Branch/worktree layout and tuple return shapes remain stable; init stays ordered and best-effort; clean checkout validation remains isolated; classifier inputs and `WtStatus` payload/classification safety remain unchanged; PID token probing continues to bypass the generic runner seam. | Work lifecycle calls locate/ensure/validate/history helpers; CLI exposes worktree verbs; MCP consumes `status_rows` and `locate`. Three `test_structural_facade_contracts.py` worktree tests execute the named seams, with `test_worktree.py` and `test_wt_status.py` covering behavior. |

The focused facade tests are intentionally more than import checks: each installs a sentinel at
the documented old-module patch point, invokes the facade, and asserts the sentinel's argv,
payload, precedence, or callback result. A bare re-export that binds collaborators in a new
module will fail these tests even if the original name still imports.
