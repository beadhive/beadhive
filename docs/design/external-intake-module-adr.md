# ADR: The external-intake module, its consumer port, the outbox and the report dimensions

- **Status:** Accepted (verdict **GO**), effective when `bh-sa91g` merges
- **Date:** 2026-10-25
- **Decision owner:** Beadhive maintainers
- **Related work:** `bh-sa91g` (this decision), epic `bh-wjvq9` (the external-intake design
  molecule, prerequisite 3 of the tracker loop), spikes `bh-mj7cl`, `bh-fptob` and `bh-6wj9y`;
  the port decision `bh-x6f9l` (epic `bh-ire9a`) and the plugin decision `bh-h7nw7` (epic
  `bh-mobxo`); placeholder for the whole loop `bh-33d5a`; intake state-machine redesign
  `bh-elkv`; egress policy `bh-o2mjz`; projection defect `bh-cm4er`
- **Evidence (all merged, all verdict GO):**
  - [`docs/spikes/bh-mj7cl-external-intake-boundary.md`](../spikes/bh-mj7cl-external-intake-boundary.md)
    (module boundary, `IntakePort`, `ReportStore`, import refusals, the `tracks` link, discovery,
    checker probe of every proposed edge, measured closure)
  - [`docs/spikes/bh-fptob-conversation-model.md`](../spikes/bh-fptob-conversation-model.md)
    (thread and comment model, storage on the report bead, revisions, tombstones, own-message
    recognition, 19 offline test cases)
  - [`docs/spikes/bh-6wj9y-outbox-and-dimensions.md`](../spikes/bh-6wj9y-outbox-and-dimensions.md)
    (outbox records and state machine O1 to O19, the no-duplicate argument, the `delivery` and
    `notified` dimensions, the `intake-hold:` gate, field ownership, divergences D-a to D-g)
- **Builds on:** [`tracker-adapter-port-adr.md`](tracker-adapter-port-adr.md) (the port, its
  direction block, the projection decision) and
  [`tracker-adapter-plugin-adr.md`](tracker-adapter-plugin-adr.md) (the `tracker.adapter`
  category, the `tracker-github` plugin, the store binding, the credential home), plus
  [`external-issue-intake.md`](external-issue-intake.md) (the source design),
  [`two-store-scope-adr.md`](two-store-scope-adr.md) and
  [`github-issue-projection.md`](github-issue-projection.md).
- **Amends:**
  - the port ADR, in three additive points: the direction block's "`(+ application)`" for
    intake is withdrawn (gap G5); an optional `IssueResolver` protocol is added (amendment A1,
    API version stays 1); the conformance kit gains `fail_next_write_ambiguously(applied=)`
    (extension K1);
  - [`external-issue-intake.md`](external-issue-intake.md) section 1, in that a **bound** report
    is never itself the implementation work item (D-a below);
  - the three spikes where they diverge, as settled in "Resolutions". Nothing else.

## Decision summary

Paste-ready for the close reason of `bh-sa91g`.

- **Verdict: GO.** Adopt external intake as the explicit internal module
  `beadhive.modules.intake`, owning the one shared intake queue, behind the consumer port
  `IntakePort` and its own bead-store port `ReportStore`, over the accepted tracker port.
- **Storage:** module-owned flat metadata documents on the internal report bead, one top-level
  key per writer: `intake.source` (issue snapshot), `intake.thread` (conversation) and
  `intake.outbox` (communication records). This settles the naming split (`bh.intake.source`,
  `external_intake`, `external_outbox`) noted in `bh-6wj9y` R12 D-e.
- **A bound report is never work.** It is held out of `bd ready` for life by its own typed
  `intake-hold:` gate and `tracks` a separate work bead. Completed work closes at merge as today;
  the report carries the new `delivery` and `notified` dimensions. This is the
  merged-but-unreleased mechanism.
- **Outbox:** a per-operation state machine (`bh-6wj9y` R5, O1 to O19), supervised only (D2).
  Exactly two tracker writes exist (`comment.create`, `labels.change`); each needs its own
  operator approval minting its own single-use `WriteGrant`.
- **Composition:** the adapter comes from the `tracker.adapter` v1 capability (`tracker-github`,
  bound by `managed_repos[].stores.public.adapter`); `bd github sync` stays; `bh-cm4er` stays on
  the bd path.
- This ADR files no bead, does not run `/bh:replan` and does not close `bh-33d5a`. The
  implementation molecule is filed later through `/bh:replan` on epic `bh-wjvq9` by the
  operator or planner, using "Implementation molecule input" below.

## Context

The port ADR settled a tracker-neutral port and a GitHub adapter; the plugin ADR settled how that
adapter is a plugin and how a store is bound to it. Both deliberately left the consumer to this
molecule: threads, revision history, own-message classification, the outbox and the report
dimensions are "not in the port" (port ADR section 3), and the plugin ADR lists "the
conversation model, outbox and gate implementation (`bh-wjvq9`)" as open.

Operator direction of 2026-10-24 makes the logic that manages the intake queue against external
trackers its own tracker-neutral module, depending on the tracker port and never on GitHub.
Three binding operator decisions frame it:

- **D1.** One internal report bead per external issue: `external_ref` is the issue URL,
  `origin:github`, `intake:untriaged`, keyed on the URL and never on a public-store id
  (`bh-nkssm`). It links to canonical work through a native edge inside the internal store, many
  reports to one work bead. No edge crosses stores.
- **D2.** The first outbox slice is supervised only: a write happens only on an explicit
  per-record operator confirmation after a dry-run render.
- **D3.** Import goes ahead of `bh-elkv`, lands in `intake:untriaged` only, under a verb that is
  not `promote`. Wake-on-reply waits for `bh-elkv`'s needs-info state.

The constraints are also binding: configurable and off by default; every tracker write an
operator-approved gate; nothing writes into the public store except the pull; no `bh-*` id or
internal content leaves unless deliberately published (`bh-o2mjz`); closed issues in scope;
tests offline and never timing-dependent; the public `refs/dolt/data` stays frozen pending
`bh-l3zvc.5`; never `bd compact` or `bd flatten`.

Three research-only spikes ran (no product code, no GitHub request, no write to a hive store):
`bh-mj7cl` (module boundary, import, link), `bh-fptob` (conversation model) and `bh-6wj9y`
(outbox, milestones, dimensions). Each returned **GO**. `bh-6wj9y` built on the other two and
recorded seven divergences from them (R12, D-a to D-g); reading the three side by side shows
five more. This ADR settles every one and records the single design the implementation
molecule builds on.

## Decision

**Overall verdict: GO.** The three verdicts are consistent and none rests on another's
unverified claim:

- `bh-mj7cl` showed the module boundary is admissible under the real checker: every edge the
  module must take is allowed and every edge it must never take is already rejected, except
  three small, named gaps (G5 to G7). It scored 12 of 14 (14 once the gaps close), against 9 or
  less for every alternative.
- `bh-fptob` showed one storage location and one pure rule set satisfy every conversation
  constraint, with the port already transporting every input the rules need.
- `bh-6wj9y` showed the outward half fits the port and both siblings with one structural
  choice (a bound report is never work) and a set of additive changes, and gave a proof sketch
  that timeout reconciliation never posts twice under one stated visibility assumption.

What is not free, and owned by the implementation molecule (none changes the verdict): the
checker gaps G5 to G7; the port amendment A1 and kit extension K1; three to four new
`beadhive-bd-cli` routes; the operator-view blocker fix; a rollout order for the new vocabulary
and config; and the unverified assumptions in "Risks" (A1 visibility, metadata growth, closure
figures).

### 1. The module and its consumer port

| Element | Decision |
|---|---|
| Capability | **External intake**: getting reports into the one shared queue, binding them to their source, disposing of them, linking them to work, the conversation model, the outbox and the report dimensions |
| Package | `beadhive.modules.intake` with `domain/`, `contracts/`, `application/` (the `modules/hives` shape), layout as in `bh-mj7cl` section 1 |
| Consumer port | `beadhive.modules.intake.contracts.IntakePort` (`Protocol`); `IntakeService` its one implementation; `NullIntake` bound when intake is off (empty queue reads, `reports_for_work` returns `()`, every write raises `IntakeRefused(INTAKE_DISABLED)`) |
| Outbound ports | `ReportStore` (intake's own narrow bead-store port), `CheckpointStore`, `LabelPolicy`, `Sanitizer`, `ReleaseVerifier`, `IntakeLock` (section 4), `IdSource`; plus the tracker port's `TrackerReader`, `CommentWriter`, `LabelWriter` and the optional `IssueResolver` (A1) |
| Adapters | `beadhive.adapters.intake`: `BdReportStore` over an injected `beadhive_bd_cli.CliRoutes`, a file-backed `CheckpointStore` in the hive's private `.bh/` state |
| Test kit | `beadhive.testing.intake`: `MemoryReportStore` and `ReportStore` contract cases run against both implementations; consumer tests use `MemoryTracker` from `beadhive.testing.tracker` |
| Composition | `beadhive.intake_services`, a legacy composition root (the `hive_services.py` precedent: neither `bootstrap` nor an adapter may import legacy) |
| Tests | `tests/unit/modules/intake/` under `_unit_tests("modules/intake", ...)`, `attest:unit`, with `test_intake_independence.py`; one bd-binary integration test replaying `bh-6wj9y` E4 |

**One queue, one module.** `bh report` and `bh escalate` move behind the port too, so imported
issues and internal reports share one queue, one closed `origin` channel and one set of
dispositions (`bh-mj7cl` candidate A). Splitting import from the queue (candidate B) or folding
intake into planning, work or the tracker were rejected (see "Alternatives rejected").

**The port** is `bh-mj7cl` section 2 (`file_report`, `import_issue`, `link`, `unlink`, `queue`,
`dispose`, `binding_of`, `reports_for_work`, `discover`) plus the `bh-6wj9y` R11 additions
(`on_work_merged`, `on_release`, `draft_reply`, `outbox`, `render`, `approve`, `send`,
`reconcile`, `discard`, `retry`, `work_progress`). Further operations are added to this port,
not beside it. If its width hurts review, the contracts may split out an `OutboxPort` with one
`IntakeService` implementing both; either shape is one module. No bd argv, label string or
store path crosses the port.

**`ReportStore`** is `bh-mj7cl` section 3 with the `bh-6wj9y` R11 additions: `create` opens the
hold gate, `close` is the held close (section 7), and `read_outbox`, `write_outbox(...,
expect_seq)` and `reports_with_outbox()` are added, with the matching thread-document pair for
`bh-fptob`. The composition root builds it only over
`store_target.routes(store_target.operational_store(entry))`, never over a public store; a test
asserts the intake composition never resolves `SCOPE_PUBLIC`.

**New named routes in `beadhive-bd-cli`:** `create_report(..., external_ref=)`,
`issue_list(external_ref=, has_metadata_key=, all_=True)`, `issue_close_held` (`bd close
--force`, for the held close only) and `issue_set_metadata(bead, key, doc)` (`bd update
--metadata @file`). `dependency_list(..., type_="tracks")` and `gate_create` already exist.

### 2. Dependency direction and enforcement

```text
cli.py / work.py / mcp.py / host daemon --> beadhive.intake_services   (legacy composition root)
beadhive.intake_services --> beadhive.modules.intake (public API), beadhive.adapters.intake,
                             store_target / registry / validate / hub / engine (legacy),
                             bootstrap/tracker.py's bound adapter (lazy, via the plugin kernel)
beadhive.adapters.intake --> beadhive.modules.intake.contracts, beadhive_bd_cli
modules.intake.application --> modules.intake.{domain,contracts}, modules.tracker.contracts
modules.intake.contracts   --> modules.intake.domain, modules.tracker.contracts
modules.intake.domain      --> standard library only
merge / release hooks      --> modules.intake.contracts (IntakePort) only
forbidden: intake -> any legacy module, adapters, integrations, bootstrap, CLI, typer, httpx,
           githubkit, tracker.application, modules.work.*, modules.planning.*;
           tracker -> intake (G3)
```

**Enforced today** by `scripts/check_import_boundaries.py` (probed edge by edge in `bh-mj7cl`
E8): intake layers importing legacy (`triage`, `cli`, `bd_cli`, `work`), intake importing
adapters, the domain importing tracker contracts, and `typer`/`httpx`/`fastmcp` in the domain or
application layer.

**Closed in the module's first bead**, each with a negative-control test in the style of
`probe_boundary_rules.py`:

| Gap | Passes today | Decision |
|---|---|---|
| G5 | The port ADR's direction block lets intake import `tracker.application` (`iterate`, `snapshot_ids`), which `_direction_allowed` actually forbids | **Intake owns its own pager and snapshot loop** (about 20 lines, same semantics: `complete=False` on any port error). Do not relax `_direction_allowed`. The port ADR's "`(+ application)`" is withdrawn |
| G6 | Intake importing a consumer's contracts (for example `modules.work.contracts`) | A consumer-exclusion rule: owner `intake` may not import `modules.work.*` or `modules.planning.*` (a two-entry table beside the port ADR's G3 leaf rule) |
| G7 | Legacy files importing intake internals (legacy importers are unchecked) | Module-local AST test: no legacy file except `intake_services.py` imports `modules.intake.domain` or `.application`; plus the hives-style side-effect sentinel (no subprocess, socket, file read or `typer` on import) |
| G1, G2 | `contracts` importing `httpx`; any layer importing `githubkit` | Owned by the tracker implementation's first bead (plugin ADR W1). Intake's first bead depends on it, or carries them if it lands first |

The existing `modules/state` (durable read projections) is a different module and does not own
the intake vocabulary; there is no overlap.

### 3. What moves, what stays

Adopted as `bh-mj7cl` section 8, in short:

- The intake and origin vocabulary (`STATE_DIMENSIONS["intake"]`, `["origin"]`, the `INTAKE_*`
  and `ORIGIN_*` constants, `DISPOSITION_STATE`, the predicates, `channel_of`,
  `origin_from_source_system`) moves to `modules/intake/domain/vocabulary.py`
  (`INTAKE_DIMENSIONS`). `state.py` stays as a **facade** that re-exports them and builds
  `STATE_DIMENSIONS` from `INTAKE_DIMENSIONS` plus its own entries, so
  `registry.closed_dimensions` and the 238-module reverse closure of `state.py` are unchanged.
  The contributor (`outbound`, `publish`) and dispatch (`escalation`, `dispatch`) vocabularies
  stay in `state.py`.
- `report.file_report` policy moves to `IntakeService.file_report` + `BdReportStore.create`;
  `report.py` keeps hive resolution, clone-on-demand and push.
- Queue listing and dupes (`list_intake`, `find_dupes`, `dupes_touching`) move to
  `IntakeService.queue` + `ReportStore.duplicate_pairs`; dispositions move to
  `IntakeService.dispose`. `triage.py` keeps facade functions until `contributor.py` migrates.
  `reroute --to <hive>` becomes an orchestration in `intake_services`, which retires the owned
  legacy cycle `cycle-edge-036`.
- `work_intake.py` stays a thin CLI over `intake_services` and gains the new verbs; `escalate.py`
  keeps HQ resolution and files through the port; `adopt.py`/`plan.py` stay facade consumers.
- Out of the boundary: `report_target.py`, `github_projection.py`, `tracker_sync.py`,
  `triage_store.py`.

The first module bead is behaviour-preserving: the existing intake tests pass unchanged.

### 4. Storage: three module-owned documents, one lock, one checkpoint store

Each document is one **flat top-level metadata key** on the **internal report bead** (the bead
D1 already creates), schema-versioned, owned by exactly one writer, and rewritten whole in one
`bd update --metadata @file` against `store_target.operational_store`. Top-level keys merge and a
key's value is replaced wholesale (`bh-fptob` E11), so each writer rewrites only its own key and
every other key survives.

| Key | Holds | Sole writer | Source spike |
|---|---|---|---|
| `intake.source` | the issue snapshot (`SourceSnapshot`: state, `closed_reason`, labels, author key, opaque revision, `source_missing`) | import and discovery | `bh-mj7cl` section 4 (was `bh.intake.source`) |
| `intake.thread` | the thread: comment records, revisions, tombstones, per-issue checkpoint, `seq`, `state` | the thread ingest pass | `bh-fptob` R1, R4 (was `external_intake`) |
| `intake.outbox` | delivery facts and communication records with their operations | the outbox service | `bh-6wj9y` R4 (was `external_outbox`) |

**Naming (resolves D-e).** One convention for all three: a flat key whose string is
`intake.<document>`, the dot being part of the key and not a JSON path. This is the repository's
existing metadata-key precedent (`git.commits`, `docs/design/bead-commit-linkage-contract.md`),
it names the owning module, and it keeps the documents separate. They must stay separate:
`bh-6wj9y` R1 showed that sharing a key would rewrite a possibly multi-megabyte thread on every
outbox transition, and the three have different writers and sequence counters. The `bh.` prefix
of `bh-mj7cl` is dropped (no other bead metadata key carries it). The first store bead verifies
that `bd list --has-metadata-key intake.outbox` and `--metadata` merging behave for a dotted key
as they did for the undotted keys probed in `bh-fptob` E11 and `bh-6wj9y` E4; if bd mishandles
the dot, the fallback spelling is `intake_source`, `intake_thread`, `intake_outbox`, chosen once
for all three before any lands.

**Verified (I1, `bh-j3ayl`, bd 1.3.0; settles the dotted-key question and acceptance item 2).**
`tests/test_intake_bead_store_int.py` pins, against a real bd store, that a dotted key is one flat
top-level key: `bd update --metadata` merges top-level keys and replaces a key's value whole (the
nested keys of the old value are gone), another dotted key survives a rewrite, `bd list
--has-metadata-key intake.thread` matches exactly that key and never its `intake` prefix or a
`intake.thread.seq` path, and metadata stays writable and listable on a closed bead. The fallback
spelling (`intake_source` and the like) is **not** needed. Documents travel on stdin
(`--metadata @/dev/stdin`), not argv: a multi-megabyte thread would exceed one argv string.

Growth was re-measured on an isolated scratch Dolt sql-server store (server mode, the engine mode
the shared server runs; never the fleet's shared server), with seeded realistic comments (median
about 400 B, about 600 B mean), no garbage collection, on a loaded host:

| Thread | Document | Rewrite (`--metadata`) | `bd show` | Store growth per appended-comment rewrite |
|---|---:|---:|---:|---:|
| 10 comments | 6.8 KB | 0.26 s | 0.19 s | |
| 50 comments | 46 KB | 0.26 s | 0.20 s | |
| 200 comments | 154 KB | 0.27 s | 0.21 s | median 42 KB (31 to 184 KB), 32% of a naive full copy |
| 1000 comments | 825 KB | 0.36 s | 0.25 s | median 54 KB (41 to 893 KB), 16% of a naive full copy |

Rewriting the whole document is cheap in time and Dolt shares the unchanged chunks, so growth per
transition is roughly 5 to 25% of the document, with an occasional near-full copy when a chunk
boundary moves. **The finding for the queue and thread beads:** `bd list --json` (and `--brief`)
return every row's whole `metadata`, so the untriaged-queue read carries each report's documents:
20 untriaged reports with 200-comment threads read as 3.4 MB in 0.35 s, against 15 KB in 0.22 s for
20 reports without a thread, and `bd` has no metadata-free list. Keep the queue read to the fields
it needs by routing it through a per-row read only where a thread exists, or by adding a
metadata-free list route, before thread ingest (I7) ships.

**Why the report bead and not elsewhere** (adopted from `bh-fptob` R4): records and checkpoint
commit in one write; it is keyed where D1 keys; no queue or `bd ready` reads metadata; cost is
per thread, not per comment. Native bd comments (add-only, no external id, actor key, revision or
tombstone), one child bead per comment (a permanent bead per comment in an archive-only hive), a
host-local sidecar (lost on failover) and the public store (breaks pull-only) are rejected.

**Checkpoints (an unrecorded divergence, resolved here).** `bh-mj7cl` keeps the issue-discovery
checkpoint in a file-backed `CheckpointStore` in `.bh/`; `bh-fptob` puts the project-wide
comment-sweep checkpoint "in the same kind of module-owned metadata key on one per-binding
internal record". **Decision:** both **per-binding** checkpoints (issue discovery and comment
sweep) go through the `CheckpointStore` port, implemented first by `bh-mj7cl`'s file-backed
store. Losing either only forces a full pass, which the `(key, revision)` upserts make
idempotent, so failover loss is acceptable and no extra bead record is needed. **Per-issue
thread checkpoints stay inside `intake.thread`**, because they must commit atomically with
their records (`bh-fptob` R5 rule 4).

**One lock (an unrecorded divergence, resolved here).** `bh-mj7cl` serialises import and
discovery per hive; `bh-fptob` asks for one writer per binding for ingest; `bh-6wj9y` R6 defines
the send lease. **Decision:** one injected `IntakeLock` seam, keyed per binding (in v1 a hive has
one bound public store, so per binding equals per hive), taken by every mutating intake
operation: import, discovery, thread ingest and every outbox transition. The root implements it
as `bh-6wj9y` R6 item 4: `guard.guard_primary(hive)` (one host), an `fcntl.flock` on a
per-binding lock file in `.bh/` following `checkpoint._checkpoint_lock` (one process), and a
`seq` re-read of the document under the lock (one record version; bd has no compare-and-swap).
`Attempt.epoch` records `guard.live_epoch`. No lease expires by time; a dead holder is detected
by a released flock or a moved epoch. Tests use an in-process lock with a settable epoch.

**Privacy guard.** Intake refuses to enable on a hive whose operational store would be published
(a single-store hive, or an internal remote on `git-origin`) unless the operator sets an explicit
acknowledgement (`bh-fptob` R4, R9). Purging a tombstoned body removes it from the current
document only; under the archive rule earlier versions remain in store history, and the
documentation must say so and never present purging as erasure.

### 5. Import (D1, D3)

Adopted as `bh-mj7cl` section 4, with the `bh-6wj9y` hold folded in:

- **Verb:** `bh work import <issue-url> [--hive H] [--dry-run] [--json] [--as SEAT]` (name a
  proposal for the implementation to ratify; never `promote`). It lands only in
  `intake:untriaged`, channel from the bound tracker (`origin:github`, never `origin:import`).
- **Refusals before any tracker request**, in order, each a one-line `IntakeRefused(reason)`
  naming the fix, exit 1, zero store writes: R0 `INTAKE_DISABLED` (intake off, no store bound,
  or `issues.read` not usable; the plugin ADR's R13 "store binds nothing" maps here), R1
  `PUBLIC_STORE_ID`, R2 `NOT_A_URL`, R3 `UNKNOWN_URL`, R4 `OTHER_PROJECT`, R5
  `CHANGE_REQUEST`, R6 `NOT_VISIBLE`, R7 `OTHER_PROJECT` after a transfer, R8
  `DUPLICATE_BINDING`.
- **Idempotence key:** the canonical `IssueKey.url` returned by the adapter, never the typed
  string. Lookup is `find_by_external_ref(url)` across every status, closed included, **only in
  the operational store**. The public projection's row for the same URL is never read or linked.
- **Create order:** `bd create --external-ref <url>` (binding atomic with the bead), then
  **the `intake-hold:` gate immediately** (section 7), then `origin`, then `intake=untriaged`,
  then `intake.source`. A found row missing any of these is repaired for the missing piece
  only, never overwriting an existing disposition. Unchanged revision: no write. Newer revision:
  only `intake.source` is rewritten; title, description, `intake`, status and links are never
  touched, so re-import never resets triage.
- **Closed issues** import exactly like open ones: an open, held report in `intake:untriaged`
  with `state: closed` in the snapshot. Upstream reopen or close is a snapshot update only.
- **Concurrency:** under `IntakeLock`; after every create, re-read by URL and raise
  `DUPLICATE_BINDING` on a second row without auto-closing anything.
- **Dry run:** classifies R0 to R8, reports `would create` or `would update`, zero store writes,
  read requests only.
- **Amendment A1** (to the tracker implementation, additive, API v1): optional
  `IssueResolver.resolve(url) -> IssueKey` (pure) and `get(key) -> TrackerIssue` (one read).
  Until it lands, import pages `list_issues` and matches the URL, collapsing R3 to R6 into
  `NOT_FOUND_IN_PROJECT`. A1 should land with or before the import bead.
- Import, link and discovery perform **no tracker write**, hold no `WriteGrant` and call no
  writer. Issue bodies are untrusted input, stored as data and never interpreted.

**As built (I4, `bh-2ef4g`).**

- **Where.** `application/importer.py` (`IssueImporter`, the only tracker-typed code of import)
  behind `IntakeService.import_issue`; `intake_services.import_issue` composes the
  `intake.external.enabled` switch (host-owned, off), the tracker the hive's public-store binding
  serves (an injected `TrackerAdapters`, asked for `issues.read` only; R13, a disabled plugin and
  a disabled read are all R0 `INTAKE_DISABLED` naming the key to set), the operational
  `BdReportStore`, the root `FlockIntakeLock` and `ValidateLabelPolicy`. `work_intake.import_issue`
  is the command body and `work.import_cmd` the Typer function. **No production credential wiring exists**
  (`bh-kzs0b`): `intake_services.process_adapters` returns `None`, so the verb refuses with R0 until
  that bead binds a real `TrackerAdapters`; tests inject `adapters=` or `tracker=`.
- **Resolver and fallback.** With an `IssueResolver`: `resolve(url)` (pure) gives R3, R4 and the
  URL form of R5; `get(key)` (one read) gives R6 and the read form of R5; R7 is the canonical URL
  `get` answered differing from the one asked for and no longer resolving in this project (a
  rename inside the project is accepted and keys on the new URL). Without one, `list_issues` is
  paged and matched on the URL stripped of query, fragment and trailing slash (the only
  canonicalisation that needs no grammar), and R3 to R7 are the one `NOT_FOUND_IN_PROJECT`. Any
  other tracker failure is `TRACKER_ERROR` (a new reason) with the adapter's already-scrubbed
  message; `OperationNotEnabled` is R0.
- **Revision.** The snapshot's `revision` token is the tracker's `updated_at` plus its optional
  version, `<iso8601>[#<version>]`; `domain.rules.revision_is_newer` decides: equal or older is a
  no-op, an unreadable token is refreshed rather than frozen. Repairs of an interrupted create
  (`hold`, `origin`, `intake`, `source`) are reported in `ImportOutcome.repaired`, fix only the
  missing piece and never touch an existing disposition; a closed report is never given a hold.
  The hold repair is a targeted `holds()` check for this report, not `sweep_holds`, so a re-import
  does not scan every report.
- **Lock and config.** Import uses the root's one `FlockIntakeLock` (`intake_services.intake_lock`,
  the same lock the outbox sender holds): `guard_primary` first (`NOT_PRIMARY`, naming `bh work
  import`), then an advisory never-waiting `flock` under the hive's `.bh/intake/locks/`
  (`INTAKE_BUSY`), yielding `live_epoch`. A dry run takes no lock.
  `intake.external` carries `enabled` and `default_type` only, is **host-owned** and global (a
  per-hive override in the fleet-owned `managed_repos` would let a fleet push enable import, so none
  exists yet); it is documented in `docs/CONFIGURATION.md`.
- **Tests.** `tests/unit/modules/intake/test_intake_import.py` (R0 to R8 on the resolver and the
  fallback, dry run, repair, idempotence, closed issues) and
  `tests/test_intake_import_composition.py` (the composition: acceptance item 5 with a
  byte-fingerprinted public-store fixture and a `resolve_store` spy after every case, the config
  switch, store failures, rendering).
  `MemoryWorld.transfer_issue` is the minimal model of a transfer that R7 needs (`bh-ukesf`).
- **Not done here.** The verb is **intentionally not registered** in `CLI_HANDLERS` or the
  operation catalog: registering it needs `just wire-publish`, which cuts the next wire release
  (v2.16.0), a release step reserved for the operator. `work.import_cmd` and
  `work_intake.import_issue` are implemented and tested but unreachable from the CLI until then.
  **Registration recipe**, in order:
  1. `src/beadhive/kernel/operations/__init__.py`: add the `work import|url:string:r,hive:string:o,dry_run:boolean:o,as_json:boolean:o,as_:string:o`
     row to `_CLI_ROWS` (match the encoding, option names and canonical-name map of the sibling
     `work accept` / `work promote` rows).
  2. `src/beadhive/work.py`: add `"work.import": import_cmd` to `CLI_HANDLERS` (no
     `_bind_cli_handler` rename is needed: the Python parameters are already `url`, `hive`,
     `dry_run`, `as_json`, `as_`).
  3. `src/beadhive/work.py`: add `import_cmd = _CLI_CALLBACKS["work.import"]` to the block of
     callback assignments after `_CLI_CALLBACKS`, as `reject_cmd` and `promote_cmd` have, and
     drop the "Not yet in `CLI_HANDLERS`" sentence from the `import_cmd` docstring.
  4. Run `just wire-publish` (operator, release step) to cut v2.16.0 and regenerate the derived
     artifacts, then the catalog-drift tests and `bh work check`.

**As built (I6, `bh-df64b`).**

- **Where.** `application/discovery.py` (`IssueDiscoverer`) behind `IntakeService.discover`
  (`INTAKE_DISABLED` without a bound tracker, as import; `NullIntake` as before).
  `IssueImporter` gains the lock-free `upsert(issue, actor)` and `binding_for(issue)` that import
  itself uses, so discovery runs the import upsert **through the module** and never reaches the
  adapter, `bd github sync` or a writer: the discoverer holds a `TrackerReader` only. The root is
  `intake_services.discover` (the `intake.external.enabled` switch, the hive's tracker, the
  operational `BdReportStore`, the root `FlockIntakeLock`, a `FileCheckpointStore`), the command
  body `work_intake.discover` with `render_discover`, the Typer function `work.discover_cmd`, and
  the timer `intake_discovery.py` wired into `host_daemon.serve` as a lifespan component.
- **Incremental.** `list_issues(IssueQuery(since=<mark>, include_closed=True))` through intake's own
  pager, then `CheckpointStore.put` of the page's own `checkpoint` token (the tracker's mark, never
  the local clock) **after that page and every earlier one are upserted**; an adapter that only
  returns the mark on its last page moves it once. `since` is inclusive, so the boundary issue
  repeats as `unchanged` and a re-run writes nothing. A crash resumes from the last durable page.
  The key is `issues:<tracker>:<project>` (`checkpoint_key`), so the comment sweep keeps its own.
- **Full** (`--full`, and the timer's every `full_reconcile_every`-th run). The same pull **without**
  `since` (open and closed, so a closed or newly closed issue is refreshed whatever the stored mark
  says), then `collect_snapshot(IdQuery(ISSUES))`. Only a **complete** snapshot judges
  `source_missing`: a bound report of this project whose URL is absent gets
  `snapshot.source_missing=true`, one that is present again gets it cleared (counted `updated`); an
  incomplete listing (any port error) changes nothing and `DiscoveryOutcome.reconciled` is false.
  A full pass re-stores the marks it passes, so a crash mid-way only re-pulls (idempotent). No
  report is ever deleted, closed or moved by discovery; another project's reports are skipped.
- **Failure.** A refusal or store failure on any issue (`DUPLICATE_BINDING`, a failed create) stops
  the run where it is: the mark never passes an issue that was not ingested, the error reaches the
  caller (the CLI renders `store_error` with "run discover again"), earlier pages stay ingested.
  A tracker failure is `TRACKER_ERROR`, a disabled `issues.read` is `INTAKE_DISABLED`, a busy
  binding `INTAKE_BUSY`, a non-primary host `NOT_PRIMARY`, all before any write.
- **Config.** `intake.external.discovery.{enabled, interval_seconds, full_reconcile_every}` (section
  15), host-owned, in the generated config schema: `enabled` false; `interval_seconds` has no
  default and is **required** when enabled (validation fails otherwise, and an older host rejects
  the unknown keys, so nothing can switch the timer on by accident); `full_reconcile_every` 24.
  `bh work discover` needs only `intake.external.enabled`; the timer needs both switches.
- **The timer.** `intake_discovery.DiscoveryTimer.poll()` is one scheduling decision against an
  injected monotonic clock: the first poll is due at once, the next one `interval` after the last
  one **started**, no burst after a long gap, every Nth run full, a failing run or hive never stops
  the loop, and a hive with no tracker, a non-primary host or a busy binding is a quiet skip. Its
  `run()` is an asyncio loop around it with an injectable `sleep`, `component()` the
  `HostDaemonLifespanComponent`. `build_loop(cfg)` is `None` unless both switches and a positive
  interval are set, and **refuses daemon startup** when enabled without a `TrackerAdapters`:
  `intake_services.process_adapters` is `None` until production credential wiring (`bh-kzs0b`)
  lands, so the timer cannot run, and is never enabled in any real config, before that. Wiring it
  needed no catalog or wire change (a lifespan component and config keys only).
- **Tests.** `tests/unit/modules/intake/test_intake_discovery.py` (idempotence, resume, page-wise
  durability under an injected crash, `--full`, `source_missing` and its incomplete-listing guard,
  zero tracker writes, the lock), `tests/test_intake_discover_composition.py` (the byte-fingerprinted
  public store untouched after every case, off by default, the file checkpoint, rendering),
  `tests/test_intake_checkpoints.py` (the `CheckpointStore` contract on both implementations),
  `tests/test_intake_discovery_timer.py` (the schedule on a **mocked clock**, off by default, the
  daemon component) and `tests/test_config_intake.py`.
- **Not done here.** The verb is **intentionally not registered** in `CLI_HANDLERS` or the operation
  catalog: registering it needs `just wire-publish`, an operator-reserved release step.
  `work.discover_cmd` and `work_intake.discover` are implemented and tested but unreachable from the
  CLI until then. **Registration recipe**, in order:
  1. `src/beadhive/kernel/operations/__init__.py`: add the `work discover|hive:string:o,full:boolean:o,as_json:boolean:o,as_:string:o`
     row to `_CLI_ROWS` (match the encoding, option names and canonical-name map of the sibling
     `work accept` / `work promote` rows).
  2. `src/beadhive/work.py`: add `"work.discover": discover_cmd` to `CLI_HANDLERS`.
  3. `src/beadhive/work.py`: add `discover_cmd = _CLI_CALLBACKS["work.discover"]` to the callback
     assignments after `_CLI_CALLBACKS`, and drop the "Not yet in `CLI_HANDLERS`" sentence from the
     `discover_cmd` docstring.
  4. Run `just wire-publish` (operator, release step) to cut the next wire release and regenerate
     the derived artifacts, then the catalog-drift tests and `bh work check`.
  The timer needs none of this: it is **wired** in `host_daemon.serve` and only waits on `bh-kzs0b`.

### 6. Link, and a bound report is never work (D1)

- **Edge:** `tracks`, report to work (`bd dep add <report> <work> -t tracks`): directional,
  non-blocking in bd (`AffectsReadyWork` false), unused by bh so the type alone identifies a
  report binding, rendered `TRACKED BY` on the work, and it survives the report's closure, so a
  duplicate closed early still receives milestones. `blocks`, `parent-child`, `duplicates`,
  `relates-to`, `caused-by` and `discovered-from` are rejected for the reasons in `bh-mj7cl`
  section 5.
- **Verbs:** `bh work link <report> --to <work> [--replace]`, `bh work link <report> --remove`,
  and `bh work accept <report> --work <id>` or `--new-work [--type T --priority P]`
  (`DispositionRequest` gains `work` and `new_work`). Linking sets `delivery:pending`.
- **A bound report is never work (D-a, `bh-6wj9y` wins).** One-to-one is the report tracking a
  work bead that triage creates with `accept --new-work`: born internal, no `external_ref`, no
  `origin:github`, description written by triage and not copied from the issue body. So the
  `bh-mj7cl` link rule "an accepted report may act as its own one-to-one work and may be a
  target" is **replaced**: a link target that is itself a bound report is refused
  (`TARGET_IS_REPORT`), which also subsumes `CHAINED_REPORT`. The other `bh-mj7cl` link rules
  stand (`NOT_A_REPORT`, `PUBLIC_STORE_ID`, `CROSS_STORE_LINK`, `SELF_LINK`,
  `TARGET_UNTRIAGED`, `already_linked`, `ALREADY_LINKED_ELSEWHERE` unless `--replace`; several
  reports to one work is the point). The `external_ref` is never copied onto the work.
- **Born-native reports are unchanged.** `bh report` / `bh escalate` beads carry no binding and
  no hold; `accept` still leaves them as ordinary backlog. Invariant H below applies to bound
  reports only.
- **Adopt (D-c, `bh-6wj9y` wins).** For a bound report, the planner's adopt path links the report
  as `tracks` to the filed epic and does **not** copy `external_ref` onto it. A second bead
  carrying the URL would make `find_by_external_ref` return two rows and R8 would refuse every
  later import and discovery of that issue; a held report as a `parent-child` molecule step would
  also be offered to `work_next`.

**As built (I5, `bh-v2rr6`).**

- **Where.** `domain/link.py` holds the pure rules (`check_link`, `delivery_at_link`,
  `work_progress_of`); `application/linker.py` is `LinkService`, behind the `IntakePort` operations
  `link`, `unlink` and the new `work_progress`, and behind `dispose(accept)` when the request
  carries `work` or `new_work`. `ReportStore` gains `create_work` (a plain `bd create`: the hive
  triplet labels, type, description and priority, and no `external_ref`, `origin`, `intake` state
  or hold), run on `MemoryReportStore`, on `BdReportStore` over the fake bd and over a real bd store
  as the `store.create-work-is-plain-work` contract case. `intake_services.link_report` and
  `accept_to_work` compose them over the operational store and the root `FlockIntakeLock`, behind
  `intake.external.enabled` (off: `NullIntake`, every write refuses `intake_disabled`, no store is
  built). A link composes no tracker. The command bodies are `work_intake.link` and
  `work_intake.accept(..., work=, new_work=)`; `work.link_cmd` is the Typer function.
- **Rules, in order, each a refusal before any write.** A public-store id on either side
  (`PUBLIC_STORE_ID`), `SELF_LINK`, an unknown report (`NOT_FOUND`), a report with no binding
  (`NOT_A_REPORT`), a closed report (`REPORT_CLOSED`, new), a work id outside the operational prefix
  or not found there (`CROSS_STORE_LINK`; a foreign id is never looked up), a target that is itself
  bound (`TARGET_IS_REPORT`, D-a, which also covers chains), an untriaged target
  (`TARGET_UNTRIAGED`), then `already_linked` (no edge write), `ALREADY_LINKED_ELSEWHERE` unless
  `--replace` (untrack the old edge, then track the new, `relinked`). An accepted born-native report
  is ordinary backlog and may be a target. `--work` with `--new-work` is `INVALID_REQUEST` (new).
- **`delivery:pending` at link.** A new edge writes `pending` when the report has no value, keeps
  `pending`, and sets `pending` again when the value was a milestone of other work (`merged`,
  `released`, `reverted`); an existing edge only repairs a missing value. The exception is a re-link
  to the very work the report was unlinked from, whose milestone value still describes it.
- **Where the `unlinked` fact lives (decided here).** In the report's `intake.outbox` document,
  `facts.unlinked = {"work": <id>}` (`UnlinkedFact` beside `MergedFact` and `ReleasedFact` in
  `DeliveryFacts`), as `bh-6wj9y` R2 proposed. It is encoded only when set, so a document without
  one is byte-identical to before; the document write is the usual whole-document rewrite with the
  `seq` re-check, under the `IntakeLock`, and a link that follows clears it. Unlink writes the fact
  first, then removes the edge, so a crash between them leaves a fact and an edge that the next
  unlink or link tidies, never a delivery value no fact explains. The value stays (`pending` stays
  `pending`); `work_progress` reports `unlinked` because the edge is gone.
- **Accept with work.** `accept <report> --work <id>` links (idempotently) then clears `intake`;
  repeating it finishes an accept that was interrupted after the link. `--new-work` first checks,
  with nothing written, that the report is bound, open and tracks nothing, creates the work bead
  (title: the report's title; description: triage's own sentence naming the report, never the issue
  body; type: `--type` or the report's; priority: `--priority`), links it, and closes that bead if
  the link then fails. With `--new-work` the `--type` and `--priority` go to the new bead; with
  `--work` they go to the report, as a plain accept's do. A born-native report is refused
  `NOT_A_REPORT` (a plain accept already leaves it as backlog).
- **Work progress** is `IntakePort.work_progress(report)`, derived on read from the tracked bead:
  `closed` if its status is closed, `in_review` if it carries `review:pending`, `in_progress` for
  `in_progress` or `hooked`, else `open`; `unlinked` when the report tracks nothing. Nothing is
  stored. It has no verb yet (I10's templates read it).
- **Adopt (D-c).** `adopt.frame_from_beads` treats a report that carries the `intake.source`
  document as bound: it goes under the frame's `tracks` (not `adopts`), contributes no
  `source_system` or `external_ref` to the epic, and seeds the epic text with its id and title only
  (the issue body is untrusted public text and is read on the report). `plan_filing.file` passes
  `tracked_reports` to `beadhive_core.compile_molecule`, which lowers each to `bd dep add <report>
  <epic> -t tracks` (report to epic, non-blocking, the report stays open and held) instead of
  `parent-child`. Born-native adopted reports are unchanged. `plan show` does not list tracked
  reports yet.
- **Tests.** `tests/unit/modules/intake/test_intake_link.py` (rules, service, accept, progress,
  delivery table, the fact's codec), `tests/test_intake_link_composition.py` (the root: off by
  default, the public-store fixture byte-identical and `SCOPE_PUBLIC` never resolved after every
  case, no tracker write, rendering, the command bodies), `tests/test_intake_link_int.py` (a real
  bd store: two reports to one work, unlink one, nothing held becomes ready, the work is ready
  throughout, `accept --new-work`, a `tracks` edge to an epic), plus the adopt, plan and core
  planning cases.
- **Not done here.** `bh work link` and the `--work` / `--new-work` options of `accept` are
  **intentionally not registered**: each changes the published operation catalog and so needs
  `just wire-publish`, a release step reserved for the operator (as for `import`, I4).
  `work_intake.link` and `work.link_cmd` are implemented and tested but unreachable from the CLI;
  `work_intake.accept` already takes `work` and `new_work` but `accept_cmd` does not pass them.
  **Registration recipe**, in order:
  1. `src/beadhive/kernel/operations/__init__.py`: add the row
     `work link|bead:string:r,to:string:o,remove:boolean:o,replace:boolean:o,as_:string:o,hive:string:o,as_json:boolean:o`
     to `_CLI_ROWS` (keep the rows sorted), and extend the `work accept` row to
     `work accept|bead:string:r,type_:string:o,priority:string:o,as_:string:o,hive:string:o,work:string:o,new_work:boolean:o`.
  2. `src/beadhive/work.py`: add `"work.link": link_cmd` to `CLI_HANDLERS` and
     `link_cmd = _CLI_CALLBACKS["work.link"]` to the callback assignments; drop the "Not yet in
     `CLI_HANDLERS`" sentence from the `link_cmd` docstring.
  3. `src/beadhive/work.py`: give `accept_cmd` the two options
     (`work: str = typer.Option("", "--work", help="track this existing work bead")` and
     `new_work: bool = typer.Option(False, "--new-work", help="create a work bead and track it")`)
     and pass them on: `return work_intake.accept(bead, issue_type, priority, as_, hive, work,
     new_work)`; update its docstring.
  4. Run `just wire-publish` (operator, release step), then the catalog-drift tests and `bh work
     check`.

### 7. Report dimensions, the hold gate and field ownership

A report has four dimensions, each with one writer (`bh-6wj9y` R2):

| Dimension | Stored as | Values | Sole writer |
|---|---|---|---|
| Triage | `intake` (existing) | `untriaged`, `accepted`, `rejected`, `rerouted`, `promoted` (plus `bh-elkv`'s later values) | `IntakeService.dispose`; import stamps `untriaged` once |
| Work progress | **derived on read**, never stored | `unlinked`, `open`, `in_progress`, `in_review`, `closed` | nobody: `IntakeService.work_progress(report)` from the tracked bead |
| Delivery | `delivery` (**new** closed dimension) | `pending`, `merged`, `released`, `reverted` | the intake module only: link, the merge trigger, the release trigger, the operator correction verb |
| Notified | `notified` (**new** closed dimension) | `replied`, `merged`, `released`, `corrected` | the outbox sender only, after a comment operation reaches `sent` |

- **Registration.** Both live in `INTAKE_DIMENSIONS` and reach `state.STATE_DIMENSIONS` through
  the facade (or, if the facade has not landed, directly in the `state.py` literal with the same
  names). Both names are free in code and in the live label vocabulary; `outbound`/`publish`
  (the contributor's, single-valued per bead) and `release` (code-owned) are not reused. Each is
  written by `bd set-state` through `ReportStore.set_state`, a few times per report, so the
  permanent event bead per transition is acceptable. Per-operation outbox states are **not**
  dimensions; they live in `intake.outbox`.
- **Gate marker.** `INTAKE_HOLD_GATE_MARKER = "intake-hold:"` in `guard.py` beside
  `RELEASE_HOLD_GATE_MARKER`, with `guard.is_intake_hold_gate`; `work_logic._gate_kind` returns
  `"intake-hold"`; the seat policy in `beadhive_core.review` lets **no** seat resolve it through
  `bh work approve`; only `ReportStore.close` resolves it. `bh work claim` refuses a bead with an
  open `intake-hold:` gate as defence in depth.
- **Field ownership** is `bh-6wj9y` R2's two tables, adopted whole: on the internal side every
  field (title, description, type, priority, `external_ref`, `origin`, `intake`, `delivery`,
  `notified`, the three documents, the hold gate, the `tracks` edge, the report's status) has
  exactly one writer, the work bead is written only by the work layer and only read by intake,
  and `bh work merge` never closes a report. On the tracker side Beadhive may only append its
  own marked comments and change labels under `write.labels.owned_prefixes`; every other field
  has no port operation. **Owned-label conflicts: humans win until the next milestone** (a label
  diff carries `base_revision` and drops back to `drafted` when an owned label moved, O7).
- `bh doctor` reports a `delivery` value with no matching `intake.outbox` facts, an open bound
  report without an open hold (and re-opens one), and a hold whose report is closed (and resolves
  it).

### 8. Merged but unreleased, the close rule and the operator views

**Invariant H.** A bound report (`origin:github` with an `external_ref` binding) is never in
`bd ready`; completed work is closed. Together they keep completed work out of dispatch while
the report stays open as long as delivery needs it (`bh-6wj9y` R3, probed against bd 1.3.0 in
E4):

1. The hold gate (`bd gate create --blocks <report> --reason "intake-hold: ..."`) is native
   readiness; a `tracks` edge alone is not, and a label alone is insufficient.
2. Work closes at merge as it does today and is never reopened for delivery tracking.
3. The merge trigger sets `delivery:merged` on every report that tracks the work, open or closed.
   Under `close-on-release` the report stays open and held until the release is verified.
4. **The close rule.** `ReportStore.close(report, reason)` is the only way a report closes
   (reject, reroute, `bh-elkv`'s dedupe, delivery completion). It refuses unless every open
   blocker is one of its own `intake-hold:` gates, runs `bd close --force` through
   `issue_close_held` (overriding only the live-blocker refusal), then resolves the hold as
   bookkeeping. Closing first leaves no instant at which the report is open and unblocked; a
   crash between the steps leaves a closed report with an open hold, which is harmless and swept
   by `bh doctor`.
5. **Completion policy** (per hive): `close-on-merge` (default) closes the report once
   `delivery:merged` holds and the `merged` record's comment is `sent`, `abandoned` or
   `superseded` (at merge if the outbox is off); `close-on-release` waits for
   `delivery:released` and the `released` record likewise. `notified` is not advanced for
   `abandoned` or `superseded`, so `notification_behind` stays visible.

**As built (I3, `bh-eo8br`).**

- **Held create.** `ReportStore.create` opens the hold (`bd gate create --blocks <report> --type
  human --reason "intake-hold: external report, never work"`) straight after the `bd create`, for a
  draft with a binding only; a born-native report (`bh report`, `bh escalate`) never gets one. bd
  has no transaction across the two calls, so a failure on the second leaves the report open and
  unheld, raises `IntakeStoreError(operation="open_hold")` carrying the new id, and is exactly the
  "open bound report without a hold" case the sweep finds.
- **Held close.** `BdReportStore.close` runs the ordinary `bd close` first, so a bead with no hold
  closes (and fails) byte for byte as before. Only a refusal reads the bead's blockers
  (`blockers_of`, from `bd show`): when every live one is its own `intake-hold:` gate it runs
  `issue_close_held` (`bd close --force`) and then `gate resolve` on each hold; any other live
  blocker is `IntakeRefused(LIVE_BLOCKER)` with nothing written. A second `close` on an already
  closed report is a bd no-op (bd 1.3.0 closes a closed bead again with exit 0 and leaves its
  gates alone), so a hold left by a crash between the two steps is resolved by the sweep, not by
  a retry; it is harmless meanwhile because a closed bead is never ready.
- **Claim refusal.** The dispatch gate of `claim` and `assign` (`ShellWorkspace.dispatch_gate`)
  and the member check of `claim --group` refuse a bead with an open `intake-hold:` gate
  (`work_logic._guard_not_held_report`), naming the gate and pointing to triage. It is one
  `bd gate list` read and silent for every other bead.
- **Doctor.** `bh doctor` gains an `intake_holds` section (payload key `intake_holds`, a list of
  findings, empty and unrendered when nothing is wrong). A finding is `missing` (an open bound
  report without an open hold), `unresolved` (an open hold on a closed report) or `orphaned` (a
  hold that blocks no bound report); bound reports are the beads carrying `intake.source`. Only
  hives whose public store binds a tracker adapter are read (the only hives that can import a
  report), so a fleet that binds none pays nothing. Each finding carries the exact `bh -r <hive>
  bd ...` command that fixes it. The repair itself is `IntakeService.sweep_holds(repair=True)`
  (open a missing hold, resolve a stale one, report an orphan), which the import verb's "repair a
  missing piece" step (I4) calls. A `bh doctor` flag and a `bh hive repair` mode were not added,
  for the reason the `report` lane was not: each widens the published operation catalog and so
  needs a wire release.
- **Tests.** The `store.bound-create-opens-a-hold`, `store.close-is-the-held-close` and
  `store.holds-open-and-resolve-one-at-a-time` contract cases run on `MemoryReportStore`, on
  `BdReportStore` over the fake bd and over a real bd store. `tests/test_intake_hold_int.py`
  replays E4 against the bd binary (hold, refused plain close, forced close with a ready probe
  after every mutation showing no window, metadata on a closed bead, `--has-metadata-key`) and
  breaks each invariant by hand to drive the sweep.

**Operator-view blocker list (D-f, `bh-6wj9y` wins).** `operator_contract.py` and
`operator_work_items.py` both define `_NON_BLOCKING_DEPENDENCIES = {"parent-child", "related",
"discovered-from"}` and count every other edge type as a blocker (verified for this ADR). Replace
the allow-list in both with `_BLOCKING_DEPENDENCIES = {"blocks", "conditional-blocks",
"waits-for"}` (bd's `AffectsReadyWork` set without `parent-child`, which bh already treats as
non-blocking). Adding only `"tracks"` to the allow-list (the `bh-mj7cl` proposal) is rejected: it
leaves every future edge type miscounted, and the views already disagree with bd in both
directions. In `_readiness`, a bead whose only open gates are `intake-hold:` gates gets its own
`report` lane instead of `blocked`; until that lands, held reports read "blocked: 1 open gate",
which is accurate but noisy.

### 9. The conversation model

Adopted as `bh-fptob` R1 to R8, stored in `intake.thread`:

- **Identity.** One thread per report bead (the issue URL, scoped by the binding); a comment
  record keyed on `CommentKey.id` (`global_id` kept, never compared); the author keyed on
  `ActorKey(id, kind)`, display never compared; a local integer `rev` per content state plus the
  tracker `Revision` and a body digest; a local monotone `seq` per thread. No GitHub vocabulary
  enters the model.
- **Revisions** (R5): stale and boundary repeats are no-ops (no write at all when nothing
  changed); a newer timestamp with the same digest updates in place; a different digest at
  `r >= r0` appends a `rev` (coarse-clock ties: the later read wins). The checkpoint advances
  only in the same write as its page's records; a page with `checkpoint=None` never advances it;
  a checkpoint is passed back only to a query of the same scope.
- **Deletions** (R6): only complete snapshots count; a record first seen after the snapshot
  began is never a candidate; two agreeing complete snapshots (default) tombstone it; the body
  is purged by default (not erasure, section 4); a reappearing id resurrects. A thread whose URL
  is missing from two complete issue snapshots is marked `missing` and surfaced to triage; the
  report is never closed or re-triaged automatically.
- **Own-message recognition** (R7) is the consumer's, per the port ADR's inconsistency 3, in the
  layered order: receipt id, then self marker with a pending or issued outbox record and a known
  actor (the `WriteOutcomeUnknown` recovery), then a configured integration, then self marker
  with a known actor, then `peer`, else `external`. A marker alone is never sufficient; a known
  actor without a marker is not own. The outbox view is `receipt_ids()`, `pending(outbox_id)`
  (now covering `sending` and `unknown`), the new `issued(outbox_id)` (every operation that ever
  had an attempt, including `abandoned` and `failed`: D-d, `bh-6wj9y` wins, additive) and
  `known_actors()`.
- **Triage watermark** `considered_rev` derives "new evidence"; it never changes the `intake`
  dimension (wake-on-reply waits for `bh-elkv`, D3).

**How triage marks `considered_rev` (decided in I7, `bh-rkmq1`; was open).** Through an explicit
module operation, `IntakePort.mark_considered(ConsiderRequest(report, actor, through_seq))`, and
**never** as a side effect of a disposition. Triage reads `IntakePort.thread_of(report)` (the
document plus the derived `new_evidence` and `flagged` reads) and passes back that view's `seq`;
each record's `considered_rev` becomes its highest `rev` seen at or before that seq, and the
thread's `considered_seq` covers tombstones up to it, so a comment that arrived after the read stays
new evidence. Marking is idempotent, takes the `IntakeLock`, writes only `intake.thread` (one
write with the `seq` re-check) and never the `intake` dimension. Why not a disposition side
effect: a disposition can be made without reading the thread (a spam reject from the title alone)
and would then hide unread evidence; an accepted report keeps receiving comments with no further
disposition to mark them; and a disposition writes the `intake` dimension through `set_state`,
which cannot commit atomically with an `intake.thread` write. The CLI spelling (a `bh work intake`
sub-verb or a flag on the thread read, never `promote`) is the CLI bead's to ratify; a disposition
verb may later offer an explicit opt-in flag that calls the same operation.

**As built in I7 (`bh-rkmq1`).** The pure rules are `domain/thread.py` over intake-owned records
(`Observation`, `CommentRecord`, `Thread`); the schema-versioned codec is
`domain/thread_document.py` (re-exported from `contracts`, so every `ReportStore` shares one
format; a malformed or newer-schema document is a store error and is never read as empty, so it is
never overwritten). `application/threads.py` (`ThreadService`) is the only tracker-typed code of
the model. `IntakePort` gains `ingest_thread`, `reconcile_thread`, `reconcile_presence`,
`thread_of` and `mark_considered`; `IntakeService` delegates them to an optional `ThreadService`
and refuses with `INTAKE_DISABLED` without one (comment ingestion off, the default), as does
`NullIntake`. `ReportStore` gains `read_thread` and `write_thread(bead, thread, expect_seq=,
actor=)`: the store re-reads the stored `seq` and refuses with `THREAD_MOVED`, writing nothing,
unless it still equals `expect_seq`; `BdReportStore` then rewrites the whole key in one `bd update
--metadata @/dev/stdin`. `IntakeLock.hold(binding_key)` and the read-only `OutboxView`
(`receipt_ids`, `pending`, `issued`, `known_actors`) are new contracts; the kit ships a
never-waiting `InProcessIntakeLock` (refuses `INTAKE_BUSY`) and `MemoryOutboxView`. The root's
lock implementation (guard, flock, epoch) stays with I9. Every operation refuses before any tracker
read on a published operational store without acknowledgement (`PUBLISHED_STORE`; the posture is
injected, and the root computes it when it composes ingestion), on a born-native report
(`NOT_BOUND`), on a report bound to another project (`OTHER_PROJECT`) and on a stored token of
another scope (`CHECKPOINT_SCOPE`). A reconcile pass collects its snapshot outside the lock and
applies it under the lock with the `seq` it read before the snapshot began, so a record ingested
meanwhile is never a candidate. Recognition uses the evidence of a record's first sighting and
never demotes an own record, so editing a marker into or out of a comment never changes its role.
The project-wide sweep and its `CheckpointStore` home are not built here (discovery, I6).

**Layering (an unrecorded divergence, resolved here).** `bh-fptob` R10 and `bh-6wj9y` R16 each
describe a "domain bead" that "depends only on the tracker contracts", and `bh-6wj9y`'s domain
records embed tracker types (`Receipt.comment` as `CommentKey`). The checker forbids
`modules.intake.domain -> modules.tracker.contracts` (`bh-mj7cl` E8), and `bh-mj7cl` makes
`application/mapping.py` "the only tracker-typed code". **Decision (`bh-mj7cl` wins):** the domain
holds intake-owned value records only (its own comment, actor and issue references mirroring the
port's fields), and the pure rules (`apply`, `reconcile`, the outbox state machine, the leak
check) are domain functions over those records. Translation to and from `TrackerComment`,
`IssueKey`, `WriteReceipt` and `WriteGrant` happens in `application/`. Test cases are unchanged:
they drive the application layer with `MemoryTracker`, and the pure domain functions directly.

### 10. The outbox state machine

Adopted as `bh-6wj9y` R4 to R7, stored in `intake.outbox`. Records are keyed by a stable
`DraftKey(binding_url, event, source_revision)`; drafting an existing key is a no-op, so a re-run
hook never drafts twice. A record renders only its `PublicFacts` and `message.text`; a template
has no read path to the report, the work, notes or the thread.

The machine is **per operation** (each write is its own gate). States: `drafted`, `held`,
`approved`, `sending`, `unknown`, `failed` (non-terminal) and `sent`, `superseded`, `abandoned`
(terminal). Every transition is one durable document write under `IntakeLock` with a `seq`
re-check, appending `(seq, from, to, event, actor)` to the operation's history. The transitions,
in summary (full table with guards and triggers: `bh-6wj9y` R5, **O1 to O19**):

| Path | Transitions |
|---|---|
| Draft and check | O1 draft (trigger or operator reply), O2 existing key is a no-op, O3 a leak or sanitizer finding or a `missing` thread holds it, O4 edit or override returns it to `drafted` |
| Approve (D2) | O5 operator seat only, digest of a fresh dry-run render, no unresolved finding, the operation usable, **`comments.read` usable** (so an unknown outcome can be reconciled): mints one `WriteGrant`; O7 a changed render or moved label base invalidates back to `drafted` |
| Retire before sending | O6 superseded by a newer record in the same group (never once `sending` or later), O8 operator discard; each voids the grant |
| Send | O9 write-ahead `Attempt` then one request, only from `approved`, never from a timer; O10 receipt to `sent`; O11 a refusal that proves nothing was written to `failed` (grant consumed); O12 an ambiguous result to `unknown`; O13 a dead holder's `sending` to `unknown` |
| Reconcile | O14 own marker observed to `sent` (reconciled); O15 a complete snapshot begun after the attempt ended without the marker counts one absence; O16 k absences (default 2) to `failed(absent)`; O17 operator retry from `failed` back to `drafted` (new render, digest and grant); O18 operator may abandon an `unknown` explicitly; O19 a late marker moves `abandoned`/`failed(absent)` to `sent` and flags `duplicate_observed` |

Absent on purpose: `unknown -> approved|drafted`, `sending|unknown -> superseded`, any automatic
`-> approved`, any transition out of `sent`. A report-level `notified=<event>` write and the close
rule (section 8) are evaluated in the same write as the operation transition.

**No duplicate posts.** Adopted as `bh-6wj9y` R7: invariants I1 (at most one attempt in flight),
I2 (every attempt has a fresh grant) and I3 (`failed` after an attempt implies evidence nothing
was written) give, under assumptions A1 (read-after-write visibility on complete listings) and
A2 (the adapter classifies "nothing written" correctly), that Beadhive never causes two comments
carrying one `outbox_id` marker to exist. When A1 fails, O19 flags the late duplicate for the
operator; with no delete operation Beadhive never "fixes" it. This is the honest form of "do not
promise exactly-once remote writes". The marker carries `hive=<owner/repo>`, an install UUID and
an opaque `outbox_id` (`ob1_` plus 26 base32 characters, never derived from a bead id), so it can
never match a `<prefix>-<id>` bead shape.

**As built in I8 (`bh-g62yn`; pure domain, nothing sends).** The records and the machine are
`domain/outbox.py`, the leak check `domain/leaks.py`, the `delivery`/`notified` rules and the close
rule's inputs `domain/delivery.py`, and the schema-versioned `intake.outbox` codec
`domain/outbox_document.py` (re-exported from `contracts`; a malformed or newer document is an
error, never an empty outbox). `application/outbox.py` only translates (`WriteGrant` from an
approval, `CapabilityReport` to the usable set, `WriteReceipt` to a receipt, a listing to marked
comments, and which port errors prove nothing was written); the sender, `read_outbox` /
`write_outbox`, the `IntakePort` operations, the verbs and the config keys stay with I9 and I10, and
`OutboxPolicy()` is off. The contracts gain `Sanitizer` and `IdSource`;
`beadhive.adapters.intake.ScrubSanitizer` implements `Sanitizer` over the shared `scrub()` and
`TOKEN_PATTERN` (binding it is the root's, with I9); the kit adds `SequentialIdSource` and
`RecordingSanitizer`. Interpretations the table left open:

- One applied rule is one document write: `seq` is bumped once and every history step of that
  call carries it (O1 with its O3 and O6, O15 with O16).
- `Attempt.end_seq` is the last `seq` at which the request could still have been in flight (the
  `seq` before the O10 to O13 write), so O15's guard is literally `start_seq > end_seq`; a read
  counts as an absence once (its `start_seq` must exceed the last counted one). An unsatisfied
  complete label read counts the same way for a label operation.
- The operator types the full digest or at least its 12-hex prefix. A label render keeps the
  drafted `base_revision` while the owned labels are unchanged (another revision bump does not
  void an approval); once a human changed an owned label the digest moves, O9 refuses
  `STALE_DIGEST`, and O7 rebases the diff onto the current labels.
- "No sanitizer bound" is a `no_sanitizer` finding, never overridable like `secret` and
  `label_not_owned`. A publish (O4) names a finding id and is matched by kind and text, so an edit
  that changes the text needs a new one. A secret's finding is stored redacted.
- An operator seat is any named actor outside `OutboxPolicy.agent_seat_prefixes` (`dev/`, `disp/`
  and their legacy spellings); O4, O5, O8, O9, O17 and O18 require one.
- A second marked comment on a `sent` operation sets `duplicates` (`duplicate_observed`) without a
  transition. A record whose operations all retired unsent is `retired`. A draft carrying a
  public commit URL while `commit_links` is off is refused, not silently stripped. With the outbox
  on and no completion record yet, the close rule waits.

R14 coverage: `tests/unit/modules/intake/test_intake_outbox_*.py` run X2, X4 to X13 and X16 on the
pure rules (X8 to X10 also on `MemoryTracker`, X9 with K1), and X1, X3 and X18 at the domain level
(the triggers are I10's). X14 and X15 are the hold bead's (I3) and X17's store half is I9's.

### 11. Writes and gates

There are exactly two tracker writes, and nothing else can be written because the port has no
other operation:

| Write | Port call | Gate | Grant | Also required |
|---|---|---|---|---|
| post a comment | `CommentWriter.post_comment(issue, body, marker=, grant=)` | O5 for that operation: a human operator seat, the digest of the dry-run render | `WriteGrant(grant_id, comment.create, issue)`, consumed by one O9 | plugin's effective `comment.create`; `comments.read` usable; a clean leak check on the exact bytes |
| change owned labels | `LabelWriter.change_labels(issue, add=, remove=, ownership=, grant=)` | O5 for that operation, confirmed separately | `WriteGrant(grant_id, labels.change, issue)`, used once | plugin's effective `labels.change` (non-empty `owned_prefixes`); every label owned |

- Approving a record's comment does not approve its label change: `bh work outbox approve <id>
  --op comment:<digest> [--op labels:<digest>]` resolves each `--op` as its own gate and mints its
  own grant.
- **Single use** is enforced twice: across processes by the state machine (the grant is
  recorded, consumed by O9, voided by O6, O7, O8; re-entering `approved` mints a new id) and
  in-process by the adapter's reused-grant refusal (plugin ADR's write gate).
- **The gate is a recorded approval (D-g, `bh-6wj9y` wins).** It is a durable, attributed
  `Approval{actor, digest, grant_id, overrides, seq}` written in the same document write as O5,
  not a bd gate bead per write. This is what the plugin ADR asks for ("each write is its own
  consumer-side operator-approved gate minting one single-use `WriteGrant`"; "durable single use
  is the consumer's outbox"); a bead per approval would add a permanent bead per message
  operation and could not commit atomically with the transition. Only a human operator seat can
  produce it: agent seats (`dev/`, `disp/`), timers, config, environment variables and flags
  cannot.
- No public-store write exists. The next projection pull imports Beadhive's label changes as
  GitHub-authoritative data; that is the pull, not an outbox write.

**As built in I9a (`bh-czes8`; `comment.create` only, every case offline).** The sender is
`application/sender.py` (`OutboxService`), behind the new `IntakePort` operations `draft_reply`,
`outbox`, `render`, `approve`, `send`, `reconcile`, `discard` and `retry`; `IntakeService` delegates
them to an optional `OutboxService` and refuses with `OUTBOX_DISABLED` without one, `NullIntake`
with `INTAKE_DISABLED`. `ReportStore` gains `read_outbox`, `write_outbox(..., expect_seq=)` (refused
with `OUTBOX_MOVED`, writing nothing, when the stored `seq` moved; a bound report only) and
`reports_with_outbox()`, with five contract cases run on `MemoryReportStore`, on `BdReportStore` over
the fake bd and over a real bd store (the dotted `intake.outbox` key merges and lists like the other
two). `StoreOutboxView` is the store-backed `OutboxView`, `issued()` included, that thread
recognition reads. The root (`intake_services`) implements `IntakeLock` as `FlockIntakeLock`
(`guard.guard_primary`, refused as `NOT_PRIMARY`; an exclusive never-waiting `fcntl.flock` on
`.bh/intake/locks/<sha256(binding)>.lock`, held elsewhere as `INTAKE_BUSY`; `guard.live_epoch` as
the epoch) and composes `outbox_service_for` with a `RandomIdSource`, the fleet `leak_policy`, the
`store_posture` and the shared `ScrubSanitizer`. Interpretations:

- Off twice over: no sender is bound unless the caller passes an enabled `OutboxPolicy` (the
  default is off), and an enabled one still posts nothing without the adapter's own enable
  (`probe_capabilities` must report `comment.create` and `comments.read` usable for O5; the
  adapter is a `CommentWriter` only with its write flag on; a disabled writer is O11 before any
  request) and an operator approval minting a single-use grant.
- Every step is one durable `intake.outbox` write under the lock, and every lock acquisition first
  recovers each `sending` operation to `unknown` (O13 with `holder_gone`): the holder now has the
  exclusive lock its writer held, so that writer is gone. A process killed between O9 and the
  request is therefore recovered by the next operation of any kind.
- `approve` with `--publish` applies O4 and O5 in one write, so a refused approval publishes
  nothing; its result carries the dry run and the minted grant id. A send whose fresh render no
  longer has the approved digest takes O7 and posts nothing.
- `reconcile` reads one complete comment listing per call, outside the lock, with `start_seq` the
  document `seq` read (after O13) before the listing began, and applies it under the lock. Two
  calls without the marker after the attempt ended are needed for `failed(absent)` (k = 2); a
  listing that fails part-way changes nothing.
- `notified=<event>` is written by `set_state` straight after the transition's document write (bd
  has no transaction across the two); a failure is reported in the result and never un-sends. The
  close rule is not evaluated here: only milestone records reach it (I10).
- The marker line is rendered by an injected `marker_line` codec (the adapter's own), so the dry
  run shows the posted bytes. The tracker adapter itself is injected: building it from the store
  binding and credentials is `bh-kzs0b`'s, not this bead's.

Not built here, pending an operator decision: the `bh work outbox` verbs (`show`, `approve`,
`send`, `reconcile`, `discard`, `retry`, `draft`) and the `intake.external.outbox.*` config keys.
Each verb widens the published operation catalog, which cuts a new wire release (`just
wire-publish`), so they wait for that release to be authorised; the port operations above are the
whole behaviour they would call. **Acceptance item 4 is pending:** no real tracker write has been
made. The first real `comment.create` is an operator-approved step recorded on the bead, and the
re-verification of assumption A1 (read-after-write visibility on complete listings) and of the
`tracker-github` refusal diagnostics against recorded real responses happens on that first gated
write; it has not been performed.

**As built in I9b (`bh-88vji`; `labels.change`, every case offline).** The same `OutboxService`
sends and reconciles a label operation with the same receipts; nothing new is registered (no verb,
no catalog entry, no config key). Interpretations:

- A separate gate: `OutboxPolicy.labels_enabled` (default `False`) beside `enabled`. Without it
  `approve`, `send` and the pure `begin_send` refuse a label operation with `LABELS_DISABLED`
  (before any tracker request, the capability probe included) while the comment's gate is
  untouched; with it, the adapter's own `labels.change` enable, `issues.read` and `comments.read`
  must all be usable (O5), and the operator approves each operation separately: a label approval
  mints its own `grant_id` and its own digest, and approving the comment never approves the labels.
  A change the issue already has (empty effective add and remove) cannot be approved.
- Only owned labels: the owned prefixes are the root's `LeakPolicy.owned_label_prefixes` (the
  adapter's `write.labels.owned_prefixes`, passed to `outbox_service_for`; none means no label can
  be written). A foreign label is refused at draft (`LABEL_NOT_OWNED`), held by the never
  overridable `label_not_owned` finding, refused again at approve and at O9 BEFORE the write-ahead
  from the stored diff itself (an edited store, a narrowed config), and the writer is handed exactly
  those prefixes as its `LabelOwnership`, which it checks again before any request.
- Reconciliation: labels carry no marker, so `reconcile` reads the issue's labels back (one
  `IssueResolver.get`, else a paged `list_issues` matched on the issue), outside the lock and after
  the attempt ended, and the unchanged machine applies it: labels that satisfy the diff are O14 (O19
  late), a complete read that does not is one absence (O15), k absences `failed(absent)` (O16); a
  read that fails or never finds the issue infers nothing. Only the reads an operation needs are
  made (a label-only report makes no comment listing). An ambiguous label write therefore never
  changes labels twice: a retry needs `failed(absent)`, an operator `retry` and a new approval.
- `IntakePort.send` and `retry` gain `kind` (default `comment.create`), `discard` an optional one,
  and `OutboxResult.kind` says which operation a result is about. `MemoryTracker.change_labels`
  now honours `fail_next_write_ambiguously` like `post_comment` (kit extension K1).
- Drafting a label diff stays the milestone trigger's job (I10, `labels.on_delivery`); the tests
  draft through the pure `draft` rule.

**Acceptance item 4 stays pending for `labels.change`:** no real tracker write has been made. The
first real label change is an operator-approved step recorded on the bead, and the acceptance
re-verification against recorded real responses (the label write's diagnostics and the
read-after-write visibility of the labels read-back) happens on that first gated write; it has not
been performed.

### 12. Milestone triggers and the release seam

Adopted as `bh-6wj9y` R9: `on_work_merged(MergeFact(work, commit, actor))` from `work.merge` /
`work.finish` after the close, and `on_release(ReleaseFact(tag, commit, actor))` from a new
operator verb `bh work delivery release <tag>` (bh has no single release-publish function).
`ReleaseVerifier.contains(release_commit, merge_commit) -> Tri` is implemented in the root with
`git merge-base --is-ancestor`; only `YES` moves `delivery` to `released`, `NO` and `UNKNOWN` are
reported as "not verified" and never assumed. Both triggers are `IntakePort` operations, wrapped
so an intake failure is reported and **never fails the merge or the release**, are free under
`NullIntake`, **draft and never send** (`write_requests() == 0` across every trigger case) and
supersede an unsent `merged` draft with a `released` one in the `milestone` group. A molecule
merge runs the trigger for the epic and every closed child a report tracks. Out of scope for v1:
started, blocked and deprioritised milestones; revert detection (only an operator-recorded
`reverted`, which drafts `corrected`).

**As built (I10, `bh-56s4l`; milestones only draft).**

- **Where.** `domain/milestones.py` holds the pure parts (`MergeFact`, `ReleaseFact`,
  `MilestonePolicy`, `MilestoneItem` / `MilestoneOutcome`, the fixed public wording of the two
  messages and `fold_facts`); `application/milestones.py` is `MilestoneService`, behind the new
  `IntakePort` operations `on_work_merged` and `on_release` (a service without it refuses
  `INTAKE_DISABLED`, `NullIntake` returns an empty outcome: free). The `ReleaseVerifier` port is in
  `contracts` (`resolve(tag) -> commit | None`, `contains(release, merge) -> Verdict`);
  `beadhive.adapters.intake.GitReleaseVerifier` implements it with `git rev-parse --verify
  refs/tags/<tag>^{commit}` and `git merge-base --is-ancestor`, read-only (an operator-typed tag
  or sha that is not a plain name is refused before git runs; only exit 0 is `YES`, only exit 1
  `NO`, anything else `UNKNOWN`). The root adds `intake_services.work_merged`, `delivery_release`,
  `milestone_settings` and `MilestoneResult`; the work layer adds `work_intake.on_work_merged`,
  `delivery_release` and `render_delivery_release`.
- **Drafts, never sends.** `MilestoneService` has no tracker, marker codec, identity, grant or
  writer; `write_requests() == 0` across every trigger case. The `merged` record is keyed on the
  merge commit and the `released` record on the tag (`DraftKey`), so a re-run drafts nothing (X2); a
  `released` draft supersedes an unsent `merged` one (O6, X4) and leaves an `unknown` one alone
  (X5). The wording is a constant per event with `{tag}` as its only placeholder, so a message
  carries no bead id, branch or path by construction; a tag shaped like a bead id is held by the
  leak check, never sent. A draft refused (a published store, no sanitizer) never stops the
  `delivery` write: it is reported on the item.
- **The merge trigger.** For each report that tracks the work (open or closed, X1): `delivery`
  moves `pending|reverted` to `merged`; `facts.merged` and the drafted record are ONE
  `intake.outbox` write made BEFORE the `set_state`, so a crash between the two leaves a fact the
  re-run turns into the dimension, never a dimension no fact explains; then the completion policy.
  Under the default `close-on-merge` with the outbox off the report closes at the merge (held
  close, no ready window); under `close-on-release` merged but unreleased keeps the report open,
  held, `delivery:merged`. One report failing (a held lock, a store error) is returned on its item
  and the next is still handled.
- **The release trigger.** Candidates are every bound report at `delivery:merged` (any status, so a
  closed duplicate hears of it) plus open reports already at `released` (a crash between the
  dimension and the close). The verifier is asked once per distinct merge commit. `YES` sets
  `released`, `facts.released` and, when `milestones.draft_on_release` is on, a `released` record
  (a closed report only with `append_release_to_closed`); `NO` and `UNKNOWN`, a report with no
  recorded merge commit, or an unlinked one are reported "not verified" and change nothing (X3).
  `bh work delivery release <tag>` refuses an unknown tag (`unknown_tag`) before recording
  anything and refuses `intake_disabled` before it asks the verifier.
- **The completion policy and the close rule.** `MilestoneService.complete(report, actor)`
  evaluates `close_rule` and closes through `ReportStore.close`. The trigger calls it, and the
  sender calls it (the new optional `OutboxService(completion=)`, composed by `outbox_service_for`)
  after a milestone comment settles (`sent`, `abandoned` via `discard`, or reconciled), OUTSIDE the
  binding's lock; a reply never runs it. X16: a sent `released` comment gives `notified:released`
  and a closed report; a discarded one closes it with `notification_behind` true. Interpretation
  (to be confirmed by the operator, as I8's): the close rule's `outbox_enabled` is "the outbox is
  on AND drafting is on for the completion milestone", so an outbox that will never draft a
  `merged` record does not strand a `close-on-merge` report open forever; with the outbox on and
  drafting on, the report waits for the record, as I8 recorded. A superseded `merged` record
  counts as settled, so under `close-on-merge` a release that supersedes it also completes the
  report even though the `released` record is still a draft.
- **The seam.** `work_merge` calls `_milestone_merged` after `close_merged` succeeds in `merge`,
  the idempotent reconcile of an already landed bead, `finish` / `merge --molecule` (for the epic
  and its children via `_molecule_members`), the molecule reconcile and the batch land
  (`work_group`). Only a land onto the canonical integration branch counts: a merge into a
  container is not on main yet. `work_intake.on_work_merged` returns before any bd or git call
  unless `intake.external.enabled`, then reads the tip of the base (the merge commit), runs the
  root function and turns every failure, unexpected exceptions included, into a stderr warning:
  a hook failure never fails the merge. Not hooked: the PR landing path (`work.landing: pr`, where
  the merge happens on GitHub) and the batch reconcile; a follow-up.
- **Config (all off).** Under the host-owned `intake.external`: `completion_policy`
  (`close-on-merge` | `close-on-release`), `milestones.draft_on_merge`, `.draft_on_release`,
  `.append_release_to_closed`, `outbox.enabled`, `outbox.mode` (`supervised`, the only value). Only
  a literal `true` enables; an unrecognised policy takes the conservative `close-on-release`. No key
  sends, approves or mints a grant. `outbox.labels.on_delivery`, `public_evidence.commit_links` and
  `absent_confirmations` stay with later beads; the milestones draft no label diff.
- **Tests.** `tests/unit/modules/intake/test_intake_milestones.py` (X1 to X5, X16, X18, nothing
  sends), `tests/test_intake_milestones_composition.py` (the root, the verifier, rendering, the
  public store untouched), `tests/test_work_merge_milestones.py` (the seam and the non-fatal hook)
  and `tests/test_intake_milestones_int.py` (a real bd store, `integration`).
- **Not done here.** `bh work delivery release <tag>` is **intentionally not registered**: it
  changes the published operation catalog and so needs `just wire-publish`, a release step
  reserved for the operator (as for `import`, I4, and `link`, I5). `work_intake.delivery_release`
  and `work.delivery_release_cmd` are implemented and tested but unreachable from the CLI.
  **Registration recipe**, in order:
  1. `src/beadhive/adapters/cli/projection.py` projects a migrated group's verbs as two-token
     paths only (`<group> <verb>`; `project_cli_group` splits `path` into exactly two parts), so
     the three-token `work delivery release` needs either a nested-path extension of that
     projection plus the row below, or the flat spelling `work delivery-release` with the row
     `work delivery-release|tag:string:r,as_:string:o,hive:string:o,as_json:boolean:o`. Decide
     the spelling first (the ADR says `work delivery release`).
  2. `src/beadhive/kernel/operations/__init__.py`: add that row to `_CLI_ROWS` (keep the rows
     sorted).
  3. `src/beadhive/work.py`: add `"work.delivery-release": delivery_release_cmd` to
     `CLI_HANDLERS` (the key is the canonical operation identity), bind the `--json` option name
     with `_bind_cli_handler(delivery_release_cmd, as_json="json_out")` if the catalog row names it
     differently, and drop the "Not yet in `CLI_HANDLERS`" sentence from its docstring.
  4. Run `just wire-publish` (operator, release step), then the catalog-drift tests and `bh work
     check`.

### 13. Leak check and sanitizer seam

Adopted as `bh-6wj9y` R10: a pure `domain.leaks.check(rendered, policy)` runs at O1, at every
render and on the exact bytes at O9. Findings: `secret` and `label_not_owned` are **never**
overridable; `bead_id` (every fleet prefix, operational and public), `branch`, `private_label`,
`seat` and `path` are overridable only by a named per-finding `--publish` recorded in
`Approval.overrides` with the actor (that is "unless deliberately published"); `closing_keyword`
warns. `LeakPolicy` is built in the root (fleet prefixes, patterns, `registry.closed_dimensions`,
path roots). `Sanitizer` is a contracts `Protocol` (`scan`, `scrub`); until `bh-o2mjz` lands the
root binds the shared `scrub()` from `beadhive_plugins.credentials` plus the token-shape regex,
and **if no sanitizer is bound every operation is `held`** (fail closed). Content fed to inference
(a triage skill reading thread bodies) passes the same sanitiser.

### 14. Composition with the plugin ADR and the port ADR

| Concern | Decision | Source |
|---|---|---|
| How intake gets an adapter | `intake_services` asks the root's `bootstrap/tracker.py` for the adapter of the hive's public store; one adapter per (hive, store scope) per process, cached by the root. Intake never discovers, imports or constructs a plugin | plugin ADR sections 1, 3, 4 |
| Capability and plugin | `tracker.adapter`, API major **1**, port `TrackerAdapter`; plugin id `tracker-github` (package `beadhive-tracker-github`, extra `beadhive[github]`). Writers are found with `isinstance` on the bound object | plugin ADR section 1 |
| Which store is read | `managed_repos[].stores.public.adapter` (fleet-owned intent); owner and repository derived from the hive triplet. **`bh-mj7cl`'s proposed `intake.external.tracker` key is dropped**: the binding already names the plugin, and a second place to name it would drift | plugin ADR section 2, B2 |
| Enablement | The plugin's per-store effective-enablement formula (plugin enabled, kernel-enabled, store bound, identity resolves, the operation's own flag) **AND** intake's own `intake.external.*` switches. Requested operations are a runtime request from intake: `issues.read` for import and discovery, `comments.read` for ingestion and for every O5, `comment.create` and `labels.change` only at O9. The plugin's refusal order (R6 to R13) is reported verbatim | plugin ADR sections 3, 4 |
| Writes | Need the host-owned `plugins.tracker-github.write.*` flag (necessary, never sufficient) **and** a per-operation single-use operator grant. No intake key, at any config layer, can enable or pre-approve a write | plugin ADR section 3; port ADR section 6 |
| Credentials | Ambient, injected by the root's single provenance-returning resolver; `ResolvedToken`, `TokenSource` and `scrub()` from `beadhive_plugins.credentials`. Intake never sees a token; outbox documents, logs and `--json` carry provenance at most | plugin ADR O1; port ADR section 2 |
| Projection | **Keep `bd github sync --pull-only` as is.** Intake reads issues and comments through the port and joins to the projection by URL without reading it; it never resolves the public store and never runs `bd github sync`. Two GitHub readers remain the port ADR's accepted cost | port ADR section 5 |
| `bh-cm4er` | **Stays on the bd path** (`github_projection`, `tracker_sync`, or upstream bd). Intake neither fixes nor depends on it | port ADR section 5 |
| Port additions | A1 `IssueResolver` (optional, additive) and kit extension K1, both to the tracker implementation molecule; no other port change | `bh-mj7cl` section 4; `bh-6wj9y` R11, R14 |

### 15. Configuration surface

The spelling below is a **proposal** for the implementation's config bead to ratify; the
semantics and the defaults are the decision. Every key is per hive; everything is off.

| Key (under `intake.external`) | Default | Meaning | Source |
|---|---|---|---|
| `enabled` | `false` | Import, link, discover; off means `NullIntake` | `bh-mj7cl` section 6 |
| `default_type` | `bug` | Type of an imported report | `bh-mj7cl` |
| `discovery.enabled` | `false` | The opt-in host-daemon timer | `bh-mj7cl` |
| `discovery.interval_seconds` | unset (required when enabled) | Timer interval (the timer only; tests drive `tick()` with an injected clock) | `bh-mj7cl` |
| `discovery.full_reconcile_every` | `24` | Full id reconcile cadence, in ticks | `bh-mj7cl` |
| `comments.enabled` | `false` | Thread ingestion | `bh-fptob` R9 |
| `comments.deletion_confirmations` | `2` | Complete snapshots before a tombstone | `bh-fptob` |
| `comments.purge_deleted_bodies` | `true` | Drop the latest body on tombstone | `bh-fptob` |
| `comments.max_body_chars` | unset | Optional body cap (digest kept) | `bh-fptob` |
| `comments.reconcile_every` | unset | Full comment-id pass cadence, in ingest passes | `bh-fptob` |
| `privacy.allow_published_operational_store` | `false` | Acknowledgement to enable on a single-store or `git-origin` hive | `bh-fptob` |
| `outbox.enabled` | `false` | The per-hive egress opt-in (`bh-o2mjz`) | `bh-6wj9y` R15 |
| `outbox.mode` | `supervised` | The only accepted value in v1 (D2); anything else fails validation | `bh-6wj9y` |
| `completion_policy` | `close-on-merge` | Or `close-on-release` | `bh-6wj9y` |
| `milestones.draft_on_merge`, `.draft_on_release`, `.append_release_to_closed` | `false` | Which milestones draft records | `bh-6wj9y` |
| `outbox.labels.on_delivery` | unset | Delivery value to owned labels; must lie inside `owned_prefixes` | `bh-6wj9y` |
| `outbox.public_evidence.commit_links` | `false` | Allow public commit or PR URLs in `PublicFacts` | `bh-6wj9y` |
| `outbox.reconcile.absent_confirmations` | `2` | k in O16 | `bh-6wj9y` |

`bh-fptob`'s unprefixed `comments.*`/`privacy.*` and `bh-6wj9y`'s `intake.external.outbox.*` are
unified under one `intake.external` namespace. No key holds a token, a grant, an outbox id or a
tracker binding. Which partition layer (fleet or host) owns each key is the config bead's call
under the existing partition rules; the fixed requirement is the one above: no intake key can
enable or pre-approve a tracker write. With every default, `bh report`, triage, merge and release
behave exactly as today.

### 16. Operator decisions and constraints honoured

| Decision or constraint | How |
|---|---|
| D1 one internal report per issue, URL key, native edge, many to one, no cross-store edge | Section 5 (URL idempotence in the operational store only, R1 refuses public ids), section 6 (`tracks`, refusals `CROSS_STORE_LINK`, `PUBLIC_STORE_ID`) |
| D2 supervised-only first slice | O5 and O9 are operator-only; `outbox.mode` accepts only `supervised`; no timer runs O5 or O9; `bh work outbox show` is the dry-run render whose digest the operator types |
| D3 import before `bh-elkv`, `intake:untriaged` only, not `promote` | `bh work import`; wake-on-reply and `needs-info` deferred; `bh-elkv`'s rename touches only `vocabulary.py` and its facade consumers |
| Off by default, configurable | Section 15; `NullIntake` makes every hook free |
| Every tracker write an operator-approved gate | Section 11 |
| Public store pull-only | Intake never resolves `SCOPE_PUBLIC`; a test asserts the public-store fixture is byte-identical after every case |
| No `bh-*` id or internal content leaves unless deliberately published (`bh-o2mjz`) | Section 13; the opaque `outbox_id`; templates read only `PublicFacts`; marker privacy test |
| Closed issues in scope | Import, discovery (`include_closed=True`) and thread ingest treat closed issues as open ones |
| Offline, clock-free tests | All cases run on `MemoryTracker`/`MemoryWorld` with explicit `at=`, in-memory stores, a deterministic `IdSource` and an in-process lock; ordering is asserted on `seq`, `rev`, `Attempt.n` and `write_requests()` only. Test plans: `bh-fptob` R8 (T1 to T19), `bh-6wj9y` R14 (X1 to X18, K1), `bh-mj7cl` section 4 refusals |
| Public `refs/dolt/data` frozen pending `bh-l3zvc.5` | Nothing here writes the public store or its ref |
| Never `bd compact` / `bd flatten` | Event beads are limited to rare report-level transitions; per-message state lives in documents; tombstone purging is documented as not erasure |

## Resolutions

### Divergences recorded by `bh-6wj9y` (R12)

| # | Divergence | Resolution | Wins |
|---|---|---|---|
| D-a | `bh-mj7cl`: an accepted report may be its own one-to-one work | A bound report is never work; one-to-one is `accept --new-work`; a bound report is refused as a link target (`TARGET_IS_REPORT`) | `bh-6wj9y` |
| D-b | `bh-mj7cl` `ReportStore.create`/`close`, `DispositionRequest` | `create` opens the hold, `close` is the held close, `DispositionRequest` gains `work`/`new_work` | `bh-6wj9y` |
| D-c | Adopt copies `external_ref` onto the epic and links `parent-child` | For bound reports, adopt links `tracks` and copies nothing | `bh-6wj9y` |
| D-d | `bh-fptob` R7 outbox view | `pending` covers `sending` and `unknown`; `issued()` added | `bh-6wj9y` (additive) |
| D-e | `bh.intake.source` vs `external_intake` vs `external_outbox` | One convention: flat keys `intake.source`, `intake.thread`, `intake.outbox` (section 4) | neither; the repository's `git.commits` precedent |
| D-f | Add `"tracks"` to `_NON_BLOCKING_DEPENDENCIES` | Invert to `_BLOCKING_DEPENDENCIES = {"blocks", "conditional-blocks", "waits-for"}` in both files, plus a `report` lane | `bh-6wj9y` |
| D-g | Port ADR "operator-approved gate" | A recorded `Approval` in the outbox document per operation, not a bd gate bead | `bh-6wj9y`; consistent with the plugin ADR's consumer-side gate |

### Divergences found by this ADR

| # | Divergence | Resolution | Wins |
|---|---|---|---|
| X1 | Port ADR: intake imports `tracker.contracts (+ application)`; `bh-fptob` uses `iterate`/`snapshot_ids` | The checker forbids it (G5); intake owns its pager and snapshot loop | `bh-mj7cl` |
| X2 | `bh-fptob`/`bh-6wj9y` domain beads "depend only on the tracker contracts" | The domain may not import them; intake-owned records in the domain, translation in `application/` (section 9) | `bh-mj7cl` |
| X3 | Discovery checkpoint in a `.bh/` file (`bh-mj7cl`) vs sweep checkpoint in bead metadata (`bh-fptob`) | Both per-binding checkpoints through `CheckpointStore` (file-backed); per-issue checkpoints stay in `intake.thread` | `bh-mj7cl` for per-binding, `bh-fptob` for per-issue |
| X4 | Per-hive import lock (`bh-mj7cl`), per-binding ingest lease (`bh-fptob`), send lease (`bh-6wj9y`) | One `IntakeLock` per binding over every mutating operation, implemented as `bh-6wj9y` R6 | `bh-6wj9y` mechanism, applied to all |
| X5 | `bh-mj7cl` `intake.external.tracker` key; three config spellings | Dropped in favour of the plugin ADR's `stores.public.adapter`; one `intake.external` namespace (section 15) | the plugin ADR |

## Implementation molecule input

This is **input** for a later, human-interactive `/bh:replan` on epic `bh-wjvq9` run by the
operator or planner. This ADR files nothing. Names below (I0, I1, ...) are stable labels for the
planner, not bead ids; the planner shapes, splits or merges them.

### The draft decomposition, adjusted

The planning session's draft (10 issues, from the `bh-wjvq9` design field) is restated with
what the spikes changed:

| Draft | Draft item | Adjusted by the spikes |
|---|---|---|
| 1 | Pull commits what it imports and reports uncommitted rows (absorbs `bh-cm4er`) | **Unchanged in substance, stays on the bd projection path** (port ADR section 5). Not intake-module code. Link the existing `bh-cm4er` rather than filing a duplicate; whether it is a child or an external dependency is the planner's call. It **no longer gates** draft items 3 and 6, because intake reads through the port, not the projection. The port ADR's flagged `last_sync` wall-clock risk sits beside it |
| 2 | Prove the pull includes closed issues and updates an issue that closes | **Re-scoped to the port read path:** a conformance case on `MemoryTracker` plus a recorded-response test in the adapter read path (plugin ADR W7: `state=all`, an issue updated on close). Live import depends on it; module work does not. The projection half, if wanted, stays with item 1 |
| 3 | Opt-in scheduled pull-only discovery | **Becomes `IntakePort.discover`** (I6): `bh work discover [--full]` and an opt-in host-daemon timer on the `release_catalog_auto_import` pattern, through the module and never the adapter or `bd github sync`; checkpoints through `CheckpointStore`. Moves **after** import, because it runs the same upsert |
| 4 | Import an external issue as an internal report bead (D1, D3) | `bh work import` (I4) with refusals R0 to R8 and URL idempotence. Requires the module (I0), `BdReportStore` (I1) and **the hold (I3) first**, so no imported issue is ever ready work. A1 lands with or before it, else the fallback |
| 5 | Many-to-one report to work link | `bh work link` and `accept --work`/`--new-work` (I5), with D-a's refusal and `delivery:pending` at link; adopt's D-c change |
| 6 | Comment ingestion with thread identity | I7: the `bh-fptob` model in `intake.thread` (pure rules in the domain over intake records, X2), the store adapter with `IntakeLock` and `seq` re-check, the privacy refusal. Depends on I0, I1; not on item 1 |
| 7 | Report dimensions with field ownership | **Split and moved earlier:** I2 (vocabulary: `delivery`, `notified`, the `intake-hold:` marker and seat policy, the D-f blocker inversion and `report` lane) right after the module, then I3 (the hold at create, the held close, `issue_close_held`, the doctor sweeps, the claim refusal) before import. `work_progress` lands with I5 |
| 8 | Communication records with publish intent and a leak check | I8: the outbox domain (records, `DraftKey`, the pure O1 to O19 machine, supersession, the leak check), plus kit extension K1 |
| 9 | Supervised sender with receipts, dedup and timeout reconciliation | I9: the root `IntakeLock` implementation, write-ahead, receipts, reconciliation, the `bh-fptob` view with `issued()`, and the `bh work outbox` verbs (`show`, `approve`, `send`, `reconcile`, `discard`, `retry`, `draft`). **`comment.create` first (I9a), `labels.change` as its own later bead (I9b)**, each behind its own gate |
| 10 | Merge and release milestones that draft records | I10: `on_work_merged` from `work.merge`/`finish` (non-fatal), `bh work delivery release <tag>` with `ReleaseVerifier`, the completion policies |

Three items are new:

- **I0 Module skeleton.** `modules/intake` with the vocabulary move behind the `state.py`
  facade, `MemoryReportStore`, the `ReportStore` contract cases, G5 to G7 with negative controls,
  `test_intake_independence.py`, the `tests/BUILD` unit target. No behaviour change; the existing
  intake tests pass unchanged.
- **I1 Bead-store adapter and migration.** `BdReportStore`, the new `beadhive-bd-cli` routes,
  `file_report`/`queue`/`dispose` behind the port, retire `cycle-edge-036`.
- **IR Rotation gate** (a gate, not code, like plugin ADR W10): the vocabulary (I2), the hold
  marker and seat policy (I3) and the `intake.external` config schema ship and **rotate to every
  host before any hive enables intake or any bound report is created**. An old host would reject
  `delivery:` labels in `bh label validate`, classify `intake-hold:` as `other` (so its `bh work
  approve` would not refuse it) and, because `BeadhiveConfig` is `extra="forbid"`, report a config
  with `intake.external` invalid (the plugin ADR's item (a) hazard, applied here). Probe any new
  required `hive ready` check on all frames first.

### Ordering

```text
I0 -> I1 -> I2 -> I3 -> I4 -> I5 -> I6
            I2 ------------------> I8
      I1 -> I7
I7, I8, I3 -> I9a -> I9b
I9a, I5 -> I10
IR after I2, I3 and the config schema; before any fleet use
```

This follows the draft's order where it still holds (import before link, link before records,
records and ingestion before the sender, the sender before milestones) and changes it where the
spikes require: the dimensions vocabulary and the hold move **before** import (invariant H), and
discovery moves **after** import.

**Cross-molecule dependencies** (owned by the other implementation molecules; the planner wires
the edges):

| Intake item | Needs | From |
|---|---|---|
| I0 | `modules/tracker` contracts, `MemoryTracker`, G1 to G3 | tracker implementation (plugin ADR W1) |
| I4, I6, I7 live use | category composition and the adapter read path; A1 for distinct refusals; draft item 2's proof | plugin ADR W4, W7; tracker molecule |
| Any fleet use | the config schema rotated (`adapter`, `plugins`) | plugin ADR W2, W10 |
| I9a, I9b live use | `comment.create`, `labels.change` | plugin ADR W8, W9 |
| I9 sanitizer | `bh-o2mjz`'s sanitizer when it lands; until then the shared `scrub()` from plugin ADR W3 | `bh-o2mjz`, plugin ADR W3 |
| I8, I9 tests | K1 | tracker testing kit |

Every module item can be built and tested against `MemoryTracker` and `MemoryReportStore` before
any of the live dependencies land.

### Acceptance items the planner must carry

1. Every bead records its measured `pants dependents` closure once the Pants ownership defect in
   `bh-mj7cl` E10 is fixed (expected about 15 modules forward against 147 today).
2. I1 or I7 re-measures metadata growth on a shared-server store with a realistic thread
   (`bh-fptob` E13 is one embedded, ungarbage-collected measurement), and verifies dotted-key
   behaviour for `--metadata` merge and `--has-metadata-key` (section 4).
3. I2 verifies whether `bd set-state` can clear a `delivery` value on unlink; if not, keep
   `pending` with an `unlinked` fact.
4. I9a re-verifies assumption A1 and the documented refusal diagnostics against recorded real
   responses on the first gated write.
5. A test asserts the intake composition never resolves `SCOPE_PUBLIC` and the public-store
   fixture is byte-identical after every case.
6. One bd-binary integration test replays `bh-6wj9y` E4 (hold, refused plain close, forced close
   with no window, metadata on a closed bead, `--has-metadata-key`).

## Consequences

### Positive

- One module owns the one queue: imported issues and internal reports share vocabulary,
  dispositions and dupes, and the `report`/`triage` legacy cycle is retired.
- The module depends on two narrow ports and nothing behind them, enforced by the checker once
  G5 to G7 land. A second tracker is a new plugin, not an intake change.
- Intake tests move from the legacy-stateful suite faking bd argv (194 test files reach the
  intake code statically today; four files take 12.7 s) to module unit tests over in-memory fakes
  (the `modules/hives` reference runs 11 tests in 0.58 s).
- Untrusted public text never becomes dispatchable work, and completed work is never kept open
  for delivery tracking. Merged-but-unreleased is a first-class, visible state.
- Every write is supervised, digest-bound, single-use and attributed; reconciliation never
  re-sends an ambiguous message without confirmed absence.
- With every default, behaviour is today's, byte for byte.

### Negative or accepted costs

- One permanent hold-gate bead per bound report and one work bead per one-to-one acceptance
  (bounded per report, not per comment or message).
- Held reports read "blocked: 1 open gate" in operator views until the `report` lane lands.
- `IntakePort` is wide (about 20 operations); the `OutboxPort` split stays available.
- `reports_with_outbox()` scans every report that ever had a record; an index key is added only
  if measured slow.
- The `state.py` facade keeps its 238-module reverse closure; only callers that migrate shed it.
- Two GitHub readers (port and projection) remain, as the port ADR accepted.
- A rotation gate (IR) precedes any use.

## Alternatives rejected

Boundary candidates (`bh-mj7cl` E13, seven `bh:modularize` dimensions, 0 to 2 each):

| Candidate | Total | Why not |
|---|---|---|
| **A: `modules/intake` owns the whole queue behind `IntakePort` over the tracker port and `ReportStore`** (chosen) | 12 (14 once G5 to G7 land) | n/a |
| B: `modules/external_intake` for import and link only; `report`/`triage` stay legacy | 9 | Splits one queue and its vocabulary between two owners; dupes would need a forbidden legacy import or a copy; the legacy cycle survives |
| C: extend `report.py`/`triage.py` in place | 2 | Hard stop: no port, no direction rule |
| D: fold intake into planning | 5 | Contradicts the recorded rejection of a global planning/report module; planning consumes intake through `intake:promoted` |
| E: put import inside `modules/tracker` | 3 | Hard stop: the tracker is a leaf; threads and the outbox are not port business (port ADR candidate E) |
| F: a library or plugin package | 11 | Hard stop on direction: a library package may not import the tracker contracts; the merge hook may not statically import a plugin; intake is core behaviour |
| G: fold intake into `modules/work` | 4 | Breaks "intake imports no work layer"; every work test gains tracker contracts |

Other rejections, with their spike reasons: native bd comments, a child bead per comment, a
host sidecar or the public store as the thread's home (`bh-fptob` R4); a label, the `tracks` edge,
the built-in `blocked` status, a custom status, `bd defer`, one shared hold gate, or keeping work
open as the merged-but-unreleased mechanism (`bh-6wj9y` R3); `blocks`, `parent-child`,
`duplicates`, `relates-to`, `caused-by` or `discovered-from` as the link edge (`bh-mj7cl` section
5); reusing `outbound`/`publish` or a `release` dimension (`bh-6wj9y` E1, E2); a bd gate bead per
write (D-g); one shared metadata key for thread and outbox (`bh-6wj9y` R1).

## Risks

- **A1 visibility is unverified** for GitHub comment listings. Mitigated by k = 2 confirmations
  and O19's `duplicate_observed`; re-verified on the first gated write (acceptance item 4).
- **Creation window.** A report exists for one bd call before its hold. Mitigated by opening the
  hold immediately, the stamp repair, the claim refusal, and the fact that untriaged reports are
  not auto-dispatched today.
- **A hold resolved or deleted by hand** through raw bd. The seat policy refuses it through bh;
  `bh doctor` re-opens a missing hold.
- **Closing keywords** in a commit pushed to the default branch close the public issue early
  under `close-on-release`, outside Beadhive's control. The leak check warns in rendered text; a
  merge-time commit-message lint is follow-up work.
- **Metadata growth** on very long threads: re-measured in I1 (section 4): time is flat to about
  1000 comments and Dolt shares unchanged chunks, but the queue read carries every document.
- **Page-shift false deletions** under page-number pagination; mitigated by two confirmations and
  resurrection (a false tombstone costs one flip).
- **Closure figures** are structural, not measured with Pants (acceptance item 1).
- **Unrun surfaces.** No spike ran the checker against a modified tree, `bh label validate` with
  the new dimensions, or the Linear and Jira mapping rows (documentation only).

## What remains open

- ~~Whether triage marks `considered_rev` through a module verb or as a side effect of a
  disposition~~: decided in I7, a module operation (section 9).
- The untriaged-queue read still carries every report's `intake.thread` (bd 1.3.0's `bd list
  --json` has no metadata-free projection, section 4). The queue path never decodes it; trimming
  the transfer needs a route change (a metadata-free list, or a template `--format` list) that
  also changes the raw rows `bh work intake --json` emits, so it is its own decision.
- ~~Clearing a dimension value on unlink (acceptance item 3).~~ Resolved by I2 (`bh-cokbi`), see
  "I2 measurement" below.
- The `needs-info` reply event and wake-on-reply, after `bh-elkv` (D3).
- Started, blocked and deprioritised milestones; revert detection.
- A closing-keyword lint on merge commits.
- Issue identity across a transfer or key change (port ADR O3): v1 refuses a transferred URL as
  `OTHER_PROJECT` and marks a thread `missing` for a human; no automatic re-binding.
- GitHub App installation identity for own-message row 3 (deferred with App support).
- Whether the public store should ever carry comments (a separate decision, port ADR section 5).

## Next steps

This ADR does **not** file the implementation molecule, does not run `/bh:replan`, does not close
`bh-33d5a` and touches no other bead.

1. The close reason of `bh-sa91g` carries the verdict (GO) and this ADR's path; the "Decision
   summary" above is paste-ready.
2. The implementation molecule is filed later by the operator or planner through a
   human-interactive `/bh:replan` on epic `bh-wjvq9`, using "Implementation molecule input" above,
   linked back to `bh-wjvq9`.
3. Placeholder `bh-33d5a` stays open until that molecule is filed, and is then closed pointing at
   it.
4. A1 and K1 go to the tracker implementation molecule as additive items; the G5 withdrawal of
   "`(+ application)`" is recorded here for that molecule's checker work.

## I2 measurement: `bd set-state` cannot clear a `delivery` value

Acceptance item 3, measured by I2 (`bh-cokbi`) on bd 1.3.0 against a throwaway embedded store
(no real hive touched; `tests/test_intake_bead_store_int.py::test_set_state_cannot_clear_a_delivery_value_so_unlinking_keeps_it`
re-runs it):

| Attempt | Result |
|---|---|
| `bd set-state <id> delivery=` | refused, exit 1: `invalid state format "delivery=", expected <dimension>=<value>`; the label stays |
| `bd set-state <id> =pending` | refused the same way, exit 1 |
| `bd set-state <id> delivery` (no `=`) | refused, `expected <dimension>=<value>` |
| `bd set-state --help` | no unset or clear flag |
| `bd set-state <id> delivery=merged` after `pending` | replaces: the label cache becomes `delivery:merged`, an event bead records `Previous: pending` |
| `bd label remove <id> delivery:pending` | drops the label cache only. No event bead is written, so the history still reads `delivery -> pending` while the label says nothing |

**Chosen behaviour.** Unlink never clears `delivery`. The value stays (`pending` stays
`pending`) and an `unlinked` fact is recorded beside it, because a raw label removal would leave
the event-sourced history and the label cache disagreeing. The pure rule is
`delivery_after_unlink(labels)` in the intake domain (`DeliveryAfterUnlink(value, unlinked=True)`);
the ReportStore contract case `store.set-state-replaces-but-never-clears` pins that a store
replaces a dimension but refuses an empty value, on `MemoryReportStore`, on `BdReportStore`
over the fake bd and over a real bd store. Where the `unlinked` fact is stored was the link bead's
(I5) decision: the report's `intake.outbox` document, `facts.unlinked` (see "As built (I5)" in
section 6); I2 only fixed that the dimension is not the place.

## I2 scope note: the `report` lane

The `report` lane in `_readiness` (a bead whose only open gates are `intake-hold:` gates) is
**not** part of I2's delivered change. Both operator views emit `readiness` on published wire
types (`WorkItemReadiness`, `SnapshotWorkItemSummary` and `RemoteSnapshotWorkItemSummary` in
`daemon_contract.py`, and the OpenAPI document they generate), so a new lane value widens a
published contract that older clients validate strictly. Until it is decided and rotated, a held
report reads `blocked: 1 open gate`, which is accurate. An `intake-hold:` gate already reaches
the wire gate list as kind `other`.
