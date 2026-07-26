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

Once `bh sync` sees an `hq`-kind hive registered, it targets HQ instead of the disposable
[hub](HUB.md) for cross-hive aggregation — so `bh hq bd ready` / `bh hq intake` keep working
the same way whether or not HQ has ever been given a remote.

## Repo layout

Local-only (no remote wired yet), HQ is just a `bd`-initialized store — `.beads/` and nothing
else. `bh hq init` scaffolds the distributable layout the first time it wires a remote:

```text
~/.beadhive/hq/
├── .beads/            # embedded Dolt — hq-prefixed beads + the cross-hive aggregate
├── fleet.yaml         # fleet-wide config base (CONFIGURATION.md#fleet-host)
├── workspace.toml     # git-workspace providers — fleet truth (the clone PATH stays host-local)
└── hosts/
    └── README.md      # placeholder — per-host manifests (`<host_id>.yaml`) are a planned
                        # follow-on, not yet consumed. Today the HOST side of the fleet/host
                        # split lives in each host's own local ~/.beadhive/config.yaml, not
                        # in a file under hosts/.
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
`bh hive init`/`add`/`rm`, `~/.beadhive/hq` is left git-dirty with no automatic next step. There
is no `bh hq push` verb yet to reconcile it — until one exists, share the change with the rest
of the fleet by hand:

```sh
git -C ~/.beadhive/hq add fleet.yaml
git -C ~/.beadhive/hq commit -m "chore(fleet): update managed_repos"
git -C ~/.beadhive/hq push
```

You can skip this if you don't yet need other hosts to see the change — the local HQ working
copy stays correct and usable for this host either way; it's just unsynced from the fleet until
pushed.

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

## Hub vs HQ — which one is authoritative {#hub-vs-hq}

Both are cross-hive read caches over the same hives, but they are not interchangeable:

- The **[hub](HUB.md)** (`~/.beadhive/hub/`) is purely local and entirely disposable — it has
  no remote of its own, and `bh sync` rebuilds it wholesale from every hive's own git remote
  on every run. Nothing you write there persists past the next sync.
- **HQ** is the durable, shareable form: once `bh hq init` has wired a remote, its
  `fleet.yaml`/`workspace.toml` are genuinely authoritative content — pushed, pulled, and
  cloned across hosts — and `fleet.yaml` is the base every host's `config.load()` merges its
  own `~/.beadhive/config.yaml` over (see
  [CONFIGURATION — Fleet + host config](CONFIGURATION.md#fleet-host)). Its `hq`-prefixed
  beads are likewise authoritative: they originate in HQ, not derived from any hive.

The part of HQ that mirrors every hive's issues (the aggregation role it took over from the
hub) is still a *derived* read cache, same as the hub always was — only the `hq`-prefixed
beads and the fleet config files are HQ's own authoritative content.

## See also

- [HUB](HUB.md) — the disposable local aggregation cache HQ supersedes once registered.
- [CONFIGURATION — Fleet + host config](CONFIGURATION.md#fleet-host) — how `fleet.yaml` merges
  with a host's own config, the override allowlist, `--scope`, and the flat-config migration.
- [CONTROL-PLANE](CONTROL-PLANE.md) — `bh hq intake`, the fleet-wide untriaged-intake inbox.
