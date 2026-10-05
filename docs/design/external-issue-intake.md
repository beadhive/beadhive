# External issue intake, triage, and human communication

**Status:** Initial design ideas, 2026-10-04; split from [Bead groups](BEAD_GROUPS.md) on
2026-10-05. This document proposes a workflow and skill boundaries; it does not add commands,
state vocabulary, comment synchronization, or automation.

**Reader:** A contributor designing tracker-backed intake. After reading, they should be able
to trace an external report from ingestion through triage, implementation milestones, and
public replies, and identify which guards from the bead-groups design each step relies on.

The [report-channel proposal](../REPORT-CHANNEL.md) describes where a report can be filed.
The existing `bh report` and `bh work intake|accept|reject|reroute|promote` surfaces provide
a common intake queue and dispositions. External trackers should feed that same model,
with an additional responsibility: a human reporter must be able to follow the outcome
in their original issue thread without access to the private bead store.

## 1. Reports, work, and conversations are related but distinct

An external report has a verified tracker binding, reporter identity, original submission,
and conversation history. Its bead may also be the implementation work item when the
relationship is one-to-one. When several reports concern the same defect, retain each
report's binding and link them to one canonical work bead. That work may be private core,
already tracker-linked, or newly created during triage. Do not transfer a duplicate's
`external_ref` onto the canonical bead or publish private core merely to explain the link.

This distinction lets GitHub and Linear reports converge on the same work without requiring
multiple native `external_ref` values on one bead. Each report remains its own connector
record, with relations to the work it describes. A proposed subscription relation records
which report threads should receive subsequent work milestones, even if a duplicate report
has already been closed. Cross-group relations must respect each destination's publication
policy; a private canonical bead is not itself a public link.

Triage disposition, execution status, release inclusion, and communication delivery should
remain separate dimensions. Existing intake values are `untriaged`, `accepted`, `rejected`,
`rerouted`, and `promoted`; waiting for information, reproduction outcomes, duplicates, and
merged-but-unreleased are proposed extensions or additional dimensions. They need registered
vocabulary and readiness rules before use. A comment saying "needs reproduction" must not
leave the implementation bead accidentally eligible for ordinary implementation dispatch.

## 2. Ingestion through polling or events

Each connector binding declares its destination, target group, intake policy, poll cadence
or event source, and read/write capabilities. Begin with scheduled pull-only discovery;
add event-driven wakeups using GitHub's `issues` and `issue_comment` webhooks. Merge and
release detection can consume `pull_request` and `release` events. GitHub exposes a delivery
identifier and signature headers for validating and identifying webhook deliveries.
See [GitHub webhook documentation][github-events].

Both triggers should invoke the same ingestion operation. Record the external identity,
source revision, timestamps, and reporter separately from the service actor performing the
import. Deduplicate on connector instance plus stable external object identity, preserve
the binding through repository renames or issue transfers, and route changes of destination
through explicit reconciliation. A redelivery or repeated poll updates the existing report.
Webhook handlers durably record receipt before acknowledging; they enqueue reconciliation
instead of running a full agent triage session inline.

New reports enter the shared untriaged queue with their connector provenance. Existing
reports update their source snapshot and conversation, scheduling another triage pass when
new evidence, edits, or reopening change the decision. A routine poll must not reset every
accepted report to untriaged. Periodic reconciliation still runs when webhooks are enabled,
so a missed event does not leave the bead permanently stale. Polling must include relevant
closed reports and comment changes, with pagination and checkpoints advanced only after
durable ingestion.

Thread replies are first-class input. Fetching issue title/body/state alone does not collect
answers in comments. Preserve comment identity, author, revisions, and deletions so the
triage skill can distinguish new evidence from already-considered material. Exclude pull
requests from issue intake and recognize our own outbound messages to avoid feedback loops.

## 3. Triage and evidence gathering

The triage skill uses the following decision sequence. A report retains a reasoned local
record at every disposition; public communication is a separately selected projection.

| Stage | Local decision and record | Human-facing result |
|---|---|---|
| Duplicate review | Search existing open and resolved work, including affected versions and prior fixes. Existing duplicate detection supplies candidates; similarity alone does not authorize closure. Confirm the same underlying defect, retain new evidence, and link to canonical work. | Explain the duplicate decision, link to an accessible public issue when one exists, and close this report in favor of that work. If the canonical work is private, give a permitted explanation without private IDs or links. |
| Information sufficiency | Evaluate the report against a versioned template for its type: observed/expected behavior, affected version, environment, steps or evidence, and impact as applicable. Require information needed to decide, rather than every field mechanically. | Ask for the specific missing facts and an actionable way to provide them. Keep the report waiting for a reply; explain what investigation is blocked. |
| Reproduction decision | Schedule a distinct reproduction task when behavior or scope needs verification. Known defects, credible evidence, or an agreed small fix can proceed directly with the reason recorded. Acceptance without reproduction must not be described as a successful reproduction. | Acknowledge what is understood and whether reproduction is pending or unnecessary. |
| Investigation result | Record verified reproduction, affected and unaffected versions, feature/interface impact, severity, possible solutions, and unresolved uncertainty. A failed attempt distinguishes insufficient evidence, environment mismatch, already-fixed behavior, and a disproven claim. | Summarize findings and any further request. Explain a rejection or existing-version fix when justified; otherwise keep uncertainty explicit. |
| Backlog disposition | Accept or promote the work with priority and rationale, dependencies, scheduling constraints, and an owner where available. Keep reproduction or missing evidence as an explicit readiness gate when still needed. | Confirm it has been reviewed, give its backlog/blocked status and priority rationale, and say what happens next. Give an ETA only when supported by an actual commitment. |

The reproduction task should produce reusable evidence: minimal steps or a regression test,
the tested commit/version and environment, observed versus expected behavior, and a bounded
impact assessment. Run submitted examples in an appropriate isolated checkout and retain
only shareable excerpts in public messages. Feature requests and straightforward corrections
may need design assessment rather than reproduction testing.

A human response wakes the waiting report for assessment against the outstanding questions.
It does not automatically make the issue ready. A clarified report can also reveal a
duplicate or a broader problem requiring separate work. If the reporter is silent, reminder
and inactivity closure behavior should be explicit hive policy, with a reopening path;
do not silently interpret lack of response as proof that the defect does not exist.

Duplicate closure must not imply the underlying fix is complete. If canonical work was
already closed, check whether the new report is about an old affected release, a regression,
or a distinct defect before closing it. Rerouting similarly requires a public explanation
and an accessible destination where permitted, rather than only a private hive relation.

## 4. Implementation, merge, and release milestones

Once work actually starts, record the execution transition and send an in-progress update
to linked report threads. Claims, abandoned attempts, and retries need not each generate a
message; publish meaningful changes in what the reporter can expect. If work becomes blocked
or is deprioritized, communicate the material change and reason instead of leaving a stale
in-progress impression.

Merge detection records the actual commit on canonical main, its associated PR when present,
and the resolved work relations. Use the final merge, squash, or rebased commit identity,
not only a branch-head hash. The public update links accessible evidence and explicitly says
the fix is on main and whether it has been released. A merged PR by itself is not evidence
that a release contains the fix.

A hive should choose a completion policy, with per-type overrides if needed:

| Policy | At merge | At release |
|---|---|---|
| Close on merge | Close implementation work and its active report projections; explain that the fix is on main and may be unreleased. | Optionally append the released version and upgrade guidance to the closed reports. |
| Close on release | Mark implementation complete and delivery `merged/unreleased`; retain an open report or delivery-tracking bead without making completed implementation dispatchable. | Record verified release inclusion, close the remaining delivery/report work, and announce the version. |

These are proposed policies, not additional native Beads statuses. The second policy needs
separate execution and delivery readiness; a label alone is insufficient. If selected,
PR closing keywords and tracker state mapping must not close the public report prematurely.
Canonical work and report projections can have different completion states, especially when
duplicate reports were closed earlier.

Release association should identify which published version contains the fix and any
relevant backports. A version bump or release event triggers verification against the
release commit/artifact; it is not automatic proof of inclusion. Preserve separate merge
and release facts so a revert, failed release, or regression can reopen the appropriate
work and send a correction. Availability expectations differ for libraries, applications,
and deployed services; the hive declares which delivery milestone matters to its users.

Ordinary backlog updates can say that no timeline is committed. Security-specific deadlines
and disclosure communications would require a future explicit policy and private channel;
they should not be inferred from priority or posted by the ordinary public flow.

## 5. Public projection and reliable communication

A proposed communication record contains a target binding, semantic event, source revision,
public message, permitted label/state changes, and delivery state. Store the reviewed response
as a bead comment or attached communication record with explicit publication intent. Keep
ordinary bead comments, internal notes, and agent transcripts private by default.
Explicitly select public facts, comments, and labels;
being tracker-linked does not authorize publishing every field on that bead. Duplicate and
dependency explanations must apply the same rule to referenced beads.

The reporter owns the original submission and human replies. Beadhive owns its published
comments and configured status/label projection. Preserve the source report separately from
internal analysis; update a marked maintainer summary only if that is the chosen interface.
Do not overwrite the reporter's description to turn it into an evolving internal plan.
Conflicting human label/state edits need field ownership and reconciliation policy, rather
than letting the last broad sync arbitrarily win.

Record the local transition and durable communication intent in Beads, with a private
outbox representation and reconciliation for interrupted writes. The deterministic sender
applies the same destination, group, binding, and update-only guards [proposed for `bh bd`][guarded-sync],
plus the content projection policy. Existing issue updates and a new comment on an existing
issue are permitted operations; creating a new external issue remains explicit enrollment.
Human approval follows configured publication policy and existing contributor gates, rather
than requiring a fresh approval for each authorized routine status message.

Delivery needs retry and deduplication semantics. Use a stable identity for each binding,
semantic event, and source revision; persist remote comment IDs and delivery receipts.
After an ambiguous timeout, reconcile a message marker or receipt before posting again.
Do not promise exactly-once remote writes where the API lacks that guarantee. Only one
publisher owns a pending delivery at a time, stale queued updates are superseded when
appropriate, and partial label/state/comment updates remain pending until reconciled.
An unavailable tracker leaves the local work intact and an observable communication backlog.

Messages should state the finding, the requested action or next step, and the reason for
priority or scheduling when relevant. Prefer a short actionable response to a transcript
of agent reasoning. Avoid repeated acknowledgements on unchanged polls, invented affected
versions, and timelines derived from a priority label. A periodic digest or edited summary
can cover minor changes; durable milestone comments provide a useful human-readable history.

## 6. Proposed skill and operation boundaries

These are responsibilities for future skills and guarded operations, not newly installed
skills or a commitment to particular command names:

| Responsibility | Inputs | Durable result |
|---|---|---|
| Ingestion operation | Connector configuration, polls/events, external issue and thread revisions | Verified report binding, source snapshot, conversation records, and queued triage work. Transport and checkpointing are deterministic. |
| Triage skill | Report evidence, type-specific template, duplicate candidates, existing work | Explained disposition, missing-information request, canonical-work relations, and proposed public response. |
| Reproduction skill | Bounded investigation task, versions/environment, supplied evidence | Reproduction result, regression evidence, impact assessment, and unresolved questions. |
| Planning skill | Sufficient evidence, candidate solutions, current backlog/dependencies | Accepted/promoted work, priority rationale, readiness gates, and scheduling decision. |
| Communication skill | Approved public facts, target audience, event and hive policy | A human-readable message and permitted external state/label changes queued for publication. It does not directly run unrestricted tracker sync. |
| Publisher and milestone operations | Validated outbox records, execution/merge/release evidence | Guarded external updates, receipts, reconciliation, and new milestone intents. |

This extends the common report/triage flow across human and agent reporters. Channel choice
changes the binding and communication transport; duplicate reasoning, evidence collection,
planning, and ownership should remain shared. Agents can consume structured outcomes while
humans receive the relevant explanation in the thread they already use.

## 7. Current support and first implementation boundary

The existing report and triage modules provide the shared queue, duplicate candidates, and
basic dispositions. The v1.3.0 GitHub [mapping][github-mapping] pushes title, description,
state, and labels; the [tracker adapter][github-tracker] and [sync engine][tracker-engine]
do not transport bead comments or human comment threads. Adding a local comment and running
`bd github sync` therefore does not implement the communication workflow above. Its body
and label mapping also requires additional projection controls for a private mixed store.

GitHub provides [issue-comment APIs][github-comments] for reading, creating, and updating
thread comments. A Beadhive connector could use these for the missing conversation transport
without first waiting for native Beads comment synchronization. Such transport still needs
the destination guards, thread provenance, outbox, and field ownership described here.
Linear or other trackers would need equivalent capability-specific transports.

A useful initial slice is one GitHub binding with scheduled pull-only discovery, comment
ingestion, triage/missing-information handling, and guarded public replies. Exercise duplicate
closure without external issue creation, a human reply returning to triage, and timeout
reconciliation without duplicate messages. Then connect reproduction and planning, followed
by verified start/merge milestones and optional release tracking. Redelivery, private-note
exclusion, shared canonical work, and wrong-destination refusal should be acceptance cases
throughout. Automatic descriptor routing and unattended end-to-end publication remain
separate implementation decisions.

## Sources

- [Guarded tracker sync][guarded-sync] in the bead-groups design.
- [Tracker sync engine][tracker-engine], [GitHub reference handling][github-tracker], and
  [GitHub field mapping][github-mapping] at Beads v1.3.0 (unchanged in v1.3.1).
- GitHub [webhook events][github-events] and [issue-comment APIs][github-comments].

[guarded-sync]: BEAD_GROUPS.md#guarded-tracker-sync-through-bh-bd
[tracker-engine]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/tracker/engine.go
[github-tracker]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/github/tracker.go
[github-mapping]: https://github.com/gastownhall/beads/blob/v1.3.0/internal/github/mapping.go
[github-events]: https://docs.github.com/en/webhooks/webhook-events-and-payloads
[github-comments]: https://docs.github.com/en/rest/issues/comments
