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
an `s3://` remote on the pinned bd 1.3.0 the **shared dolt sql-server** performs the transfer,
so the AWS credentials (ambient chain or an instance role) must be in the server's environment
when it starts; credentials on the `bh`/bd process alone fail with `get credentials: failed to
refresh cached credentials` (`bh-pphik`). An `s3://` internal store needs **server mode**; bd's
embedded engine cannot open `s3://`.

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
  Seed **before** anything has published to the code origin's `refs/dolt/data`: bd's own
  `bd init` auto-clones that ref when it exists (`bh-wy2ia`).
- A server-mode clone first waits about 70 s for its not-yet-existing database
  (bd 1.3.0 retries 10 + 20 + 40 s, `bh-mi40l`). That wait is expected, not a hang.
- The public store is always minted fresh on a host today; a second-host clone of the public
  corpus is `bh-nkssm`.

## Explicit public sync

Nothing moves between stores implicitly. Three explicit operations, each touching one store:

1. **GitHub → public** (pull-only; GitHub is authoritative):

   ```sh
   bh hive project-issues github/acme/widget --dry-run   # preview, writes nothing
   bh hive project-issues github/acme/widget             # pull; re-runs update in place
   ```

   Each issue keeps its GitHub URL as `external_ref`; a re-pull never duplicates it, and nothing
   is ever written to GitHub. Credentials stay ambient (`GITHUB_TOKEN` / `GH_TOKEN` /
   `gh auth`). Mapper limits: [github-issue-projection.md](design/github-issue-projection.md).

2. **Public → its Git Dolt ref** (publication). The sanctioned entry point is
   `beadhive.public_publish.publish_public(entry)`: it resolves only the public store, refuses
   any non-Git destination and the internal store's own remote, registers the destination
   idempotently and pushes **without `--force`**. No `bh` verb wraps it yet (`bh-31rf7`); the
   store-scoped equivalent is:

   ```sh
   P=~/.beadhive/stores/github/acme/widget/public
   BEADS_DIR=$P/.beads bd -C "$P" dolt remote add git-origin git+https://github.com/acme/widget.git
   BEADS_DIR=$P/.beads bd -C "$P" dolt push --remote git-origin    # never --force
   ```

   A code origin whose `refs/dolt/data` still carries another history (an unmigrated single
   store) rejects the push as a non-fast-forward and keeps its data — that is the safety, not a
   fault to force past.

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
| `bh hive project-issues <hive> --dry-run` | which issues a pull would import or update |

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
| `project-issues` fails | GitHub unreachable, bad token, or store config names another repository | fix and re-run; the public store keeps nothing half-imported and the internal store is untouched |
| publication rejected as non-fast-forward | the code origin carries another `refs/dolt/data` history | migrate that corpus first (`bh-l3zvc`); never `--force` |
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
4. pick the public prefix and path, run the first `project-issues`, and publish;
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
