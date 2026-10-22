# Frame release ranges and the signed release catalog ADR

**Status:** accepted · **Date:** 2026-10-22 · **Decision bead:** `bh-kmxyp` ·
**Epic:** `bh-vfrem` · **Amends:**
[active-frame-release-rotation-adr.md](active-frame-release-rotation-adr.md) (rotation stops being
the only way to change a frame's release) ·
**Related:** [frame-release-upgrade-runbook.md](frame-release-upgrade-runbook.md),
[frame-eligibility.md](frame-eligibility.md), `docs/FRAME-FLEET-MEMBERSHIP.md` (release ranges
note), `bh-71wk9` (fleet reader-version guard)

Lets an operator grant a frame a **range** of `bh` releases, for example "any 0.28 patch", instead
of one pinned digest. An in-range upgrade is then an install and a restart, with no operator
signature and no rotation. A range on its own names versions, and a version string proves
nothing. So each admitted version is bound to its measured digest by a **release catalog**. The
release workflow signs each catalog entry, and HQ ingests an entry only after verifying it.

## Context

A frame's signed grant pins one release, `desired.release = {id, digest}`. The digest is
`release_measurement.installed_release()`, a hash over the installed `beadhive/` files. Every
check compares against that pin, either as the whole `{id, digest}` or as the digest alone:

| Site | Comparison |
| --- | --- |
| `ELIGIBILITY_SQL` (ADP `_incarnation.py:88`) | `e.release_digest = <desired digest>` |
| `frame_eligibility.py` signed carrier (`:362`) | beat == manifest == desired (whole dict) |
| `frame_eligibility.py` session carrier (`:245`) | SQL predicate, plus manifest == desired |
| `heartbeat_report.py:165-185` | measured digest == desired; manifest == desired |
| `hq_control_plane.py` admit (Git `:1505`, SQL `:2405`) | beat and registration == desired |
| `hq_authority_guard.py` (`:243`, `:666`, `:681`, `:801`), `hq_sql_receiver.py` (`:251`, `:553`, `:799`) | legacy Git/receiver carrier, whole dict |
| `frame_emergency.py:49-68`, `:125-140` | emergency grant == desired; `execution_digest` |

Today any new install therefore makes the frame ineligible until the operator runs an active
rotation (`bh host release-upgrade`). That is 9 steps:

1. Measure the new install.
2. `check` the current state.
3. Budget the ineligibility window against the lease TTL.
4. `prepare-config` the manifest.
5. `plan`.
6. `provision` the new principal.
7. `apply` with the operator key.
8. Cut over the frame's credentials and restart it.
9. Retire the old environment.

The 0.28.4 rollout needed this on every host. The rotation also advances the frame epoch, which
mints a new SQL principal, account, password and session/evidence tables. The rotation ADR
justifies that advance because the grant's digest changes. If the release changed in place, old
evidence measured against the old digest could satisfy the new grant.

Two more facts shape the design:

- **The frame reports the grant's release, not the measured one.** `heartbeat_report.py:238`
  copies `desired.release` into the beat and the evidence row. The measured value is written only
  on refusal, with status `unavailable`.
- **Evidence is self-attested** (`EvidenceReport`, T9). The frame writes its own `release_id` and
  `release_digest`. A range check over a self-reported version string would therefore be weaker
  than today's digest pin. The catalog closes that gap: a version is admitted only through a
  digest that the release workflow signed for it.

## Decision

### 1. The grant carries an optional release policy

`desired` gains one optional key, `release_policy`. A record without it is an exact-pin grant and
behaves exactly as today. That is the default, and existing grants need no migration.

```json
"desired": {
  "declared": false,
  "release": {"id": "0.28.4", "digest": "sha256:…"},
  "release_policy": {"format": 1, "spec": ">=0.28.2,==0.28.*", "catalog": "beadhive"},
  "caps": [...],
  "profile": "..."
}
```

- `spec` is a PEP 440 specifier set, evaluated with `packaging.specifiers.SpecifierSet`.
  `packaging` (Apache-2.0/BSD) is promoted from a transitive dependency to a declared one.
  Prereleases are never admitted unless the spec names one explicitly, which is the `packaging`
  default.
- The CLI also accepts npm-style shorthand and **normalises it before signing**. The signed record
  holds only the canonical PEP 440 form, so no verifier ever has to parse shorthand:

  | Operator intent | Shorthand | Stored `spec` |
  | --- | --- | --- |
  | any patch of one minor | `0.28.x`, `~0.28` | `==0.28.*` |
  | patches 2 through 6 | `0.28.2 - 0.28.6` | `>=0.28.2,<=0.28.6` |
  | a range of minors | `0.27 - 0.29` | `>=0.27,<0.30` |
  | any release of one major | `1.x`, `^1.2` | `==1.*` (`^1.2`: `>=1.2,==1.*`) |
  | at least a floor, same minor | `^0.28.2` | `>=0.28.2,==0.28.*` |

  npm's caret on a `0.x` version keeps the minor fixed, as the last row shows. That surprises
  people, so the CLI echoes the stored form before it asks for confirmation.
- `release` stays and becomes the policy's **reference release**. It is the release the frame was
  admitted on, and emergency grants still pin it (section 6). It must itself satisfy `spec`.
- `catalog` names the catalog namespace. Only `beadhive` exists; the field reserves room for forks
  and private builds.
- `hq_authority_guard.validate_record` accepts the new key and validates `format`, the parse of
  `spec`, and that `release.id` satisfies `spec`. The Git-backed HQ **refuses** `release_policy`
  and fails closed, because it has no catalog. Ranges are a SQL-HQ feature.

**Changing the policy** is a reviewed authority publication, signed with the operator key, through
the new `bh hq authority release-policy {plan,apply}`. It does **not** advance the epoch.
Eligibility judges each evidence row against the policy at read time, so no evidence row ever
"carries over" a decision made under another policy. The rotation ADR's rejected alternative ("old
evidence could satisfy the new release") doesn't apply: under a policy, any evidence row whose
digest is catalogued and in range is *meant* to satisfy the grant.

- **Narrowing** takes effect on the next read. A frame left outside the range goes ineligible.
- **Downgrade guard:** `plan` refuses a policy whose admitted set excludes the version the frame
  currently attests while still admitting lower ones, unless the operator passes
  `--allow-downgrade`. The epic's floor rule is covered by the spec's own lower bound.
- Moving from exact pin to policy, or back, is the same publication. Dropping the policy restores
  exact-pin behaviour against `release`.

### 2. The release catalog

There is one fleet-wide table in the HQ runtime database, `hq_release_catalog`. It is append-only,
and a tombstone row revokes an entry. Columns:

| Column | Meaning |
| --- | --- |
| `catalog`, `version` | namespace and PEP 440 version |
| `measured_digest` | the `installed_release()` digest of that version's wheel |
| `algorithm` | `beadhive-tree-sha256-v1` (section 5) |
| `wheel_sha256`, `commit` | the published wheel, and the tagged commit from the certificate |
| `bundle` | the Sigstore bundle, stored so anyone can re-verify offline |
| `status`, `reason`, `recorded_at` | `active` or `revoked` |

Rules:

- One active `measured_digest` per `(catalog, version)`. A second, different digest for a version
  is refused unless the first is revoked. This covers a re-pushed tag; PyPI already refuses
  re-uploads.
- **Writer:** a new, separately credentialed `hq.sql.release_catalog` binding with `INSERT` on this
  table only. It is not the authority writer and it holds no operator key. Frames get `SELECT` on
  it, added to the provisioning grant plan and to `check_provisioning`/`check_grants`.
- **Restore floor:** `hq_sql_runtime.py`'s floor refuses any change to the runtime database other
  than the reviewed ones. It gains one narrow exception: a commit whose only diff is rows added to
  `hq_release_catalog`. Edits and deletes stay refused.
- **Read point:** the eligibility statement reads the catalog at the runtime branch's current
  committed head, **not** `AS OF` the signed authority head. Otherwise a new release would need an
  authority publication before it counts, and that is the per-release operator step this ADR
  removes. This is safe because every row has already been verified at ingest and carries its own
  bundle, and the rows only arrive through the INSERT-only binding behind the restore floor.
  Implementation must confirm this in the first SQL child; it is open question 1.

### 3. The release workflow signs each entry (trust anchor (a))

The operator chose trust anchor (a) on 2026-10-21. A new `attest` job in `.github/workflows/release.yml`
runs after `build`:

1. It computes the measured digest **from the wheel**, with a new `scripts/release-manifest.py`.
   - That script and `installed_release()` share one pure fold over `(name, bytes)` pairs, moved
     into `release_measurement.py`, so the two cannot drift.
   - The build job's existing smoke install asserts that the wheel-derived digest equals
     `installed_release()["digest"]`.
   - Research on 0.28.4 found this reproducible across builds. A tree build, a build from the
     sdist and a build from a copied tree gave identical wheels and the same 622-file digest.
     Homebrew builds from the sdist, so it lands on the same `beadhive/` bytes.
2. It writes the manifest predicate `{version, measured_digest, algorithm, file_count,
   wheel_sha256, commit}`.
3. It signs that predicate with `actions/attest`, keyless through GitHub OIDC.
   - The subject is the wheel. The predicate type is
     `https://beadhive.ai/attestation/release-manifest/v1`.
   - The job needs `id-token: write` and `attestations: write`. It uploads the bundle as the GitHub
     release asset `release-manifest-vX.sigstore.json`.
   - PyPI's PEP 740 publish attestation is unchanged. It remains an independent cross-check on the
     same wheel hash.

**Ingest** is `bh hq release-catalog import <version>`, run from any host holding the catalog
binding. It:

1. fetches the bundle and the wheel;
2. verifies the bundle with the identity policy below;
3. requires the statement's subject to equal the wheel's sha256;
4. **recomputes** the measured digest from the wheel and requires it to equal the predicate;
5. inserts the row.

Identity policy:

- issuer `https://token.actions.githubusercontent.com`;
- SAN `https://github.com/beadhive/beadhive/.github/workflows/release.yml@refs/tags/v<version>`;
- a `refs/tags/v*` ref only, so a `workflow_dispatch` run on a branch is refused;
- the certificate's source-repository digest equal to the predicate's `commit`;
- the repository ID checked as well as its name.

Verification uses `sigstore` (Apache-2.0) in a new optional extra, `beadhive[release-verify]`.
Frames never verify anything and gain no dependency. Offline ingest needs a stored bundle and a
pinned trusted root (`--trusted-root`). Online ingest refreshes the TUF root, which makes it the
default.

Further verbs:

- `bh hq release-catalog verify [--all]` re-verifies stored rows from their bundles, for audit.
- `bh hq release-catalog revoke <version> --reason` appends a tombstone. The catalog binding may
  revoke, because revoking only fails safe. A PyPI yank does **not** revoke an entry; that is an
  explicit operator decision.
- **Automation:** an opt-in host-daemon timer (`hq.release_catalog.auto_import`, default `false`)
  imports each new PyPI release. With it on, a release needs no manual HQ step at all. The
  operator's signature is needed only to change a range.

### 4. Release eligibility under a policy

Both carriers evaluate `release_matches` with one shared pure function in a new module,
`release_policy.admits(measured, desired, catalog_row)`. It holds when all of these are true:

- the evidence (or beat) epoch is the current grant epoch, as today;
- a catalog row exists with `measured_digest == measured.digest`, `status == active` and
  `catalog == policy.catalog`;
- `measured.id == row.version`, so a self-reported id can't disagree with the signed binding;
- `SpecifierSet(policy.spec).contains(row.version)`.

Per carrier:

- **Session carrier.** Without a policy, `ELIGIBILITY_SQL` keeps `e.release_digest = %s`, so an
  exact-pin grant runs the same statement and keeps the same parameters. With a policy, the
  statement left-joins `hq_release_catalog` on the evidence digest and returns the row's version
  and status. Python evaluates the spec in `hq_sql_session.read_eligibility`, which already judges
  TTLs there; a specifier can't be evaluated in SQL. The manifest-vs-grant conjunct in
  `_eligible_session` becomes `admits(manifest.release, …)`.
- **Signed-beat carrier.** `frame_eligibility.py`'s three-way equality becomes
  `admits(beat.release, …)`. The beat and the manifest must still agree.
- **Legacy Git/receiver carrier.** It keeps exact pins, since `release_policy` is refused there
  (section 1).
- **Waived mode** (`BH_HQ_AUTHORITY_ENFORCE=false`). It drops the policy conjunct exactly as it
  drops the desired-release conjunct today.

Frame side:

- **`heartbeat_report`** reports the **measured** release in the beat and the evidence row.
  - Under a policy it refuses only when `admits` is false. It can, because the frame holds
    `SELECT` on the catalog.
  - Under an exact pin it behaves exactly as today.
  - The refusal names the measured version and the policy's spec.
- **The committed manifest** (`hosts/<uuid>.yaml` `release`) is not rewritten for in-range
  upgrades. That is the per-host `prepare-config` step this ADR removes. Under a policy, the
  manifest release must itself be admitted, and it is reported as the reference.

### 5. Measurement algorithm is named, not widened

The manifest records `algorithm: beadhive-tree-sha256-v1`. That is today's fold: the `beadhive/`
files only, without `__pycache__` or `.pyc`.

Research found that this fold does not cover the other top-level packages in the same wheel:
`beadhive_core`, `beadhive_plugins`, `beadhive_hq_storage*`, `beadhive_bd_cli` and others. Some of
them hold security-relevant code. Widening the fold changes every digest, which breaks every
existing pin, so it is **out of scope** here. It is filed as a follow-up for `-v2`. The catalog's
`algorithm` column lets v1 and v2 rows coexist during that transition.

### 6. What stays exact

- **Emergency admission.** `emergency.release` and `execution_digest` stay pinned to the exact
  release authorised at emergency time. An emergency is a decision about one execution, not a
  range.
- **Lifecycle CAS** (`--expected-release` is the last beat's digest) is unchanged.
- **Out-of-range upgrades** still go through the full active rotation, as do conformance-profile
  changes and moves between exact pin and policy that change the profile.

## Consequences

- An in-range upgrade on a frame is `uv tool install …` followed by a daemon restart. The epoch,
  principal and credentials stay the same, there is no ineligibility window beyond the restart,
  and no operator key is needed. With `auto_import` on, a release needs no manual HQ step.
- A mixed-version fleet becomes normal: frames can sit on different in-range patches. That raises
  the priority of `bh-71wk9`, so that a fleet-document field change checks every reader's release
  first.
- The rotation machinery stays for out-of-range moves. The 16-entry rotation history horizon is
  reached far less often.
- Tests use moto, `file://` or private servers only. Sigstore verification is tested with locally
  generated bundles and a test trust root, never the public-good instance. Clocks are mocked.

## Implementation slices (children of `bh-vfrem`)

1. **Measurement and CI.** The shared digest fold, `scripts/release-manifest.py`, an equality test
   against an installed wheel, the `attest` job and release asset, and `tests/test_release_workflow.py`.
2. **Catalog.** The `hq_release_catalog` DDL, the catalog binding, frame `SELECT` grants and their
   checks, the restore-floor append-only exception, and the `import`/`verify`/`revoke` verbs behind
   `beadhive[release-verify]`. This slice settles open question 1.
3. **Grant policy.** The `release_policy` schema and its validator, the Git-HQ refusal, the spec
   parser with shorthand normalisation, `bh hq authority release-policy plan/apply`, and the
   downgrade guard.
4. **Eligibility.** `release_policy.admits`, the session-carrier statement variant, the signed
   carrier, and the waived mode. Tests cover each operator form (patch of a minor, patch range,
   minor range, same major), out-of-range, revoked, unknown digest, id/version mismatch and
   tampered bundle.
5. **Frame side.** `heartbeat_report` reports the measured release, the policy-aware refusal, and
   the manifest relaxation.
6. **Docs and dogfood.** `frame-release-upgrade-runbook.md`, release notes, and
   `FRAME-FLEET-MEMBERSHIP.md`. Then move the factory from one in-range patch to the next without
   a grant change, which is the epic's acceptance.

Slices 1 and 3 are independent. Slice 4 needs 2 and 3, slice 5 needs 4, and slice 6 needs all of
them.

## Open questions

1. **Catalog read point.** This ADR reads the catalog at the runtime branch head, not `AS OF` the
   signed authority head. Slice 2 must confirm that this composes with the restore floor and with
   the head the frame verifies. The fallback is a separately committed catalog database that is
   cross-referenced like `config_head`, but only if that doesn't bring back a per-release operator
   step.

   **Resolution (bh-vfrem.4, 2026-10-22): it composes, with one refinement.** The "authority
   head" the eligibility statement already reads `AS OF` is not the commit where the authority
   row was last signed. It is the runtime branch head that the same transaction read and the
   restore floor verified (`hq_sql_runtime`: `head` is `DOLT_HASHOF('HEAD')`, the registry join
   is `AS OF` it, and `fresh_runtime_head_fence(head)` re-checks it afterwards). So the catalog
   is read `AS OF` that same head, not on the working set. A newly imported release moves the
   runtime head at an unchanged authority sequence, and a frame sees the row the next time it
   reads, with no authority publication. Nothing is added to the per-release steps.
   The restore floor needed the exception this ADR already names, and nothing wider. A commit
   that only adds catalog rows is accepted at an unchanged sequence, and tests show it advances
   the floor of every reader. An edit, a delete, a primary-key rewrite and any mixed commit
   still refuse. `DOLT_DIFF_SUMMARY` reports an append, an edit and a delete alike as
   `modified`, so the floor adds a row-level `DOLT_DIFF` and requires every diff row to be
   `added`. Two details came out of the real-server tests. An empty new table shows
   `data_change` 0, so the one-time creation commit is accepted only when it is empty with
   exactly the reviewed columns. The writer needs `EXECUTE` on `dolt_add` and `dolt_commit`,
   because Dolt refuses both to an account without it, which is the authority writer's shape
   too. The separately committed catalog database is not needed.
   Slice 4 must read with `AS OF <the verified head>`, never the working set: a dirty working
   set (an append whose commit failed) would otherwise be visible to a frame before any floor
   has seen it. A head that moves between the read and the fence fails closed, as it does for
   every other read in that transaction.
2. **Should a policy cap how far an auto-imported release can move a frame?** For example, refuse
   a new minor unless the spec names it explicitly. The PEP 440 spec already expresses this
   (`==0.28.*`), so nothing extra is proposed.

## Rejected alternatives

- **Range over the self-reported version string, with no catalog.** Evidence is self-attested, so
  any build could claim `0.28.5`. That is weaker than today's pin.
- **An operator-signed `{version, digest}` allowlist** (option (b)). Sound, but it keeps one manual
  signed step per release. The operator chose (a).
- **An "any release" mode for dev frames** (option (c)). Not needed once a range exists. A
  deliberately wide spec such as `>=0` is still visible in the signed record.
- **Re-using only the PyPI PEP 740 attestation.** It signs the wheel hash, not the version →
  measured-digest binding. It would also need PyPI online at every verification, and PyPI may
  withhold provenance (403). It is kept as a cross-check.
- **Verify Sigstore on frames.** It would add a heavy dependency (`sigstore` 4.x pulls
  `cryptography>=47`, `tuf` and `requests`) to every frame for no gain. HQ already verifies once,
  at ingest, and keeps the bundle.
