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

## Closeout at the assembled container tip

Closeout source commit: `cf0aeb417f03104f69a27a8bc805a5baea78ec4a`
(`chore(merge): bead bh-1jhk4.11`, measured 2026-08-24 UTC). This is the assembled container tip
after children `.1` through `.11`; the two deterministic closeout tests described below are the
only subsequent changes in `.12` before this ledger was written.

The same Python, pytest, xdist, pytest-cov, coverage.py, worker count, selection, and statement
coverage mode recorded in the baseline produced 5,944 passed, 12 skipped, and the same known
asyncio subprocess-cleanup warning in 108.87 seconds. The machine-readable sources are
`/tmp/bh-1jhk4-12-final-coverage.json` and `.xml`; like the baseline artifacts they are generated,
gitignored evidence rather than repository inputs.

### Before/after structural result

Physical lines count the checked-in files with `wc -l`; executable statement counts and coverage
come from coverage.py. RepoWise 0.45.0 was explicitly refreshed to the closeout commit: its state
records `last_sync_commit=cf0aeb41...`, 3,813 knowledge-graph nodes, four layers, and 12 tour
steps. Its full analysis indexed 12,636 internal nodes and 36,176 edges. The health comparison is
between the baseline table above and that exact-tip refresh.

| Scope | Physical lines before -> after | Statements before -> after | Coverage before -> after | RepoWise health before -> after |
|---|---:|---:|---:|---:|
| all `src/beadhive` | n/a | 26,610 -> 27,482 | 23,855 / 26,610 (89.6467%) -> 24,681 / 27,482 (89.8079%) | average 7.03 / hotspot 4.58 -> average 7.34 / hotspot 5.54 |
| `work.py` facade | 4,044 -> 1,749 (-56.8%) | 1,682 -> 469 | 82.5803% -> 95.3092% | 1.96 -> 4.81 |
| `config.py` facade | 2,613 -> 363 (-86.1%) | 984 -> 167 | 96.5447% -> 99.4012% | 3.21 -> 5.18 |
| `worktree.py` facade | 2,917 -> 1,477 (-49.4%) | 1,311 -> 672 | 84.3631% -> 89.2857% | 2.79 -> 4.07 |

The program changed 38 files by 9,594 insertions and 6,628 deletions; production source accounts
for 8,101 insertions and 6,612 deletions. That churn is intentionally concentrated in extracting
service modules and executable boundary tests. The three old import locations remain facades, so
their broad caller fan-in and test patch points do not migrate to every service file. Work now
delegates reads, intake, assignment/dispatch, submission, merge/refine, grouping, and shared
logic; config delegates paths/store, editing/partition, schema/policy/validation, release, and
typed settings; worktree delegates verification/git, inventory/cleanup, and merge mechanics.

The compatibility matrix remains the dependent-risk control. Five executable facade contracts,
the claim-fence ownership contract, and the config/work/worktree boundary suites prove that
historical imports and module-local patch seams still execute through their real owners. The
RepoWise refresh retained four architectural layers and reported no new conformance category.
The exact-tip dead-code presentation contained the same nine audited compatibility, demo,
profiling, asset, and fixture signals classified in `repowise-signal-audit.md` (D01-D08 and D43),
with zero unused exports. There is therefore no new production dead-code or cycle disposition to
hide behind a threshold; intentional facade back-edges remain compatibility seams.

### Observed coverage floors, not global thresholds

These values are the closeout observations for files created or retained by this structural
program. They are review baselines, not a global percentage gate. A later change below one of
these per-file observations must explain the exercised behavior and update this ledger; it must
not lower a repository-wide threshold or copy a rounded terminal percentage.

| Work boundary | Covered / statements | Observed floor |
|---|---:|---:|
| `work.py` | 447 / 469 | 95.3092% |
| `work_assignment.py` | 110 / 120 | 91.6667% |
| `work_dispatch.py` | 155 / 186 | 83.3333% |
| `work_group.py` | 284 / 338 | 84.0237% |
| `work_guards.py` | 68 / 71 | 95.7746% |
| `work_intake.py` | 9 / 35 | 25.7143% |
| `work_logic.py` | 274 / 297 | 92.2559% |
| `work_merge.py` | 338 / 411 | 82.2384% |
| `work_metrics.py` | 107 / 109 | 98.1651% |
| `work_next.py` | 162 / 162 | 100.0000% |
| `work_reads.py` | 163 / 192 | 84.8958% |
| `work_refine.py` | 15 / 84 | 17.8571% |
| `work_show.py` | 120 / 141 | 85.1064% |
| `work_submission.py` | 224 / 260 | 86.1538% |

| Config boundary | Covered / statements | Observed floor |
|---|---:|---:|
| `config.py` | 166 / 167 | 99.4012% |
| `config_edit.py` | 185 / 189 | 97.8836% |
| `config_partition.py` | 25 / 25 | 100.0000% |
| `config_paths.py` | 103 / 107 | 96.2617% |
| `config_policy.py` | 50 / 50 | 100.0000% |
| `config_release.py` | 67 / 74 | 90.5405% |
| `config_schema.py` | 407 / 416 | 97.8365% |
| `config_services.py` | 260 / 268 | 97.0149% |
| `config_split_migration.py` | 59 / 59 | 100.0000% |
| `config_store.py` | 137 / 140 | 97.8571% |
| `config_validate.py` | 94 / 97 | 96.9072% |
| `config_work_settings.py` | 141 / 150 | 94.0000% |

| Worktree boundary | Covered / statements | Observed floor |
|---|---:|---:|
| `worktree.py` | 600 / 672 | 89.2857% |
| `worktree_cleanup.py` | 208 / 217 | 95.8525% |
| `worktree_git.py` | 195 / 258 | 75.5814% |
| `worktree_inventory.py` | 284 / 353 | 80.4533% |
| `worktree_merge.py` | 80 / 100 | 80.0000% |
| `worktree_verify.py` | 238 / 267 | 89.1386% |

The first closeout run exposed two environment-sensitive misses in unchanged files: the
cross-hive scope fence lived only in the excluded integration selection, and an unreadable file
during disk measurement depended on ambient filesystem behavior. Hermetic unit contracts now
exercise both. The final run restored `safety.py` exactly to its baseline 623 / 706 (88.2436%)
and improved `localloop.py` from 723 / 842 (85.8670%) to 727 / 842 (86.3420%). `doctor.py`,
`cli.py`, and `onboard.py` remained exactly 956 / 1,054, 902 / 1,164, and 572 / 582. There is no
unexplained coverage regression.

### Stewardship and deferred-hotspot decision

Changes to `work.py` or `work_*.py`, `config.py` or `config_*.py`, and `worktree.py` or
`worktree_*.py` require a reviewer who owns the corresponding facade/service boundary. Review
must check the compatibility matrix, the owning boundary suite, and the observed per-file floor;
cross-boundary changes must name which facade remains the public patch surface.

`.github/CODEOWNERS` intentionally remains the repository-wide `* @briancripe` rule because the
repository still has one maintainer. The trigger is explicit: when a second active maintainer is
added, the same change that grants that role must add path-specific CODEOWNERS entries for the
three families above, listing the available boundary stewards so reviews are routed rather than
silently falling back to the global owner. This closeout does not invent an unavailable second
approver or alter the prior operator decision.

| Area | Closeout disposition | Owner/evidence |
|---|---|---|
| Work/config/worktree structural split | **resolved** | Facade size, health, and coverage all improved; boundary and compatibility contracts own future drift. |
| Safety | **deferred**, no replan | `bh-m8ts` owns implementation. Coverage is exactly baseline after the new unreadable-file contract. |
| Doctor | **deferred**, no replan | `bh-5sizy.3`, `bh-il87m`, and `bh-8vx8c` retain their distinct source/cache/schema work; coverage is unchanged. |
| LocalLoop | **deferred**, no replan | `bh-e8s3i` and `bh-c6dk` retain journal/runtime work. Coverage improved after making the scope fence unit-visible. |
| Broad CLI | **deferred**, no replan | `bh-j4gbx` owns the operation catalog and generated surfaces; coverage is unchanged. |
| Onboarding/raw `bd` consolidation | **deferred**, no replan | `bh-8kn42` retains transport consolidation; onboarding coverage is unchanged. |
| RepoWise presentation signals | **resolved** | D01-D08/D43 remain the complete audited set; zero unused exports and no new production signal. |

No new evidence invalidates those existing bead boundaries, so this closeout files no replan.
Future work should be ranked by the existing safety/doctor/runtime/CLI/onboarding beads rather
than reopening the completed structural extraction or creating a duplicate umbrella hotspot.
