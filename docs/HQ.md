# Factory HQ — the fleet's durable, authoritative store

**Factory HQ** is the one durable central store a fleet of hives can share (module: `hq.py`).
It plays two roles at once:

1. **Aggregation primary** — the same cross-hive read-cache role the [hub](HUB.md) plays
   (`bd repo add` every registered hive + sync), but durable and, once distributed,
   **shared across hosts** instead of purely local.
2. **Authoritative fleet store** — it holds `hq`-prefixed control-plane beads created
   directly in HQ (escalations, fleet-wide work — these *originate* in HQ, they are not
   derived from any hive), and, since `bh-e0y8`, the fleet-wide config base (`fleet.yaml`)
   every host's `config.load()` merges its own config under (see
   [CONFIGURATION — Fleet + host config](CONFIGURATION.md#fleet-host)).

It is registered as a **singleton** (`kind=hq`) under the reserved synthetic identity
`local/factory/hq` — local infra like the hub/cache, never a git-workspace provider, never
a real repo you clone by hand.

## Where it lives

`~/.beadhive/hq/` (override `$BH_HQ`, legacy alias `$WS_HQ`) — a durable git + `bd` store
(embedded Dolt under `.beads/`, prefix `hq`).

HQ holds only what originates in HQ. Cross-hive aggregation belongs to the [hub](HUB.md) and
always lands there (`bh sync`, read with `bh hub bd ready` / `bh hub intake`); `bh hq bd …` and
`bh hq intake` read HQ's own store. See [Hub vs HQ](#hub-vs-hq) — this reverses what bh-ohx2
recorded, deliberately.

## Repo layout

Local-only (no remote wired yet), HQ is just a `bd`-initialized store — `.beads/` and nothing
else. `bh hq init` scaffolds the distributable layout the first time it wires a remote:

```text
~/.beadhive/hq/
├── .beads/            # embedded Dolt — hq-prefixed beads + the cross-hive aggregate
├── fleet.yaml         # fleet-wide config base (CONFIGURATION.md#fleet-host)
├── workspace.toml     # git-workspace providers — fleet truth (the clone PATH stays host-local)
└── hosts/
    └── README.md      # placeholder — per-host manifests land here as `<host_id>.yaml`.
                        # Schema + read/write/validate API: beadhive.hosts (bh-ytbb.3). No
                        # writer populates this directory yet — `bh host init` (bh-ytbb.5) is
                        # the CLI that will. Today the HOST side of the fleet/host split still
                        # lives in each host's own local ~/.beadhive/config.yaml.
```

`fleet.yaml` is written from the subset of the initializing host's own resolved config that
belongs to the fleet partition (`schema_version`, `delimiter`, `orgs`, `dimensions`,
`exclude`, `managed_repos`, `work`, `passthrough` — see
[config_partition.py](../src/beadhive/config_partition.py)). `workspace.toml` copies that
host's own `workspace*.toml` when the git-workspace integration is enabled and resolvable,
else a placeholder a later host fills in.

## Naming pattern

The distributable remote is always `<owner>/beadhive-hq` on GitHub. `config.hq_remote()`
resolves it: an explicit `hq.remote` config key wins; otherwise `<owner>` is derived from the
resolved workspace identity's org. Set it explicitly with:

```sh
bh config set hq.remote <owner>/beadhive-hq
```

`hq.remote` is host-scoped config (it derives from the identity resolved *on this host*), so
it is not itself carried inside `fleet.yaml`.

## `bh hq init` — stand up, scaffold, wire, push {#hq-init}

```sh
bh hq init             # stand up (first call) / scaffold + wire + push (idempotent)
bh hq init --dry-run   # preview the pre-push backup plan; no writes
```

**First call ever** (no `hq`-kind hive registered): `bd`-inits the store at `~/.beadhive/hq`
(prefix `hq`), registers the synthetic `local/factory/hq` identity, then `bd repo add`s every
registered hive and syncs — aggregation moves off the disposable hub onto HQ.

**Every call** (including the first) then wires the remote, which is itself idempotent:

- Already has a `git remote origin`? Prints its URL and no-ops.
- No `hq.remote` resolvable? Skips wiring with a hint to set one.
- Otherwise: probes the remote (`git ls-remote --heads`) and refuses — **never force-pushes**
  — if it's unreachable or already carries content on `refs/heads/*`.
- Takes and verifies a **three-level pre-push backup** under `~/.beadhive/hq-backups/<date>/`
  before touching anything: a portable JSONL export (line count cross-checked against `bd
  status`'s reported issue count), a tarball of the local embedded-Dolt store, and — only when
  the remote already carries a pre-existing `refs/dolt/data` — a copy of it pushed to a
  `refs/backup/dolt-data-schema-<...>-bd-<...>-<date>` ref outside `refs/dolt/`, so it can
  never be clobbered by the push that follows. Refuses to push if any level fails to verify.
- Scaffolds `fleet.yaml`/`workspace.toml`/`hosts/` (only what's missing — idempotent),
  commits if anything changed, `git remote add origin` + `git push origin main`, then `bd dolt
  remote add origin` + push `refs/dolt/data`.

Re-running `bh hq init` once the remote is wired is a clean no-op.

### Fleet writes after init — routine commands leave HQ dirty {#fleet-writes-after-init}

Once this host has a real `fleet.yaml` (i.e. `bh hq init`/`bh hq clone` has run), `managed_repos`
becomes fleet-scoped truth, so **every** `bh hive init` / `bh hive add` / `bh hive rm` on this
host writes the updated list straight into the HQ working copy's `fleet.yaml`
(`~/.beadhive/hq/fleet.yaml`) instead of the host's own `config.yaml` — not just once at
init time, but on every one of those routine calls from then on.

That write is **local-only** to the HQ working copy: nothing commits or pushes it. So after any
`bh hive init`/`add`/`rm`, `~/.beadhive/hq` is left git-dirty with no automatic next step. Publish
it with `bh hq push` (below) — it commits the dirty `fleet.yaml`, refreshes the aggregate, and
pushes both halves in one call:

```sh
bh hq push
```

You can skip this if you don't yet need other hosts to see the change — the local HQ working
copy stays correct and usable for this host either way; it's just unsynced from the fleet until
pushed.

## `bh hq push` — publish HQ again, after `init` {#hq-push}

```sh
bh hq push             # push both halves of HQ; reports what moved on each
bh hq push --dry-run   # preview only; no writes
bh hq status           # read-only: ahead/behind for BOTH halves, no push
bh hq status --json    # versioned identity, location, availability, and freshness contract
```

`bh hq init`'s `engine.push_state` call is **one-shot** — it fires only the first time a remote
is wired; every later `bh hq init` hits the "remote already configured" no-op and pushes
nothing. Before `bh hq push` existed, keeping HQ current took three hand-run, hand-ordered
commands (`bh sync`, `git -C ~/.beadhive/hq push`, `cd ~/.beadhive/hq && bd dolt push`), with
nothing in the CLI surfacing that HQ had drifted from its remote at all (bh-z9hl).

`bh hq push`:

1. Refreshes the aggregate (`bh sync`) — the SAME fleet-wide walk that can block `bh hive
   onboard` for many minutes on a large fleet (bh-d5jhc). An operator who only wants to
   publish fleet config (the git half — `fleet.yaml`/`workspace.toml`/`hosts/`) should not pay
   this: `--no-sync` skips the refresh but still publishes both halves as they already are;
   `--git-only` also skips the Dolt half, since there is then nothing freshly aggregated to
   push there anyway.
2. Commits any dirty tracked content (e.g. the `fleet.yaml` drift above) — safe to auto-commit
   because HQ's tracked files are fleet configuration, not arbitrary work-in-progress.
3. Pushes the git half (`main` — fleet.yaml/workspace.toml/hosts/) if it's ahead of `origin/main`.
4. Pushes the Dolt half (`bd dolt push` via the Engine seam) if it has anything to push.
5. Reports what moved on each half; idempotent — prints "nothing to push" cleanly when there's
   nothing to do.

`bh hq status` is the read-only half of the same machinery (`safety.scan(hq_dir, fetch=True)` —
the same ahead/behind primitive `bh hive sync-remote` and `bh doctor`'s fleet-health section
already trust): it reports ahead/behind for both halves without pushing anything, paying for one
real network call (`bd federation status`) so the Dolt count is verified rather than guessed.

`bh hq status --json` emits the v1 machine contract documented by
[`docs/schemas/hq-status-v1.schema.json`](schemas/hq-status-v1.schema.json). Its canonical
`cwd` comes from Beadhive's own `BH_HQ` → `BH_HOME/hq` resolution, so an integration must not
reconstruct or guess the path. The projection distinguishes available, authoritatively absent,
and unavailable observations. Its retirement intent is advisory: incomplete facts always say
retain, and consumers still own proof that a target is plugin-owned and locally safe to remove.
The command remains read-only in every state.

Both depend on `main` carrying upstream tracking, which `bh hq init`'s first push now sets
(`git push -u origin main`) — a bare `git push`/`git pull` in `~/.beadhive/hq`, and the
ahead/behind detection itself, both silently failed/hid drift without it.

The raw passthrough also works for the one-shot case: `bh hq bd dolt push` publishes the Dolt
half directly. It needs no special allowance any more — `bh hq bd …` goes to HQ's own store,
which is authoritative and takes ordinary writes. (The *hub* refuses `bd dolt push` outright:
it has no remote and is never published. See [HUB — the contract](HUB.md#contract).)

## `bh hq clone` — bootstrap a second host {#hq-clone}

```sh
bh hq clone
```

For a host with **no local HQ at all**: refuses (never clobbers) if `~/.beadhive/hq` already
exists. Requires `hq.remote` to resolve to something (explicit config or a derivable identity).
Clones `main` (`fleet.yaml`/`workspace.toml`/`hosts/`), hydrates bead state from
`refs/dolt/data` via `bd bootstrap` — the same seam the hub uses to hydrate an uncloned hive —
then registers the `local/factory/hq` synthetic identity so `bh hq bd ready` resolves to it
afterward.

## Hub vs HQ — two stores, two jobs {#hub-vs-hq}

**Settled by bh-89wxf: they are DIFFERENT STORES.** This supersedes bh-ohx2, which recorded the
opposite — that `bh hq bd …` and `bh hub bd …` were one code path hitting one store. That was
accurate when it was written and is no longer true.

| | [hub](HUB.md) — `~/.beadhive/hub/` | HQ — `~/.beadhive/hq/` |
|---|---|---|
| What it holds | every hive's beads, hydrated | HQ's own `hq-`prefixed beads + fleet config |
| Where truth lives | in each hive | here |
| Remote | none, ever | `hq.remote` (git + `refs/dolt/data`) |
| Rebuildable | yes — `rm -rf` + `bh sync` | no; it is the original |
| Issues ids | **no** | yes (`hq-…`) |
| Refreshed by | `bh sync` | nothing — it is authored, not derived |
| Read with | `bh hub bd …` | `bh hq bd …` |

**Why they had to split.** bd's own sync-concepts and bucket-federation guides give one rule:
**one database per remote path**, and "one path, multiple databases" is named there as creating
irreconcilable divergence. HQ's path was carrying two — HQ's authoritative beads, and a
per-host derived aggregate that *every* host rebuilt wholesale and pushed. Since every mutation
touches `updated_at`, the guide warns that even disjoint edits to one issue between syncs
conflict; a per-host wholesale rebuild inside a replicated database is the maximal-conflict
shape available. And `bh hq clone` supports a second host explicitly, so this was designed for,
not hypothetical.

So each half went to the side of the line bd already draws:

- **HYDRATION** (`bd repo add` / `bd repo sync`, reading derived JSONL, N databases one-way
  into one) → the hub. Derived, per-host, rebuildable, never pushed.
- **DOLT REPLICATION** (`bd dolt push`, one database, many hosts) → HQ. Authoritative,
  exactly one database on its path.

`bh hq push` therefore no longer refreshes anything, and the `--no-sync` / `--git-only` flags
that existed only to dodge that refresh are gone with it.

### `intake` — the naming, said out loud {#intake-naming}

`bh hq intake` used to be the director's **fleet-wide** inbox: a cross-hive read wearing HQ's
name. One verb was carrying two scopes silently. The verb keeps its meaning — "untriaged
inbox"; the **surface names the scope**:

```sh
bh hub intake        # fleet-wide, every hive — the director's inbox (was `bh hq intake`)
bh hq intake         # HQ's OWN escalations, filed by `bh escalate`
```

`bh hq intake` prints a pointer to the fleet-wide one, so the rename does not strand anybody
mid-habit.

### Migrating a host whose HQ already carries the aggregate {#prune-aggregate}

No Dolt surgery, and nothing to hand-edit. Two halves:

- **The hub half needs nothing.** It is derived: `bh sync` builds it. (A pre-existing
  `hub`-prefixed hub is moved aside and rebuilt automatically — see
  [HUB — Migrating an existing host](HUB.md#contract).)
- **The HQ half** drops the rows that were never HQ's:

```sh
bh hq prune-aggregate              # dry run: how many, and a sample
bh hq prune-aggregate --confirm    # bd delete them, then re-assert HQ is clean
bh hq push                         # publish the now-single-purpose database
```

`bd delete` is an ordinary bd mutation, so it replicates through `refs/dolt/data` like any
other — a second host picks the cleanup up on its next pull instead of each host repairing its
own copy. The alternative (export the `hq-` beads, re-init, re-import) was rejected: it
discards HQ's Dolt lineage and leaves every other host's clone diverged from a store it can no
longer fast-forward to.

Nothing is lost either way. Every pruned bead is a derived copy of a bead that still lives in
its own hive, and `bh sync` puts the cross-hive view back in the hub where it belongs.

## See also

- [HUB](HUB.md) — the derived per-host cross-hive aggregate, and its contract.
- [CONFIGURATION — Fleet + host config](CONFIGURATION.md#fleet-host) — how `fleet.yaml` merges
  with a host's own config, the override allowlist, `--scope`, and the flat-config migration.
- [CONTROL-PLANE](CONTROL-PLANE.md) — `bh hub intake`, the fleet-wide untriaged-intake inbox.
