# Local-only hive registration — design note

> Status: **approved 2026-11-15** (bh-ndkos; operator answers in the bh-ndkos approval
> comment), implemented by bh-sj2bf. Implements part 2 of the bh-dl3su decision (option B).
> Part 1, the tri-state push-access probe, is bh-9eua2 and is not changed here.
> Line references are to `main` at f17b4b93 (2026-11-15).

## Problem

Onboarding a repo without confirmed push access unsets the beads remote
(`_guard_beads_remote`, `src/beadhive/onboard.py:1636`), so the publish step returns early
(`onboard.py:1678`) and `register` still writes a fleet-wide `managed_repos` entry
(`onboard.py:1710`). External hives also skip publication (`onboard.py:1674`). Two failures
follow:

1. **Fencing.** Every frame's `hives-ready` check requires a ready local replica of every
   registered hive (`heartbeat_report.py:89-113`, `hive_ready` at `:31-40`). Other frames
   cannot clone an unpublished hive, so the check fails. A failed conformance check makes the
   frame ineligible for every lease and claim (`frame_eligibility.py:399-405`,
   `hq_authority_guard.py:757-765`), not just for that hive.
2. **Divergence.** Onboarding the hive on a second host finds no `refs/dolt/data` on origin
   and mints a fresh store (`_bootstrap_from_origin_or_mint`, `onboard.py:948`). The result
   is a second, unrelated database under the same prefix. This is inferred from the code and
   has not been observed.

## Decisions (recommended)

### R1 — Field: `managed_repos[].local_only`

An optional nested object on `ManagedRepoEntry` (`modules/config/contracts.py:4475`):

```yaml
managed_repos:
  - {provider: "github", org: "acme", repo: "tool", prefix: "tl", kind: "prototype",
     local_only: {owner_host: "6f1c…-uuid4", reason: "push-denied"}}
```

| Key | Type | Meaning |
|---|---|---|
| `owner_host` | string, required | The owning host's `host.host_id()` (UUID4 in `host.yaml`, `host.py:136-148,196`) |
| `reason` | enum, required | `push-denied`, `opt-in`, `external` or `migrated` |

- **Default:** the key is absent, which means today's replicated behaviour. Existing entries
  do not need to change.
- **Presence is the flag.** The object has no `enabled: false` form. Clearing the flag deletes
  the key.
- **Owner identity is `host_id`, not `frame_id` or `label`.** `host_id` is minted once and
  never regenerated. It keys `hosts/<host_id>.yaml` (`host_manifest_contracts.py:166`), and
  it is the frame lease's `holder_identity` (`frame_eligibility.py:382-388`). `frame_id` does
  not exist on legacy hosts and can change with each incarnation. `label` is cosmetic.
  Human-facing text resolves the label from the HQ host manifest when it is shown, and falls
  back to the id.
- **Ownership.** The key is fleet data, because `managed_repos` is a fleet prefix
  (`modules/config/application/partition.py:89`). It is **not** added to
  `REGISTER_OWNED_KEYS` (`registry.py:621`), so a `--force` re-register carries it across in
  the same way as `frame_policy` and `dolt_remote`. Only the paths in R2 set or clear it.
- **Validation.** `owner_host` must be a non-empty trimmed string. The model rejects
  `local_only` together with `dolt_remote` or `stores`, because an HQ-declared remote is
  already replicated by `_bootstrap_from_hq_remote`.

### R2 — Lifecycle

| Event | Where | Effect |
|---|---|---|
| First onboard, push **denied** | `_act_register` | writes `{owner_host: <this host>, reason: push-denied}` |
| First onboard, explicit opt-in (bh-9eua2's flag, proposed `--local-only`) | `_act_register` | writes `reason: opt-in`; also unsets `sync.remote` and skips publish even when push is confirmed |
| First onboard, `kind: external` | `_act_register` | writes `reason: external` (see R7) |
| Push **unknown** | bh-9eua2 | refuses before anything mutates; no entry is written |
| Existing unpublished entry | `bh hive repair --local-only --yes` (new mode) | writes `reason: migrated` (see R9) |
| Push access gained later | `bh hive replicate <hive>` (new verb) | publishes first, then deletes `local_only` |

`register` writes `local_only` whatever the config scope (host-only or fleet). A host-only
registry is later unioned into the fleet when the host joins (`registry._managed_repos_base`,
`registry.py:647`), and the marker has to arrive with it. If `host.yaml` is missing, onboard
refuses and points to the setup path that mints it (`config_paths.py:291`). It never records an
empty owner.

`bh hive replicate <hive>` runs only on the owner. It refuses when `reason` is `external`,
because an external hive never replicates (R7). Each step is safe to re-run:

1. Require a **confirmed** push probe (bh-9eua2 tri-state). Refuse on denied or unknown.
2. Restore the beads remote. `_guard_beads_remote` unset `sync.remote`, so re-onboarding cannot
   publish (`onboard.py:1678`).
3. If origin already carries `refs/dolt/data` (`storage_migrate.origin_has_dolt_data`,
   `storage_migrate.py:414`), **refuse**. Someone else has published a database, and merging
   the two is out of scope. Otherwise push through the same leg that `_act_publish` uses
   (`sync_remote._push_dolt_state`, `onboard.py:1684`). That leg reserves the remote fence.
4. Check that origin now carries `refs/dolt/data`.
5. Delete `local_only` from the entry, under the same director registry guard as `register`
   (`registry.py:697`).

The order is publish first, flip second. If any step fails, the hive stays local-only, which is
the safe state. `replicate` also fixes an unpublished entry that has no marker (R9).

### R3 — Config schema, wire and version

- **Config `schema_version` stays 1.** An optional field with an absent default is additive
  (`docs/design/config-schema-versioning.md`, bump rule).
- **Package contract bundle:** regenerate
  `src/beadhive/schemas/contracts/v2.0.0/artifacts/config-v1.schema.json` and `inventory.json`
  with `scripts/generate_config_schema.py`, as `d456e738` (stores) and `7214ed8a` did. The
  `json-schema-additive-v1` gate allows a new optional property. Enum members are compared
  exactly against the v1.5.0 baseline (`contract_release.py:1549-1555`), so the four `reason`
  members are **frozen** once published. Adding one later needs a new contract major.
- **Wire release:** the config field needs none. Wire release directories copy the previous
  `config-v1.schema.json` unchanged (`scripts/publish_wire_release.py:153`), and
  `docs/CONFIGURATION.md:209-211` states that a new config key is not a wire bump. The **new CLI
  surface** needs one `just wire-publish` minor: the new operation `hive.replicate` and the
  appended optional parameter on `hive.repair`. Appending a parameter to an existing
  operation needs an operator-approved entry in `_APPROVED_OPTIONAL_REPAIR_PARAMS`
  (`scripts/check_wire_schema_compat.py:850`), for example
  `"hive.repair": {"local_only": {"type": "boolean"}}`. The precedent is `25c6563f`. A new
  operation needs no compat entry. bh-9eua2 owns the onboard opt-in flag and its wire entry.
- **Package version:** this is a feature, so the next **minor** release. Call it R.
- **Rollout floor (required).** Every config section forbids unknown keys
  (`contracts.py:111-114`). A frame running a release older than R that reads a fleet document
  containing `local_only` fails `host-config-partition` (`heartbeat_report.py:25-28`). The
  SQL policy projection also rejects the whole catalog ("canonical managed hive entry
  invalid", `hq_hive_policy.py:93-96`). Writing the key too early therefore fences the fleet,
  which is the failure this note is meant to fix. Copy the `release_policy` pattern
  (`frame_release_policy.py:29,58-83`): add `MIN_LOCAL_ONLY_READER = R`, and have every writer
  in R2 refuse while any frame attests an older release, with no override. Frames with an
  unknown release can be waived with an explicit flag.

### R4 — hives-ready semantics and evidence

In `measure_conformance` (`heartbeat_report.py:89`), next to `_excluded_here`
(`heartbeat_report.py:72`):

- **Local-only and owned by another host:** the hive is skipped and not required.
- **Local-only and owned by this host:** the hive is still required. If the owner has lost its
  checkout, that is a real failure and the check fails.
- **No marker, or a host with no readable `host_id`:** the hive is required, as today. A
  frame that cannot name itself cannot prove it is not the owner, so it fails closed.

A hive that is local-only elsewhere and also excluded by `frame_policy` is listed once, under
local-only. The evidence string keeps its current fixed-text shape. It names prefixes only,
never host ids or labels, so the evidence never carries host identity:

```text
measured check passed; skipped (frame_policy excludes this frame): ab, cd; skipped (local-only on another host): tl
```

If the string would pass the 1024-character evidence cap (`hq_framelease_contracts.py:68`),
truncate the prefix list and end it with `, … (+N)`.

### R5 — Leases and claims: only the owner

One pure helper, `registry.local_only_refusal(entry, host_id) -> str | None`, makes the
decision. It returns `None` when the entry has no marker or the owner is `host_id`. It is used
in four places:

1. **Frame plane:** a new eligibility predicate, `local_only_owner`, in both `eligible`
   (`frame_eligibility.py:307`, next to `hive_requirements` at `:408`) and `_eligible_session`
   (`:193`, `:261`). This covers lease adopt, renew and takeover through `require_eligible`
   (`host_lease.py:240,306,373`; `host_adopt.py:330,393`) and every claim path through
   `require_intake` (`guard.py:567`, `work.py:1640`, `work_group.py:340`,
   `work_lifecycle.py:246`, `localloop.py:1008`). Refusals read
   `frame <id> ineligible: local_only_owner` (`frame_eligibility.py:91-92`).
2. **Legacy hosts:** for an unbound host, `decision_for` returns `None`
   (`frame_eligibility.py:474`), so no predicate runs. Call the helper directly in
   `host_lease.adopt` and `takeover` (`host_lease.py:265,450`) and in the legacy branch of
   `guard._admit_primary` (`guard.py:546`). The text is:
   `hive tl is local-only on host <label> (<host_id>); only that host may lease or claim its beads`.
3. **`--force` takeover is refused as well, with no override.** The data does not exist on the
   other host, so a takeover would only lead to a fresh mint.
4. **Release and renew by the owner** work as they do today.

**What other frames see:**

- The hive is skipped in their `hives-ready` evidence (R4).
- A claim or lease attempt is refused with the text above.
- `bh hive list` and `bh hive status` show `local-only (owner <label>)` rather than a missing
  clone.
- The owner's lease shows in lease listings as usual.

**Receiver side is unchanged in this version.** The signed SQL `hive_policies` projection has an
exact key set (`hq_hive_policy.py:44`). The receiver and Git guard checks
(`hq_sql_receiver.py:569`, `hq_authority_guard.py:798`) match only `requires`. Adding the owner
there changes the signed protocol for every receiver, so it is open question 3.

Frame leases already need a `frame_policy` (`hq_authority_guard.py:727-732`;
`hq_hive_policy.py:104` drops entries without one). An owner frame that leases a local-only
hive therefore still needs the hive's `frame_policy`, the same as any other hive.

### R6 — Onboarding on a host that is not the owner

Add a new **non-overridable** check, `local-only-owner`, to the `resolve` step
(`onboard.py:2055-2060`). Preflight evaluates it before `clone` (`:2061`) and `bd-init`
(`:2142`), so a refused onboard clones nothing and creates no database. The check uses the
existing entry from the fleet registry (`_ensure_derived`, `onboard.py:439`). The refusal text:

```text
✗ local-only-owner (hive is local-only to its owning host)  acme/tool (prefix tl) is local-only
  on host <label> (<host_id>): its bead database exists only there and was never published.
  Onboarding here would create a second, divergent database under the same prefix. Work on
  that host, or have its owner run `bh hive replicate tl` once it has push access.
```

For `reason: external`, the last sentence becomes "External hives are never replicated; work
on that host."

### R7 — `kind: external` hives: same problem, same rule

**Finding:**

- External hives register under the upstream triplet. `_act_clone` forks with origin set to the
  fork (`onboard.py:780-787`).
- `_guard_beads_remote` probes push access to the upstream, which is normally denied.
- `_act_publish` returns early for `kind == "external"` in any case (`onboard.py:1674`).

So an external hive is always unpublished. `hives-ready` has no external exemption: the only
filters are `registry.hives`, which drops `kind: hq` (`registry.py:168-175`), and
`frame_policy`. Other frames are therefore fenced by it. A second-host onboard mints a fresh
store, because the second host's fork has no `refs/dolt/data`. The fence and the divergence
are both present.

**Rule:** treat external hives as local-only by construction. Onboard writes
`reason: external`. `replicate` refuses them, because publishing private beads onto a public
fork's `refs/dolt/data` is not allowed. The pull-only contribution policy stays as it is.
Existing external entries have no recorded owner and are migrated in the same way as R9.
The factory fleet has no external hive registered today (2026-11-15).

### R8 — `frame_policy` relationship: separate field

Keep `local_only` separate from `frame_policy.requires`. Do not add a host-pin key to it.

- `requires` matches frame **capabilities**, not identity. Its key set is closed in five
  matchers (`contracts.py:4165-4181`, `hq_authority_guard.py:619-635`,
  `frame_eligibility.py:168-190`, and the receiver and validator copies), and caps do not
  carry `host_id`.
- `frame_policy` grants frame authority, and every hive shares one fleet-wide
  `config_revision` that the authority binds (`hq_control_plane.py:2207-2213`,
  `hq_authority_guard.py:737-740`). Pinning one hive to one host would need a coordinated revision
  bump.
- A hive without a `frame_policy`, such as one on a legacy host, still needs the marker.
- The rules differ. `frame_policy` exclusion fences nothing on the excluded frame. Local-only
  also has to refuse onboarding, and it carries a reason and a way to clear it.

### R9 — Migration of existing unpublished registered hives

**Detection** is a `bh doctor` finding, "registered hive is unpublished and not local-only".
It applies to any entry that meets all of these:

- it is not `kind: hq`;
- it has no `local_only`, `dolt_remote` or `stores`;
- `origin_has_dolt_data` is false.

The check runs on hosts that have the hive's checkout and store. It probes the network, so it
runs only in doctor (bh-dm3bs: doctor is already slow and runs in the background) and never in
the conformance job.

**Fix,** run on the host that holds the store:

| Push probe | Action |
|---|---|
| confirmed | `bh hive replicate <hive>`: publishes; there is no marker to clear |
| denied, or external | `bh hive repair --local-only --yes`: records this host as the owner, `reason: migrated` (or `external`) |
| unknown | fix `gh` or auth and re-run; never guess |

`repair --local-only` refuses in two cases:

- another host already holds an unexpired lease for the prefix;
- the entry already names a different owner.

If two hosts both hold a store for the prefix (the divergent case may already exist), the
second `repair` refuses, and the operator chooses the canonical copy. The other host exports
anything it wants to keep and then runs `bh hive reclaim`. Until a hive is fixed, behaviour is
the same as today.

## Alternatives rejected

- **Flat `replication: local|replicated` plus `owner_host`.** It allows invalid combinations,
  and the `replication` enum would be frozen by the additive gate. Presence of the object
  already says the same thing.
- **A host-pin key in `frame_policy.requires`.** See R8.
- **Record it in the per-hive HQ manifest (`hives/…yaml`, `hive_schema.py`).** That file
  holds probe-refreshed observations (`hive_schema.py:9-14`). Ownership is a declared,
  rarely-changed fact that every frame must read from the fleet config.
- **Infer local-only from a missing local checkout.** That would hide real breakage on
  replicated hives, would not stop a second-host onboard, and has no owner to name.
- **Clear implicitly by re-running onboard.** A re-run deliberately preserves settings
  (`onboard.py:1726-1733`) and skips `bd init` on an existing store, so it never restores
  `sync.remote`. Flipping implicitly would hide a publication.
- **`frame_id` as the owner.** It is absent on legacy hosts and changes with each incarnation.
- **Enforcement at the receiver in this version.** It is a signed-protocol change for every
  receiver (open question 3).
- **Options A and C of bh-dl3su.** The operator rejected them.

## Open questions for the operator

1. **Names.** Approve `local_only.owner_host` and `local_only.reason` with the members
   `push-denied | opt-in | external | migrated`. The members are frozen once published (R3).
2. **CLI.** Approve the new verb `bh hive replicate` and the new mode
   `bh hive repair --local-only`. Together they are one wire minor, plus a compat-gate entry
   for `hive.repair`.
3. **Receiver enforcement.** File a follow-up to add the owner to the signed `hive_policies`
   projection, so the authority rejects a non-owner's lease? It needs its own reader floor.
4. **Moving ownership between hosts.** Not supported in this version. The ways out are
   `replicate`, or `bh hive rm` plus a manual store move by the operator. Should there be a
   `transfer` verb?
5. **Host retire.** Recommended: `bh host retire` refuses while the host owns local-only hives
   and lists them, because their only copy would be reclaimed. The remedy is `replicate` or
   `bh hive rm`. Should it refuse, or warn and continue?
6. **Rollout floor.** Is "unknown release waivable, older release never" acceptable? Legacy
   non-frame hosts cannot be measured and stay the operator's responsibility.
7. **Onboard JSON.** Should a `local_only` property be added to `bh-hive-onboard-v1`
   (additive, wire minor)? The recommendation is no for this version; the marker appears in
   text and in `warnings`.
