# Factory HQ — the fleet's durable, authoritative store

**Factory HQ** is the fleet's authoritative home for HQ-origin Beads and HQ configuration
(module: `hq.py`). The cross-hive read aggregate is a separate, disposable per-host [hub](HUB.md)
built by `bh sync`. Fleet configuration uses explicit `hq.mode`: Git mode reads `fleet.yaml`;
`dolt-server` mode reads a committed shared SQL config snapshot. Selecting a config backend does
not move HQ's own Beads database. See [fleet membership and HQ modes](FRAME-FLEET-MEMBERSHIP.md)
and [CONFIGURATION](CONFIGURATION.md#hq-configuration-authority).

It is registered as a **singleton** (`kind=hq`) under the reserved synthetic identity
`local/factory/hq` — local infra like the hub/cache, never a git-workspace provider, never
a real repo you clone by hand.

## Where it lives

`~/.beadhive/hq/` (override `$BH_HQ`, legacy alias `$WS_HQ`) — the local Git HQ checkout and
`bd` store (embedded Dolt under `.beads/`, prefix `hq`). Fleet configuration can select this
Git carrier or a separate shared Dolt config service; that selection does not relocate the HQ
Beads store.

HQ holds only what originates in HQ. Cross-hive aggregation belongs to the [hub](HUB.md) and
always lands there (`bh sync`, read with `bh hub bd ready` / `bh hub intake`); `bh hq bd …` and
`bh hq intake` read HQ's own store. See [Hub vs HQ](#hub-vs-hq) — this reverses what bh-ohx2
recorded, deliberately.

## Repo layout

Local-only (no remote wired yet), HQ is just a `bd`-initialized store — `.beads/` and nothing
else. `bh hq init` scaffolds the distributable layout the first time it wires a remote:

```text
~/.beadhive/hq/
├── .beads/            # embedded Dolt — HQ-origin Beads
├── fleet.yaml         # Git-mode fleet config base (CONFIGURATION.md#fleet-host)
├── workspace.toml     # git-workspace providers — fleet truth (the clone PATH stays host-local)
└── hosts/
    └── README.md      # placeholder — per-host manifests land here as `<host_id>.yaml`.
                        # Schema + read/write/validate API: beadhive.hosts (bh-ytbb.3). No
                        # writer populates this directory yet — `bh host init` (bh-ytbb.5) is
                        # the CLI that will. Today the HOST side of the fleet/host split still
                        # lives in each host's own local ~/.beadhive/config.yaml.
```

In Git mode, `fleet.yaml` is written from the subset of the initializing
host's own resolved config that belongs to the fleet partition (`schema_version`,
`delimiter`, `orgs`, `dimensions`,
`exclude`, `managed_repos`, `work`, `passthrough` — see
[config_partition.py](../src/beadhive/config_partition.py)). `workspace.toml` copies that
host's own `workspace*.toml` when the git-workspace integration is enabled and resolvable,
else a placeholder a later host fills in.

## Naming pattern

In Git mode, the distributable remote is `<owner>/beadhive-hq` on GitHub. `config.hq_remote()`
resolves it: an explicit `hq.remote` config key wins; otherwise `<owner>` is derived from the
resolved workspace identity's org. Set it explicitly with:

```sh
bh config set hq.remote <owner>/beadhive-hq
```

`hq.remote` is host-scoped config (it derives from the identity resolved *on this host*), so
it is not itself carried inside `fleet.yaml`.

## `bh hq init` — initialize or attach HQ {#hq-init}

```sh
bh hq init             # Git mode: stand up/scaffold/wire/push; SQL mode: report selected attachment
bh hq init --dry-run   # Git mode: preview the pre-push backup plan; SQL mode: inspect attachment
```

**First call in Git mode** (no `hq`-kind hive registered): `bd`-inits the store at
`~/.beadhive/hq` (prefix `hq`) and registers the synthetic `local/factory/hq` identity. It does
not aggregate the fleet. `bh sync` builds the disposable per-host hub. If SQL config is already
selected, `bh hq init` reports the committed central attachment and returns without wiring a
Git remote.

**Every call in Git mode** (including the first) then wires the remote, which is itself idempotent:

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

Re-running `bh hq init` once the Git remote is wired is a clean no-op.

### Git-mode fleet writes after init {#fleet-writes-after-init}

When `hq.mode: git` is selected and this host has a real `fleet.yaml`, `managed_repos` is
fleet-scoped truth. Hive registry changes update that Git-mode config source. This section does
not describe `hq.mode: dolt-server`, where the committed SQL snapshot is authoritative.

Git HQ config edits remain local until published. In Git mode, `bh hq push` commits dirty
tracked HQ config and publishes the Git and HQ Beads halves; it does not refresh the per-host
hub or switch config authority. SQL mode reads the selected committed snapshot and does not
publish central config. `bh sync` builds the per-host hub view separately:

```sh
bh hq push       # publish in Git mode; in SQL mode, publish HQ Beads only if a local store exists
bh sync          # rebuild the local cross-hive hub
```

For central SQL config setup, source review, initial seed and guarded switch, follow the
[migration runbook](design/dolt-hq-config-migration-runbook.md).

## `bh hq push` — publish HQ again, after `init` {#hq-push}

```sh
bh hq push             # Git mode: auto-commit tracked config changes, then publish Git and HQ Beads
bh hq push --dry-run   # preview only; no writes
bh hq status           # read-only status of available Git and HQ Beads observations
bh hq status --json    # versioned identity, location, availability, and freshness contract
```

`bh hq init`'s `engine.push_state` call is **one-shot** — it fires only the first time a remote
is wired; every later `bh hq init` hits the "remote already configured" no-op and pushes
nothing. Before `bh hq push` existed, keeping HQ current took three hand-run, hand-ordered
commands (`bh sync`, `git -C ~/.beadhive/hq push`, `cd ~/.beadhive/hq && bd dolt push`), with
nothing in the CLI surfacing that HQ had drifted from its remote at all (bh-z9hl).

`bh hq push` does not run `bh sync`; the derived cross-hive aggregate belongs to the hub and is
per-host. In Git mode it commits dirty tracked HQ config, then publishes `main` and HQ's own
Dolt Beads state when either has changes. In SQL mode it reads and reports the selected committed
config revision, then publishes only HQ-origin Beads state when a local HQ Beads store exists;
without a local store, that Beads action is a no-op. It does not publish central config. The only
preview option is `--dry-run`; `--no-sync` and `--git-only` are not supported.

`bh hq status` reports available Git, config and HQ Beads observations. `bh hq status --json`
emits the v1 machine contract documented by
[`docs/schemas/hq-status-v1.schema.json`](schemas/hq-status-v1.schema.json). Its canonical
`cwd` comes from Beadhive's own `BH_HQ` → `BH_HOME/hq` resolution. The projection distinguishes
available, authoritatively absent, and unavailable observations. Its retirement intent is
advisory: incomplete facts always say retain, and consumers still own proof that a target is
plugin-owned and locally safe to remove.
The command remains read-only in every state.

### Canonical HQ instance identity

`beadyard.json` carries one anonymous, canonical UUID4 named `beadyard_id`. It identifies the
HQ instance across its Git and Dolt config carriers, portable snapshots, and backups. It does
not authenticate an operator, replace a signing key, or identify a storage backend. A new HQ
generates it once during explicit setup; ordinary loads and config publications never mint or
replace it. Copying or migrating the same HQ preserves its ID. A separately created HQ gets a
different ID even when its human name, repository name, or database name matches.

`bh hq beadyard --json` inspects the selected Git or Dolt config backend. Its `legacy` state
means no identity has been published; `pending` means an explicit Git adoption has durable
local intent and may be retried against its **original** main revision; `incomplete` means Git
main carries an ID while protected fleet config still needs its matching v2 publication.
Unavailable authority returns an error rather than a guessed legacy state. The separate
identity view leaves the published `bh hq status --json` v1 shape unchanged.

Legacy adoption is an explicit operator operation. After recording the revision from
inspection, use `bh hq beadyard-adopt --expected-revision <original> --confirm`; protected
Git also requires `--operator-key <approved-ssh-private-key>`. Git adoption checks and signs
the original main parent, then publishes the same ID under the original protected config
revision and its immutable witness. A lost reply can be retried with the **same** original
revision against a durable, owner-held completion receipt. Recovery verifies the signed
first main child, its unchanged identity, and the first protected config witness, even if
ordinary main or config edits followed. A later unrelated writer does not silently become a
new adoption parent. Selected
Dolt config-only adoption uses the original committed config CAS and a durable publication
receipt without creating a Git or Beads HQ checkout. A copied foreign ID, missing intent,
changed original parent, or mismatch between local and committed IDs refuses.

Protected legacy authority grants remain recorded during adoption. New frame admission
stays denied until an operator explicitly runs `bh hq authority bind-beadyard
--expected-revision <authority-sha> --operator-key <approved-ssh-private-key> --confirm` and
the same frame signer publishes fresh, correctly bound registration and heartbeats for the
ordinary observer to accept. Binding preserves the frame, holder, instance, key, epoch,
receipt, grant expiry, epoch floors, and retired history; it cannot revive an expired grant.
Historical unbound heartbeats and hive leases do not qualify new intake. An existing holder
may renew or release its exact live lease under a signed v2 carrier, the original lease CAS,
current bound authority and config, and the established expiry/drain rules. No actor, branch,
signing key, or hive lease is automatically reset or retired by adoption.

For a protected Git server, the committed config change also fences the server's old
`hive_policies[*].config_head` projection. An operator must explicitly run
`bh hq beadyard-policy-refresh --expected-policy-digest <original-sha256>
--expected-config-head <original-config-sha-or-empty> --operator-anchor <operator-anchor>
--expected-config-parent <signed-v1-config-sha> --anchor <operator-anchor>
--anchor <each-frame-anchor>
--confirm` before a new bound lease renewal. The command requires the complete, explicitly
named protected anchor set. It verifies that the latest signed config commit added only this
HQ's identity to the original v1 config, then changes only the projected config head and each
anchor's matching policy digest. Generation, signer, executable/runtime bytes, projected hive
requirements, validity, and lease fields stay pinned. The broker's receive lock covers the
original-head check and the entire rotation. If interrupted, old and new anchor bytes may be
temporarily mixed; readers fail closed until the same operator retries the exact original
request against its durable intent. A completed rotation retains an operator-owned receipt;
the same original request can recover a lost final reply only while the signed config head
and all published policy/anchor bytes still match. Ordinary reads never refresh an anchor
automatically. A newer config head or foreign anchor bytes refuses recovery. This is an
operator filesystem custody operation on the server; it does not authenticate with a private
key, deploy, or contact
an external HQ. The separate adoption and authority-bind commits still require approved
operator signatures. A blank original policy projection is accepted only when the pinned
signed v1 config parent is its first publication and the v2 child contains only the identity
addition.

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
| What it holds | every hive's beads, hydrated | HQ-origin `hq-` prefixed beads; Git-mode config files may also live here |
| Where truth lives | in each hive | HQ Beads here; fleet config in the selected Git or SQL backend |
| Remote | none, ever | Git mode: `hq.remote` for Git and `refs/dolt/data`; SQL config has its own endpoint |
| Rebuildable | yes — `rm -rf` + `bh sync` | no; it is the original |
| Issues ids | **no** | yes (`hq-…`) |
| Refreshed by | `bh sync` | HQ Beads are authored; central config is published through its selected backend |
| Read with | `bh hub bd …` | `bh hq bd …` |

**Why they had to split.** The historical rows described below are migration context; current HQ
contains HQ-origin beads, while each host builds its own derived hub. bd's sync-concepts and
bucket-federation guides give one rule:
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

`bh hq push` does not refresh the hub. In Git mode it publishes HQ's Git and Beads halves; in
SQL mode it reports the central config revision and publishes HQ-origin Beads only when a local
HQ store exists. The `--no-sync` and `--git-only` flags are gone.

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

## Authority expiry and renewal {#authority-expiry}

The protected authority carrier carries an `expires_at`. When it passes, every frame is fenced
until an operator publishes a renewal. The lapse used to be silent until it fenced. These are
the read-only surfaces that make it visible:

- `bh hq authority status` reports `expires_at`, `expires_in_s`, `expires_in`, `revision`,
  `config_bound` (the authority's config head equals the latest head) and `expiring_soon` at the
  top level. It works on a runtime host and with `BH_HQ_OPERATOR_SETTINGS`, on the SQL and Git
  backends, and still reports once the authority has expired.
- `bh hq authority check` (floor from `BH_HQ_AUTHORITY_MIN_REMAINING`, e.g. `6h`) exits
  non-zero, with the exact renew command, when the authority is expired, not bound to the
  latest config head, or has less than that floor left. It exits 0 when healthy. Use it in
  scripts and as the release-upgrade preflight.
- `bh work claim|check|submit|merge`, `bh plan file` and the start of every validation gate print
  one stderr `WARN` line (time remaining and the renew command) when expiry is inside the lead
  time. Nothing is printed outside it, and a warning never fails a command.
- `bh doctor` reports an `HQ authority expiry` section: WARN inside the lead time, FAIL when
  expired or config-unbound.

The lead time is the `BH_HQ_AUTHORITY_WARN_WITHIN` environment variable (duration such as
`90m`, `24h`, `2d`; default `24h`). It is deliberately not a fleet or host key: a fleet edit moves
the HQ config head and a new host key breaks older readers of a shared HOST file.

### Renewing from an operator host (for example the laptop) {#authority-laptop-renew}

A released `bh` builds its control plane from the running host's `host.yaml`. An operator host
whose `host.yaml` has no `hq.sql.authority_writer` binds it from a file instead, with
`BH_HQ_OPERATOR_SETTINGS=<file>` on `bh hq authority renew|grant|observe|bind-beadyard|status|check`
and `bh host release-upgrade plan|apply|check`. The file is JSON or YAML:

```json
{
  "hq": {
    "sql": {
      "reader": {"host": "hq.example.net", "port": 3306, "database": "beadhive_hq",
                 "user": "reader", "tls_mode": "required", "server_name": "hq.example.net",
                 "ca_file": "/abs/path/ca.crt",
                 "credential": {"config_path": "/abs/fnox.toml", "profile": "hq", "key": "READER"}},
      "authority_writer": {"host": "hq.example.net", "port": 3306, "database": "beadhive_hq_runtime",
                           "user": "authority_writer", "tls_mode": "required",
                           "server_name": "hq.example.net", "ca_file": "/abs/path/ca.crt",
                           "credential": {"config_path": "/abs/fnox.toml", "profile": "hq", "key": "WRITER"}},
      "runtime": null,
      "runtime_backend_identity": "<pinned>", "runtime_generation": "<pinned>",
      "runtime_operator_public_key": "ssh-ed25519 AAAA..."
    }
  }
}
```

Credentials stay references (fnox config path, profile and key); the file holds no secret. It is
refused with an error naming the key when `hq.sql.runtime` is set or `hq.sql.authority_writer` is
missing, so it cannot make a frame an authority writer. Renewal:

```sh
BH_HQ_OPERATOR_SETTINGS=settings.json bh hq authority status      # note `revision`
BH_HQ_OPERATOR_SETTINGS=settings.json bh hq authority renew \
  --expected-revision <revision> --operator-key <key> --duration 86400 --confirm
```

## UNSUPPORTED: disabling HQ authority enforcement {#unsupported-disabling-hq-authority-enforcement}

> **UNSUPPORTED — dev/prototype instances only.** Do not set this on a production executor.

`BH_HQ_AUTHORITY_ENFORCE=false` turns off HQ runtime-authority enforcement for every `bh`
process that has it in its environment. Unset, empty, or `true` keeps today's fail-closed
behavior. Any other value is an error, never a fallback: `bh` exits with status 2.

**What stops being enforced on that host:**

- authority expiry, both the signed runtime authority and the candidate grant;
- the config binding, so a config head that the authority does not cross-reference is
  tolerated;
- the authority-derived eligibility predicates, which become satisfied-with-warning:
  `authority_available`, `admitted_active`, `not_cordoned`,
  `reviewed_admission_or_emergency`, and the desired-state halves of
  `current_frame_incarnation`, `beadyard_binding`, `release_matches`, `conformance_pass`
  and `capabilities_match_admission`;
- in hive-lease ownership reads: admission state, cordon, and per-hive policy validity.

**What is unchanged:**

- signatures, the replay floor, and the principal-to-incarnation route;
- the frame-manifest-to-own-lease identity checks;
- the heartbeat, which follows `BH_FRAME_HEARTBEAT`;
- hive-lease holder and lease expiry, renewal through the receiver, and validation.

**Trust delta.** With enforcement off, the frame accepts work with no valid operator
authority. Cordon, release pins, caps and expiry are not enforced there, so the frame is
**self-asserted**. The scope is one host and one process environment, never the fleet. The
operator key still never touches the frame.

**How to set it.** It is an environment variable, like `BH_FRAME_HEARTBEAT` and
`BH_HQ_SQL_LIVENESS`. It needs no fleet-config publish and no `host.yaml` change:

- The host schema rejects unknown keys, and pre-0.22.x readers (the 0.21.3 heartbeat sender)
  share the HOST file, so a `host.yaml` key would break them.
- A fleet key would itself need an authority-bound publish.

Set it in the systemd unit environment of **every** `bh` process on that host: the host
daemon, the frame bridge, and the heartbeat. A `host.yaml` key (`hq.authority.enforce`) may
replace the variable once no pre-0.22.x reader shares a HOST file. That is a follow-up, not
part of this switch.

**How it shows up.** While it is disabled:

- every `bh` command, the host daemon and the frame bridge print one stderr banner: "HQ
  authority enforcement DISABLED on this host — unsupported, dev/prototype only";
- `bh doctor` lists a WARN;
- `bh hq authority status` includes `"enforcement": "disabled"` (otherwise `"enabled"`);
- `bh host eligible` marks each waived predicate `waived`, and its `--json` output carries
  `"enforcement": "disabled"` and a `"waived"` list.

**Re-enable before admitting new executors.** The operator owns turning enforcement back on
(unset the variable on every executor) **before the three new executor frames join**. On a
four-executor fleet, enforcement must be on everywhere before admission. This is the same
gate as [hive-writer partitioning ADR](design/hive-writer-partitioning-adr.md#binding-conditions)
binding conditions 13 (no single-executor escape survives admission of another executor) and
18 (state and work travel together). It precedes outline task O8 (enroll and admit the three
new executor frames). This switch does not itself gate on O8.

## See also

- [HUB](HUB.md) — the derived per-host cross-hive aggregate, and its contract.
- [CONFIGURATION — Fleet + host config](CONFIGURATION.md#fleet-host) — how `fleet.yaml` merges
  with a host's own config, the override allowlist, `--scope`, and the flat-config migration.
- [CONTROL-PLANE](CONTROL-PLANE.md) — `bh hub intake`, the fleet-wide untriaged-intake inbox.

## Signed-mode inbox retention

With `hq.sql.liveness: signed` every heartbeat adds one row to the frame's own inbox table
(`hq_live_inbox_<principal>_<epoch>`). Since bh-ce886 the operator bounds that table:

```sh
bh hq authority prune-inbox --frame <frame-id>            # dry run: counts only
bh hq authority prune-inbox --frame <frame-id> --confirm  # delete
```

It runs with the `authority_writer` credential (directly or through
`BH_HQ_OPERATOR_SETTINGS`), never on a frame. Grant that account `SELECT, DELETE` on each
inbox table it should prune. Frames keep `SELECT, INSERT, UPDATE` and gain no DELETE.

**The bound.** A row is deleted only when all of these hold:

1. it is a `heartbeat` row (`registration` and `hive_lease` rows are never touched, because
   the receiver re-verifies them as prior incumbent evidence);
2. its claimed signed `renewTime` + `leaseDurationSeconds` + 30 s skew + the retention margin
   is at or before the operator's clock;
3. it is not the newest canonical heartbeat that verifies against the incarnation's granted
   key and binds to that incarnation.

Rule 2 is why the prune never removes a beat anyone still needs. A 0.22.x signed reader
counts a beat only while its age on the reader's clock is under the lease, with at most 30 s
of skew, and the receiver refuses a first observation at or past the lease. A row past rule
2 cannot make the frame eligible for either reader, whether or not its signature verifies.
Rule 3 keeps the last beat however old, because the heartbeat sender derives its next `seq`
from it in signed mode. Rows that do not parse are left and counted, so sender misbehaviour
stays visible.

With an honest sender the table therefore holds at most one row per heartbeat interval
inside lease + skew + margin, plus the newest verified beat. A frame can still INSERT junk
into its own inbox; the bound covers honest growth, which was the unbounded part.

**The margin.** Default 1 hour. Resolution order, first match wins:

1. `hq.sql.inbox_retention_s` in the operator settings file;
2. env `BH_HQ_INBOX_RETENTION`;
3. the 1 hour default.

There is no `--retention` flag yet: a new parameter on the published `hq.authority`
operation needs a wire-catalog decision. Deletes commit in batches of 500, so a large first
backlog that outruns the `authority_writer` `operation_timeout` drains over reruns. The
result reports `"complete": false` until it has. Nothing prunes on its own; schedule the
verb if you want it periodic.

## Authority duration ceiling

`bh hq authority renew --duration <seconds|7d|36h>` signs an authority that stays valid for
that long. `--duration` defaults to 3600 s (1 h) so every renewal is an explicit lifetime
choice; renew prints the resulting `expires_at`. Use `--duration 7d` for laptop-off operation.

The signing side refuses a duration above a configurable **ceiling**. The default is 7 days
(`AUTHORITY_MAX_DURATION_DEFAULT_S` = 604800) and there is **no hard maximum**: the operator
may set any positive, finite ceiling. The same ceiling applies to the SQL and Git backends and
to Git fleet-config publication. Resolution order, first match wins:

1. `--max-duration` on `bh hq authority renew` (pending: adding a CLI parameter to the published
   `hq.authority` operation needs a wire-catalog decision; the resolver already accepts it)
2. `hq.sql.authority_max_duration_s` in the operator settings file (read only through
   `--operator-settings`; it is never a frame or fleet key)
3. env `BH_HQ_AUTHORITY_MAX_DURATION`
4. the 7 day default

A duration above the ceiling is refused with the ceiling and its source in the message.
Frame verifiers (`verified_state_at`, `validate_state`) enforce each signed `expires_at` and
have no maximum, so no frame change is needed; they accept 7 d and 30 d authorities and fence
after expiry.

Trust delta: revocation latency is unchanged. Cordon, retire and emergency actions are
operator-signed republications and take effect at once. What grows is the window in which a
stolen or forgotten authority stays valid with no operator action: up to the configured
ceiling instead of 24 h, for all executors at once because one authority row covers the fleet.
The operator owns that choice explicitly.
