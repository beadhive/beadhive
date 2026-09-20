# `bh-28emy.3`: Beads 1.3.0 schema and service certification

Captured on `beadhive-factory` on 2026-09-20 after the remote/ref inventory in
`bh-28emy.2`. This machine is both the local and Factory install target: host
`6ae345b9-81a8-4c9b-8661-c5a4420fc12d`, account `bees` (uid 8335). No second machine or
independent clone performed a migration.

## Qualified artifacts and stop gate

- Candidate repository revision before this proof: `ab0bc276efd213f84f7d9394c0be13f4ecc87a1d`
  on `wt/bead/epic/bh-28emy`.
- Nix profile generation: `3`, target
  `/nix/store/7ymxvgx0wdvkcn82csg1rp02nwcfnnyl-profile`.
- Beads: `/nix/store/b7lc4xl35b2sl6h9pg492gh634pdxafy-beads-1.3.0/bin/bd`,
  `1.3.0 (f45b249ce: HEAD@f45b249ce6b4)`.
- Dolt: `/nix/store/3gsing0yw4rqfmh5hlz0lm99jqwysd7d-dolt-2.3.5/bin/dolt`, `2.3.5`.
- A fresh login shell and a transient user-service probe both resolved those exact profile and
  store paths. The transient unit exited `0/SUCCESS`.
- All 33 database rollback refs were present in the committed `bh-28emy.2` inventory before the
  stop gate. No unrecorded database was touched.
- Immediately before shutdown, port 3310 had no connected TCP client and no active `bd` or `bh`
  writer. The Beads 1.1.2 agent image remained quarantined and cache-local servers were excluded
  from publishing for the duration of the gate.
- `bh bd dolt stop` gracefully stopped PID 431647, the old
  `/nix/store/bk091l5yqzm7zdvhsv7rks4h5wbqpm39-dolt-2.2.3/bin/dolt` server, after flushing three
  working sets. The process exited and port 3310 was proven unbound.
- During the stop window an `agentguides/infra` checkout briefly auto-started a separate-data-dir
  Dolt 2.3.5 server on port 3310. It was identified by config and executable, stopped through its
  own `bd dolt stop`, and made no shared-store change.
- Beads 1.3.0 initially consulted a stale dynamic port allocation (3308). The replacement was
  therefore started through the same managed `bd dolt start` lifecycle with the documented
  `BEADS_DOLT_SERVER_PORT=3310`, `BEADS_DOLT_SHARED_SERVER=1`, and
  `BEADS_SHARED_SERVER_DIR=~/.beads/shared-server` selectors. It started as PID 409202.
- `/proc/409202/exe` resolved to the Dolt 2.3.5 store path above; its command line named the
  shared-server config, it listened only on `127.0.0.1:3310`, and `bh bd dolt test` passed.

No `bd compact`, `bd flatten`, raw `dolt push`, `dolt gc`, schema-skew override, or force push was
used.

## Schema migration and data invariants

The sole designated host/account migrated every v62 database serially with explicit
`bd migrate schema --force`, selected by `BEADS_DOLT_SERVER_DATABASE`. The command stopped on the
first error; all 23 invocations completed and reported schema v66. The ten databases already at
v66 were not independently re-migrated. Auto-export correctly refused to overwrite the `bh`
checkout's unrelated JSONL file while a different database was selected.

Every post-migration count below came from the final Beads 1.3.0 client. It exactly equals the
pre-migration count captured with a schema-compatible client. A successful final-client read,
together with the explicit migration receipt (or the recorded pre-existing v66 state), certifies
schema v66 for every row.

| database | before | after | final schema |
|---|---:|---:|---:|
| `_HUB_ISSUES_NO_IDS` | 9536 | 9536 | 66 |
| `ag_cp` | 7 | 7 | 66 |
| `ag_hp` | 14 | 14 | 66 |
| `ag_infra` | 165 | 165 | 66 |
| `ag_run` | 235 | 235 | 66 |
| `agf` | 3 | 3 | 66 |
| `ah` | 767 | 767 | 66 |
| `beads_global` | 0 | 0 | 66 |
| `bex` | 0 | 0 | 66 |
| `bh_app` | 1024 | 1024 | 66 |
| `bh_cp` | 233 | 233 | 66 |
| `bh_frame` | 168 | 168 | 66 |
| `bh_ndl` | 5 | 5 | 66 |
| `bh_oci` | 58 | 58 | 66 |
| `dxnvh` | 109 | 109 | 66 |
| `exh` | 2 | 2 | 66 |
| `hl` | 600 | 600 | 66 |
| `hq` | 728 | 728 | 66 |
| `nvhack` | 348 | 348 | 66 |
| `obs` | 369 | 369 | 66 |
| `rst` | 2 | 2 | 66 |
| `sgen` | 0 | 0 | 66 |
| `unp` | 1 | 1 | 66 |
| `bbox` | 56 | 56 | 66 |
| `bd_0xl0c1` | 54 | 54 | 66 |
| `beads` | 84 | 84 | 66 |
| `bh` | 4451 | 4451 | 66 |
| `bh_baml` | 296 | 296 | 66 |
| `bh_infra` | 446 | 446 | 66 |
| `bhui` | 1198 | 1198 | 66 |
| `bvend` | 9 | 9 | 66 |
| `jsmd` | 38 | 38 | 66 |
| `jsmm` | 41 | 41 | 66 |

`bd doctor --server --json` on the primary database returned `overall_ok: true`: server reachable,
Dolt 2.3.5, database accessible, schema compatible, pool healthy, and no stale test/polecat
database. The live status reported 5898 total primary-database records; the table uses the stable
`bd list --all` projection count above for its before/after invariant.

## Managed publication and rollback boundary

The 22 managed databases were published serially through `bh ... bd dolt push` with
`BEADS_FSCK_TIMEOUT=900s`, explicitly bound to `127.0.0.1:3310` and their recorded shared database.
The first `ag-cp` attempt lacked those selectors, tried a stale per-project port, failed before
opening or pushing a database, and stopped the sweep. No failed auto-start process remained. The
correctly routed retry and every subsequent managed push succeeded.

Fresh, independent `git ls-remote <remote> 'refs/dolt/data'` results after publication:

| database | GitHub repository | post-migration `refs/dolt/data` |
|---|---|---|
| `ag_cp` | `agentguides/claude-plugin` | `088d4f372f7cb618f62ddf82572a1b0a6d05c06d` |
| `ag_hp` | `agentguides/hermes-plugin` | `8cc3351c9c398609536f8ee9dc3fa38a2ffc2c1a` |
| `ag_infra` | `agentguides/infra` | `8d7144440611ea5fc4f054b1d254e6a96ab1fbad` |
| `ag_run` | `agentguides/runtime` | `ef8511c9c39ade3f9d4bc428c689269680b47e55` |
| `agf` | `briancripe/agentic-git-flow` | `84643fb3bdc0241cc22460f83be5a0eed0538a5c` |
| `ah` | `briancripe/agent-hitch` | `ec17b0286808876495e99b587056891536c16554` |
| `bbox` | `briancripe/baml-box` | `3851221b7edc4c47f4ca9f95f163aa82abca843e` |
| `beads` | `beadhive/beadhive-gateway` | `757f3868662d04a1c5da1885a4f63c04897d60c4` |
| `bh` | `beadhive/beadhive` | `48c482cd65182633f5ffab3aad66b959bb30cb6b` |
| `bh_app` | `beadhive/beadhive-app` | `6d3e4ab215caac68a379d5530b9523dc54a7fe3b` |
| `bh_baml` | `beadhive/baml-harness` | `f93766d975d41e20384ec3b2de89d680bf210949` |
| `bh_cp` | `beadhive/claude-plugin` | `cdacbb1e064d6d9c661868248f850327e7706621` |
| `bh_frame` | `beadhive/frame` | `033d5e3ef2f5869f056d2b7b1d0b103df70d9495` |
| `bh_infra` | `beadhive/infra` | `2ae7fa45878f83d69c8a6e443df38adf55daa75c` |
| `bh_ndl` | `beadhive/cactus-needle-shim` | `70b2d46433225c596b224f8436c43bcc267d3bc0` |
| `bh_oci` | `beadhive/containers` | `cd38541888ef016b5f311332fd25e3ff8544896a` |
| `bhui` | `beadhive/beadhive-ui` | `3fb348a0f912ddf92d06393d044d9b413170ca89` |
| `bvend` | `briancripe/baml-vendor` | `55fabc628e9353edef055648aa0a73742f3841d8` |
| `nvhack` | `briancripe/nvidia-hackathon` | `82f374351104b7979be86570b89d9b71ff5c4c51` |
| `obs` | `briancripe/observaloop` | `4f8bf7e15f677ce3037f42a0e923f202db2200ba` |
| `sgen` | `briancripe/sampler-gen` | `35abca95441d57d721fcc2a0ec8c12c0f0f4a82f` |
| `hq` | `briancripe/beadhive-hq` | `9e4644be1b984d19176dbf4608b7bd080660ab2c` |

Rollback rehearsal remained deliberately **not executed**, matching the operator's `.2` descope.
The rollback result is therefore readiness, not a restore claim: the exact last-v62 identifiers
for all 33 databases remain committed in `bh-28emy.2-pre-migration-inventory.md`, and the 22 remote
copies were proven present before migration. No command rewrote or garbage-collected those local
Dolt commits. The table above separately binds the successfully published v66 tips.

## `bd serve` contract

Two loopback-only canaries exercised the same physical host through distinct launch contexts:

1. ordinary fresh-login context, ephemeral `127.0.0.1:44521`;
2. Factory transient-service context, fixed `127.0.0.1:44522`, launched after the first canary's
   graceful shutdown to prove restart.

Both reported Beads `1.3.0`, backend `dolt`, database `bh`, and server mode from
`GET /v0/beads/context`. The advertised capabilities were:

```text
config.get config.list config.set config.unset
dependencies.add dependencies.blocking dependencies.count dependencies.cycles
dependencies.list dependencies.remove dependencies.tree
events.list events.watch
issues.addComment issues.batchApply issues.batchClose issues.batchCreate
issues.casMetadata issues.claim issues.claimNext issues.close issues.count issues.create
issues.delete issues.get issues.list issues.query issues.related issues.release issues.reopen
issues.sweep issues.update
memories.forget memories.get memories.list memories.remember
project.enforce ready.count ready.list stats.get
```

Each canary passed:

- `GET /healthz` -> 200 `{"status":"ok"}`;
- `GET /v0/beads/ready?limit=1` -> 200 with one item and `has_more: true`;
- `GET /v0/beads/issues?limit=1` -> 200 with one item and a non-empty next cursor;
- a nonexistent issue -> typed 404 `application/problem+json`, `code: not_found`;
- an empty create -> typed 400, `code: invalid_argument`, `param: actor` (ordinary canary);
- isolated create and supported `issues:delete` cleanup -> 200 in both contexts.

The ordinary canary created/read/deleted `bh-aoqrc`; the service canary created/deleted
`bh-6p1sa`. Both delete receipts reported `deleted: 1` and `events: 1`, with no dependency, label,
or reference residue. These ids no longer resolve. SIGINT on the first process and `systemctl
--user stop` on the second both logged `shutdown_start`, zero live connections, and
`shutdown_complete`; both exited cleanly.

## Events journal and upstream issue #6142

- `events-journal` is unset/disabled in this workspace. `bd events tail --since 0 --limit 1
  --json` succeeded with the expected disabled-journal notice and no missing-table error.
- Upstream `gastownhall/beads#6142` remains open. It describes Beads 1.2.2 embedded stores whose
  recorded v53 schema lacks the legacy audit `events` table, making the first issue mutation fail.
- That failure is **not reproduced** on this migrated shared-server lineage: both isolated HTTP
  creates succeeded, and each cleanup deleted the associated legacy audit event (`events: 1`).
  Journal experiments may proceed only with the separate `bd_events_journal` replica-local and
  disabled-by-default semantics documented by Beads 1.3.0; the absence of #6142 here does not
  broaden those semantics.

## Disposition

The Beads 1.3.0 / Dolt 2.3.5 shared-server floor is certified for the downstream v1.3 spikes.
Only Beads 1.3.0-compatible clients may resume. The Beads 1.1.2 agent image remains quarantined
from this v66 store until its own upgrade lands.
