# Spike `bh-fptob`: a tracker-neutral conversation and comment model with thread identity

**Bead:** `bh-fptob` · **Seat:** `dev/fptob` · **Type:** research-only (no product code)
**Feeds decision on:** the external-intake design molecule `bh-wjvq9` (prerequisite 3 of the
public-issue loop `bh-33d5a`), and its implementation beads for comment ingestion, communication
records and the supervised sender.

Terminology in this document is neutral: the module is **external intake**; a tracker issue's
conversation is a **thread**; one stored comment is a **comment record**. The port names
(`TrackerReader`, `TrackerComment` and so on) are the ones accepted in
[`tracker-adapter-port-adr.md`](../design/tracker-adapter-port-adr.md).

## Question

Can external intake own a conversation model that is tracker-neutral (built only on the tracker
port's raw comment operations), that preserves comment identity, author identity, revisions,
deletions, per-thread checkpoints and recognition of Beadhive's own messages, and that has one
justified storage location which keeps the public store pull-only and internal content private,
with semantics an offline, clock-free test can exercise? GO if one storage location and one rule
set satisfy every constraint below; NO-GO if any of them forces a write into the public store,
a GitHub-shaped field in the model, or a wall-clock dependence in the tests.

Binding inputs are operator decisions D1 to D3 of epic `bh-wjvq9` (one internal report bead per
external issue, keyed on the issue URL; a supervised-only first outbox slice; import into
`intake:untriaged` under a verb that is not "promote"), and its constraints (off by default,
every tracker write an operator-approved gate, nothing writes into the public store except the
pull, no `bh-*` id or internal content leaves unless deliberately published, closed issues in
scope, tests offline and never timing-dependent).

This spike does **not** ask:

- what the outbox state machine, the communication record or the report dimensions (triage,
  delivery, notified) look like. They are sibling concerns of `bh-wjvq9`. This model consumes
  only a small read interface from the outbox (E13, R7);
- where the external-intake module boundary sits or how it is scored. A sibling spike covers
  that, and this document does not depend on it;
- whether to change the tracker port. The port is accepted; the model has to fit it as it is;
- how webhooks, wake-on-reply (it waits for `bh-elkv`'s needs-info state, D3) or a second
  live tracker adapter are built.

## Method

1. Read the accepted port decision
   [`tracker-adapter-port-adr.md`](../design/tracker-adapter-port-adr.md) (sections 1 to 6 and
   the "inconsistencies" section) and its prototype
   [`docs/spikes/artifacts/bh-gj1hb/`](artifacts/bh-gj1hb/) (`tracker_contracts.py`,
   `github_adapter.py`, `memory_tracker.py`). Also read the sibling spikes
   [`bh-gj1hb-tracker-port.md`](bh-gj1hb-tracker-port.md) (E19, second-tracker sketch),
   [`bh-mqvdq-github-identity.md`](bh-mqvdq-github-identity.md) (own comments) and
   [`bh-a1svm-github-transport.md`](bh-a1svm-github-transport.md) (E5, wire facts).
2. Read the intake design
   [`external-issue-intake.md`](../design/external-issue-intake.md) sections 1, 2, 4, 5 and 7,
   the projection record [`github-issue-projection.md`](../design/github-issue-projection.md),
   the store contract [`two-store-scope-adr.md`](../design/two-store-scope-adr.md) and the
   operator guide [`TWO-STORES.md`](../TWO-STORES.md).
3. Read the existing intake code for vocabulary and write paths: `src/beadhive/state.py`
   (`STATE_DIMENSIONS`, `channel_of`, `origin_from_source_system`), `src/beadhive/report.py`
   (`file_report`, `_create_bead`, `_set_state`), `src/beadhive/triage.py` (`CHANNELS`,
   `list_intake`, `_clear_intake`), `src/beadhive/work_intake.py`, and
   `src/beadhive/store_target.py` (`operational_store`, `require_operational`,
   `PublicStoreNotOperational`).
4. Ran a throwaway experiment against bd 1.3.0 (`f45b249ce`) in an **embedded** scratch store
   (`bd init -p cmx` under the session scratchpad; no shared server, no hive store touched, no
   network). I probed what native bd comments can represent (`bd comments --help`,
   `bd comments <id> --json`); how `bd update --metadata` / `--set-metadata` merge; whether
   `bd history --json` exposes metadata; and how the store grows when a large metadata document
   is rewritten. The scratch store was discarded. No GitHub request was made.

## Evidence

### The port gives the model what it needs, and nothing thread-shaped

- **E1. A comment arrives with every identity the model needs.** `TrackerComment` carries
  `key: CommentKey(id, global_id)`, `issue: IssueKey`, `author: Actor | None`, `body` (the
  adapter's marker removed), `created_at`, `revision: Revision`, `via: IntegrationRef | None`
  and a decoded `marker: OwnMarker | None` (`artifacts/bh-gj1hb/tracker_contracts.py:152-166`).
  `IssueKey` equality is `(identifier, url)`, and the URL is the D1 join key (`:55-67`).
  `Actor` equality is `ActorKey(id, kind)`; `display` is `compare=False`, "display only"
  (`:85-95`).
- **E2. Revisions are ordered by the tracker's timestamp, never the local clock.**
  `Revision(updated_at, version=None)` is `order=True` and compares `updated_at` only. Its
  docstring says it is "the TRACKER's timestamp, never the local clock" (`:129-134`).
  `TrackerComment.edited` is `revision.updated_at > created_at` (`:162-164`).
- **E3. Checkpoints are opaque and scoped by the query that produced them.** `Checkpoint.token`
  is persisted and "never parsed". `since` is inclusive: "the boundary item may repeat
  (consumers upsert on `(key, revision)`); it never silently skips" (`:173-181`).
  `CommentQuery(since, cursor, issue=None, page_size)` reads either one issue or every comment
  in the project (`:197-201`). The prototype rejects a token from another adapter by prefix
  (`github_adapter.py:220-225`), but nothing distinguishes a project-wide token from a
  per-issue one. The consumer has to keep that scope.
- **E4. A per-issue comment read withholds its checkpoint until the last page.** The
  repository-wide list is `sort=updated&direction=asc` and checkpoints every page. The per-issue
  list comes in creation order, so the prototype carries a running maximum inside the opaque
  cursor and emits a `Checkpoint` only on the final page (`github_adapter.py:279-301`; ADR
  section 3, "Read semantics").
- **E5. Deletions are found only by a complete id set.** `list_ids(IdQuery(subject, issue))`
  enumerates comment ids, and `snapshot_ids` returns `IdSnapshot(ids, complete=False)` on any
  `TrackerError`. "Only a complete snapshot may infer a deletion" (`tracker_contracts.py:210-250`,
  `:464-480`; ADR section 3, "Deletions"). On the wire, deleted comments vanish and are
  invisible to `since`. GitHub listings page with `per_page`/`page` plus `Link: rel="next"`
  (`bh-a1svm` E5).
- **E6. The port explicitly leaves the thread model to the consumer.** It excludes "threads,
  revision history, own-message classification, the outbox and the report dimensions
  (prerequisite 3)" (ADR section 3, "Not in the port"). Inconsistency 3 settles that the port
  transports the evidence (`author`, `via`, decoded `marker`, `whoami()`), while "the
  classification rules and the known-actor set ... belong to the intake consumer". Candidate
  E, a wide port owning threads, was rejected as a hard stop (ADR, "Alternatives rejected").
- **E7. Writes are append-only and grant-gated.** `CommentWriter.post_comment(issue, body, *,
  marker, grant) -> WriteReceipt` is the only comment write (`tracker_contracts.py:429-433`).
  There is no edit or delete operation. `WriteReceipt.comment` returns the created
  `TrackerComment`, so its id. `WriteOutcomeUnknown` is never retried, and "the comment may
  exist exactly once and is found again by its decoded marker, which is the outbox's reconcile
  path" (ADR section 3, "Rate-limit semantics").
- **E8. The own-message marker is adapter-owned and carries no bead id by construction.** The
  GitHub encoding is `<!-- bh:v1 hive=... install=... outbox=... -->` as the **last non-empty
  line** only, so a quoted copy is ignored (`github_adapter.py:71-98`; ADR inconsistency 2).
  `bh-mqvdq` uses the repository identity for `hive` (`hive=<owner/repo>`, its options table).
  Its layered recognition is: comment id in the outbox, then the configured App with a known bot
  author, then a known actor **and** the marker, and a known actor without the marker is
  **not** own (`bh-mqvdq`, "Recommendation (layered, in this order)", E1 to E5).
- **E9. An offline, clock-free fake already exists.** `MemoryWorld` scripts
  `add_comment(issue, body, *, at, author, integration)`, `edit_comment(key, body, *, at)`,
  `delete_comment(key)`, `fail_next_write_ambiguously()`, `write_requests()` and
  `set_write_time(at)` with explicit `at=` timestamps and a recording `sleep`
  (`memory_tracker.py:84-160`). The ADR moves it to `beadhive.testing.tracker` as the substitute
  for every consumer test (section 4).

### Where records can live

- **E10. Native bd comments cannot represent the model.** On bd 1.3.0 the `comments`
  subcommands are `add` and an invalid `list`; there is no edit or delete
  (`bd comments --help`). A comment's JSON is exactly `{id, issue_id, author, text,
  created_at}`, with the author a free string from `--actor` or the git user (scratch
  `bd comments <id> --json`). So a native comment has no slot for an external id, an actor key,
  a tracker revision or a tombstone, and it cannot be edited. The intake design also states
  that "ordinary bead comments, internal notes, and agent transcripts [stay] private by default"
  (`external-issue-intake.md` section 5). Mirroring the public thread into the same list
  would mix public-origin text with private notes in one undifferentiated channel.
- **E11. Bead metadata can carry a structured document, with known merge semantics.** In the
  scratch store, `bd update <id> --set-metadata 'ext.ckpt=a'` followed by
  `bd update <id> --metadata '{"ext_intake":{"v":1,"threads":{...}}}'` left **both** keys
  (`{'ext.ckpt': 'a', 'ext_intake': {...}}`). A further `--metadata '{"ext_intake":{"v":2}}'`
  replaced the **whole** nested value (`threads` was gone) and kept `ext.ckpt`. So top-level
  keys merge and a nested value is replaced wholesale. A module that owns exactly one top-level
  key can rewrite its document in one call without disturbing other metadata.
- **E12. `bd history --json` does not expose metadata.** The scratch bead's history entries are
  `{CommitHash, Committer, CommitDate, Issue}`, and `Issue` holds only `created_at, created_by,
  id, issue_type, owner, priority, status, title, updated_at`. No version showed metadata, and
  history grew by one entry per `bd update`. Earlier comment-record revisions therefore cannot
  be recovered through bd's history read. A revision chain the model needs must be stored
  explicitly. That read is also the noisy, expensive one (`state.py` module docstring, around
  line 83, warns off `bd history`).
- **E13. Size is workable, with structural sharing after the first write.** A 1.5 MB
  metadata value (`--metadata @file`) was accepted and round-tripped intact in 0.54 s wall
  time. Rewriting a ~1 MB incompressible document four times, changing only a trailing field,
  grew `.beads` from 2,240 KiB to 4,276, 5,308, 5,336 and 5,356 KiB. The first two writes added
  about 2.0 MiB and 1.0 MiB; each later small edit added about 20 to 30 KiB. This is one embedded
  measurement without garbage collection, so it is indicative, not a bound.
- **E14. Per-item event beads are discouraged on purpose.** `state.py:80`: "Write on failure,
  not on attempt: event beads are permanent (this hive has no compaction tier — `bd compact` /
  `bd flatten` are forbidden until bh-3vs6c lands)". `CLAUDE.md` repeats that bead history is an
  archive and must never be squashed. One child bead per external comment would add a
  permanent bead per comment, plus a permanent version per edit, for every public thread.
- **E15. The public store does not carry comments, and must not be written by anyone but the
  pull.** The projection imports no comments: "comments | **not imported** (the bead's
  `comment_count` stays 0)", and deletions are "**not represented**"
  (`github-issue-projection.md`, "Mapper limits"). Its only admitted write is
  `bd github sync --pull-only --prefer-github` (`guard.public_store_github_refusal`,
  `src/beadhive/guard.py:873`). The ADR keeps the projection as is and names "if the public
  store must carry comments" as a separate future decision (section 5). Routing sends plan,
  work, report and gates "only [to] the internal store (`store_target.operational_store`)", and
  "no dependency or gate edge crosses from internal to public" (`two-store-scope-adr.md`
  section 5; `src/beadhive/store_target.py:287`, `:304`).
- **E16. The internal store is private only when the hive is partitioned.** With `stores:`
  declared, the internal remote "must not be `git-origin` — that would publish private beads on
  the public code origin" (`two-store-scope-adr.md` section 3). Without `stores:`, the default
  single store rides `refs/dolt/data` on the code origin (section 1). For a public repository
  that store is public.

### The existing intake surface

- **E17. The vocabulary already has the channel and queue the import needs.**
  `STATE_DIMENSIONS["intake"]` is `{untriaged, accepted, rejected, rerouted, promoted}` and
  `STATE_DIMENSIONS["origin"]` includes `github` (`src/beadhive/state.py:100-117`).
  `report.file_report` writes a born-native bead with `bd create`, then
  `bd set-state origin=report` and `intake=untriaged` (`src/beadhive/report.py:1-30`, `:64`,
  `:86`). `triage.CHANNELS = STATE_DIMENSIONS[ORIGIN_DIM]` makes the queue source-agnostic
  (`src/beadhive/triage.py:51`, `:65`), and dispositions go through `_clear_intake`
  (`:126`). None of these surfaces reads or writes comments, so a thread document is additive
  to them. Nothing in the queue branches on bead metadata.
- **E18. The privacy policy for outbound payloads is local sanitisation plus explicit opt-in.**
  `bh-o2mjz` makes "EVERY outbound payload [pass] a local secret sanitizer first" with egress
  "an explicit per-hive opt-in". The ADR's `scrub(text, secrets)` and
  `github_projection._scrub` (`src/beadhive/github_projection.py:149`) are the existing
  mechanisms.

## Verdict: **GO**

One storage location and one pure rule set satisfy every constraint. The store is a single
module-owned metadata document on the internal report bead, written in one `bd update` with the
thread's checkpoint (E11). The rules are `(key, revision, digest)` upserts, a local sequence
counter, tombstones confirmed by complete snapshots, and layered own-message evidence. The port
already transports every input the rules need (E1 to E8) and deliberately leaves the thread to
the consumer (E6). Nothing in the design writes to the public store (E15), and the only ordering
inputs are tracker revisions and local integers, so the clock-free fake (E9) can drive every
case. Three residual risks are named, each with a mitigation in the rules: metadata growth on
very long threads (E13), page-shift false deletions (E5), and a single-store public hive (E16).
None is a blocker.

## Recommendation

R1 to R7 are the model, and they are what the implementation beads should build. R8 is the test
plan, R9 the configuration, R10 the follow-up work.

### R1. The model in tracker-neutral terms

| Concept | Definition | Identity (equality) | Port source |
|---|---|---|---|
| **Binding** | One configured `(tracker, project)` pair that intake reads | `ProjectRef(tracker, locator)` | `describe().project` |
| **Thread** | The conversation of one external issue. Exactly one per internal report bead (D1) | the issue URL, `IssueKey.url`, scoped by the binding | `TrackerIssue.key`, `TrackerComment.issue` |
| **Comment record** | Beadhive's stored view of one tracker comment in one thread | `CommentKey.id`, scoped by the binding. `global_id` is kept and never compared | `TrackerComment.key` |
| **Author** | Who wrote a comment | `ActorKey(id, kind)`. `display` is stored for rendering only and never compared | `TrackerComment.author` |
| **Revision** | One observed content state of a comment record | a local integer `rev` (1, 2, ...), plus the tracker `Revision` and a body digest | `TrackerComment.revision`, `body` |
| **Tombstone** | A comment record whose id is confirmed absent from the tracker | the same key, `state = tombstoned` | `list_ids` / `IdSnapshot` |
| **Thread checkpoint** | The opaque high-water mark of the last durably ingested per-issue read | `(scope = issue, token)` | `Page.checkpoint` of `CommentQuery(issue=K)` |
| **Sweep checkpoint** | The opaque high-water mark of the project-wide read, one per binding | `(scope = project, token)` | `Page.checkpoint` of `CommentQuery(issue=None)` |
| **Sequence** | A local, monotone integer per thread, bumped by every applied change | an integer, never a time | none (local) |
| **Role** | How a comment record relates to Beadhive: `external`, `own` or `peer` (another hive's marked message) | derived, re-derivable from stored evidence | `author`, `via`, `marker` plus the outbox read (R7) |

One comment record stores `key {id, global_id}`, `author {id, kind, display}`, `via {id}`,
`marker {hive, install, outbox_id}` when one is decoded, `role` with its `evidence`, the
tracker's `created_at` value, `body` (the latest, marker removed), `state` (`live` or
`tombstoned`), `revisions [{rev, tracker_revision, digest, seen_seq}]`, `first_seen_seq`,
`last_seen_seq`, `tombstone_seq`, `absent_confirmations` and `considered_rev`. The last one is
the triage watermark: the highest `rev` a triage pass has read. "New evidence" is any live
external record with `rev > considered_rev` (or `considered_rev` unset), plus any record
tombstoned after the last triage pass. That is a derived read. It never changes the `intake`
dimension, because wake-on-reply waits for `bh-elkv` (D3).

The thread document also stores `binding`, `issue {identifier, url, internal_id, global_id}`,
`schema`, `seq`, `checkpoint {scope, token, seq}`, `state` (`present` or `missing`; see R6) and
the comment records keyed by id. Every field is a tracker value, an opaque token or a local
integer, so no GitHub vocabulary enters the model. GitHub's `#12` survives only as the opaque
`IssueKey.identifier`.

The port's operations map onto the model as follows:

| Model operation | Port call | Notes |
|---|---|---|
| Incremental thread refresh | `list_comments(CommentQuery(issue=K, since=thread.checkpoint))`, following `next_cursor` (`iterate`) | the checkpoint may be `None` until the last page (E4) |
| Project sweep (discovery) | `list_comments(CommentQuery(issue=None, since=sweep.checkpoint))` | records are routed to threads by `TrackerComment.issue.url`; a URL with no report bead is skipped, and creating one is the import's job |
| Deletion reconcile | `snapshot_ids(reader, IdQuery(COMMENTS, issue=K))` | only `complete=True` counts (E5) |
| Thread presence reconcile | `snapshot_ids(reader, IdQuery(ISSUES))` | closed issues are included (`include_closed` defaults to true) |
| Who am I | `whoami()` | adds to the known-actor set (R7) |
| Post an own message | `post_comment(issue, body, marker=OwnMarker(...), grant=WriteGrant(...))` | outbox-owned and supervised (D2). The returned `WriteReceipt.comment` is ingested as an `own` record |

The model never needs a comment edit or delete operation. The port has neither (E7), and the
first outbox slice only appends.

### R2. GitHub mapping

| Model field | Port field | GitHub REST source |
|---|---|---|
| thread id | `IssueKey.url` | issue `html_url`. For a comment row, the comment's `html_url` up to `#` (`_issue_key_from_comment`) |
| issue identifier | `IssueKey.identifier` | `#` + `number` (opaque to the model) |
| comment id | `CommentKey.id` | comment `id` as a string (repository-wide unique) |
| comment global id | `CommentKey.global_id` | `node_id` (stored, never compared) |
| author | `ActorKey(id, kind)` | `user.id`, with `user.type` `User` mapped to human and `Bot` to bot, otherwise unknown |
| author display | `Actor.display` | `user.login` (mutable, never compared) |
| integration | `IntegrationRef.id` | `performed_via_github_app.id` |
| tracker revision | `Revision.updated_at` | `updated_at` |
| created | `created_at` | `created_at` |
| body | `TrackerComment.body` | `body` with the trailing marker line removed |
| own marker | `OwnMarker(hive, install, outbox_id)` | last non-empty line `<!-- bh:v1 hive=<owner/repo> install=<uuid> outbox=<opaque id> -->` |
| sweep read | `CommentQuery(issue=None)` | `GET /repos/{o}/{r}/issues/comments?sort=updated&direction=asc&since=`, with pull-request comments (`/pull/` in `html_url`) dropped |
| thread read | `CommentQuery(issue=K)` | `GET /repos/{o}/{r}/issues/{n}/comments?since=` (creation order, so the checkpoint is withheld until the last page) |
| id snapshot | `IdQuery(COMMENTS, issue=K)` | the same per-issue listing without `since`, ids only |
| post | `post_comment` | `POST /repos/{o}/{r}/issues/{n}/comments`, which returns the new `id` |
| deletion | (absent from a complete snapshot) | a deleted comment vanishes and `since` cannot report it |

### R3. Second tracker sketch: Linear (from documentation, not exercised)

| Model field | Linear GraphQL source |
|---|---|
| thread id | `Issue.url`. The identifier `ENG-12` is opaque |
| comment id | `Comment.id` (UUID). `global_id` is the same value |
| author | `Comment.user.id` (human). `botActor` maps to bot and `externalUser` to unknown, each keyed by its own id |
| tracker revision | `Comment.updatedAt` (`editedAt` signals a user edit) |
| sweep read | `comments(filter: {updatedAt: {gte}}, orderBy: updatedAt)`, scoped to the team's issues |
| thread read | `comments(filter: {issue: {id: {eq}}})`, or `Issue.comments`. If the order is not by update, the adapter withholds the checkpoint until the last page, as E4 does |
| id snapshot | the thread read without a date filter, ids only. Archived comments count as absent unless `includeArchived` is set, and the adapter must choose and document one |
| own marker | adapter-chosen. Linear Markdown may not hide an HTML comment, so a visible footer or a structured field is the adapter's choice (`bh-gj1hb` E19 lists it as unknown) |
| post | `commentCreate`, which returns `comment.id` |

Two Linear features the model absorbs without a port change. **Reply chains**
(`Comment.parent`) flatten into the issue's single thread; a future additive port field
`in_reply_to` would let the record keep the parent, with no model change. **Resolved
sub-threads** (`resolvedAt`) are presentation and are not modelled. Jira fits the same way
(`/rest/api/3/issue/{key}/comment`, numeric comment ids, `author.accountId` with `accountType`,
the marker as a comment entity property). Its minute-resolution timestamps are the reason for
R5's tie rule.

### R4. Storage: one module-owned metadata document on the internal report bead

**Decision.** A thread's comment records, its checkpoint, its sequence and its deletion
bookkeeping live in **one top-level metadata key owned by external intake** (proposed
`external_intake`, schema-versioned) on the **internal report bead** that D1 already creates for
the issue. Each ingest step that changes anything is a read-modify-write of that one document,
persisted in **one** `bd update --metadata @file` against `store_target.operational_store`.

Why this location:

1. **The checkpoint and the records commit together.** The requirement is to advance a
   checkpoint only after a durable write. Here it is advanced *in* that write: one
   `bd update` is one history entry (E12). A crash leaves either the old document or the new
   one, never records without their checkpoint or a checkpoint without its records. Every
   other location needs N+1 writes and an ordering argument.
2. **It is keyed exactly where D1 keys.** The report bead is the thread (one per issue URL),
   so no new identity, edge or join is introduced, and no edge crosses stores.
3. **It cannot leak into the dispatch or triage surfaces.** No queue, `bd ready` or triage
   listing reads metadata (E17), and other metadata keys survive because top-level keys merge
   (E11).
4. **Cost is bounded per thread, not per comment.** The bead count does not grow (contrast
   E14), and after the first write an edit costs roughly the changed chunks (E13).

Alternatives rejected:

| Location | Why not |
|---|---|
| The public projection store | Breaks "nothing writes into the public store except the pull" (E15). The projection carries no comments today, and adding them is a separate future decision (ADR section 5) |
| Native bd comments on the report bead | Add-only, with no external id, actor key, revision or tombstone slot and a free-string author (E10). It also mixes public-origin text into the private-notes channel |
| One child bead per comment (an event-style child) | A permanent bead per comment and a permanent version per edit in an archive-only hive (E14). Records and checkpoint become N+1 writes. Children appear in `bd list --parent` reads that other features share (`state.py`) |
| A host-local sidecar (file or SQLite) | Not replicated to the internal remote, so it is lost on host failover. It is a second source of truth beside the report bead and sits outside the bead archive |

**Guards and limits that come with the decision:**

- **One writer per binding.** bd has no compare-and-swap on metadata, so a concurrent
  read-modify-write would lose an update. Ingest holds one per-binding lease (the
  implementation reuses an existing lease or slot primitive; it does not invent one). Before
  writing, it re-reads `seq` and aborts the step if `seq` moved.
- **No write when nothing changed.** A poll that yields only boundary repeats leaves `seq` and
  the document untouched. If the checkpoint token is unchanged too, there is no `bd update` at
  all. This avoids "repeated acknowledgements on unchanged polls" at the storage layer too
  (`external-issue-intake.md` section 5).
- **Growth.** Bodies are stored whole by default. GitHub caps a body at 65,536 characters, so
  the default never truncates on GitHub. An optional per-record cap (R9) keeps the latest body's
  digest plus a `truncated` flag when exceeded. The revision chain keeps digests for every
  revision and the body for the latest only. Older text is not recoverable through bd (E12), and
  the tracker remains the system of record for it.
- **Sweep checkpoint home.** The per-binding sweep checkpoint is an optimisation: losing it
  only forces a full sweep, and the `(key, revision, digest)` upsert keeps that idempotent. It
  lives in the same kind of module-owned metadata key on one per-binding internal record. Which
  record that is belongs to the module-boundary and import beads; the model needs only that it
  is in the internal store.

**Why the public store stays pull-only.** The module's only store writes are `bd update` calls
on internal report beads, resolved through `store_target.operational_store`. The module never
resolves the public store, because it joins to the projection by issue URL (ADR section 5), and
it reads comments from the tracker port, not from the projection. A test asserts that the
public store fixture is byte-unchanged after ingest, as the projection's own tests do for the
internal `.beads` (`github-issue-projection.md`, "Failure isolation").

**Why internal content stays private.**

1. The document lives in the internal store, which is private on a partitioned hive (E16).
   **Guard:** external intake refuses to enable on a hive whose operational store would be
   published, meaning a single-store hive or an internal remote on `git-origin`, unless the
   operator sets an explicit acknowledgement (R9). Mirrored comments are public anyway, but
   `considered_rev`, roles and the outbox linkage are internal.
2. Nothing in a thread document is ever an outbound payload. The supervised sender (D2)
   renders only an explicit communication record, passed through the `bh-o2mjz` sanitiser and
   `scrub()` (E18). The renderer has no read path into thread documents beyond quoting a body
   that a communication record deliberately selects.
3. The marker carries no `bh-*` id. `hive` is the repository identity (E8), `install` is a
   UUID, and **`outbox_id` must be minted opaque** (a random or hash id), never a bead id or a
   value derived from one. A contract test asserts that an encoded marker matches no bead-id
   pattern of the fleet's prefixes.
4. Content fed to inference, such as a triage skill reading thread bodies, passes the same
   sanitiser (`bh-o2mjz`). A comment that a reporter posted and then deleted may have been a
   mistake; see R6 on purging.

### R5. Checkpoint and revision semantics

The ingest step is a pure function, `apply(thread, page, ctx) -> (thread', changed)`, followed
by one durable write. `ctx` carries the outbox read and the known-actor set (R7).

1. **Upsert key.** A record is matched on `CommentKey.id`. A `global_id` that differs from the
   stored one is stored and flagged, never used to split or merge records.
2. **Revision rule.** For an observed `(tracker_revision r, digest d)` against the stored latest
   `(r0, d0)`:
   - if `r < r0`, the observation is stale: ignore it (no change);
   - if `r == r0` and `d == d0`, it is a boundary repeat: ignore it (no change);
   - if `r > r0` and `d == d0`, the timestamp advanced with the same content: update
     `tracker_revision` in place, with no new `rev`;
   - if `d != d0` and `r >= r0`, it is a new content state: append `rev = rev0 + 1`. The case
     `r == r0` is a coarse-clock tie (Jira minute resolution), and the later read wins, because
     every read returns the tracker's current state and one writer applies pages in order.
   An author change on an existing id is recorded and flagged; it is not expected.
3. **Sequence.** Every applied change bumps `thread.seq` once. `first_seen_seq`,
   `last_seen_seq`, `seen_seq` and `tombstone_seq` are values of that counter, so ordering is
   always by local integer, never by a host time.
4. **Checkpoint advance.** The new checkpoint is written in the **same** document write as the
   records of its page and every earlier page. If a page carries `checkpoint=None` (E4), its
   records may be written, but the stored checkpoint stays where it was. A crash there re-reads
   from the old checkpoint, and rules 1 and 2 make the replay a no-op. A failed write leaves
   the checkpoint unadvanced by construction.
5. **Scope.** A stored checkpoint records its scope (`issue` with the thread URL, or `project`)
   and is only ever passed back to a query of the same scope. Tokens are never compared or
   merged, because they are opaque (E3).
6. **Closed issues.** A closed issue's thread is read and checkpointed like an open one.
   Closing changes nothing in the model.

### R6. Deletion and tombstone semantics

1. **Only complete snapshots count.** A reconcile pass records `snapshot_start_seq =
   thread.seq` and collects `snapshot_ids(IdQuery(COMMENTS, issue=K))`. With `complete=False`
   the pass changes nothing.
2. **Candidates.** A `live` record is a deletion candidate only if its id is absent from the
   snapshot **and** `first_seen_seq <= snapshot_start_seq`, so a record ingested after the
   snapshot began is never a candidate. Own records with no confirmed remote id are never
   candidates.
3. **Confirmations.** Each complete snapshot that confirms the absence increments
   `absent_confirmations`. Each one that contains the id resets it to 0. When the count reaches
   the configured threshold (R9, default **2**), the record becomes `tombstoned` with
   `tombstone_seq`. The default is 2 because GitHub listings use page-number pagination (E5): a
   deletion earlier in the list during paging can shift one live comment past a page boundary,
   so it looks absent once. Two independent complete snapshots must agree.
4. **Tombstone content.** The key, author, role, the revision digests and `considered_rev` are
   kept. The latest `body` is **purged** by default (configurable), because a reporter's delete
   is often a retraction. Purging removes the body from the current document only. Under the
   archive rule (no `bd compact` or `bd flatten`) earlier versions stay in the store's history.
   The design must say so plainly and not present purging as erasure.
5. **Resurrection.** A tombstoned id seen again, in any read or snapshot, returns to `live`.
   Its `rev` is unchanged unless the content differs (R5 rule 2), and a `resurrected` count is
   kept. A tombstone is never final, so a false deletion costs one flip and loses nothing.
6. **Thread presence.** A complete `IdQuery(ISSUES)` snapshot that lacks the thread's URL,
   confirmed twice in the same way, marks `thread.state = missing`. The report bead is **not**
   closed or re-triaged automatically. A missing thread is surfaced to triage, because a
   transfer, a deletion or a URL change (ADR O3) each need a human decision.

### R7. Own-message recognition

The model classifies; the port only supplies evidence (E6). The inputs are the record's
`author`, `via` and `marker`, plus a read-only outbox view: `receipt_ids()` (remote comment ids
returned by a `WriteReceipt`), `pending(outbox_id)` (records whose outcome is unknown) and
`known_actors()` (the current `whoami()` plus every actor recorded on an outbox receipt).
`self_marker` means `marker.hive` and `marker.install` equal this hive's identity and install.

| Order | Evidence | Role and evidence tag | Side effect |
|---|---|---|---|
| 1 | `key.id` is in `receipt_ids()` | `own` / `receipt` | none |
| 2 | `self_marker`, `pending(marker.outbox_id)` exists, and the author is in `known_actors()` | `own` / `reconciled` | binds the remote id to that outbox record. This is the `WriteOutcomeUnknown` recovery, and the sender must never post that record again |
| 3 | a configured integration id equals `via.id` and the author kind is bot | `own` / `integration` | none (deferred until App support lands) |
| 4 | `self_marker` and the author is in `known_actors()` | `own` / `actor+marker` | flagged `unledgered` when no outbox record matches |
| 5 | a `bh:v1` marker naming another hive or install | `peer` | none |
| 6 | anything else, including a known actor without a marker and a self marker from an unknown actor | `external` | none |

Row 6 is deliberate on both sides. A human on a shared token is not "own" (`bh-mqvdq`), and a
human who pastes the marker cannot suppress their own comment from triage, because the marker
alone is never sufficient. Own records stay in the thread for context but never count as new
evidence. An own record whose `rev` later increases (someone edited Beadhive's comment on the
tracker) keeps `role = own` and is flagged `edited_externally` for triage.

Roles are re-derivable from stored evidence, so a re-classification after the outbox ledger
grows is a pure recomputation and never needs a tracker read.

### R8. Offline, clock-free test plan

Every case runs against `MemoryTracker` and `MemoryWorld` (E9), with explicit `at=` tracker
timestamps, and against an in-memory fake of the one-key metadata store. No case reads the
host clock, sleeps or opens a socket. Ordering assertions are on `seq`, `rev` and tracker
revisions only.

| # | Case | Asserts |
|---|---|---|
| T1 | First ingest of a thread with three comments | three `live` records at `rev 1`; the checkpoint is stored in the same single write |
| T2 | Re-run with the same `since` (an inclusive boundary repeat) | no write at all; `seq` unchanged |
| T3 | `edit_comment` then ingest | `rev 2` appended; `considered_rev` stays 1; the record is reported as new evidence |
| T4 | Equal tracker revision with a different body (coarse clock) | a new `rev`; a later identical read is a no-op |
| T5 | A stale observation (`r < r0`) | ignored |
| T6 | Injected store-write failure after fetch | the checkpoint is unchanged; a re-run converges to the same document as an uninterrupted run |
| T7 | Per-issue pages with `checkpoint=None` until the last page, crash mid-thread | the stored checkpoint is not advanced; the replay is idempotent |
| T8 | `delete_comment` then one complete snapshot | `absent_confirmations = 1`, still `live` |
| T9 | A second complete snapshot | `tombstoned`, body purged, digests kept |
| T10 | Snapshot with a scripted `TrackerError` mid-pagination | `complete=False`; no change |
| T11 | A record first seen after `snapshot_start_seq` | never a deletion candidate |
| T12 | A tombstoned id reappears | `live` again, `resurrected = 1`, `rev` unchanged |
| T13 | Own-recognition matrix (rows 1 to 6 of R7, including a pasted marker from an unknown actor) | the expected role and evidence tag for each row |
| T14 | `fail_next_write_ambiguously()` on a supervised post, then ingest | row 2 binds the comment to the pending outbox record; `write_requests()` shows exactly one post |
| T15 | Comments on a closed issue | ingested like an open issue's |
| T16 | An issue URL absent from two complete issue snapshots | `thread.state = missing`; the report bead's status and `intake` are untouched |
| T17 | A project-scope token offered to an issue-scope query | refused before any read |
| T18 | Isolation | `write_requests() == 0` across every ingest case; the public-store fixture is byte-identical; other metadata keys on the report bead survive a write |
| T19 | Marker privacy | an encoded marker matches no fleet bead-id pattern |

### R9. Configuration (proposals; off by default)

These sit under the external-intake namespace that the module-boundary and plugin work settle.
The spelling is not decided here; the defaults are.

| Proposed key | Default | Meaning |
|---|---|---|
| `comments.enabled` | `false` | Ingest threads at all (also needs the adapter's `read.comments.enabled`) |
| `comments.deletion_confirmations` | `2` | Complete snapshots that must agree before a tombstone |
| `comments.purge_deleted_bodies` | `true` | Drop the latest body on tombstone |
| `comments.max_body_chars` | none | Optional per-record body cap (digest kept) |
| `comments.reconcile_every` | operator-chosen; no schedule by default | Cadence of the full id pass, counted in ingest passes, not in time |
| `privacy.allow_published_operational_store` | `false` | Explicit acknowledgement required to enable intake on a single-store or `git-origin` hive (R4) |

No key holds a token, a grant or an outbox id. The whole feature is inert until
`comments.enabled` and the adapter's read flag are both on, and it never writes to the tracker
by itself.

### R10. Follow-up work for the implementation molecule (not filed here)

1. A domain bead with the pure `apply` and `reconcile` functions, the record schema, R5 to R7
   and T1 to T13, T15 to T17 and T19. It depends only on `beadhive.modules.tracker.contracts`
   and the testing kit.
2. A store-adapter bead: the one-key metadata read-modify-write through the bead-store port,
   the single-writer lease with the `seq` re-check, and T6 and T18. It also covers the
   enable-time privacy refusal (R4).
3. An outbox-integration bead, after the communication-record spike: the read interface of R7,
   the row 2 binding, and T14. It also enforces the opaque `outbox_id` rule.
4. Open items for the module-boundary work: the home of the per-binding sweep checkpoint
   (R4), and whether triage marks `considered_rev` through a module verb (not "promote", D3)
   or as a side effect of a disposition.
5. Unverified here: the Linear and Jira rows (documentation only); whether GitHub comment
   listings are offset-paged under concurrent deletes, which R6's default of 2 assumes
   pessimistically; and the metadata growth figures beyond one embedded, ungarbage-collected
   measurement (E13). The first store-adapter bead should re-measure on a shared-server store
   with a realistic thread.
