# Changelog

All notable changes to this project are documented in this file, generated from
[Conventional Commits](https://www.conventionalcommits.org/) via
[Commitizen](https://commitizen-tools.github.io/commitizen/) (`just bump` / `just bump-dry`).
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); SemVer, with
`major_version_zero = true` (breaking changes bump MINOR, not MAJOR, until `1.0.0` is chosen
deliberately — see [`docs/design/limn-naming-strategy-adr.md`](docs/design/limn-naming-strategy-adr.md#versioning-the-100--010-walkback)
for why the version was walked back from an early `1.0.0` draft to `0.1.0`).

## Unreleased

### Feat

- **sync-remote**: HQ exclusion, fetch=True assessment, parallel pre-assess
- **hive**: bh hive sync — bidirectional federation sync with conflicts-as-data
- **safety**: opt-in fetch= flag maps bd federation status onto DoltRefInfo
- **engine**: federation_status + sync_state on the Engine seam
- **otel**: deferred-start + conflicts-avoided release counters
- **release-order**: advisory merge ordering + release-hold gate RBAC
- **schedule**: start-gate work that would only wait behind higher-priority merges
- **plan**: release-hold gate + submit-time release-hint reconcile
- **conflict-estimator**: ConflictEstimator protocol + file-overlap floor
- **release-order**: stable-versioning scorer + strategy registry
- **labels**: add release: closed dimension + wave: open label
- **config**: release: section + per-hive layered getters
- **cli**: wire the contribution-plane verbs
- **contributor**: add the contributor seat — dossier + outbound editor

### Fix

- **doctor**: count unknown fleet state honestly, never fail-to-green
- **work**: submit defaults to the recorded claim holder, not re-derived identity
- **cli**: don't fire schema-warning/setup-gate on --help or completion
- **worktree**: scrub color-forcing env from clean-checkout validation
- **tests**: seed schema_version in sandboxed config.yaml fixture
- **deps**: sync uv.lock to the version bump

### Refactor

- **guard**: extract publish_refusal decision + public is_contributor

## v0.5.1 (2026-07-23)

### Fix

- **sync-remote**: show recently-touched beads under --verbose for unpushed-dolt hives
- **deps**: sync uv.lock to the version bump

## v0.5.0 (2026-07-23)

### Feat

- **hive**: base contribution branches off upstream, push only to the fork

### Fix

- **safety**: detect embedded/local Dolt engine push state (bh-fl26)
- **deps**: sync uv.lock to the version bump

## v0.4.1 (2026-07-22)

### Fix

- **sync-remote**: match dry-run dolt-push condition to live-run; surface git push stderr
- **deps**: sync uv.lock to the version bump

## v0.4.0 (2026-07-22)

### Feat

- **hive**: add sync-remote --all — guarded fleet-wide push+verify
- **work**: wire Engine push_state/pull_state into assign/submit/claim/resume
- **engine**: extract bd behind an Engine protocol + beads: config
- **safety**: detect unpushed refs/dolt/data alongside branch scan
- **hive**: kind=external onboarding — fork/dual-remote wiring, pull-only upstream rail
- **opencode**: hooks parity — approve-readonly permission rules + bd-steer plugin
- **hive**: OpenCode hive furnishing (--opencode)
- **worktree**: add --preview/--json contract for external orchestrators
- **role**: add harness config + seat-launch seam (claude|opencode)

### Fix

- **hive**: widen ManagedRepoEntry.kind Literal to accept "external"
- **deps**: fold uv.lock sync into the bump recipe
- **deps**: sync uv.lock version to 0.3.3

## v0.3.3 (2026-07-22)

### Fix

- **hive**: add hive repair to reconcile registry/beads-DB prefix drift
- **deps**: sync uv.lock version to 0.3.2

## v0.3.2 (2026-07-21)

### Fix

- **work**: point per-bead submit/check on a batch member at the group procedure
- **work**: accept commitizen 'bump:' subjects in the conventional-history guard
- **work**: claim --group reconciles the scheduler's collapsed groups by synthesizing batch labels
- **work**: collapsed claim provisions the epic container so the batch lands into it, not main
- **work**: first-class bounce verb; reconcile review gates orphaned by raw set-state bounces
- **work**: close the batch review fail-open — group submit opens one gate; merge --group fails closed under review_gate:human
- **work**: review gates carry an explicit bh:review marker so ad-hoc human gates aren't misclassified

## v0.3.1 (2026-07-18)

### Fix

- **work**: close the kickoff swarm bead when its molecule lands (bh-7tno)
- **hive**: share onboard's kind translation so classify|prefix compose (bh-skbo)
- **plan**: verify refuses work children carrying origin:/intake:/kickoff: state labels (bh-l9s8.2)
- **plan**: name the real cause when all children are origin-filtered in verify (bh-l9s8.1)
- **work**: pass --limit 0 to bd gate list so gates past the 50-result window stay visible (bh-pwi2)
- **work**: accept dep-less gh:pr gate in _ensure_pr_gate too (bh-pctz)
- **work**: accept dep-less review gate when bd refuses blocks edge onto an epic (bh-pctz)

## v0.3.0 (2026-07-17)

### BREAKING CHANGE

- --prime is removed from the CLI/MCP/python surfaces, and
the default onboard no longer commits tracked scaffolding (declare with
--furnish or --claude/--agents/--skills). Beads: bh-7yhl.1, bh-7yhl.2.

### Feat

- **config**: stale config → paste-ready agentic-update offer + --fix (bh-5cgm.7)
- **config**: `bh config validate` command over the schema validator (bh-5cgm.5)
- **config**: validate_config() — pydantic errors + ws→bh rename table (bh-5cgm.2)
- **config**: bh config schema dump + did-you-mean on unknown keys (bh-5cgm.4)
- **config**: lightest load-time schema-version staleness warning
- **config**: define BeadhiveConfig pydantic-settings schema + SCHEMA_VERSION=1
- **toolchain**: knowledge-only registry + bh toolchain list/show/exec + MCP surface (bh-d0kb)
- **otel**: bh.work.validation.reused counter for ledger reuse hits (bh-dfx0)
- **worktree**: validation verdict ledger — reuse green clean-checkout verdicts at submit (bh-dfx0)
- **worktree**: mark-landed escape hatch + landing docs (bh-v0wu)
- **work**: PR-merged completion (work land) + squash-aware is_landed (bh-v0wu)
- **work**: PR landing path for work.landing: pr (bh-v0wu)
- **config**: work.landing (local|pr) + work.push_remote settings (bh-v0wu)
- **work**: gates section in work show — kind/status/reason/id (bh-i371)
- **worktree**: verify-flagged init rules + bare-checkout hint (bh-7k1p)
- **plan**: structured missing-acceptance listing + STUB marker semantics (bh-lwdn)
- **escalate**: consent-prompted HQ auto-init, never lose the signal (bh-ufne)
- **cli**: regroup the command surface onto the 6-panel scheme (bh-2l1m.7)
- **cli**: unify flag/param declarations to the ADR conventions (bh-2l1m.5)
- **mcp**: align MCP tool/resource names + add bh hive status (bh-2l1m.3)
- **hive**: rename persisted config keys, bh.hive telemetry, statusline, HQ guard
- **hive**: rename CLI tree, --hive flags, MCP tools/resources, user-facing strings to hive
- **rig**: 'bh rig context --hook-json' — registry-driven AGF steering payload
- **onboard**: zero-footprint onboarding — furnish axis + PRIME.md deprecation

### Fix

- **config**: narrow migrate_home_if_needed's race handling to real sub-cases (bh-2gd1.1)
- **config**: tolerate a concurrent bh migrating the same home dir (bh-2gd1.1)
- **worktree**: record verdicts against the validated checkout HEAD (bh-dfx0)
- **config**: probe-guard the default just-setup init rule (bh-17n4)
- **labels**: per-bead create gate, full-corpus lint, origin:backfill (bh-vfx9)
- **work**: canonical review-gate selector + idempotent submit (bh-c3il)
- **worktree**: per-invocation verify dirs + liveness sweep (bh-nikb)
- **onboard**: remote marketplace fallback, skip-if-installed, non-fatal claude step (bh-9n2f)
- **mcp**: purge ws residue from the MCP surface (bh-2l1m.4)
- **hive**: sweep residual rig prose in justfile + plan.py comment

### Refactor

- **registry**: share one cwd->hive resolver across work/plan/worktree (bh-2l1m.6)
- **hive**: rename internal identifiers, rig*.py modules, and test fixtures to hive

## v0.2.0 (2026-07-15)

### Feat

- **plan**: add 'bh plan repair' + shared kickoff plumbing (bh-u28l)

### Fix

- **plan**: make 'bh plan approve' reconciling and idempotent (bh-75mi)

## v0.1.4 (2026-07-15)

### Fix

- **doctor**: use uv tool install --force in stale-reinstall hint
- **rig**: fully strip bd's fork-protection exclude across bd versions (bh-2w8d)
- **worktree**: surface unregistered repos' worktrees in status/list (bh-ea1i)
- **work**: crash-safe merge-slot release + stale-holder reclaim (bh-62ex)
- **strings**: correct stale ws-era paths, package names, and otel.env echo (bh-bwhq)
- **observaloop**: verify collector preset persisted, warn on silent no-op (bh-0fk9)
- **worktree**: explicit conventional subject for container refresh merge (bh-cgxc)
- **test**: clear BH_DEV so controller-deny test is env-independent (bh-go6i)

## v0.1.3 (2026-07-15)

### Fix

- **doctor**: report per-repo-group auth (identity, signing, insteadOf)
- **orca**: stop _sync_worktree_wiring mis-mapping deep-nested clones

### Refactor

- **plugins**: promote git-workspace to a bh Plugin
- **registry**: migrate triplet consumers to group semantics
- **gitworkspace**: model repo groups as first-class RepoGroup

## v0.1.2 (2026-07-13)

### Fix

- **report**: accept a body via --description and non-TTY stdin (bh-u0qd)
- **report-target**: warn when the self rig is unregistered (bh-pfgx)
- **validate**: aggregate identical unregistered-prefix findings (bh-9iiz)
- **registry**: suggest next steps on 'no rig matching' (bh-xy83)
- **bd**: let --help bypass the label-violation gate (bh-8krs)
- **report**: use the real CLI alias in the filed-report reason (bh-nqyv)
- **registry**: accept flagship bare org-code prefix (bh-sva7)
- **otel**: quiet the per-invocation init log and grpc fork-fd warning (bh-sb9l)
- **worktree**: generalize the index.lock retry to all ws git mutations (bh-i6o7)
- **worktree**: re-point a stale empty child branch on re-assign after container refresh (bh-4wwi)
- **schedule**: never group on a batch label whose group branch already merged (bh-bfoy)
- **work**: attribute submit via --as so the claim-ownership guard is correct (bh-rddl)
- **work**: clear stale review:pending label on approve/merge + backfill (bh-mgo3)
- **work**: derive review_pending_at from the review gate created_at (bh-yocq)
- **work**: submit refuses when submitter no longer holds the claim (bh-rddl)
- **hub**: reconcile removed rigs on sync via bd repo remove (bh-1x5p)
- **hub**: correct stale gitignore comment on sync export ledger (bh-vsf1)
- **cli**: purge stale `ws` from user-facing strings, help text, config template
- **rig**: migrate .claude/agents/*.md and .beads/PRIME.md ws->bh
- **schedule**: topo-sort collapsed leaves before chunking so no chunk deadlocks
- **onboard**: detect forks by resolved host + upstream remote, not the path label
- **onboard**: never configure a beads remote for a repo we cannot push to
- **worktree**: resolve merge container via bd parent-link, not stale id prefix
- **work**: emit Conventional-Commits merge subjects

### Refactor

- **worktree**: shorten a test assertion message under the line limit (bh-4wwi)
- **work**: early-continue in backfill loop to satisfy line length (bh-mgo3)

## v0.1.1 (2026-07-12)

### Fix

- **otel_lgtm**: pass the ~/.ws/.env overlay to otel compose invocations (bh-nf1.2)

### Refactor

- **mcp**: split build_server into a short assembly over register groups (bh-nf1.3)
- **retire**: extract named consent-gate helpers from retire_rig (bh-nf1.4)
- **safety**: one os.walk for _measure_disk_usage (bh-nf1.6)
- **otel**: parametrize the record_mcp tool/resource emitters (bh-nf1.7)
- **work**: move guard helpers to work_logic so work_group drops its work import (bh-nf1.9)
- **work**: dedupe the merge path (bh-nf1.8)
- **bd**: consolidate duplicated bd/state helpers into bd.py (bh-nf1.1)
- **config**: add layered() lookup helper, extract home-migration cluster

## v0.1.0 (2026-07-11)

### Feat

- **release**: add PyPI Trusted Publishing workflow (bh-6iv)
- resolve the bh plugin from its own repo instead of a vendored copy
- **orca**: delegate worktree ops to orca-managed seats
- **orca**: git-workspace-aware project sync
- rename to Beadhive (bh) and prepare the first release
- **plugin**: promote the MCP client to a core dependency
- **coordinator**: batch scheduling — multi-bead work groups
- **plugin**: bundle the MCP server — user-scope, on by default
- **mcp**: MCP resources layer — read-only surface + change signals
- **hub**: sync progress output — banner and per-rig lines
- **hq**: factory HQ store, escalation UX, and report-to protocol
- **fleet**: wave-1 rig adoption
- **onboarding**: wave-1 onboarding readiness
- **plugin**: vend seat agents via a Claude Code plugin
- **coordinator**: tier-aware coordinator with nested workstreams
- **onboarding**: step/check framework with preflight gate
- **worktree**: worktree status and merge-aware safe prune
- **work**: wire dispatch config into the work scheduler
- **coordinator**: collapsed dispatch mode — fewer sessions per epic
- **work**: first-class review-approve verb and epic-sibling fix
- **work**: harden plan/work layering — gated passthrough + reads
- **rig**: safe rig retire with data-loss guardrails
- **fleet**: fleet survey and onboarding triage
- **roles**: role modes — injected role agents and status line
- **control**: superintendent control plane — config and rig verbs
- **metrics**: commit-flow throughput and efficiency metrics
- **observaloop**: OpenTelemetry integration — profiles and dashboards
- **otel**: operational telemetry — metrics and OTLP transport
- **otel**: observability — OpenTelemetry tracing + structlog logging
- **mcp**: FastMCP stdio server — dual CLI + MCP entrypoint
- **review**: interactive merger/reviewer review flow
- **merge**: union merge driver for append-only conflicts
- **work**: two-level molecule integration branch
- **plan**: planning plane — ideation-to-molecule planner
- bootstrap the integration engine — worktrees, work driver, skills

### Fix

- **docs**: drop trailing double blank line in README (markdownlint MD012)
- **tests**: restore bead-id fixtures and tidy the promote message
- **work**: fleet-run robustness — worktree safety and attribution

### Refactor

- **coordinator**: relocate molecule kickoff to integration plane

### Perf

- **doctor**: workspace-metadata cache for doctor and survey
