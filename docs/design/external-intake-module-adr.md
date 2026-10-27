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
- Clearing a dimension value on unlink (acceptance item 3).
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
