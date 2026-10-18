# Runbook: HQ-configured Dolt remotes

**Applies to:** the HQ remote descriptor shipped by epic `bh-b1scz`.
**Contract:** [hq-dolt-remote-descriptor-adr.md](hq-dolt-remote-descriptor-adr.md)
(`dolt-remote-descriptor/v1`).

This runbook moves a hive's *declared* Dolt remote into Factory HQ and keeps every host's
registration in line with it. It configures and checks remotes. It never moves an existing
corpus, never deletes a remote, and never re-points one that already exists: moving data is
`bh-ksmt0`'s transactional migration.

Nothing here changes a hive that has no descriptor. Every step is opt-in per hive.

## Before you start

- **Every frame and operator host runs a `bh` that knows the descriptor.** Fleet configuration
  is strictly validated, so an older `bh` rejects a fleet document that carries
  `dolt_remote`. Upgrade first, then publish the first descriptor.
- **Pinned toolchain:** bd 1.3.0 and Dolt 2.3.5. Check with `bd version` and `dolt version`.
- **`s3://` needs server mode.** The pinned bd's embedded engine registers an `s3://` URL but
  cannot open it ("unknown url scheme: 's3'"). Hives onboarded by `bh` use the shared server,
  which can. An embedded hive can use `aws://[table:bucket]/db` instead, or be moved with
  `bh hive migrate-storage`. `bh hive remote` reports an embedded `s3://` hive as
  `unsupported` and says why.
- **One location per database.** Two hives must never share a bucket path; HQ refuses a fleet
  document that tries (`dolt_remote_path_shared`).

## 1. Choose the location

Use one fleet bucket and one path per hive, derived from the hive identity:

```text
s3://<bucket>/beads/<provider>/<org>/<repo>
```

Create the bucket once. Nothing needs to exist under the path: the first push creates it.

Supported URL families (the full table is in the ADR):

| URL | Status |
|---|---|
| `s3://bucket/path` | implemented (AWS S3, standard endpoint, server mode) |
| `aws://[dynamo-table:bucket]/db` | implemented (AWS S3 + DynamoDB, any mode) |
| `file:///absolute/path` | implemented (local disks, test fixtures) |
| `git+ssh://…`, `git+https://…`, `git+file://…` | implemented (today's default) |
| `s3://bucket/path?endpoint=…&region=…&path-style=…` | implemented (S3-compatible endpoint, server mode; proved by `bh-82gpi`) |
| `gs://bucket/path` | extension point, refused |
| `https://doltremoteapi.dolthub.com/…`, DoltLab | extension point, refused |

## 2. Give each host credentials

Credentials never go into fleet configuration, a repository, `.beads/config.yaml`, output or
logs. Pick one source per host.

**Ambient (default).** Leave the descriptor's `credentials` empty and provide the standard AWS
SDK chain to the account that runs `bh` and bd's server: `AWS_PROFILE`, or
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, or `~/.aws/credentials`/`~/.aws/config`. A shared
Dolt server only sees the environment it started with — and for `s3://` (plain or
S3-compatible) on the pinned bd 1.3.0 the **server** performs the transfer, so the credentials
must be in the shared server's environment when it starts. Credentials on the `bh`/bd process
alone fail with `get credentials: failed to refresh cached credentials` (measured by `bh-82gpi`;
follow-up `bh-pphik`). Restart the shared server after changing them.

**Labelled profile.** Set `credentials: "<label>"` in the descriptor and map the label in this
host's own `config.yaml`:

```yaml
dolt:
  remote_credentials:
    prod:
      source: aws-profile        # or: ambient | instance-role | fnox
      aws_profile: beads-prod
```

For `source: fnox`, give `access_key_id` and `secret_access_key` (and optionally
`session_token`) as fnox references (`config_path`, `profile`, `key`); values are read through
the fnox broker for the one bd call that needs them. `instance-role` trusts the EC2 instance
metadata service and skips the local probe. On a server-mode hive with an `s3://` remote the
per-call `aws-profile` and `fnox` values do not reach the running server (`bh-pphik`); use
ambient credentials in the server's environment or an instance role there.

`bh` checks that a credential source exists **before** it changes anything and names what is
missing, never a value.

## 3. Declare the remote in HQ

```sh
bh config set hives.<hive>.dolt_remote.url s3://<bucket>/beads/<provider>/<org>/<repo> \
  --scope fleet
# optional: bh config set hives.<hive>.dolt_remote.credentials prod --scope fleet
```

or edit the hive's `managed_repos` entry in `fleet.yaml`:

```yaml
- {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api", "kind": "org-native",
   "dolt_remote": {"url": "s3://acme-beads/beads/github/acme/api"}}
```

A URL with credentials, an unknown S3 query key or a shared location is refused with a
path-specific code such as `validation_dolt_remote_embedded_credential`. Publish the fleet
change as usual. `bh hive init`/`add` and `bh hive repair --prefix` keep the descriptor.

## 4. Check, preview, reconcile

```sh
bh hive remote --all                       # read-only comparison
bh hive remote <hive> --reconcile --dry-run  # the exact mutation, nothing written
bh hive remote <hive> --reconcile          # install a missing registration
bh hive remote --all --json                # versioned report (bh.hive-remote-reconcile/1)
```

| Outcome | Meaning | What to do |
|---|---|---|
| `in-sync` | bd's registration matches HQ | nothing |
| `would-add` / `added` | the registration was missing / has been installed | nothing (a second run is `in-sync`) |
| `no-descriptor` | the hive has no HQ remote | nothing (today's behaviour) |
| `no-local-store` | no `.beads/` on this host | onboard the hive (section 5) |
| `migration-required` | the remote name points elsewhere | move the corpus with the `bh-ksmt0` migration; reconcile never replaces |
| `name-mismatch` | the HQ URL is registered under another name | rename deliberately (`bd dolt remote remove`/`add`) or fix the descriptor's `name` |
| `credentials-missing` | no credential source on this host | section 2 |
| `unsupported` | an extension-point family, or `s3://` on an embedded store | pick an implemented URL, or move to server mode |
| `error` | the registration could not be read | re-run; check `bd dolt remote list` |

Registering a remote transfers no data. `bh doctor` shows the same comparison under
**HQ Dolt remotes** for every hive that declares one, and stays silent otherwise.

## 5. Bootstrap a hive on a new host

`bh hive onboard <provider/org/repo>` (or `bh hive init` in a local checkout) consults the
descriptor **before** any Git-origin probe, so a private hive never bootstraps from the code
repository's `refs/dolt/data`.

- **Populated remote:** the store is cloned with full history (`bd bootstrap` against the
  declared URL). The URL is handed to bd for that one call and is not left in
  `.beads/config.yaml`.
- **Empty remote (a brand-new hive):** seed it explicitly. The value must name the hive being
  onboarded:

  ```sh
  BH_ONBOARD_SEED_REMOTE=<prefix or provider/org/repo> bh hive onboard <provider/org/repo>
  ```

  Onboarding mints a fresh store, registers the remote and pushes it. A remote that already
  holds data rejects the seed as a non-fast-forward; it is never forced.

A failed bootstrap names its phase (`preflight`, `credentials`, `clone`, `register`,
`seed-push`), removes the partial store it created, and leaves the HQ declaration alone, so a
re-run is safe. A failed clone is never taken to mean "empty": a wrong URL, a missing bucket
path and failed authentication all fail the same way, and only the explicit seed variable says
"empty".

## 6. Day-to-day sync

`bh hive sync remotes <hive> [--pull|--push] [--dry-run]` pushes and pulls through bd's
registered remote with this host's credentials. For an object-store remote it:

- names the remote by family and redacted URL (`origin (s3 remote s3://…)`), never
  `refs/dolt/data`;
- reports ahead/behind as **unknown**, because object stores have no read-only diff, and
  attempts the idempotent push;
- refuses the hive with the missing credential named, before any transfer, when the host has
  none.

The host epoch fence (`refs/bh/epoch`) still lives on the Git origin. For a non-Git data plane
it is a sequenced, advisory check, not an atomic guarantee (`bh-tfapu`); `bh hive remote`
prints this note for a leased hive.

## 7. Recovery

| Symptom | Cause | Recovery |
|---|---|---|
| sync: `failed to push dolt: … (s3 remote …)` with a transport error | bucket unreachable, endpoint down | fix reachability and re-run; a failed push transfers nothing that a retry does not complete |
| sync: `failed to push dolt` with a permissions error | the credential lacks write access | fix the IAM policy or profile; re-run |
| sync: non-fast-forward | another host pushed first | `bh hive sync remotes <hive> --pull`, then push |
| onboarding: `phase: clone` | wrong URL, missing data or bad credentials | check the URL and credentials; seed only if the remote is verified empty |
| `migration-required` after a manual change | someone re-pointed the remote by hand | put it back (`bd dolt remote add <name> <hq-url>` re-points), or migrate deliberately |

An interrupted push (proved in `tests/test_hq_dolt_remotes_e2e.py`) fails loudly and changes no
configuration; re-running the same sync after the fault clears lands the change.

## S3-compatible services

`s3://<bucket>/<db>?endpoint=<url>&path-style=true&region=<region>` addresses any S3-compatible
service (Dolt 2.3.2 and later). It is implemented for server-mode hives since `bh-82gpi` proved
the lifecycle against an S3-compatible endpoint (moto in server mode). The proof covers the S3
API as moto models it; verify a specific provider (Cloudflare R2, Ceph, Wasabi, …) on a
disposable bucket before relying on it. An embedded hive refuses it, as it refuses `s3://`.

## Extension points

These families are classified and reported but refused for mutation until proved:

- **Google Cloud Storage** (`gs://`) needs Application Default Credentials support in
  `beadhive.dolt_remote_credentials`.
- **Hosted Dolt** (DoltHub, DoltLab, any remotesapi over `https://`) needs Dolt credential
  (`dolt creds`) handling.

## Proof and tests

- `tests/test_hq_dolt_remotes_e2e.py` runs the whole lifecycle against the pinned bd with
  `file://` remotes: distinct paths, reconcile, a causal bead pushed through
  `bh hive sync remotes`, a fresh store cloned with identical commit history, an interrupted
  push and its retry, drift, and doctor.
- The S3 leg (`test_s3_leg_round_trip`, `bh-82gpi`) runs the same lifecycle in server mode
  against moto in server mode on loopback (`tests/harness/s3.py`, dummy credentials, never real
  S3). `moto[server]` is a dev-only dependency; without it the leg skips, never fails. It runs
  under the integration gate key (attest/push), not the per-bead submit keys.
- `tests/test_dolt_remote_credentials.py` proves, offline, that the pinned embedded bd speaks
  `aws://` and rejects `s3://`.
