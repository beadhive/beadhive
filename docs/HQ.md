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
- `bh hq authority check` (floor from `--min-remaining` (0.23.0+) or
  `BH_HQ_AUTHORITY_MIN_REMAINING`, e.g. `6h`) exits non-zero,
  with the exact renew command, when the authority is expired, not bound to the
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

### Fleet-config edits and the bound head {#config-edit-tolerance}

The SQL authority is signed against one config head (H0) and carries a `hive_policies`
projection. Since 0.23.1 (`bh-u67ve`) a frame, and the receiver, keep accepting the authority
after a **later** fleet-config publish (H1) that changes nothing a frame enforces, instead of
fencing every frame until the operator renews. The test is
`beadhive.hq_hive_policy.config_head_tolerated`, behind
`SqlRuntimeAuthority.bound_config_at` (`src/beadhive/hq_sql_runtime.py`). H1 is tolerated only if
all of these hold; anything else, or any doubt, still fences with "HQ config and authority
publications are not bound":

- same config backend and generation, and H1 descends from H0 (`HAS_ANCESTOR`). A **non-descendant
  head** (rewritten history) fences;
- H0 still re-verifies (document hashes, digest, witness, pins);
- every document other than `fleet.yaml` is byte-identical: `hosts/*.yaml`, `allowed_signers`,
  `beadyard.json`. Any change there fences, as does a changed **beadyard identity**;
- the ordered `managed_repos` identity (provider, org, repo, **prefix**, **kind**, upstream) is
  equal, and the policy projection of H1 rebased onto H0 equals the signed one, so any
  `frame_policy` change fences.

So an edit such as a managed repo's `work.validation_bypass` no longer fences, while a
`frame_policy`, prefix, kind, repo identity, beadyard identity, `hosts/*` or `allowed_signers`
change still needs an operator renew. **Frames keep enforcing the signed H0 snapshot**, never H1,
and the frame's own fresh config fence pins the head it actually read. Expiry, the replay floor,
signatures and `BH_HQ_AUTHORITY_ENFORCE=false` are unchanged. The signed format is unchanged:
this is verifier-side only.

`bh hq authority status`, `check`, `bh doctor` and the publish notice use the same predicate
(`bh-3h6al`, `src/beadhive/hq_authority_expiry.py`, `SqlFleetConfigRevisionStore.tolerated_bound_at`):
`config_bound` is true for a tolerated head, so a tolerated edit no longer reads as unbound.

**Rollout order: receivers first.** A 0.23.0 frame or receiver still fences on any head move.
Upgrade the receivers, then the frames, before relying on tolerance. Until every verifier is
upgraded, an edit still fences the not-yet-upgraded ones.

**Trust delta.** The key-less SQL config publisher can now commit `fleet.yaml` edits that frames
do not enforce without fencing them (it could already commit them; frames fenced). It still
cannot change a `frame_policy`, managed hive identity, host manifest, `allowed_signers` or
beadyard identity, nor rewrite history, without an operator renew.

### Long authorities stay long (`bh-oywx8`, `bh-u4cip`)

Operator-signed SQL actions (`grant`, `observe`, lifecycle verbs, `release-upgrade`) used to
reset `expires_at` to now + 1 h, silently shortening a 7 d or 30 d authority. They now sign
`max(original expiry, now + 3600)` (`operator_signed_expiry` in
`src/beadhive/hq_authority_guard.py`): a long authority is never shortened, and a short or lapsed
one gets the 1 h floor. The Git plane never reset it. A candidate grant's own cap is separate and
unchanged.

To renew for a long duration, pass `--duration` up to the configured ceiling; weeks are accepted
(`2w`, units `s m h d w`, `src/beadhive/hq_authority_ceiling.py`). The expiry warnings and
`check` print the renew command for the **resolved ceiling** (default 604800 s), adding
`--max-duration` only when the ceiling is above 7 d. See
[the ceiling](#authority-duration-ceiling).

Renewal still needs the operator key. Laptop-free renewal (a scoped delegate key) is out of scope
for 0.23.x: every frame pins one operator key, so a 0.23.0 frame would reject a delegate-signed
record. It is deferred to 0.24.0 (`bh-rjjjo`).

### Renewing from an operator host (for example the laptop) {#authority-laptop-renew}

A released `bh` builds its control plane from the running host's `host.yaml`. An operator host
whose `host.yaml` has no `hq.sql.authority_writer` binds it from a file instead, with
`BH_HQ_OPERATOR_SETTINGS=<file>` (or, from 0.23.0, `--operator-settings <file>`, which wins
over the env var) on `bh hq authority renew|grant|observe|bind-beadyard|status|check`
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
  --expected-revision <revision> --operator-key <key> --duration 7d --confirm   # --duration up to the ceiling
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
- hive-lease holder and lease validation. Renewal through the trusted receiver is the 0.22.x path
  and is **going**: on a director-placed hive the lease row is placement, written by the director
  ([below](#hive-placement-by-the-director-dolt-server-hq-023)), and expiry is only a failover
  hint.

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

> **Going.** The signed inbox and the trusted receiver that reads it are the 0.22.x liveness
> carrier. 0.23.0 replaces them on `dolt-server` HQ with [session and evidence
> rows](#session-rows) and director placement. Keep pruning the inbox while a 0.22.x reader or
> the receiver still runs (the Φ3 soak); deleting the inbox code is a later, unscheduled removal
> (ADR Decision 5).

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

## Hive placement by the director (dolt-server HQ, 0.23) {#hive-placement-by-the-director-dolt-server-hq-023}

In `dolt-server` HQ, **placement** (which frame should write a hive) is one
`hq_live_hive_leases` row per hive. Before 0.23.0 the trusted receiver wrote it from a frame's
signed proposal, and 0.22.8 let a frame resolve it from its own proposals at the hive's
`refs/bh/epoch`. In 0.23.0 the **director** writes it directly, in SQL, and the receiver is going
(ADR §1 design A, `bh-a94qw`):

- A compare-and-swap (CAS) on the row's `revision`, issued on the **director credential**: an
  account with `UPDATE` on that table and no other write right on it. Frames hold `SELECT` only.
  The director never inserts: a never-placed hive is seeded once by operator provisioning.
- Every write gets a fresh revision (`sha256(record || uuid)`, the receiver's 64-hex format). A
  lost CAS, a `1213` serialization failure or `rowcount 0` is never retried with the same
  expectation, and an unknown commit acknowledgment is read back, never re-issued.
- The row keeps the receiver's shape (`lease_json` is `{authority, lease}` with the five-field
  lease), so the receiver, 0.22.x readers and the 0.22.8 bridge all still parse it and a Φ3
  rollback to the receiver works.
- A director-written row is self-identifying (`request_sha256` is a witness over the row's own
  prefix, revision and record). That is the data switch that **retires the 0.22.8 proposal
  resolver for that hive**: `BH_HQ_SQL_HIVE_LEASE=proposal` still governs hives whose row the
  receiver wrote and no longer matters for a director-placed hive: a frame's own proposal on
  such a hive is refused before anything moves. Per hive, not per host.
- Each placement records its **cause** (`planned` for an operator `place`, `failover` only when
  the failover loop placed) in the row's `request_id` column. Only a `failover` adopt reclaims
  the dead frame's claims, and a frame that does not read the cause reclaims nothing.
- Lease expiry (the tenure) is a failover hint, never a write gate. An HQ outage blocks handoff,
  never writes.

The operator drives it with the hidden `bh hq placement show|seed|place|release|check|policy`
verb, documented only in the [placement runbook](design/hq-placement-runbook.md). It binds the
director credential in an operator settings file (`hq.sql.placement_writer`), never in a frame's
`host.yaml`; name the file with the `BH_HQ_OPERATOR_SETTINGS` environment variable or, from 0.23.0,
`--operator-settings`. Git HQ is unchanged: placement stays the `refs/bh/lease/<prefix>`
CAS.

**Unattended failover** is a host-daemon loop, off by default
(`host.daemon.failover.enabled`). It fails a hive over only when
`min(server staleness, observed window) > failover_after`, where staleness is the age of the
frame's session row on the HQ server's own clock, and it places the successor that holds the
fewest hive primaries (then the freshest session, then the lowest frame id). A placed hive is
never moved just to rebalance, and `bh doctor` warns when primaries are lopsided beyond
`host.daemon.failover.max_primary_spread` (default 2). `failover_after` (defaults 60 min
executor, 30 min transient) and the executor floor (default 45 min) live in the
`hq_live_failover_policy` table, not on the placement row and never in `host.yaml` or fleet
config; see the runbook's section 6 and [CONFIGURATION](CONFIGURATION.md#hq-configuration-authority).
Advanced scheduling beyond this (the operator may move it to external quorum tooling such as
ZooKeeper) is deferred.

## Session and evidence rows (dolt-server HQ, 0.23) {#session-rows}

0.23 replaces the signed heartbeat on `dolt-server` HQ with two operator-provisioned,
single-row tables per frame incarnation (ADR §5, bh-owqdg). The data is the switch: a reader
uses them exactly when both tables exist for the incarnation, and reads the signed inbox (or
the receiver's observation) otherwise. There is no config key. git HQ is unchanged.

- `frame_<principal>_<epoch>_session` is liveness. The frame renews it with one `UPDATE`
  per tick (`python -m beadhive.heartbeat_sender renew`, timer `beadhive-session-renew`),
  and an operator trigger stamps `renewed_at = UTC_TIMESTAMP(6)`.
- `frame_<principal>_<epoch>_evidence` is conformance. The conformance job writes it after
  each run, and `measured_at` is server-stamped the same way.
- `hq_liveness_policy` holds the operator's `session_ttl_s` (default 300) and `evidence_ttl_s`
  (default 900), committed with the `frame_*` ignore rule. Values outside 1 s to 7 days are
  refused, never clamped.

Eligibility is one statement joining the grant, both rows, the policy and placement, keeping
the predicates `authenticated_fresh_heartbeat`, `conformance_pass`, `release_matches` and
`current_hive_lease_holder`. A claim records the admitted `renewed_at`, `measured_at` and
evidence digest. On a switched frame `BH_FRAME_HEARTBEAT` is logged as ignored.

Provision with `beadhive.hq_sql_session_provision` as the server-local operator:
`provision_liveness_schema` once, then `provision_incarnation` per incarnation. It refuses
unless the ignore rule is committed first, creates `'<principal>'@'<frame address>'` with
`REQUIRE SSL`, and runs `check_provisioning`. That check refuses a wildcard or TLS-optional
account for the principal, extra frame write rights, a trigger that does more than stamp
time, and any hive database on the HQ server.

During Φ3 an incarnation has both tables and its inbox. The registry still names the inbox,
so 0.22.x readers and the receiver keep working, and the sender dual-writes signed beats.
An incarnation provisioned without an inbox is registered under its session table name, which
0.22.x readers refuse: provision session-only incarnations only once no 0.22.x reader remains.

### Soak check: sender stall (Φ3, O4) {#sender-stall-soak}

The Φ3 soak (O4) must show that a session sender which stalls for longer than `session_ttl_s`
costs eligibility but not the hive. While the sender is stuck, `authenticated_fresh_heartbeat`
turns false as soon as the row is older than the TTL. The director fails the frame over only
after the stall also outlasts `failover_after`, by both server staleness and its own observed
window. A stall that ends before then leaves a fresh session and no placement change. Time
decides when to reassign a hive, never who may write: the hive's `bh_writer` decides that.

The M10 suite carries the check as
`test_sender_stall_longer_than_the_ttl_is_stale_but_fails_over_only_past_failover_after` in
`tests/test_fence_composed_int.py`. It runs on a private scratch Dolt server: two stalls,
one shorter and one longer than `failover_after` (four TTLs). The gate runs it with a 2 s TTL.
For the soak, scale it to the production timer and repeat it while the full gate loads the
host:

```sh
BH_M10_SESSION_TTL_S=60 uv run pytest -p no:cacheprovider -s \
  tests/test_fence_composed_int.py -k sender_stall
```

Each run prints one `BH_M10` JSON line with the TTL, `failover_after` and the measured
`due_after_stall_s`. The soak passes when every run is green and the due time stays just
above `failover_after`. `BH_M10_SEEDS=0,1,…` widens the suite's fixed-seed product schedule
in the same way.

## Authority rebind

`bh hq authority rebind [--expected-revision N] --operator-key <key> --confirm` re-signs the
current authority against the current config head (revision + 1, policies re-projected) -
the fix after a frame-relevant config edit fences frames. With no `--duration` it signs the
no-expiry sentinel, so it also converts a 0.23.x expiring authority into a non-expiring one;
pass `--duration <seconds|7d|36h>` to opt into a finite expiry under the ceiling below.
`bh hq authority renew` is a deprecated alias that prints a deprecation line. `rebind` is a new
value of the existing `action` argument of the `hq.authority` operation, so the operation
catalog and wire contract are unchanged.

## Authority duration ceiling

`bh hq authority renew --duration <seconds|7d|36h>` signs an authority that stays valid for
that long. `--duration` defaults to 3600 s (1 h) so every renewal is an explicit lifetime
choice; renew prints the resulting `expires_at`. Use `--duration 7d` for laptop-off operation.

The signing side refuses a duration above a configurable **ceiling**. The default is 7 days
(`AUTHORITY_MAX_DURATION_DEFAULT_S` = 604800) and there is **no hard maximum**: the operator
may set any positive, finite ceiling. The same ceiling applies to the SQL and Git backends and
to Git fleet-config publication. Resolution order, first match wins:

1. `--max-duration` on `bh hq authority renew` (0.23.0+; seconds or e.g. `7d`)
2. `hq.sql.authority_max_duration_s` in the operator settings file (read only through
   `BH_HQ_OPERATOR_SETTINGS` (all versions) or `--operator-settings` (0.23.0+); it is never a
   frame or fleet key)
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
