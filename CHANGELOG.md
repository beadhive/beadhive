# Changelog

All notable changes to this project are documented in this file, generated from
[Conventional Commits](https://www.conventionalcommits.org/) via
[Commitizen](https://commitizen-tools.github.io/commitizen/) (`just bump` / `just bump-dry`).
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); SemVer, with
`major_version_zero = true` (breaking changes bump MINOR, not MAJOR, until `1.0.0` is chosen
deliberately — see [`docs/design/limn-naming-strategy-adr.md`](docs/design/limn-naming-strategy-adr.md#versioning-the-100--010-walkback)
for why the version was walked back from an early `1.0.0` draft to `0.1.0`).

## v0.8.4 (2026-08-06)

### Fix

- **provision**: a gitignored .beads is not an unpublished store (bh-22z70, bh-xonqg, bh-712wt, bh-bj219)

## v0.8.3 (2026-08-06)

### Fix

- **guard**: moving the store needs no lease — the intake tier was hollow (bh-qzoo1)

## v0.8.2 (2026-08-06)

### Fix

- **guard**: filing a bead needs no lease — the intake tier (bh-lkbas)
- **host-lease**: parse expiry with timegm — mktime read every stamp an hour early (bh-nf902)

## v0.8.1 (2026-08-06)

### Fix

- **hosts**: rename the role vocabulary — executor/transient/viewer (bh-7ztwe, bh-6rmpy)
- **install**: INSTALL.md's managed path upgrades an existing bh instead of no-opping (bh-6x5xj)
- **git**: the workspace passthrough supplies GITHUB_TOKEN like provision does (bh-ajnkx)
- **identity**: a keyless agent seat signs with the host's key, not unsigned (bh-y3lp)
- **guard**: refresh a stale cached host lease instead of refusing forever (bh-sks7f)

## v0.8.0 (2026-08-06)

### Feat

- **install**: the three onboarding postures — laptop, legacy upgrade, second host (bh-vmdq)
- **assurance**: scan the image closure with grype (bh-e6uk)
- **assurance**: an image SBOM beside the package SBOM (bh-btry)
- **assurance**: the licence gate reads nix metadata, not a comment block (bh-8b8o.2)
- **image**: nix supplies the toolchain binaries (bh-8b8o.1)
- **work**: work.enforce_signing refuses a merge on ANY unverified commit
- **identity**: a provisioned host can produce an attributable, signed commit
- **run**: ONE launcher constructs the child environment (bh-9qor)
- **bootstrap**: `just local-install` — the native router from checkout to provisioned host
- **ci**: wire the full gate at the main-merge point, and stop it passing vacuously (bh-dfz2)
- **cli**: `bh dep list|show|install|auth` — one surface over the table, `bh harness` becomes an alias
- **host**: `bh host provision --answers <file>` — declarative, no prompts (bh-q160.2)
- **harness**: `bh harness auth <name>` drives the login flow, then re-probes
- **harness**: `bh harness auth` — probe credentials and name the fix (bh-q160.3)
- **deps**: derive the setup probes from the table (bh-hsus.3)
- **bootstrap**: commit flake.lock — the pin the reproducibility claim rests on
- **bootstrap**: flake.nix — the local-install toolchain (bh-q160.12)
- **onboard**: default new hives to bd's shared server, invisibly (bh-areg.7)
- **dolt**: report store-engine liveness in doctor/setup-check/hive-ready (bh-areg.3)
- **hive**: add a Dolt storage-migration verb (bh-areg.4)
- **bootstrap**: clear bd's GH#2455 dirty-config bug on fresh server-mode init
- **guard**: bh bd write verbs respect the host lease bh work already respects (bh-edvs)
- **schema**: track each hive's real bd schema version in HQ (bh-wnly)
- **proof**: make the proof gate a runnable script, not a transcription (bh-pc2a.17)
- **assurance**: bind the image component licence policy (bh-pc2a.21)
- **image**: stop shipping the proprietary harness, install it at runtime (bh-pc2a.36)
- **container**: GH_TOKEN passthrough, and make gh credentials survive a rebuild (bh-pc2a.29)
- **image**: install bh from a local wheel, so the proof gate can verify unreleased behaviour (bh-pc2a.25)
- **container**: wire Codex credentials and the headless auth path (bh-pc2a.7)
- **image**: assert a built image preserves third-party licence notices (bh-pc2a.23)
- **container**: compose wires four role-separated volumes and caps agents
- **setup**: read the image component manifest instead of re-probing
- **image**: bake core + agent image targets from one pinned HCL

### Fix

- **doctor**: derive the upgrade from how bh was installed, not from one hardcoded command (bh-jmw0)
- **host**: bead sync stops reporting hives as synced when it only left them alone (bh-s0wj)
- **bootstrap**: pin the jq/yq the Brewfile already claimed mise pinned (bh-t2ty)
- **credentials**: derive an absent row's remedy from its install route, not from prose (bh-tccp)
- **setup**: a build marker disqualifies a version from being judged a tagged release (bh-1drz)
- **deps**: bump cryptography 49.0.0 -> 50.0.0, dispositioning the advisory (bh-hujz)
- **supply-chain**: the licence gate refuses to answer from a report it cannot read (bh-ymvn)
- **test**: reap the dolt server a test starts, by pidfile instead of a verb that refuses (bh-cbou)
- **log**: diagnostics follow sys.stderr instead of the object configure() first saw (bh-lbcf)
- **work**: a batch member resolves the SHARED worktree, and merge stops dropping members (bh-c3nf)
- **image**: a runtime-installed harness must survive a container recreate (bh-dy4g)
- **sync**: a hive with no peer towns skips its sync instead of failing it
- **sync**: sync every federation peer by name, around bd's origin-dropping enumeration
- **host**: register a hive's federation peer so the sync after bootstrap can run (bh-40uz)
- **bootstrap**: local-install puts the toolchain on PATH, not just in the devShell (bh-ytqc)
- **host**: bootstrap before sync, and stop verify's false green (bh-fxw6, bh-1atj)
- **host**: HQ's workspace.toml is inherited AND wired (bh-9bkj, bh-28ha)
- **bootstrap**: the flake devShell must carry `just` — the entry point needs itself
- **test**: bind an ephemeral dolt port instead of the literal 3399 (bh-dfz2)
- **test**: mint host.yaml in the AGF World and stop swallowing bd's own error (bh-dfz2)
- **role**: codex is refused because it cannot run a seat, not because it is "unknown"
- **deps**: derive harness/role registries from the table, fix the missing-binary guard (bh-hsus.5)
- **deps**: adopt InstallRoute — the table stops describing an install bh no longer performs (bh-hsus.3)
- **harness**: missing_hint() must not route to a command that refuses (bh-hsus.1)
- **harness**: install the native build, not npm — stop shadowing a real install (bh-hsus.1)
- **host**: provision performs the setup check it used to require out of band (bh-1kzc)
- **bootstrap**: mise exec the steps that consume mise-provided tools
- **bootstrap**: the Brewfile stops assuming macOS, and stops installing a runtime (bh-q160.1)
- **fence**: follow the transport repo out to the server, and stop [] meaning three things (bh-areg.6)
- **hive**: drop cleanup_failed_bd_init's runtime self-protection guard
- **onboard,hub**: three-way store-open branch replaces the two-way exists check
- **hub**: stream bd's real error instead of quoting its git-phase success line
- **onboard**: busy dolt-server port fails legibly and cleans up after itself
- **dolt**: probe_endpoint reads the MySQL protocol-version byte at the right offset (bh-areg.3)
- **hq**: back up and restore a non-embedded HQ over the connection
- **supply-chain**: stop license-check gating on CVE findings (bh-1kvq)
- **image**: correct the Dockerfile header, and skip a check we must not satisfy (bh-xoaw)
- **image**: stop core and agent drifting, and stop the symptom misdirecting (bh-pc2a.33)
- **config**: only nudge about missing fleet.yaml when HQ has a remote (bh-pc2a.31)
- **compose**: stop shelling out to a container runtime from inside the image (bh-pc2a.6)
- **image**: image-licenses defaulted to a tag that does not exist
- **container**: pre-create every volume mount point, owned by the agent user
- **image**: configurable runtime user, hold the update pin, correct image-cross docs

### Refactor

- **gitworkspace**: name the config globber, and make the HQ-inheritance claim checkable
- **deps**: auth is a column on the row — harness_auth becomes credentials, PROBES is deleted
- **deps**: git-workspace becomes a required dep, not a plugin (bh-hsus.4)
- **store**: one owner for store paths, and two names that cannot be confused (bh-z9h7)
- **hq**: extract the embedded-store locator into store_locator.py

### Perf

- **test**: the integration harness is parallel-safe now — stop asserting otherwise (bh-c1qp)
- **test**: check-all runs two passes, so only the 34 tests that need serial get it (bh-c1qp)
- **image**: install bh last, so a source change stops discarding the fetch (bh-41tj)

## v0.7.1 (2026-08-02)

### Fix

- **hive**: default hives to bd auto-export, never git-tracked (bh-ug5u)
- **hq**: no verified checkmark on an absent embedded store (bh-kobw)

## v0.7.0 (2026-08-02)

### Feat

- **setup**: warn when bd embeds a dolt without the #4770 pull-hang fix (bh-gnqc)
- **quality**: license gate + CVE signal over uv-exported CycloneDX (bh-zrr0.1 bh-zrr0.2)
- **hive**: pre-push fence is opt-in; onboard installs no hook files
- **hive**: bh hive hook pre-push — the fence as a verb, not a generated script
- **host**: bh host rm + bh host lease, and one teardown verb model
- **cli**: bh backup export|usage|reclaim
- **hq**: auto-prune old pre-push backups after a verified new one
- **backup**: boundary + retention core for the three backup roots
- **work**: warn when validate_cmd looks compile-only and unconfigured (bh-l44i)
- **work**: check seeds the verdict ledger submit reuses from, and clean_checkout announces long runs
- **hq**: bh hq push/status — publish HQ again after init, and report drift
- **host**: bh host remove — gated deregister for orphaned manifests
- **host**: add `bh host retire` — host-scope safety verdict + guarded teardown
- **host**: add `bh host provision` — idempotent new-host adoption
- **hive**: add host-local `bh hive reclaim`
- **hq**: bh hq restore — consume the three levels _take_backup writes
- **hq**: --create makes the HQ remote private and empty when missing
- **hitch**: bh doctor reports which seats this host can actually run
- **plugin**: hitch integration — bh plugin hitch up <target> <profile>
- **packaging**: add [project.urls] to pyproject.toml
- **github**: add bug report / feedback issue template
- **github**: add CODEOWNERS with global maintainer entry
- **guard**: pre-push fence hook + doctor drift check for direct bd use
- **host-cli**: `bh host adopt|release|packup` + lease state in `list` (bh-ytbb.13)
- **host-lease**: opportunistic renewal loop + local cache use (bh-ytbb.11)
- **claim**: ClaimRecord carries host_id + epoch as a fencing token
- **guard**: guard_primary() gating the write verbs on the host lease
- **host-adopt**: two-phase fail-closed adopt — fence first, lease second
- **host-fence**: refs/bh/epoch fence + atomic fenced data push
- **host-lease**: CAS the host lease at refs/bh/lease/<prefix> in HQ
- **host**: bh host CLI group — init, list, show (bh-ytbb.5)
- **hosts**: hosts/<host_id>.yaml manifest schema + read/write/validate API
- **host**: mint a stable ~/.beadhive/host.yaml host_id at config init
- **config**: one-time migration splitting a flat config.yaml into fleet.yaml + host
- **config**: bh config get/set/unset --scope fleet|host, show provenance
- **hq**: add `bh hq clone` — bootstrap a host with no local HQ
- **config**: deep-merge fleet base + host override in config.load()
- **hq**: scaffold HQ layout, wire remote, back up, and push (bh-e0y8.2)
- **config**: define the fleet/host key partition (bh-e0y8.3)
- **config**: add hq.remote key with <owner>/beadhive-hq derivation
- **plan**: declare tag: labels in a molecule spec (bh-0a6g)

### Fix

- **quality**: osv-gate reports a missing scanner as missing, not as bad input
- **tests**: sandbox $GIT_WORKSPACE — the suite was scanning real repos (bh-myp0)
- **hive**: gate `bh hive rm` behind --confirm, with --dry-run and a restore hint
- **doctor**: register backups/ as a known ~/.beadhive layout entry
- **work**: resolve validate_cmd through the justfile instead of guessing (bh-l44i)
- **work**: make `ready`'s truncation impossible to miss (bh-i0p1.2)
- **lint**: shorten 3 lines the format sweep's reflow pushed past E501 (bh-ukzy)
- **merger**: record a merge conflict as routable bd state, not just a transcript
- **work**: close a merged bead/epic/batch as its own assignee, and stop lying about it
- **config**: self-heal a stale un-migrated host config at hive/hq entry points (bh-17eb)
- **config**: unset_value prunes emptied ancestor sections (bh-o9x1)
- **hq**: reconcile the host config when hq clone lands a fleet.yaml
- **guard**: allow publish-only `bd dolt` verbs through the hub write-guard
- **docs**: tag fenced code blocks with a language in slop-bench doc
- **config**: schema describes what bh writes, and a guard keeps it that way
- **hq**: derive hq.remote from host identity, confirm it interactively
- **config**: persist hitch config directories, decoupled from worktrees.ephemeral
- **role**: construct the launched harness's PATH instead of inheriting it
- **contributing**: point CODEOWNERS link to .github/CODEOWNERS
- **docs**: tag bare code fences in the runtime ADRs (MD040)
- **worktree**: honest prune output + self-heal stale admin entries
- **guard**: resolve host_lease/hq_dir locally in guard_primary()'s renewal call
- **host**: replace socket.gethostname() with host_id() at all merge-slot/liveness sites
- **registry**: route fleet-scoped managed_repos writes through save_fleet()
- **engine**: bound dolt state verbs so a wedged remote can't hang the hive
- **work**: deterministically block agent self-approval of type:human gates
- **docs**: add language tag to untagged fenced code block
- **docs**: add language to fenced code block in multi-host-model-adr
- **bd**: resolve relative import sources against real cwd, not None
- **engine**: stop stringifying None cwd in import_jsonl
- **deps**: sync uv.lock to the version bump

### Refactor

- **registry**: add hives(cfg) as the one shared HQ-exclusion helper
- **config**: extract scaffold_home for reuse beyond config init
- **registry**: extract_method on classify, resolve_hive, derive_prefix, docs, repos_sync
- **work_logic**: extract_method on validate_plan + gate helpers
- **onboard**: extract_method on _ensure_derived and _gate
- **cli**: extract_method on _root, archive_prune, config_validate
- **worktree**: extract_method on prune/status/clean_checkout, fix N+1 pid probe
- **work**: extract_method on the 4 brain methods + batch N+1 close/list spawns

### Perf

- **tests**: run the unit suite in parallel; fix the 4 tests that blocked it (bh-z5h3)

## v0.6.0 (2026-07-24)

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
