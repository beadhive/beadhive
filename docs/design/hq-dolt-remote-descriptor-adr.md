# HQ Dolt remote descriptor and credential boundary ADR

**Status:** accepted · **Date:** 2026-10-16 · **Decision bead:** `bh-b1scz.1` ·
**Supersedes:** nothing · **Amends:** nothing
**Related:** epic `bh-b1scz` (this molecule), `bh-0wewj` (relocatable data plane; this ADR settles
its remote-configuration slice), `bh-5lpl9.4` question 5 (default endpoint scheme), `bh-lniwf`
(two stores per repository; reuses the descriptor defined here), `bh-ksmt0` (verifiable backups
and transactional remote migration; owns every data move this ADR refuses),
[backup-retention-boundary-adr.md](backup-retention-boundary-adr.md),
[dolt-server-mode-adr.md](dolt-server-mode-adr.md),
[configuration-ownership-and-composition-adr.md](configuration-ownership-and-composition-adr.md),
[hq-dolt-remotes-runbook.md](hq-dolt-remotes-runbook.md) (the operator procedure)

Records where a hive's authoritative Dolt remote is **declared** (Factory HQ fleet
configuration), where it is **realised** (the remote registration bd keeps in the hive's own
Dolt store), where its **credentials** live (on each host, never in configuration), and what bh
may and may not do when the declaration and the reality disagree.

## Context

Today a hive's Dolt remote is whatever `bd init` derived from the code repository's Git
`origin`. bd writes it into `.beads/config.yaml` as `sync.remote` and registers a Dolt remote
named `origin`; bead history then rides `refs/dolt/data` on the same Git repository as the code
([Sync Concepts][sync-concepts]). That is the right default for getting started: there is
nothing to provision. It stops fitting when the code repository is public and the beads are
not (`bh-0wewj`: two public repositories were leaking beads), when storage volume outgrows a
Git host, or when an organisation requires its own cloud account.

Dolt and bd already support other data planes. The pinned Dolt 2.3.5 accepts `http`, `https`,
`aws`, `s3`, `gs` and `file` URLs plus Git transports; bd's
[Bucket Federation Quickstart][bucket-federation] documents an S3 or GCS bucket path as a
first-class remote. What is missing is a **fleet-level declaration**: today each host and each
repository decides independently, and a fresh host onboarding a private hive will bootstrap
from whatever the public code repository carries.

## Decision

1. **HQ declares, bd realises, the host authenticates.** The desired remote for a managed hive
   is a non-secret *descriptor* in HQ fleet configuration. The remote registration inside the
   hive's Dolt store (read with `bd dolt remote list`, written with `bd dolt remote add`) is
   reality. Credentials are host-local or ambient and never appear in either.
2. **No descriptor means today's behaviour, exactly.** The feature is off until an operator
   writes a descriptor for a hive. Nothing else in this ADR runs for a hive without one.
3. **Model the remote by URL and derived capabilities, not a provider enum.** The descriptor
   stores a URL; bh derives a *family* and its capabilities from the URL at runtime. A new
   provider is a new row in a code table, never a schema change.
4. **Configuration never moves, overwrites or deletes data.** Reconciliation only *adds* a
   missing registration. A conflicting registration is reported as `migration-required` and
   left alone; moving a corpus is `bh-ksmt0`'s transactional job.
5. **S3 first.** The documented AWS forms (`s3://bucket/path` and `aws://[table:bucket]/db`)
   are implemented. An S3-compatible service addressed with a custom `endpoint` is an extension
   point: classified, reported, and refused until a compatibility proof lands.

## The descriptor contract (`dolt-remote-descriptor/v1`)

This section is the stable contract other molecules reference. `bh-lniwf` (two stores per
repository) nests the same model per store rather than inventing a second one: the hive's
`dolt_remote` is the internal store's descriptor and a public store nests its own at
`managed_repos[].stores.public.dolt_remote` (bh-rz379, two-store ADR section 2).

| Item | Value |
|---|---|
| Contract id | `dolt-remote-descriptor/v1` |
| Model | `DoltRemoteDescriptor` in `beadhive.modules.config.contracts` |
| Config path | `managed_repos[].dolt_remote` (fleet-scoped, like all of `managed_repos`) |
| Host credentials | `dolt.remote_credentials.<label>` (`DoltRemoteCredentialConfig`, host-scoped) |
| Absent | `dolt_remote` missing or `null`: no HQ policy, today's behaviour |

Fields:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `url` | string, required | none | The remote URL, as Dolt accepts it. No user info, no secrets. |
| `name` | string | `origin` | The canonical Dolt remote name the URL is registered under. |
| `credentials` | string | `""` | Non-secret label of a host credential profile. Empty means ambient. |

```yaml
managed_repos:
  - {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api",
     "kind": "org-native",
     "dolt_remote": {"url": "s3://acme-beads/beads/github/acme/api"}}
```

Validation is done at write time and on every read, so a bad descriptor never reaches bd.
Errors are value-free and path-specific (the established `DocumentValidationError` shape,
for example `fleet document invalid at managed_repos.<item>.dolt_remote.url:
validation_dolt_remote_embedded_credential`):

| Code | Rejected input |
|---|---|
| `dolt_remote_url_required` | an empty or whitespace URL |
| `dolt_remote_url_malformed` | no scheme, control characters, a fragment, or an unparsable URL |
| `dolt_remote_embedded_credential` | user info (`user:pass@`, `token@`), or a query key that names a credential |
| `dolt_remote_query_unsupported` | a query on a scheme that takes none, or an unknown S3 query key |
| `dolt_remote_name_invalid` | a name outside `[A-Za-z][A-Za-z0-9._-]{0,62}` |
| `dolt_remote_credentials_label_invalid` | a label outside `[a-z0-9][a-z0-9_-]{0,62}` |
| `dolt_remote_path_shared` | two managed hives declaring the same object-store location |

Git SSH URLs carry a login name (`git+ssh://git@github.com/...`). That user name is a routing
identity, not a secret, and is accepted for the Git family only when it has no password.

## Ownership and precedence

| Fact | Owner | Where it lives |
|---|---|---|
| Desired remote (name, URL, credential label) | HQ director, fleet scope | `managed_repos[].dolt_remote` |
| Actual registration | bd, inside the hive store | `bd dolt remote list` (the SQL-backed `dolt_remotes`) |
| Bootstrap hint | bd | `.beads/config.yaml` `sync.remote` |
| Credentials | each host | ambient AWS chain, or `dolt.remote_credentials.<label>` |

bd's server-side registration is authoritative for reality because the running SQL server reads
it; a remote added with the raw `dolt` CLI lands in filesystem config only and push/pull fail
until restart ([Bucket Federation Quickstart][bucket-federation], "remote does not exist").
bh therefore reads and writes remotes only through `bd dolt remote`, never raw Dolt and never
by inspecting `refs/dolt/data`.

bh never writes a descriptor URL into `.beads/config.yaml`. For a furnished hive that file is
tracked, and the point of a private remote is that the code repository does not name it. When
bh bootstraps from a descriptor it gives bd the URL for that one call and removes the hint
afterwards; push and pull use the registered remote.

Precedence when a hive is onboarded or bootstrapped on a host:

1. An HQ descriptor, when present, decides the source. Git-origin probing does not run.
2. Otherwise the current behaviour: bootstrap from the Git origin when it carries
   `refs/dolt/data`, else mint a fresh store.

## URL families and capabilities

The family is derived from the URL; nothing persisted names it. Support levels:
`implemented` (bh drives it and has tests), `extension-point` (classified and reported,
refused for mutation until proved), `unsupported` (unknown scheme).

| Family | URL forms | Support | Object store | Git-ref fence | Read-only diff |
|---|---|---|---|---|---|
| `git` | `git+ssh://`, `git+https://` | implemented (today's default) | no | yes | partial (`ls-remote`) |
| `file` | `file:///abs/path` | implemented (fixtures, local disks) | yes | no | no |
| `s3` | `s3://bucket/path` | implemented (AWS S3, standard endpoint; server mode only) | yes | no | no |
| `aws` | `aws://[table:bucket]/db` | implemented (AWS S3 + DynamoDB) | yes | no | no |
| `s3` + `endpoint=` | `s3://b/p?endpoint=...&region=...` | extension point (S3-compatible) | yes | no | no |
| `gs` | `gs://bucket/path` | extension point | yes | no | no |
| `hosted` | `https://`/`http://` (DoltHub, DoltLab, remotesapi) | extension point | no | no | no |
| unknown | anything else | unsupported | n/a | n/a | n/a |

**The storage mode matters for `s3://`.** The pinned bd 1.3.0 registers an `s3://` URL in any
mode, but its *embedded* Dolt engine cannot open one ("unknown url scheme: 's3'", evidence item
10); the Dolt 2.3.5 CLI and sql-server that server mode uses do speak it. bh therefore treats
`s3://` as unsupported for an embedded hive and says why. `aws://` works in both modes.

An unknown scheme is a typed `unsupported` result. It is never treated as Git: the old
assumption that every remote is a Git repository carrying `refs/dolt/data` is exactly the
misclassification this table removes.

The S3-compatible row is deliberately not claimed. Dolt 2.3.2 and later (dolthub/dolt#11571)
implement `s3://` with `endpoint`, `region` and `path-style` query parameters for any provider
with conditional writes; that a binary accepts the URL is prerequisite evidence, not proof that
a given provider works (`bh-hx39m`). A provider is promoted to `implemented` only by a recorded
end-to-end proof.

## One location per database

Every object-store database needs its own path. Two databases sharing a bucket path share one
Dolt history and diverge beyond repair ([Bucket Federation Quickstart][bucket-federation], "One
bucket path, two databases"). Fleet validation rejects two managed hives whose object-store or
file URLs normalise to the same location. Normalisation lowercases scheme and bucket, drops a
trailing slash, and keeps the path and the S3 query (a different endpoint is a different
location).

Recommended layout, answering `bh-5lpl9.4` question 5: one fleet bucket, one path per hive,
derived from the hive identity, `s3://<bucket>/beads/<provider>/<org>/<repo>`. It is a
documented convention, not a template: each descriptor states its full URL. A fleet-level
template (`remote_template`) is deferred. Explicit URLs keep the stored value equal to what bd
is told, and `bh-lniwf` needs per-store URLs anyway.

## Reconciliation never moves data

`bh hive remote` compares the descriptor with bd's registration for one hive or the fleet;
`--reconcile` applies the table below and `--reconcile --dry-run` previews it:

| Actual registration | Result | Mutation |
|---|---|---|
| no descriptor | `no-descriptor` | none |
| same name, same normalised URL | `in-sync` | none |
| name absent | `add` (or `would-add` under `--dry-run`) | `bd dolt remote add <name> <url>` |
| same name, different URL | `migration-required` | none; non-zero exit |
| URL registered under another name | `name-mismatch` | none; non-zero exit |
| family unsupported or extension point | `unsupported` | none; non-zero exit |
| credentials missing for an object store | `credentials-missing` | none; non-zero exit |
| no local store on this host | `no-local-store` | none |

It never pushes, pulls, removes or replaces, and a second run after `add` is `in-sync`. Fleet
mode isolates each hive's failure and always prints the complete summary before exiting
non-zero. bd's own `dolt remote add` re-points an existing name without complaint (evidence
item 9), so the Engine's add reads the registration first and refuses a different URL under an
existing name; a re-point is only ever the explicit `replace_remote` operation.

## Bootstrap from a descriptor

Measured against the pinned bd 1.3.0 and Dolt 2.3.5 (evidence below):

- `bd bootstrap` with `sync.remote` set clones the full history. It exits non-zero when the
  remote has no Dolt data, and it reports a missing path the same way as an empty one.
- `bd init --remote <url>` treats a remote with no data as empty, mints a fresh database,
  registers the remote, and commits `.beads` files to Git. It cannot tell an empty remote from a
  wrong URL or a failed authentication.

So bh never uses `bd init --remote` for a descriptor. A **populated** remote is cloned with
`bd bootstrap` (full history). A clone failure is a failure that names its phase; it is never
taken to mean "empty". An **empty** remote is seeded only on explicit, per-invocation operator
request: `BH_ONBOARD_SEED_REMOTE=<hive prefix or provider/org/repo>` on the onboarding command.
The value must name the hive being onboarded, so a leftover export cannot seed a different hive.
Seeding mints a fresh local store, registers the remote and pushes. A seed push to a remote that
turns out to hold data fails as a non-fast-forward and is never forced. A failed bootstrap
removes what it created, so the hive's prior state and the descriptor remain intact for a retry.

Two facts of `bd bootstrap` shape the clone on the shared server (bh-rz379, measured with the
pinned bd): it never auto-starts the shared server, unlike `bd init --shared-server`, so bh
starts it first (the probe-then-start step hub hydration and `hq clone` already take); and it
clones into bd's generic `beads` database unless `.beads/metadata.json` names one, so bh stamps
`dolt_mode: server` plus `dolt_database: <sanitized prefix>` before the clone and records
`dolt_server_database` after it (`bh-g5ujg`). `Engine.clone_remote` accepts a `.beads/` holding
only that stamp and still refuses any initialised store.

The seed switch is an environment variable rather than a `--seed-remote` flag on `bh hive init`
and `bh hive onboard` because adding a parameter to an already-published operation is a wire
catalog change that needs operator approval (`scripts/check_wire_schema_compat.py`). The flag is
the preferred long-term surface and is raised as a decision for the operator on `bh-b1scz`.

## Credential boundary and redaction

- **Ambient** (the default, `credentials: ""`): the standard AWS SDK chain the Dolt process
  already reads (`AWS_PROFILE`, `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, web identity,
  shared config files). Dolt never accepts credentials in an `s3://` URL.
- **Labelled profile**: `credentials: "<label>"` names `dolt.remote_credentials.<label>` in the
  host's own `config.yaml`. Its `source` is `ambient`, `instance-role` (trust the instance
  metadata service; no local probe is possible), `aws-profile` (sets `AWS_PROFILE`), or `fnox`
  (access key id, secret and optional session token read through the existing fnox broker).
- Resolved values are injected into the environment of the single bd child that needs them and
  nowhere else. bd routes push and pull through a Dolt CLI subprocess when it sees `AWS_*` in
  its environment, which is what makes per-call injection work against a shared server
  ([Bucket Federation Quickstart][bucket-federation], "Auth works in the shell").
- Before any remote mutation, bh checks that a credential source exists for an object-store
  family and fails with the *name* of what is missing, never a value.
- Every rendered URL passes through one redaction function: user info is replaced and any
  query value outside the S3 allowlist (`endpoint`, `region`, `path-style`) is masked. Errors,
  dry-runs, `--json` output and log events use it.
- Fleet configuration, repositories, `.beads/config.yaml`, output and logs never contain a
  credential. The secret-bearing environment is never logged or echoed.

## Compatibility

- **Additive, no schema bump.** `dolt_remote` is an optional field; `schema_version` stays 1.
- **Mixed versions.** Fleet configuration is strictly validated (`extra="forbid"`), so a `bh`
  older than this change rejects a fleet document that carries a descriptor. Upgrade every
  frame and operator host before publishing the first descriptor, as with
  `hq.default_authority_mode`.
- **Frame authority.** A descriptor edit is not part of the managed-repo identity tuple, so it
  does not fence signed frames.
- **Registry writes preserve it.** `bh hive init`/`add` rewrite only the identity fields they
  own and carry every other key of an existing entry, including `dolt_remote`.
- **Epoch fence.** The host epoch fence is a Git ref (`refs/bh/epoch`) beside `refs/dolt/data`
  on the Git origin. A non-Git data plane cannot carry it, so for such a hive the fence remains
  a sequenced, advisory check on the Git origin, not an atomic guarantee over the data
  (`bh-tfapu`). Reconcile prints this caveat for a leased hive with a non-Git descriptor.
- **Health stays honest.** Object stores have no read-only ahead/behind primitive. Sync, status
  and doctor report such a hive's remote state as `unknown`, name the remote by family and
  redacted URL, and never describe it as `refs/dolt/data`.

## Why not HQ submodules or HQ-hosted databases

HQ is the control-plane catalogue. Making it a container of project databases (one Git
submodule or one Dolt database per hive inside the HQ repository) would put every hive's corpus
behind HQ's own access control and push cadence, make HQ's size grow with every hive's history,
and couple a hive's availability to HQ's. A submodule is also a Git object reference: it pins a
commit of another repository and stores no data itself, so it cannot give a Dolt database a
location that differs from a Git repository. Dolt remotes already provide what is needed:
per-database locations with their own credentials. HQ only needs to say which one.

## Evidence

Probed on this host with the pinned binaries (bd 1.3.0 `f45b249ce`, Dolt 2.3.5) on scratch
stores, 2026-10-16:

1. `bd dolt remote list --json` prints `[]` with no remotes, and after an add
   `[{"name","url","sql_url","status"}]`.
2. `bd dolt remote add origin file:///…/remote1` then `bd dolt push` writes a Dolt chunk store
   at that path.
3. A second store with only `.beads/config.yaml` `sync.remote: file:///…/remote1` and
   `bd bootstrap --non-interactive` cloned the full history and registered `origin`.
4. The same bootstrap against an empty directory and a missing directory both failed:
   `clone failed; remote at that url contains no Dolt data`.
5. `bd init --remote file:///…/nonexist/zz` printed "Remote has no Dolt data yet; initialized
   a fresh local database", registered the remote and committed `.beads` files to Git.
6. `aws://[tbl:bucket]/db` without credentials failed in the clone phase with an AWS
   credential error after the instance-metadata timeout.
7. After removing `sync.remote` from `.beads/config.yaml`, `bd dolt push` and a peer's
   `bd dolt pull` still used the registered `origin`.
8. `dolt remote --help` (2.3.5) documents `s3://bucket/database`, credentials only from the
   AWS SDK chain, and the `endpoint`, `region` and `path-style` query parameters.
9. `bd dolt remote add origin <other-url>` on a store whose `origin` already existed exited 0
   and re-pointed `origin`; re-adding the same URL also exited 0.
10. In an embedded store with offline AWS settings (`AWS_ENDPOINT_URL=http://127.0.0.1:9`),
    `bd dolt push` to `aws://[t:b]/db` failed at the DynamoDB call (scheme understood), while
    `s3://b/db` failed with `unknown url scheme: 's3'`. A plain Dolt 2.3.5 repository pushing
    to the same `s3://` URL reached the S3 `GetObject` call (scheme understood).
11. With a closed endpoint, the failure arrived in well under a second: an unavailable service
    is an immediate, named transport error, not a hang.

## References

- Beads, [Dolt Backend for Beads][beads-dolt] (Dolt remotes, data location)
- Beads, [Bucket Federation Quickstart][bucket-federation]
- Beads, [Sync Setup Guide][sync-setup] and [Sync Concepts][sync-concepts]

[beads-dolt]: https://beads.gascity.com/architecture/dolt
[bucket-federation]: https://beads.gascity.com/multi-agent/bucket-federation
[sync-setup]: https://beads.gascity.com/getting-started/sync-setup
[sync-concepts]: https://beads.gascity.com/core-concepts/sync-concepts
