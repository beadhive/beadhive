# Two bead stores for one repository — operator guide

A public repository that wants **private planning** and a **public, issue-synced surface** gets
two bead stores: a private operational `internal` store on an HQ-declared remote, and a
`public` projection store fed from GitHub Issues and published to the code origin's
`refs/dolt/data`. The design and its rules are the
[two-store ADR](design/two-store-scope-adr.md); this guide is how to run it. Everything here is
**opt-in and off by default**: a `managed_repos` entry without `stores:` is one store, exactly
as before.

> **Enabling two stores on a live hive is a separate, operator-approved step.** Nothing in this
> guide converts an existing hive by itself, and no bh release turns it on. See
> [Instance rollout gate](#instance-rollout-gate).

| | `internal` | `public` |
|---|---|---|
| Holds | planning, molecules, gates, reports, memories | a projection of GitHub Issues |
| Where | the checkout's own `.beads/`, the hive's prefix | a detached path outside the checkout, its own prefix |
| Remote | the hive's HQ `dolt_remote` (e.g. S3) — never `git-origin` | `git-origin` (the code origin's `refs/dolt/data`) or its own nested descriptor |
| Authority, sync | local, push-pull | GitHub (remote), pull-only |
| Operational | yes — the only one | no: read, sync and admin only |

## Configuration

One fleet entry declares both stores and the internal store's remote
([field rules](CONFIGURATION.md#stores)):

```yaml
managed_repos:
  - provider: github
    org: acme
    repo: widget
    prefix: widget
    dolt_remote:                       # the INTERNAL store's remote (dolt-remote-descriptor/v1)
      name: widget-private
      url: "s3://acme-beads/internal/github/acme/widget"
    stores:
      internal: {remote: widget-private}
      public:
        prefix: widpub                 # unique across every managed store in the fleet
        path: ~/.beadhive/stores/github/acme/widget/public
        remote: git-origin             # publish to the code origin's refs/dolt/data
        source: github-issues          # authority: remote and sync: pull-only by default
```

An S3-compatible service (Cloudflare R2, Ceph, a corporate gateway, …) uses the endpoint form
`s3://<bucket>/<db>?endpoint=https://…&path-style=true&region=<r>`; see the
[HQ remotes runbook](design/hq-dolt-remotes-runbook.md#s3-compatible-services).

`bh config validate` refuses, before any store exists: a `stores` block without `internal`, a
public prefix equal to any other store's or hive's prefix, a public `remote` that names the
internal descriptor, two descriptors sharing one location, and an internal remote of
`git-origin` while a public store exists. Onboarding also refuses a public `path` inside the
code checkout.

**Credentials.** None go into fleet config, a repository, `.beads/config.yaml` or output. For
an `s3://` remote on the pinned bd 1.3.0 the **shared dolt sql-server** performs the transfer
and sees only its own start-up environment; credentials on the `bh`/bd process alone fail with
`get credentials: failed to refresh cached credentials` (`bh-pphik`). An `s3://` internal store
needs **server mode**; bd's embedded engine cannot open `s3://`. Two ways to get credentials
into the server:

- **Ambient:** start the server from an environment that already carries them.
- **Configured source (opt-in):** set `dolt.server_credentials: <label>` in the host's
  `config.yaml`, naming a `dolt.remote_credentials.<label>` profile (`fnox`, `aws-profile`,
  `ambient` or `instance-role`). When bh starts the shared server (`hq clone`, hub hydration,
  two-store clone, storage migration) it resolves that source and injects the result into the
  `bd dolt start` child only. One credential per host, scoped to that server; nothing is written
  to disk and the default (unset) changes nothing. A source that cannot be resolved stops the
  start with a message naming what is missing. A server that is already running keeps its
  environment: stop it, then let bh start it again.

**Service-managed servers (systemd / launchd, `bh-gaytj`).** When the shared server is a service
unit, bh never starts it, so the injection above never applies; the service manager must hand the
credentials over. Still opt-in: set `dolt.server_credentials` (above), then have the unit render a
fresh private file from fnox on every start. The entry point is
`python -m beadhive.dolt_server_credentials`, not a `bh` verb (it stays out of the public command
catalog); below it is written `CRED` for short, meaning the installed environment's
`python -m beadhive.dolt_server_credentials`.

- `CRED render --path FILE [--format systemd|shell]` resolves the profile exactly
  as bh does, writes FILE mode 0600 (atomically, from creation) and prints only the variable
  names. `systemd` writes `NAME="value"` lines for `EnvironmentFile=`; `shell` writes
  `export NAME='value'` for a launchd wrapper to source. A missing or unresolvable credential
  fails closed and writes nothing.
- `CRED status [--env-file FILE] [--json]` (and the Store Engine section of
  `bh doctor`, when `dolt.server_credentials` is set) reads the RUNNING server's environment and reports
  per server pid whether it holds each needed variable, by name only. `missing` means the server
  must be restarted; with `--env-file`, a differing value is flagged `rotated since start`.
  Where the environment cannot be read (another user, no `/proc` as on macOS) it says `unknown`.

Operator steps (applied by the operator with sudo; bh and agents never edit units or `/etc`).
systemd, as a drop-in via `sudo systemctl edit beadhive-dolt-hive.service`:

```ini
[Service]
RuntimeDirectory=beadhive-dolt-hive
RuntimeDirectoryMode=0700
EnvironmentFile=-%t/beadhive-dolt-hive/credentials.env
ExecStartPre=<bh-python> -m beadhive.dolt_server_credentials render --path %t/beadhive-dolt-hive/credentials.env
```

then `sudo systemctl restart beadhive-dolt-hive.service` and confirm with
`CRED status`. The service user must be the one that holds the fnox key. If your
systemd reads `EnvironmentFile=` before `ExecStartPre=` finishes, run `render` once by hand
before the first restart, or use `ExecStart=/bin/sh -c '. FILE && exec dolt sql-server ...'`
with `--format shell`. launchd: make the plist's `ProgramArguments` a wrapper that runs
`CRED render --path "$HOME/.beadhive/run/dolt.env" --format shell`, sources that
file and `exec`s `dolt sql-server`, then `launchctl kickstart -k` the job.

## Local layout

On a host that has bootstrapped the hive:

```text
~/src/github/acme/widget/            code checkout (Git); origin = the public code repository
  .beads/                            INTERNAL store metadata (prefix widget), excluded locally
                                     via .git/info/exclude — never tracked, never committed
~/.beadhive/stores/github/acme/widget/public/
  .beads/                            PUBLIC store metadata (prefix widpub), detached
shared dolt sql-server               database `widget` (internal) and `widpub` (public)
```

- The internal store's only Dolt remote is the HQ descriptor (`widget-private`). bd auto-wires
  the code origin as `origin` when it initialises a store; onboarding drops that registration
  for a partitioned hive so a bare `bd dolt push` cannot publish private beads.
- The public store is selected through `BEADS_DIR` and always run from its own root
  (`bd -C <public path>`); `bd -C <checkout>` ignores `BEADS_DIR` and opens the internal store.
- Plan, work, report, gates, memories and default `bh bd` resolve **only** the internal store.

## Bootstrap modes

`bh hive onboard` / `bh hive init` run one ordered flow (ADR section 6): **internal first**,
then **public**. Each phase names itself on failure, removes only what it created, and is
skipped when its store already exists, so re-running onboarding is the retry.

| Situation | What happens | Operator action |
|---|---|---|
| First host, internal remote verified **empty** | a fresh internal store is minted, registered and pushed to the remote; the public store is minted | onboard with `BH_ONBOARD_SEED_REMOTE=<prefix>` for this one run |
| Another host, remote **populated** | the internal store is cloned from the descriptor with full history — the code origin is never probed; the public store is minted fresh | plain onboard |
| Ordinary public clone (no HQ entry) | `git clone` + `bd bootstrap` reads the code origin's `refs/dolt/data`: the public corpus only | none — this is what contributors get |
| Store already present | skipped | none |

Notes:

- A failed clone is never taken to mean "empty"; seed only a remote you have verified empty.
  A seed never reads the code origin: it runs `bd init --remote <descriptor url>`, so bd
  consults the verified-empty HQ remote instead of auto-cloning the origin's `refs/dolt/data`
  (`bh-wy2ia`). The seeded store is fresh even when a legacy or public corpus already sits on
  the origin ref, and that ref is left untouched. bd's auto-wired `origin` registration (the
  code origin, or the descriptor URL under bd's own name) is removed whenever the descriptor
  has a different name, for one-store hives too, so a bare `bd dolt push` targets the HQ remote.
- A server-mode clone first waits about 70 s for its not-yet-existing database
  (bd 1.3.0 retries 10 + 20 + 40 s, `bh-mi40l`). That wait is expected, not a hang.
- The public store is always minted fresh on a host today; a second-host clone of the public
  corpus is `bh-nkssm`.

## Explicit public sync

Nothing moves between stores implicitly. Three explicit operations, each touching one store:

1. **Tracker store: pull, then push its Dolt replica** (one verb; trackers are a kind of
   remote and GitHub is the authority for the public store):

   ```sh
   bh hive remote sync github/acme/widget --store widpub --dry-run   # preview, writes nothing
   bh hive remote sync github/acme/widget --store widpub             # pull, then fast-forward push
   bh hive remote sync github/acme/widget --store widpub --pull-only # pull only, push nothing
   ```

   `--store` names the tracker store by its prefix (or `public`); only tracker stores are
   handled by this verb for now, the internal store keeps `bh hive sync remotes`. The pull
   keeps each issue's GitHub URL as `external_ref`; a re-pull never duplicates it, and nothing
   is ever written to GitHub. Credentials stay ambient (`GITHUB_TOKEN` / `GH_TOKEN` /
   `gh auth`). Mapper limits: [github-issue-projection.md](design/github-issue-projection.md).
   A failed pull stops before the push. `bh hive project-issues <hive>` remains as a
   **deprecated** alias of `--pull-only`.

2. **The push leg: public store → its Git Dolt ref.** It wraps
   `beadhive.public_publish.publish_public`: it resolves only the public store, refuses any
   non-Git destination and the internal store's own remote, registers the destination
   idempotently and pushes **fast-forward only**. There is no bare `--force` route. A code
   origin whose `refs/dolt/data` still carries another history (an unmigrated single store)
   rejects the push as a non-fast-forward and keeps its data — that is the safety, not a fault
   to force past. `--json` emits the versioned `hive remote sync` report. A plain `--dry-run`
   also prints the replica's current `refs/dolt/data` SHA.

   **One-time guarded replacement** (the `bh-l3zvc` rollout, step 3 of the gate below). Off by
   default; a separate, explicit direction with compare-and-swap (force-with-lease) semantics:

   ```sh
   bh hive remote sync github/acme/widget --store widpub --dry-run   # prints the current SHA
   bh hive remote sync github/acme/widget --store widpub --dry-run --force-with-lease <40-hex-sha>
   bh hive remote sync github/acme/widget --store widpub --force-with-lease <40-hex-sha> --confirm
   ```

   The dry run with a lease names the exact ref (`refs/dolt/data`), its current (old) SHA, the
   new public store identity, every branch and tag that must not change, and the rollback rule.
   The real run needs the full SHA and `--confirm`; it refuses, changing nothing, if the remote
   ref no longer equals the lease, if the replica has no such ref, or if the internal store
   still uses the code origin as its remote. It does not pull, and it moves only
   `refs/dolt/data`: branches and tags are read before and after (a change is reported as an
   error), and the old data is **not** parked under another ref. There is **no rollback by
   restoring the old SHA**: that ref held the private corpus, and re-pushing it (even with a
   lease) republishes it. Recover from the verified private archive instead (re-run the replace
   from the audited tracker store) or remove the ref.

   **CAS race window (runbook note for `bh-l3zvc.5`).** The lease is not atomic on the server:
   the ref is read with `git ls-remote`, re-read immediately before the one forced push, and
   Dolt then performs the transfer. A writer that moves `refs/dolt/data` between that last
   read and the push is overwritten without notice. The window is small but real, so the
   runbook freezes every other writer of the code origin's `refs/dolt/data` (old-corpus
   pushers, `bd dolt push`, sync daemons) for the replacement, then re-verifies freeze-free:
   `git ls-remote` shows the expected new SHA, branches and tags are unchanged, no extra ref
   exists, and a fresh `git clone` + `bd bootstrap` sees only the public store's beads. The
   real-bd/Dolt proof is `tests/test_two_store_e2e_int.py::test_guarded_replace_of_a_legacy_dolt_ref`.

   `bh hive remote [HIVE]... [--reconcile]` (the registration compare) is unchanged: `remote`
   is now a group whose default is that command.

3. **Internal → its private remote** — the ordinary sync verb:

   ```sh
   bh hive sync remotes github/acme/widget --push --dry-run
   bh hive sync remotes github/acme/widget --push
   ```

Snapshots for other consumers come from `publish_export.export_public_snapshot`, which also
takes only the public store.

## Diagnostics

| Command | Shows |
|---|---|
| `bh doctor` | a **bead stores** section (only for hives that declare `stores`): each store's scope, root, prefix, transport, authority, sync and health |
| `bh hive remote <hive>` | the internal descriptor against bd's registration (`in-sync`, `migration-required`, …) |
| `bh hive sync remotes <hive> --push --dry-run` | what a push would attempt, by family and redacted URL |
| `bh hive remote sync <hive> --store <tracker> --dry-run` | which issues a pull would import or update, and the replica's destination and current ref |

Health is honest: an object-store remote has no read-only ahead/behind, so the internal store
reads `unknown` (and its epoch fence is advisory, ADR section 9); the public store on
`git-origin` reads `present`. A missing store reads `missing`.

## Recovery

| Symptom | Cause | Action |
|---|---|---|
| onboarding `phase: credentials` | no credential source on this host | provide credentials to the shared server's environment, re-run onboarding |
| onboarding `phase: clone` | wrong URL, empty remote or bad credentials | fix the URL/credentials; seed only a verified-empty remote |
| onboarding `[public-init]` | public store init or remote registration failed | fix the cause and re-run; the fresh public store was discarded, internal untouched |
| `sync remotes --push`: `failed to push dolt: <name> (s3 remote …)` | endpoint unreachable or credentials missing on the server | restore access, re-run the same sync — nothing was written |
| `sync remotes`: `dirty` | untracked or modified files in the checkout | commit or exclude them; `.beads/` belongs in `.git/info/exclude` |
| `remote sync` pull fails | GitHub unreachable, bad token, or store config names another repository | fix and re-run; the public store keeps nothing half-imported and the internal store is untouched |
| publication rejected as non-fast-forward | the code origin carries another `refs/dolt/data` history | migrate that corpus first (`bh-l3zvc`) with the guarded `remote sync --force-with-lease`; never a bare `--force` |
| `bh work … refuses <widpub-id>` | a public projection id was used for operational work | file or link an internal bead instead |

## Instance rollout gate

Turning two stores on for a **live** hive is not a configuration edit to make in passing. It
is a separate, operator-approved rollout with its own bead (for Beadhive itself: `bh-l3zvc`).
Until that gate is passed, a hive stays on its single store. The rollout must at least:

1. choose and provision the private remote, and credentials on every host's shared server;
2. move or seed the internal corpus there with full history and verify a clone on a second
   host (the remote move is `bh-ksmt0`'s transactional migration);
3. decide what happens to the code origin's existing `refs/dolt/data` — it still carries the
   internal corpus until it is replaced, and publishing the public store over it is refused;
   the replacement is `bh hive remote sync <hive> --store <tracker> --force-with-lease <sha> --confirm`;
4. pick the public prefix and path, run the first `remote sync`, and publish;
5. update every host's fleet config together (older `bh` rejects a `stores` block).

None of the steps above happens automatically, and none is reversible by re-running a sync.

## Proof

`tests/test_two_store_e2e_int.py` exercises all of the above for one checkout with real bd and
Dolt in server mode, the internal store on an S3-compatible endpoint (moto in server mode,
`tests/harness/s3.py`), the public store on the code origin's Git ref, and an offline GitHub
stub replaying recorded issues. It also proves the failure cases — collision, wrong scope,
bootstrap order, an interrupted pull and push, retry, and accidental public export — change
nothing in the other store. These tests run under the **integration** gate key (attest/push),
and skip without bd or without `moto[server]`.
