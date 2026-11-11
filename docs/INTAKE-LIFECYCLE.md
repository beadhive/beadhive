# Report lifecycle and end states

A bound report is an internal bead that mirrors one external (GitHub) issue. This page states, once,
which combinations of its dimensions are legal, how each triage disposition ends, and which
combinations are **invalid** and what finds and repairs them. It is the user-facing form of the
rules in [the external-intake ADR](design/external-intake-module-adr.md) (section 7 dimensions,
section 8 the hold gate and the close rule, section 10 the outbox state machine). Where this page
and the code disagree, the code wins and this page is the bug.

Everything here assumes the outbox is on (`intake.external.outbox.enabled`) unless a row says
otherwise. Nothing on this page is sent by Beadhive: every reply, label change and close is drafted
and goes out only after an operator approves and sends it through `bh work outbox` /
`bh work labels` (see [CONFIGURATION](CONFIGURATION.md#intake-external)).

## The dimensions

A report's state is the product of the dimensions below. Each has one writer
(the ADR's section 7); none is derived from another except where stated.

| Dimension | Stored as | Values | Written by |
|---|---|---|---|
| Bead status | the bead's native status | `open`, `in_progress`, `closed` | the held close (`ReportStore.close`) is the only way a bound report closes |
| `intake` | `intake:<value>` label cache | `untriaged`, `needs-info` (non-terminal); `accepted`, `rejected`, `rerouted`, `promoted` (the disposition that fielded it) | the triage verbs |
| `origin` | `origin:<channel>` | `report`, `github`, `import`, `escalation`, `factory-seed`, `backfill` | import / filing; **provenance only**, see [Known issues](#known-issues) |
| `delivery` | `delivery:<value>` | `pending` (linked, not shipped), `merged`, `released`, `reverted`; absent when never linked | link, the merge trigger, the release trigger, the correction verb |
| `notified` | `notified:<value>` | `replied`, `merged`, `released`, `corrected`, `started`, `blocked`, `deprioritised` | the outbox sender, only after a comment reaches `sent` |
| Hold gate | an open `intake-hold:` gate on the bead | open / resolved | import opens it; only the held close resolves it |
| Bound issue | on the tracker | `open` / `closed`, its labels (`bh:` prefix), its comments | only operator-approved sends change it |

Two more things are not dimensions but decide the end state: the report's **outbox**
(`intake.outbox`: per-operation states `drafted` ... `sent`, `abandoned`, `superseded`, see ADR
section 10) and the **work** it tracks (a `tracks` edge to a work bead, whose merge and release
fire the delivery milestones).

## Valid non-terminal states

A bound report that is not yet finished is always one of these:

| State | bead status | `intake` | `delivery` | hold gate | bound issue |
|---|---|---|---|---|---|
| Imported | open | `untriaged` | absent | open | open |
| Waiting for the reporter | open | `needs-info` | absent | open | open (a drafted or sent question) |
| Accepted to the backlog, untracked | open | `accepted` | absent | open | open |
| Promoted to the planner | open | `promoted` | absent | open | open |
| Tracked, work not shipped | open | `accepted` | `pending` | open | open |
| Tracked, merged (`close-on-release` only) | open | `accepted` | `merged` | open | open |
| Completion drafted, waiting for its reply | open | `accepted` | `merged` or `released` | open | open |

A bound report is never in `bd ready` and cannot be claimed (the hold gate, ADR invariant H), so
dispatch never picks it up as work.

### Invariants

1. **An open bound report holds its gate.** Open, bound (carries `intake.source`) and no open
   `intake-hold:` gate is invalid (`missing`).
2. **A closed report holds no gate.** A closed report with an open hold is invalid (`unresolved`);
   harmless, because a closed bead is never ready.
3. **A closed bound report has its issue closed, or a drafted close on its way.** A closed report
   whose issue is still open must hold an unsent close operation (or a reply that carries one)
   that an operator can still approve and send. Closed, issue open, and nothing drafted is the
   invalid state behind the 0.35.9 gap (bh-cir2q).
4. **A report tracked onto work closes only through the completion path.** Under `close-on-merge`
   (default) that is `delivery:merged`, under `close-on-release` it is `delivery:released`; either
   way the milestone record's comment must be `sent`, `abandoned`, or `superseded` by a settled
   record first. The report never closes at `pending`.
5. **A rejected report has no delivery of its own.** `intake:rejected` (a `duplicate-of` ends in
   one) with a `delivery` value is a stray (bh-5kpwc); a duplicate's delivery is its canonical's.
6. **An issue's owned labels match the report's stored state.** After a closed report's issue
   settles, its `bh:` labels carry the final `intake`, `delivery` and `notified` values, and no
   `progress` once delivery is `merged` or `released`.
7. **Replies and closes are drafted, not sent.** Beadhive never writes to the tracker on its own;
   "required reply" below means "a record exists, drafted and held for review".

## Completion: how a tracked report ends

When tracked work merges (`bh work merge`) or is released (`bh work delivery release <tag>`), the
milestone trigger sets `delivery:merged` or `delivery:released` on every report that tracks it
(open or closed) and then evaluates the **close rule** (`domain/outbox.py` `close_rule`):

1. The delivery reached the completion milestone (merged for `close-on-merge`, released for
   `close-on-release`).
2. With the outbox on, the completion milestone's record **always** drafts, whatever
   `milestones.draft_on_merge` / `draft_on_release` say (those two now only govern progress
   notices), and it carries an `issue.close` with reason `completed` while the tracker enables
   `issue.close` (`plugins.<id>.write.issue_close.enabled`). With that write off, the record is
   comment-only and the issue is left open by design.
3. The report closes locally only when the **latest** record of that event has its comment `sent`,
   `abandoned` or `superseded`. A superseded comment counts only when the record that superseded
   it is itself settled: a `released` record that replaced an unsent `merged` one hands the wait
   (and the close) to the `released` record. An operator's deliberate supersede with no successor
   is settled.
4. With the outbox off nothing is drafted and the report closes at once.

The send order for the issue is comment, labels, then the close, and the close is refused
(`close_not_ready`) until the report's comments and its latest labels record are settled. So the
**local close precedes the issue close**: between them the report is closed, its issue open, and
a close is drafted or approved but unsent. That window is valid (invariant 3). If the operator
abandons or discards the comment, the report closes locally with `notification_behind` true and
the issue stays open until the operator drafts something else (see
[Invalid states](#invalid-states-and-their-repairs)).

## Dispositions and end states

"Local close" is when the bound bead goes to `closed`. "Reply" is the outbox record that must exist
for the reporter to be told. "Issue close" is the `issue.close` reason that rides on that reply
(`completed`, `not_planned`, `duplicate`) or none. "Final labels" are the owned labels the label
projection leaves on the issue (`bh work labels sync`, `labels.on_transition`).

| Disposition | Local close | Required reply | Issue close | Final labels |
|---|---|---|---|---|
| `reject` (via `triage apply`) | at once, `intake:rejected`; the bead's close reason is the reporter-visible reason | drafted **before** any other verb; the reason is its text | `not_planned`, on the reply | `intake:rejected`; no `delivery`, no `progress` |
| `reject` (plain `bh work reject --reason`) | at once, same | **none drafted** by the verb; draft one with `bh work outbox draft` | none | `intake:rejected` |
| `duplicate-of` | at once, linked to the canonical's work then rejected; `intake:rejected` | drafted first, naming the canonical (`duplicate_text`, or the reviewed text); with no outbox bound it is linked and rejected with **no reply and no close** | `duplicate`, on the reply, and the close names the canonical's issue | `intake:rejected`; no `delivery`, no `progress` |
| `fixed`, no owning work | at once, as a reject (`intake:rejected`) | drafted first; the reviewed text, else the fix-version wording computed from the tags | `completed`, on the reply | `intake:rejected`; no `delivery`, no `progress` |
| `fixed`, owning work | the accept links the report to the work; it closes through the completion path below, not at apply | **none at apply today** (the proposal's `reporter_text` is dropped, bh-1hkjm); planned: draft the reply after the accept with no close, and let completion draft the closing record once the work has merged or released | `completed`, on the completion record (that path is live today; the apply-time reply is the planned part) | `intake:accepted`, then `delivery:merged` / `released` and the completion `notified` value |
| `tracked`, `partially-fixed` (with owner) | not at apply: the accept links the report to the owner work (`delivery:pending`); closes at completion | drafted **after** the accept when the proposal carries `reporter_text` and the outbox is on, **no close** (bh-ac6tz); the completion record is drafted at merge / release | `completed`, on the completion record only | `intake:accepted`; `delivery:pending` then `merged` / `released`; `progress` while the work runs; none after delivery |
| `new`, `partially-fixed` (no owner) | not at apply: accept creates the work (`accept --new-work`) and the report tracks it; closes at completion | as `tracked` | as `tracked` | as `tracked` |
| `needs-info` | never; the report waits, held, `intake:needs-info` | the question, drafted as a supervised reply | never | `intake:needs-info` |
| `reroute --to <hive>` | at once, `intake:rerouted`, after the report is re-filed in the target hive | **none**; the verb does not touch the bound issue | none | `intake:rerouted` |
| `reroute --super <seat>` | never; reassigned, stays untriaged in the fleet inbox | none | none | `intake:untriaged` |
| `promote` | never at promote; the report waits for the planner (`intake:promoted`) and is closed, once adopted under an epic, by the epic's landing (`bh work finish`) as a plain bead close. A BOUND report is never adopted child-of (the planner links it `tracks`), and the landing refuses to plain-close one that carries a child-of edge anyway (bh-p28yo); it closes only through the intake close path | **none**; no reply or close is drafted at promotion or at the epic's landing | none | `intake:promoted` |
| `decision` | listed, never applied | none | none | unchanged |
| a needs-info report that never gets a reply | never closes by itself; there is **no timeout** | none | none | `intake:needs-info` |
| `accept` (plain, no work) | not by intake: the report stays open and held as a backlog item; nothing closes it at accept | none | none | `intake:accepted` |

Notes on the table.

- **`reject`, `duplicate-of` and a `fixed` with no owner close the report at once**, before their
  reply is sent. The reply carries the issue close, so the issue stays open until an operator
  approves and sends the reply, the labels and the close (invariant 3). `bh work outbox
  backfill-close` repairs a report closed before the close was drafted.
- **A `duplicate-of` is not an owner.** It links to the canonical's work only so the canonical's
  merge needs no second report trigger; it has no delivery of its own and drafts no milestones
  (`delivery_unowned`).
- **Completion needs the report to still track its work.** An unlinked report (`bh work link
  --remove`) never closes through completion, and `backfill-close` skips it.
- **A `released` record supersedes an unsent `merged` one in the same group.** Under
  `close-on-merge` the `released` record carries the close the superseded `merged` record held, so
  the close is not lost.
- Which tracker label names appear on the issue depends on `labels.prefix` and any alias; the
  table names the dimension values.

## Invalid states and their repairs

A report is invalid when it breaks one of the invariants above. The table lists each state, what
finds it, and what repairs it. "No check" means nothing in `bh doctor` or `bh work triage check`
looks for it today.

| Invalid state | Found by | Repaired by |
|---|---|---|
| Open bound report without an open hold (`missing`) | `bh doctor` section `intake_holds` | the exact `bh -r <hive> bd gate create --blocks <report> ...` command the finding prints; `IntakeService.sweep_holds(repair=True)` does the same |
| Open hold on a closed report (`unresolved`) | `bh doctor` `intake_holds` | the printed `bh -r <hive> bd gate resolve <gate>` |
| Hold that blocks no bound report (`orphaned`) | `bh doctor` `intake_holds` | inspect with the printed `bd show <gate>`, resolve by hand if nothing needs it |
| Closed report with an open issue and no drafted close, at delivery `merged` / `released` (the 0.35.9 gap, bh-cir2q, closed by bh-iyog8) | `bh doctor` section `intake_reports` (`closed_open_issue`, bh-cd03d); `bh work outbox backfill-close --dry-run` lists it too | `bh work outbox backfill-close`: attaches the completion close (`completed`) to the latest live reply or milestone record, or drafts the completion record with the close; then approve and send |
| Closed report (`reject`, `duplicate-of`, `fixed` no owner, applied proposal) with an open issue and no drafted close | `bh doctor` `intake_reports` (`closed_open_issue`); `bh work outbox backfill-close --dry-run` | `bh work outbox backfill-close`; drafts the missing reply too from the reviewed text |
| Closed report with the issue still open because the operator abandoned or discarded the completion comment (`notification_behind`) | `bh doctor` `intake_reports` (`notification_behind`, bh-c0x0i): lists the report with its delivery, notified and the abandoned record id; a report whose issue a SENT close closed (told by hand) is not listed | draft the reply yourself (`bh work outbox draft`), then send; `backfill-close` also attaches a close to a live reply; or leave it when the silence was deliberate |
| Closed report whose issue carries stale `delivery` / `progress` labels | `bh work labels sync --dry-run` | `bh work labels sync` (closed reports whose issue is still open are included without `--include-closed`, bh-vojht; a closed issue still needs `--include-closed`) |
| Rejected report that still carries a `delivery` value (a stray) | `bh doctor` `intake_reports` (`stray_delivery`, bh-qwv0t); `bh work delivery sweep` (a dry run) | `bh work delivery sweep --repair` (bead store only, nothing sent), then `bh work labels sync --include-closed` drafts the label removal on the issue |
| Report left open on `intake:rejected` by a failed close | the next `triage apply` or `bh work reject` | those verbs finish the close |
| Closed report with `delivery:pending`, or tracked work that never shipped | none (the work is simply unfinished) | not a bug: finish or unlink the work |
| Reply drafted for a `fixed`-with-owner proposal | none (it is dropped, bh-1hkjm) | file/apply once bh-1hkjm lands; until then draft the reply with `bh work outbox draft` |
| Promoted or rerouted bound report whose issue gets no reply or close | no check | `bh work outbox draft` by hand; no automatic path |
| `origin:` label on accepted-in-place work treated as an adopted report | no check | bh-l1m3p (known issue) |

## Known issues

- **bh-l1m3p**: the `origin:` label means both provenance (the intake channel) and, to
  `adopt.is_origin_report`, "adopted origin report under an epic, not work". A plain-accepted
  report that is later linked under an epic is misclassified as non-work. Treat `origin` as
  provenance only until it is fixed.
- **bh-1hkjm**: `triage apply` ignores `reporter_text` for a `fixed` proposal that names an owning
  work. Operator decision (2026-11-11): its reply is drafted without a close after the accept, and
  the completion path (above) drafts the closing record once the owning work has merged or
  released. Planned; not implemented.
- **`bh doctor` reads end states, not intent.** Section `intake_reports` lists a closed report whose
  issue is still open, a closed report whose completion comment was abandoned, and a stray
  `delivery` on a rejected report, all read-only (one issue listing per run, none when no closed
  report is bound; silent while intake, the outbox or the tracker plugin is off). A closed report
  whose close is merely drafted or approved is listed too, naming `outbox send`: it is the valid
  window of invariant 3 until the close goes out. `bh work triage check` is unrelated: it runs the
  deterministic pre-apply checks of open proposals (targets, fix claims, families, field
  consistency, reporter text, mentions, staleness) and never inspects a report's end state.
- **No timeout for `needs-info`.** A report waiting for its reporter stays open and held until a
  reply wakes it (`bh work wake`) or an operator rejects, rerouts or accepts it.

## See also

- [External-intake ADR](design/external-intake-module-adr.md): sections 7, 8 (invariant H, the hold
  gate, the close rule, the completion policy) and 10 (the outbox state machine).
- [CONFIGURATION: `intake.external`](CONFIGURATION.md#intake-external): the keys behind each row.
- [WORK: intake and the outbox](WORK.md#intake-and-the-outbox).
- [MODULES: `modules/intake`](MODULES.md#modulesintake).
