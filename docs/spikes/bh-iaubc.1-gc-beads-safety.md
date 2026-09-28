# Spike `bh-iaubc.1`: can Gas City v1.4.2 share a Beadhive hive's Beads state?

**Bead:** `bh-iaubc.1` · **Seat:** `dev/gc-state` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-iaubc.2` (Decide shared Beads state safety), which in turn blocks
`bh-kp64s.2` (Gas City runtime adapter feasibility). Context: decision `bh-d75bi`, whose
2026-08-11 correction keeps blocker 1, "two reconcilers over one bead store", as the main concern.

> This is the spike verdict that informs the operator. It does **not** decide `bh-iaubc.2`.

## Question

Can Gas City v1.4.2 (`gc`, which links and pins Beads v1.3.0) safely operate on the **same**
Beads state as a Beadhive hive? For Beadhive, that state means one database on `bd`'s shared
`dolt sql-server` (`127.0.0.1:3308`), a per-hive `bd serve` managed by `bh`, and `bh`'s own
claim/lease, epoch-fence, gate and label rules. If gc cannot share it, what boundary lets the two
coexist, which preconditions must hold, and which operations are unsafe?

What this spike does **not** ask:

- whether gc is a good work runtime (`bh-kp64s`)
- how Beadflow roles map onto a Gas City pack (`bh-lko4x`)
- whether to adopt Gas City at all (`bh-d75bi`, `bh-iaubc.2`)

It also leaves bead-level vocabulary contracts out of scope except where they cause a safety
failure.

## Method

1. **Upstream source reading at pinned tags.** The upstream trees were cloned read-only at
   `gascity@v1.4.2` (`d458216`) and `beads@v1.3.0`. The areas read:
   - the store factory and native store, `BdStore`, preflight contract and canonical file writers
     (`internal/beads/**`)
   - rig provisioning (`internal/rig/provision.go`, `cmd/gc/cmd_rig.go`)
   - the managed-Dolt lifecycle and port publication (`cmd/gc/dolt_*`,
     `cmd/gc/beads_provider_lifecycle.go`)
   - pool-assignment release (`cmd/gc/pool_session_name.go`), the `gc bd` wrapper, the default
     work query, and session wait beads
   - the bundled `bd`/`dolt` system packs, with their orders and commands (`examples/bd/dolt/**`)
   - the always-imported `core` pack, with its orders and scripts
     (`internal/bootstrap/packs/core/**`). The coordinator relayed leads on `gate-sweep` and
     `prune-branches` from the sibling spike `bh-lko4x.1`, and they were re-verified in source
     here.
   - design and reference docs: the storage topology, the Beads/Dolt contract redesign, system
     packs, config, and installation
   - the v1.4.2 release notes (`gh release view v1.4.2 -R gastownhall/gascity`)
   - on the Beads side: the lease and claim ops, `bd reclaim`, the shared-server migration gate,
     `AllowSharedSchemaMigration`, and shared-mode port resolution
2. **Beadhive source and docs:** [DOLT.md](../DOLT.md),
   [BEADS-SERVICE.md](../BEADS-SERVICE.md), [WORK.md](../WORK.md) (runtime invariant, reclaim,
   exclusive primary, claim fencing token, direct `bd` publication),
   [LABELS.md](../LABELS.md), `src/beadhive/localloop.py`, `validate.py`, `complexity.py`,
   [gas-frameworks-comparison.md](../design/gas-frameworks-comparison.md), the repo `CLAUDE.md`,
   and prior spikes `bh-ukit.2` and `bh-ie41e.5`.
3. **Read-only probes against this hive.** No `gc` or `bd` mutation was run.
   - `bh bd context --json`
   - `bh bd sql "SELECT MAX(version) FROM schema_migrations"`
   - `bh bd config get types.custom` and `bh bd config get issue_prefix`
   - `ls ~/.beads/shared-server/dolt`
4. **No disposable-hive experiment was run.** `gc` is not installed. Every fact the verdict rests
   on is either unconditional default behavior or a direct file or SQL write, and the source shows
   it plainly. Running an experiment would have required building `gc` from source, and it would
   only have confirmed mechanics without changing the verdict. The remaining uncertainties are
   listed in [Evidence H](#h-residual-uncertainties-not-verified-experimentally).

Citation form: `gascity@v1.4.2:path:line`, `beads@v1.3.0:path:line`; Beadhive paths are repo-relative.

## Evidence

### A. Schema and version compatibility — compatible, no migration pending

1. **Same Beads build on both sides.** gc links `github.com/steveyegge/beads v1.3.0`
   (`gascity@v1.4.2:go.mod:26`) and pins `BD_CURRENT_REF=f45b249ce6b4…`
   (`gascity@v1.4.2:deps.env:22`). The installed `bd` reports `bd version 1.3.0 (f45b249ce)`, the
   same commit.
2. **The hive is already at the v1.4.2 target schema.** `schema_migrations` reports `MAX(version)
   = 66`. That matches the release note's "migration moves schema 53 to 66" and the last Beads
   v1.3.0 migration, `0066_add_events_journal_actor`
   (`gascity@v1.4.2:docs/getting-started/installation.md:44-48`). No coordinated
   `bd migrate schema` is needed.
3. **Neither side auto-migrates a shared server.** Beads refuses to auto-apply pending migrations
   to a shared server database (`beads@v1.3.0:internal/storage/schema/remote_migrate_gate.go:182`).
   An embedder has to opt in explicitly with `AllowSharedSchemaMigration`
   (`beads@v1.3.0:beads.go:279-309`). gc never calls it: a grep for
   `AllowSharedSchemaMigration` and `BD_ALLOW_REMOTE_MIGRATE` finds nothing.
4. **gc still issues DDL when it opens its native store.** `repairIDDefault` runs `ALTER TABLE`
   to restore `DEFAULT (uuid())` on `dependencies`, `events` and `wisp_events` when the default is
   missing (`gascity@v1.4.2:internal/beads/native_dolt_store.go:26-33,392-400`). The change is
   idempotent, but it is still a schema write from a second client.
5. **On a Beadhive hive, gc would use `BdStore`, not its native store.** Native-store eligibility
   requires `project_id` in `.beads/metadata.json`
   (`gascity@v1.4.2:internal/beads/contract/preflight_checker.go:215-219`). This hive's metadata
   file has only `database`, `backend`, `dolt_mode` and `dolt_database`, so gc falls back to
   running `bd` for each call (`gascity@v1.4.2:internal/beads/factory.go:126-141`). gc's writes
   would then go through `bd`, not straight through beadslib. The direct-write findings in section
   E still apply.

Schema and version compatibility is **not** the blocker.

### B. Topology and Dolt server lifecycle — gc claims ownership

1. **One Dolt server per city, and gc owns it.** The contract's decision log says "Gas City owns
   the Dolt lifecycle for managed-local `bd` cities" and "one Dolt server per city and one logical
   Dolt database per scope" (`gascity@v1.4.2:engdocs/design/beads-dolt-contract-redesign.md:39-41`).
   It also says "one city-scoped lifecycle owner is responsible for managed-local Dolt `start`,
   `stop`, and `recover`" (`:280`). The managed data directory is `<city>/.beads/dolt`
   (`gascity@v1.4.2:cmd/gc/dolt_runtime_layout.go:37`).
2. **Beadhive runs the opposite model.** `bd` spawns and supervises one shared server per host on
   `127.0.0.1:3308`. Each hive is a database under `~/.beads/shared-server/dolt/<db>`, and bh
   "never starts" the server itself ([DOLT.md](../DOLT.md), "The store engine").
   [BEADS-SERVICE.md](../BEADS-SERVICE.md) adds that the Beadhive host runtime owns `bd serve`,
   and commands never start or stop it. gc never uses `bd serve`; a grep finds no `bd serve`
   call. It has its own supervisor API instead.
3. **The two lifecycles can only share a server through gc's "external" mode.** A gc city can be
   pinned to an external endpoint with `gc init --dolt-host/--dolt-port/--dolt-database`
   (`gascity@v1.4.2:cmd/gc/cmd_init.go:405-408`, origin `city_canonical`). In that mode gc does
   not start or stop the server. It still runs every store-level maintenance order in section F
   against the rig databases it knows about.
4. **Database names can collide.** gc reserves `hq` as the default city database
   (`beads-dolt-contract-redesign.md:42`). The shared server on this host already holds Beadhive's
   `hq` database, next to 30 other hive databases (`ls ~/.beads/shared-server/dolt`: 31
   database directories).

### C. Ownership of `.beads/` files — gc rewrites bh-tracked configuration

1. **`gc rig add` on a Beadhive hive fails closed, but only by accident.**
   - A fresh add refuses because ".beads already contains a beads store"
     (`gascity@v1.4.2:internal/rig/provision.go:459-476`).
   - `--adopt` refuses because a valid `issue_prefix` line is required in `.beads/config.yaml`
     (`:417-425`). Beadhive keeps its prefix in the database (`bh bd config get issue_prefix` →
   `bh`), and the config line is commented out.

   Adding that one line is enough to get past both guards.
2. **Once a hive is adopted, gc rewrites the tracked `.beads/config.yaml` on every
   normalization.** `EnsureCanonicalConfig` makes these changes
   (`gascity@v1.4.2:internal/beads/contract/files.go:463-564`):

   | Key | gc sets it to | Beadhive today |
   |---|---|---|
   | `dolt.auto-start` | `false` (`:489`) | not set; bh relies on `bd` auto-starting the shared server |
   | `export.auto` | `false` (`:504`) | `true` (`.beads/config.yaml:70`) |
   | `backup.enabled` | `false` (`:510`) | already `false` |
   | `dolt.disable-event-flush` | `true` | not set |
   | `gc.endpoint_origin` / `gc.endpoint_status` | added | absent |
   | `dolt.host` / `dolt.port` / `dolt.user` | deleted unless the endpoint is external | absent |
   | `issue_prefix` / `issue-prefix` | written | commented out |

   Unknown keys such as `dolt.shared-server` and `sync.remote` are preserved.
3. **gc also rewrites other bh-owned files, and the database.**
   - For every registered rig in a managed city, gc writes the city's managed port into
     `<rig>/.beads/dolt-server.port`, or deletes that file for external origins
     (`gascity@v1.4.2:cmd/gc/beads_provider_lifecycle.go:1732-1758`).
   - It writes `.beads/routes.jsonl` (`gascity@v1.4.2:cmd/gc/rig_beads.go:63-90`) and `GT_ROOT` in
     `.beads/.env` (`gascity@v1.4.2:cmd/gc/cmd_rig.go:1216-1254`).
   - It removes bead hooks under `.beads/hooks/` that gc stamped (`cmd/gc/hooks.go:61-80`).
   - `--adopt` runs a "config sync" that registers `types.custom` in the database
     (`gascity@v1.4.2:cmd/gc/cmd_rig.go:95-98`). The list is 13 types — `molecule`, `convoy`,
     `message`, `event`, `gate`, `merge-request`, `agent`, `role`, `rig`, `session`, `spec`,
     `convergence` and `step` (`gascity@v1.4.2:internal/doctor/checks_custom_types.go:30-34`).
     This hive has `types.custom (not set)`.

### D. Claims, leases and gates — the two reconcilers collide on leases

1. **gc claims through `bd` and gets a bd lease.** `BdStore.Claim` runs `bd update <id> --claim`
   (`gascity@v1.4.2:internal/beads/bdstore.go:1377-1378`). Every Beads claim grants a lease with
   `DefaultLeaseTTL = 5 * time.Minute`, and only `bd heartbeat` renews it
   (`beads@v1.3.0:internal/storage/issueops/lease.go:19-26`).
2. **gc never renews that lease.** `gc bd heartbeat <id>` is rewritten to
   `bd update <id> --set-metadata gc.last_heartbeat_at=<ts>`, a metadata stamp for gc's dashboard
   (`gascity@v1.4.2:cmd/gc/cmd_bd.go:170-193`). No gc code calls native `bd heartbeat` or
   `bd reclaim`.
3. **bh's local loop runs an unscoped `bd reclaim` on every pass**
   (`src/beadhive/localloop.py:1661-1667`, `coordination.reclaim(self.hive_dir, actor=...)`, with
   no label, assignee or id filters). By default `bd reclaim` reverts **every** stale lease that
   this replica granted. It clears the assignee and resets the status to `open`
   (`beads@v1.3.0:cmd/bd/reclaim.go:19-37`). The default grace is 2 × TTL, so any gc-claimed bead
   in a shared database is reverted about 15 minutes after gc claims it, whether or not the gc
   worker is alive.
   **Effect:** bh's reconciler undoes gc's reconciler's work. The failure is concrete, and it
   comes from normal default behavior, not a misconfiguration.
4. **gc reverts bh's claims too, through its core pack.** The Go controller's release is
   limited to gc's own routing marker: `releaseOrphanedPoolAssignments` skips any bead without
   `gc.routed_to` (or a legacy workflow target) resolving to a configured pool agent
   (`gascity@v1.4.2:cmd/gc/pool_session_name.go:114-163`). gc's default work query likewise
   claims only beads assigned to its sessions or routed with `gc.routed_to`
   (`gascity@v1.4.2:docs/reference/config.md:113-114`).
   **The core pack's `orphan-sweep` order has no such filter.** The core pack is imported by every
   city, whatever its provider (`gascity@v1.4.2:docs/reference/system-packs.md:20-36`).
   - Every 5 minutes, `orphan-sweep` lists **all** `in_progress` beads in HQ and in every
     registered rig
     (`gascity@v1.4.2:internal/bootstrap/packs/core/orders/orphan-sweep.toml:8-11`,
     `.../assets/scripts/orphan-sweep.sh:67-96`).
   - It resets each bead whose assignee is not a known gc agent or live gc session. The reset goes
     through `gc bd release-if-current`, which is the raw SQL path in E1
     (`orphan-sweep.sh:273,285-370`).
   - A Beadhive assignee such as `dev/<name>` never matches, so every bh claim in a gc-registered
     rig goes back to open and unassigned within about 5 minutes.
   - Separately, any bead that gets `gc.routed_to` — through `gc sling`, a custom `sling_query`
     or a hand edit — becomes gc-owned and is claimed without a bh worktree, identity, claim
     record or epoch token.

   **Effect:** the reconcilers undo each other's work in both directions (D3 and D4).
5. **Gates share a substrate, and gc's core pack resolves gh gates on its own schedule.**
   - gc's `bd` readiness goes through `bd ready` or `GetReadyWork`
     (`gascity@v1.4.2:internal/beads/bdstore.go:2665-2708`), so bh's open `type:human` gates keep
     bh beads out of gc's ready set.
   - gc's own gates and waits are beads of type `gate`. Session waits carry the label `gc:wait`
     (`gascity@v1.4.2:internal/session/waits.go:15-21`), and formula gates synthesize `gate` beads
     with a `blocks` edge (`gascity@v1.4.2:docs/reference/specs/formula-spec-v2.md:586-591`).
   - Formula gate `type` values (`gh:run`, `gh:pr`, `timer`, `human`, `mail`) are inert: no bundled
     watcher acts on them (`formula-spec-v2.md:914-919`).
   - The core pack's `gate-sweep` order runs every 30 s. It calls
     `gc bd gate check --type=timer --escalate` and `gc bd gate check --type=gh --escalate`
     (`gascity@v1.4.2:internal/bootstrap/packs/core/orders/gate-sweep.toml:4-8`,
     `.../assets/scripts/gate-sweep.sh:28-29`).
   - In any scope that holds Beadhive beads, that sweep resolves bh's `gh:pr` landing gates on
     gc's schedule, outside `bh work land`. `bh work land` is where bh resolves that gate and
     writes the squash-proof close reason ([WORK.md](../WORK.md) verb table and "PR-governed
     landing").
   - bh's own local loop also evaluates gh gates with an unfiltered `bd gate check`
     (`src/beadhive/localloop.py:1654-1659`), so the substrate behavior is not new. What is new is
     a foreign writer resolving bh gates whether or not any bh loop is running.
   - `gate-sweep` is a city-level order. It reaches a hive database when that database is the city
     scope or when the core layers are rig-scoped
     (`gascity@v1.4.2:engdocs/architecture/orders.md:341-350`). This was verified by reading the
     code, not by running it.
6. **gc drops states bh depends on.** gc's three-state model maps `bd`'s `blocked`/`review`/
   `testing` statuses to `open` (`gascity@v1.4.2:engdocs/architecture/beads.md:185-188`).

### E. Direct-write bypasses

1. **Raw SQL release.** `BdStore.ReleaseIfCurrent` runs this through `bd sql`
   (`gascity@v1.4.2:internal/beads/bdstore.go:1200-1204`):

   ```sql
   UPDATE issues SET status = 'open', assignee = '', updated_at = CURRENT_TIMESTAMP
   WHERE id = ... AND status = 'in_progress' AND assignee = ...
   ```

   The statement does not rewrite `row_lock` and does not touch the `leases` table. Beads states
   that any path setting status or assignee without rewriting `row_lock` "would silently
   reintroduce the zombie-merge bug" (`beads@v1.3.0:internal/storage/issueops/lease.go:45-70`).
   Two paths use it: the Go controller, for pool-routed beads only, and the core-pack
   `orphan-sweep`, for **any** `in_progress` bead with an assignee gc does not recognize (D4).
2. **gc's writes never pass through bh's guards.** Every gc write — creates, metadata stamps,
   claims, closes, `types.custom` registration, DDL repair — reaches the database through `bd` or
   beadslib. None of them goes through `bh`.
   - `guard_primary` "only gates bh's own write verbs — a raw `bd` invocation never goes through
     it" ([WORK.md](../WORK.md), "Direct `bd`: managed publication + doctor warning").
   - The claim fencing token is stamped and verified only by `bh work claim` and `bh work submit`.
   - On an adopted multi-host factory, gc commits made on a follower host appear in `bh doctor`
     only after the fact, as "N local commits made while not primary".
3. **gc changes the environment of the agents it launches.** It projects `BEADS_DIR`,
   `BEADS_DOLT_AUTO_START=0`, `BEADS_DOLT_SERVER_PORT` and `BD_EXPORT_AUTO=false` into agent and
   `bd` subprocess environments (`gascity@v1.4.2:cmd/gc/bd_env.go:1384-1401,1601`). Any `bh` or
   `bd` command run from inside a gc session inherits those overrides, and they outrank the hive's
   own port file (`beads@v1.3.0:internal/doltserver/doltserver.go:791-812`).

### F. Scheduled store-level maintenance — the decisive finding

1. **Every default city gets these orders.** The bundled `bd` pack is imported for any city on the
   default `bd` provider, and it "pulls in the `dolt` pack transitively"
   (`gascity@v1.4.2:docs/reference/system-packs.md:36-37`). That pack ships controller orders that
   run with no agent and no human:

   | Order | Trigger | Action | Source |
   |---|---|---|---|
   | `mol-dog-compactor` | every 2 h | `gc dolt compact` — **"Flatten Dolt commit history"** | `examples/bd/dolt/orders/mol-dog-compactor.toml:2,6-7` |
   | `dolt-remotes-patrol` | every 15 min | `gc dolt sync` — "Push dolt databases to configured remotes" | `examples/bd/dolt/orders/dolt-remotes-patrol.toml:2-5` |
   | `mol-dog-stale-db` | cron `0 */4 * * *` | `gc dolt-cleanup --probe --force`: drops stale-prefix DBs, kills orphan servers | `examples/bd/dolt/orders/mol-dog-stale-db.toml:5-8` |
   | `mol-dog-backup` | every 6 h | `dolt backup sync` per database | `examples/bd/dolt/orders/mol-dog-backup.toml` |

2. **These orders act on whole databases, and the target set includes every registered rig.**
   - `gc dolt compact` collects database names from the city's and **every registered rig's**
     `.beads/metadata.json`, plus every database directory in the managed data dir
     (`gascity@v1.4.2:examples/bd/dolt/commands/compact/run.sh:431-456,578-593`).
   - It flattens any of those databases with more than 2000 commits (`:62-63`). The flatten is a
     soft reset to the root commit and one "compaction: flatten history" commit (`:16-20`).
   - Loopback "external" targets are compacted too (`:86-89,221-237`).
   - `gc dolt sync` uses the same rig-list discovery (`sync/run.sh:139-159`) and pushes each
     database to the first remote in its `dolt_remotes` (`:261`).
3. **Beadhive forbids exactly this.** The repo `CLAUDE.md` says: "**Do not run `bd compact` or
   `bd flatten` on any hive.** … Both commands destroy that permanently." Bead version history is
   the only record of lifecycle timing.
   [WORK.md](../WORK.md) adds that adopted hives refuse direct `bd dolt push` and publish only
   through managed paths that CAS-reserve `refs/bh/epoch` first.
   If a Beadhive hive were registered as a gc rig, its database would be **flattened every 2 hours
   and pushed to its git remote every 15 minutes, outside the epoch fence**.
4. **The core pack adds whole-server maintenance of its own.** These orders run in every city:

   | Order | Trigger | Action | Source |
   |---|---|---|---|
   | `reaper` | every 30 min | runs `SHOW DATABASES` on the city's server and treats **every** non-system database as a bead store; raw `DELETE` of closed wisps older than 7 days; `close --force` of stale issues in the city database | `internal/bootstrap/packs/core/orders/reaper.toml`, `.../assets/scripts/reaper.sh:52-53,147-157,942,705-724` |
   | `cross-rig-deps` | every 5 min | rewrites satisfied cross-rig `blocks` dependencies to `related` | `.../assets/scripts/cross-rig-deps.sh:2-13` |
   | `jsonl-export` | every 15 min | exports Dolt databases to a JSONL git archive (`git add/commit/push`) | `.../orders/jsonl-export.toml`, `.../assets/scripts/jsonl-export.sh:2-6` |
   | `prune-branches` | every 6 h | deletes stale `gc/*` branches only; does not touch Beadhive's `wt/*` branches | `.../assets/scripts/prune-branches.sh:2,27` |

   If a gc city's server is bd's shared server, `reaper` alone reaches every Beadhive database and
   HQ.
5. **Cleanup is conservative by default, but not safe to point at the shared server.** The drop
   planner targets names with prefixes such as `testdb_`, `beads_t`, `beads_pt` and `beads_vr`,
   and it protects only gc's own registered rig databases
   (`gascity@v1.4.2:cmd/gc/dolt_cleanup_drop_planner.go:19-21`). None of today's shared-server
   databases match. Any future hive database named, for example, `beads_tools` would.
   The process reaper protects unknown servers unless their working directory has been deleted
   (`gascity@v1.4.2:cmd/gc/dolt_cleanup_reaper.go:256-340`). A Beadhive zombie server (see
   [DOLT.md](../DOLT.md), "Zombie servers") has exactly that shape.

**Why this matters for `bh-d75bi`.** Amendment 1 of that decision proposed dissolving blocker 1
by partitioning ownership by lifecycle state, so that each bead has one owner at any moment.
F1–F4 show why that does not hold: gc's maintenance works on **databases**, not beads. Compaction,
remote push and backup cannot be split per bead. A shared database therefore has two owners of
its history and its publication, even when every bead has exactly one owner.

### G. Labels and metadata

1. **A gc bead can block all of bh's writes to the hive.**
   - `bh bd create` "always refuses to create in a hive that has label violations"
     ([LABELS.md](../LABELS.md), "Enforcement").
   - The linter flags any **open** bead of a routable type (`epic`, `feature`, `task`, `bug`,
     `chore`) that lacks exactly one `complexity:` label
     (`src/beadhive/complexity.py:29,113-122`, applied in `src/beadhive/validate.py:26-55`).
   - It also flags any bead whose prefix is not registered.
   - gc creates plain `task` beads (the store default, `engdocs/architecture/beads.md:147-151`)
     without Beadhive labels.

   So one open gc task bead in a hive database blocks every `bh bd create` in that hive.
2. **gc's metadata and labels live in their own namespaces.** Engine-owned metadata uses the
   reserved `gc.*` prefix (`gascity@v1.4.2:engdocs/architecture/beads.md:194-239`). gc's labels
   (`pool:*`, `rig:*`, `order-run:*`, `gc:wait`) do not collide with Beadhive's closed dimensions.
   Collisions are unlikely, but nothing enforces the separation, and events or retry counts
   derived by one side never see the other's.
3. **The bead-type vocabulary grows.** gc's `types.custom` union adds `step`, `molecule`,
   `convoy`, `session` and other types to a hive that has none registered (C3). This registration
   never narrows the list, so it cannot be cleanly undone once written.

### H. Residual uncertainties (not verified experimentally)

- **Port file in shared mode.** Whether `bd` in shared-server mode reads the rig-level
  `.beads/dolt-server.port` that gc writes, or the port file in the shared-server directory. The
  port-source label says the shared directory is used
  (`beads@v1.3.0:internal/doltserver/doltserver.go:807`). Either way, gc-launched processes carry
  `BEADS_DOLT_SERVER_PORT`, which outranks both.
- **First start after a forced adopt.** The exact first-start behavior of an adopted hive once an
  `issue_prefix` line is added, in particular whether `initBeadsForDir` re-runs `bd init` against
  the existing database. This was not exercised.
- **`bd gate check` on gc gate beads.** Whether bh's `bd gate check` pass evaluates gc's formula
  gate beads, which lack Beadhive's gate fields. This was not traced.
- **Load on a shared server.** Controller poll rates and connection pressure on a server shared
  with 31 databases. gc's own comment records that concurrent gc/bd sessions "pegged the shared
  Dolt server's CPU" (`native_dolt_store.go:39-42`). This was not measured.

None of these would move the verdict. Sections B–G already establish NO-GO on their own.

## Verdict — **NO-GO**

gc v1.4.2 cannot safely share a Beadhive hive's database, even though the schema and Beads build
are identical (A1–A2).

- **History and publication.** A default gc city flattens history every 2 hours and pushes every
  15 minutes for each registered rig database (F1–F3). That breaks Beadhive's never-flatten rule
  and bypasses its epoch-fenced publication. Its `reaper` deletes rows in every database on its
  server (F4).
- **Claims, in both directions.** gc's `bd` claims are never renewed with a bd heartbeat, so bh's
  unscoped `bd reclaim` reverts them (D1–D3). gc's core-pack `orphan-sweep` reverts any bh claim
  whose assignee is not a gc agent within about 5 minutes (D4).
- **Gates.** gc's core-pack `gate-sweep` resolves bh's `gh:pr` gates every 30 s outside
  `bh work land` (D5).
- **Configuration.** gc takes over Dolt lifecycle and tracked `.beads/` configuration that bh and
  `bd` already own (B1–B2, C2–C3).
- **Writes.** It writes past `guard_primary`, the claim token, and `bd`'s own `row_lock` invariant
  (E1–E2).

Blocker 1 of `bh-d75bi` holds at the **database** level. The per-bead lifecycle partition from
Amendment 1 would not remove it.

**What does work:** a gc city in its **own directory, with its own gc-managed Dolt server**, run
next to Beadhive under the preconditions below.

## Recommendation

**For the operator deciding `bh-iaubc.2`:** the evidence supports recording NO-GO for shared
state, with the separate-city alternative as the "narrower alternative" that the decision's
acceptance criteria ask for. `bh-kp64s.2` should assume gc runs against its own city store and
never against a hive database.

### Preconditions for running Gas City on a Beadhive host

1. **Separate city, separate Dolt server.**
   - Create the city with `gc init` in a fresh directory outside every `bh`-managed repo,
     worktree, HQ and hub cache path, in the default `managed_city` mode.
   - Its Dolt data dir is `<city>/.beads/dolt`, and it listens on its own port, never `3308`.
   - Never pin the city to bd's shared server: no `--dolt-host 127.0.0.1 --dolt-port 3308`, and
     no `gc beads city use-external` aimed at it.
   - Isolation has to come from the **server boundary**, not from disabling orders. The `core`
     pack is imported by every city, and its `reaper`, `orphan-sweep` and `gate-sweep` act on
     whatever that server and rig list contain (D4, D5, F4).
2. **No Beadhive hive is ever a gc rig.**
   - Only scratch directories or scratch clones without a tracked `.beads/` go to `gc rig add`.
   - A plain `git clone` of a Beadhive repo carries the hive's tracked `.beads/config.yaml` and
     `metadata.json`, so remove `.beads/` from the clone first.
   - Never commit gc-normalized `.beads/*` files, `routes.jsonl`, `.env` or hooks back into a
     Beadhive repo. gc's `config.yaml` rewrites (C2) would change every clone's hive config.
3. **gc databases have no Beadhive remotes.** No database on the city's server has a
   `dolt_remotes` entry pointing at a Beadhive git remote (`refs/dolt/data`), because
   `dolt-remotes-patrol` pushes to them unattended.
4. **Work crosses between the systems as a handoff, not as shared rows.**
   - Beads move between a hive and a city only by explicit, attributed copy, such as `bh` filing a
     bead from a gc result. Neither side writes to the other's database.
   - Never set `gc.routed_to` on, and never `gc sling`, a Beadhive bead.
5. **The gc environment stays inside gc.** `bh` and `bd` commands aimed at a hive must not run
   from inside a gc agent session. If one must, strip the variables from E3 first (`BEADS_DIR`,
   `BEADS_DOLT_SERVER_PORT`, `BEADS_DOLT_AUTO_START`, `BD_EXPORT_AUTO`).
6. **Host-level side effects are accepted knowingly.**
   - `gc doctor --fix` may write the global git config key `beads.role=maintainer`
     (`gascity@v1.4.2:internal/doctor/checks_beads_role.go:45`). This host already reports role
     `maintainer`.
   - The gc supervisor and its Dolt server add host load outside Beadhive's slot budget.

### Unsafe operations (for any Beadhive hive, HQ, or bd's shared server)

- `gc rig add <hive root | hive worktree | HQ>`, with or without `--adopt`, including after
  hand-adding `issue_prefix` so that `--adopt` passes.
- `gc init --dolt-host 127.0.0.1 --dolt-port 3308 …`, `gc beads city use-external` or
  `gc rig set-endpoint --external` aimed at bd's shared server.
- Any gc city that can reach a hive database through the `mol-dog-compactor`, `gc dolt compact`,
  `dolt-remotes-patrol`, `gc dolt sync` or `mol-dog-backup` orders. These flatten, push, or back up
  outside Beadhive's rules.
- Any gc city whose server or rig list includes a hive database, through the core pack's
  `orphan-sweep` (reverts bh claims), `gate-sweep` (resolves bh `gh:pr` gates), `reaper`
  (whole-server wisp deletes) or `cross-rig-deps` (rewrites `blocks` edges) orders.
- `gc dolt-cleanup --force` (or the `mol-dog-stale-db` order) against `127.0.0.1:3308`.
- `gc beads migrate-contract`, `gc beads endpoint repair --create-missing-databases`,
  `gc beads database rename` and `gc doctor --fix` on any scope whose metadata names a hive
  database.
- `gc sling <agent> <hive-bead>`, `gc bd … --rig <hive>`, `gc bd release-if-current`, or setting
  `gc.routed_to` on a hive bead by hand.
- From the Beadhive side:
  - running `bh work loop` (unscoped `bd reclaim`) on any database where gc claims live
  - running `bd migrate schema` or setting `BD_ALLOW_REMOTE_MIGRATE` against the shared server
    for gc's benefit
  - committing gc-rewritten `.beads/` files

### If shared state is ever reconsidered

Shared state would need at least these changes. Each is a planning-plane bead, and none belongs
to this spike.

- A gc city configuration that disables the `dolt` pack's compactor, remote-patrol, stale-db and
  backup orders, and the `core` pack's `orphan-sweep`, `gate-sweep`, `reaper` and
  `cross-rig-deps`, with that suppression verified.
- `bh`'s local-loop `bd reclaim` scoped by label or assignee to Beadhive-owned claims, or gc
  renewing its leases with native `bd heartbeat`.
- An agreed owner of tracked `.beads/config.yaml` and of `types.custom`, with no rewrite of
  `export.auto` or `dolt.auto-start`.
- gc-created beads carrying Beadhive's identity and `complexity:` labels, or a lint exemption for
  a declared gc namespace.
- Every gc write passing `guard_primary` and the claim-epoch fence. Today this is structurally
  impossible, because those guards live only in `bh` verbs.
- The four interface contracts already listed in `bh-d75bi` Amendment 1: state vocabulary, event
  beads, write fence, and return path.

### Candidate defense-in-depth items for the planner (not filed here)

- A `bh doctor` check that flags `gc.endpoint_origin` or `gc.*` keys in a hive's
  `.beads/config.yaml`, or `gc.routed_to` on hive beads.
- Scoping `localloop`'s `bd reclaim` to Beadhive-claimed work, which is worthwhile regardless of
  Gas City.
