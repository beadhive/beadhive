# Two bead stores per repository ADR — scope, authority, routing, bootstrap (bh-lniwf.1)

**Status:** accepted · **Date:** 2026-10-16 · **Decision bead:** `bh-lniwf.1`
**Epic:** `bh-lniwf` (private operational and public projection bead stores for one repository)
**Discharges:** spike `bh-5lpl9.4` — evidence in
[bh-5lpl9.4-store-partitioning.md](../spikes/bh-5lpl9.4-store-partitioning.md); answers the
namespace question `bh-vcfsb` (§4).
**Reconciles:** `bh-5lpl9.2` ([divergence record](beads-methodology-divergence-adr.md)),
`bh-7vu4` and `bh-t9pe` (local-canonical tracker push, superseded), `bh-tfapu` (fence),
`bh-7jm7v` ([publish-boundary-adr.md](publish-boundary-adr.md), amended in §8).
**Depends on:** the HQ remote-descriptor contract of `bh-b1scz` — the "HQ Dolt remote
descriptor and credential boundary ADR" (`bh-b1scz.1`, `docs/design/hq-dolt-remote-descriptor-adr.md`
on its branch), contract `dolt-remote-descriptor/v1`, declared per hive as
`managed_repos[*].dolt_remote: {url, name, credentials}`. A store **names** that descriptor
and never embeds a URL, credential or transport detail. `bh-ksmt0` (remote migration) is out
of scope: managed bootstrap here is a plain clone.

## Context

A hive's beads ride `refs/dolt/data` on the repository's own Git remote and inherit its
permissions exactly. For a public repository that means public beads. Beads has no per-bead
privacy, so a public repository that wants private planning **and** a public, issue-synced
surface needs two stores. Splitting naively loses safety: an internal store that cannot ride
the public ref would exist on one host only. This ADR fixes the contract that the rest of
`bh-lniwf` implements.

Operator direction (decided before this record): GitHub Issues are a **public, pull-only
projection** store — GitHub authoritative, stable `external_ref` identity. Operational beads
live in a **private internal** store on an HQ-configured remote (S3 first).

## 1. Scope — and the default that does not change

| Store | Holds | Visibility | Operational? |
|---|---|---|---|
| `internal` | the full Beadhive corpus: planning, molecules, gates, reports, memories | private once a private remote is configured | **yes — the only one** |
| `public` | a projection of public discourse (GitHub Issues first) | public | no — read, sync and admin only |

**Default: one store.** A `managed_repos` entry with no `stores:` block is exactly today's
hive: one store at `<checkout>/.beads`, riding `refs/dolt/data`, resolved as the legacy
store by `store_target.resolve_store`. Every existing config round-trips byte-for-byte.
Partitioning is opt-in and obliges the operator to name a remote for the internal store.

When `stores:` is present the internal store **is** the hive's previous store, in place, with
the hive's existing prefix. Nothing is split, moved or re-prefixed (spike Q4).

## 2. The descriptor

```yaml
managed_repos:
  - {provider: github, org: beadhive, repo: beadhive, prefix: bh,
     stores: {
       internal: {prefix: bh, remote: beadhive-private, authority: local, sync: push-pull},
       public: {prefix: bhpub, path: ~/.beadhive/stores/github/beadhive/beadhive/public,
                remote: git-origin, authority: remote, sync: pull-only, source: github-issues}},
     dolt_remote: {name: beadhive-private, url: "s3://<bucket>/internal/github/beadhive/beadhive"}}
```

| Field | Meaning | Rules |
|---|---|---|
| `prefix` | the store's bead-id prefix | required for `public`; `internal` defaults to and must equal the hive `prefix` |
| `path` | detached store root (holds `.beads/`) | `internal`: absent = `<checkout>/.beads`; `public`: required, absolute or `~`, outside the checkout |
| `remote` | the **name** of the store's `dolt-remote-descriptor/v1`: `internal` names the hive's `dolt_remote` (whose `name` defaults to `origin`); `public` names its own nested `dolt_remote`; `git-origin` is reserved for the code origin's `refs/dolt/data` | never a URL (`://`), never credentials; unique across stores; `internal` defaults to the hive descriptor's name |
| `dolt_remote` | `public` only: the store's own nested `dolt-remote-descriptor/v1` (`{url, name, credentials}`, bh-rz379) | the same model and value-free errors as `managed_repos[].dolt_remote`; `remote` defaults to and must equal its `name`; `name` may not be `git-origin`; forbidden on `internal` |
| `authority` | who is canonical: `local` (this store) or `remote` (the external system) | `internal` must be `local`; `public` must be `remote` |
| `sync` | `push-pull`, `pull-only` or `none` | `local` allows `push-pull`/`none`; `remote` allows `pull-only`/`none` |
| `source` | projection source for a `public` store | `github-issues` (only value); forbidden on `internal` |
| `dispatch` | may this store issue operational work | `internal`: absent or `true`; `public`: absent or `false` |

Validation lives in `modules/config/contracts.py` (`StoreDescriptor`, `ManagedRepoStores`,
`BeadhiveConfig._store_references`). Declaring `stores:` without `internal` is rejected as an
ambiguous default: the operational store must be named, never inferred.

**One descriptor per store (bh-rz379).** Every store's remote is a `dolt-remote-descriptor/v1`;
no second remote model exists. The internal store's descriptor **is** the hive's
`managed_repos[].dolt_remote` (so `bh hive remote` reconciliation and onboarding keep one source
of truth): when both are declared, `stores.internal.remote` must name it. A public store that
publishes anywhere but the code origin nests its own descriptor:

```yaml
stores:
  internal: {remote: beadhive-private}
  public:
    prefix: bhpub
    path: ~/.beadhive/stores/github/beadhive/beadhive/public
    dolt_remote: {name: beadhive-public, url: "s3://<bucket>/public/github/beadhive/beadhive"}
dolt_remote: {name: beadhive-private, url: "s3://<bucket>/internal/github/beadhive/beadhive"}
```

A public `remote` other than `git-origin` without a nested descriptor is rejected: a public
store never borrows the internal store's descriptor. "One location per database" (the
`bh-b1scz` ADR) holds across every descriptor in the fleet, nested ones included.

## 3. Authority and sync direction

- **Internal: local-canonical, push-pull.** bh writes it; its remote is a replica that
  closes the single-host gap. With a public store configured the internal remote must not be
  `git-origin` — that would publish private beads on the public code origin.
- **Public: GitHub-authoritative, pull-only.** GitHub is the system of record for the
  projection, so sync pulls (`bd github sync --pull-only`) and never pushes. Each imported
  issue keeps its stable identity in `external_ref`; a re-pull updates it in place.
  Implemented by `bh hive project-issues` (`bh-lniwf.6`): `external_ref` is the issue's
  GitHub URL, and mapper limits are recorded in
  [github-issue-projection.md](github-issue-projection.md).

This **supersedes** the local-canonical push model of `bh-7vu4` / `bh-t9pe` (both closed in
favour of `bh-lniwf.6`). Pushing bead state *to* GitHub would make two systems claim the same
record; pulling keeps exactly one authority per store. Bidirectional sync is deferred until a
use case needs it.

## 4. Prefix and local-path uniqueness — and the namespace question (`bh-vcfsb`)

Every store prefix is unique across **every managed store** in the fleet, not merely within
one hive, and so is every detached `path` and every non-`origin` remote name. A shared prefix
would make id lookup ambiguous and collisions silent (spike Q2).

**`bh-vcfsb` — namespace limits, measured.** The fleet carries 28 prefixes on 2026-10-16:
length 2: 4, 3: 2, 4: 4, 5: 8, 6: 5, 7: 2, 8: 3 — median 5, none over beads' soft cap of 8.
Eleven are dash-extensions of another (`bh-*` under `bh`), which `registry.resolve_bead_hive`
already disambiguates by exact store read; no exact collision exists. Partitioning two stores
per hive at most doubles the count (28 → 56 if every hive partitioned; in practice only public
repositories with an issue tracker do).

| Option | Cost | Decision |
|---|---|---|
| wider id space (upstream) | an upstream beads format change; every id, branch and path | not needed now |
| store-set sharing one prefix, partitioned id range (upstream or fork) | needs bd to reserve ranges per store; a fork otherwise | rejected: a fork for a cosmetic gain |
| **two prefixes per partitioned repo** | one extra short prefix per partitioned hive; the visibility shows in public ids only | **adopted** |

bh absorbs it: the **internal** prefix is the hive's existing one, so no internal id, branch
name, worktree path, dependency edge or document reference changes. Only the new public store
gets a new prefix, and its ids appear only in the public projection. The prefix is chosen by
the operator (no derivation is blessed); validation enforces uniqueness. No upstream request
is needed for this design; per-bead privacy (the real gap) is recorded as an upstream feature
request in the [divergence record](beads-methodology-divergence-adr.md).

## 5. Default routing

The subject-and-visibility table in the
[divergence record](beads-methodology-divergence-adr.md) is the operative rule:

- Plan, work, report, gates, memories, guard checks and default `bh bd` reads and writes
  resolve **only** the internal store (`store_target.operational_store`).
- The public store is reached only by naming it. A public request on a hive without one
  fails (`StoreNotConfigured`); it never falls back to internal, nor internal to public.
- Claim, assign, submit, review, merge and dispatch refuse a public target
  (`PublicStoreNotOperational`).
- A public record cannot shadow, satisfy or mutate internal work: its prefix is distinct, it
  lives in a separate database, bead-to-hive resolution never reads it, and no dependency or
  gate edge crosses from internal to public.

## 6. Bootstrap precedence

Order is security-sensitive (`store_bootstrap.plan_bootstrap`):

1. **Internal first.** A managed hive whose internal store names an HQ remote descriptor
   clones it (a plain full-history clone) **before** any probe of the code origin's
   `refs/dolt/data`. A private hive therefore cannot bootstrap from a public ref by accident.
   The clone is the `bh-b1scz` HQ-remote bootstrap (`onboard._bootstrap_from_hq_remote`:
   `bd bootstrap` against the descriptor, credentials, seeding, redaction, registration);
   `store_bootstrap` only orders the phases and delegates, so there is one clone path
   (bh-rz379). It lands in the hive's own shared-server database, named from the prefix.
2. **Origin probe only when no internal remote is declared** — today's unmanaged/public-clone
   behaviour, unchanged.
3. **Public second**, initialised independently at its detached `path` with its own prefix,
   never replacing the internal store. It is minted fresh; a nested `dolt_remote` is then
   registered under its name (never cloned from, until a public second-host bootstrap is
   designed). A registration failure discards the fresh mint so a retry redoes the phase.

Each phase reports its name on failure and leaves the other store and the prior config
untouched; an already-initialised store is skipped, so a retry is idempotent. Resolving a
descriptor name to a URL and credentials is `bh-b1scz`'s; this ADR consumes it through an
injected resolver and never reads secrets.

## 7. Hub participation

Hub hydration aggregates **internal stores only** (`hub.hydration_source`). A public
projection record would otherwise appear in the aggregate as a second, dispatchable copy of
work. Bringing public stores into the hub needs store provenance and scheduler exclusion and
is deferred to a future design.

## 8. Publication — amending the publish boundary (`bh-7jm7v`)

[publish-boundary-adr.md](publish-boundary-adr.md) assumed "this hive's bead data is already
public". **Amended:** after rollout Beadhive's internal store is private, and only an
**explicitly selected public store** may be exported or published.
`publish_export.export_public_snapshot` takes a resolved public `StoreTarget` and refuses any
other scope — including the internal store of a repository whose code is public, and the
legacy single store. The public store may be published to the code origin's `refs/dolt/data`
(`remote: git-origin`).

## 9. Fencing limitation for non-Git remotes

The epoch fence (`host_fence.py`, [multi-host-model-adr.md](multi-host-model-adr.md)) is a
Git ref, `refs/bh/epoch`, reserved by remote compare-and-swap beside `refs/dolt/data`. Since
`bh-tfapu` it is already non-atomic with the data push: bd pushes through a transport repo
with hooks disabled, so bh reserves, pushes, then verifies. An S3 (or any non-Git) internal
remote cannot carry `refs/bh/epoch` at all, so it **cannot share even that guarantee**.
Fencing for such a store is advisory: the host lease still decides who should write, but no
remote CAS stops a stale host's push. Diagnostics report such stores' sync and fence state as
`unknown`; nothing represents advisory fencing as atomic. A store-native fence is future work.

## 10. Answers to spike `bh-5lpl9.4`

| # | Question | Answer | Where |
|---|---|---|---|
| 1 | Two stores for one working repo? | Yes, via `BEADS_DIR`; never `--database` | implemented here (`bh-lniwf.3`) |
| 2 | Shared prefix? | No; a second prefix, unique fleet-wide | implemented here (`bh-lniwf.2`) |
| 3 | Hub / hydration? | Internal only; `source_repo` stays meaningful | implemented here (`bh-lniwf.7`) |
| 4 | Migration without losing history? | No split needed; remote move is a full-history clone | by `bh-ksmt0` (remote move); no split here |
| 5 | Default endpoint scheme? | None blessed; stores name a descriptor; `origin` reserved | by `bh-b1scz` |
| 6 | GitHub fidelity? | Lossy; title/body/state/labels/identity survive | `bh-lniwf.6`: [github-issue-projection.md](github-issue-projection.md) |
| 7 | Fork bd? | No | implemented here |

With these answers `bh-5lpl9.4` is eligible to close **GO**, and `bh-5lpl9.6` can link
`bh-lniwf`, `bh-b1scz` and `bh-ksmt0`.

## 11. Defaults taken without an operator answer

Conservative options were taken and recorded as `DECISION ASK` comments on `bh-lniwf`:

- the internal store keeps the hive's checkout location and prefix (no detached internal
  store in this release);
- the public prefix is operator-chosen, with no derived default;
- a public store must be GitHub-authoritative and pull-only; any other combination is
  rejected until a use case exists;
- publishing from a legacy single-store hive is refused once it goes through the boundary.

## Proof and operator guide (`bh-lniwf.8`)

`tests/test_two_store_e2e_int.py` proves the contract end to end for one checkout (internal on
an S3-compatible remote, public on the code origin's Git ref, an offline GitHub stub), including
the failure cases. Publication of the public store goes through
`public_publish.publish_public` (public store only, Git Dolt refs only, never the internal
descriptor's location, never forced). Running it is documented in the operator guide
[TWO-STORES.md](../TWO-STORES.md).

## Out of scope

The GitHub projection itself (`bh-lniwf.6`), migrating the live Beadhive corpus (`bh-l3zvc`),
and remote migration (`bh-ksmt0`).
