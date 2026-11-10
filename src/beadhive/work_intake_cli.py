"""CLI handlers of the external-report verbs: ``bh work outbox``, ``thread`` and ``labels``.

They live apart from :mod:`beadhive.work` so a process running one of them imports only this
module and :mod:`beadhive.work_intake` (bh-lwz8a): :mod:`beadhive.cli_fastpath` projects these
groups from the operation catalog without the rest of the command tree. :mod:`beadhive.work`
re-exports every handler, so the full tree, the catalog projection and the historical
``work.<name>_cmd`` surface are unchanged. Keep this module's imports to ``typer`` and
``work_intake``; the import-structure test (``tests/test_cli_fastpath.py``) pins that.
"""

from __future__ import annotations

import typer

from . import work_intake

_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")
_BEAD = typer.Argument(..., metavar="<id>", help="bead id")
_AS = typer.Option("", "--as", help="dev/<name> identity (default: config/$BH_DEV/git)")

_OUTBOX_ID = typer.Argument(..., metavar="<outbox-id>", help="the record's outbox id (ob1_...)")
_OUTBOX_ID_OPT = typer.Argument("", metavar="<outbox-id>", help="the record's outbox id (ob1_...)")
_OUTBOX_KIND = typer.Option(
    "",
    "--kind",
    help="operation: comment (default), labels, close, reopen, type or fields (each its own gate)",
)
_JSON = typer.Option(False, "--json", help="emit one JSON document")
_OUTBOX_OP = typer.Option(
    ...,
    "--op",
    help="<comment|labels|close|reopen|type|fields>:<digest> reviewed in `outbox show` (full "
    "digest or 12-hex prefix); repeat to approve several, each as its own gate with its own "
    "single-use grant",
)
_OUTBOX_PUBLISH = typer.Option(
    [], "--publish", help="a finding id to publish deliberately (needs exactly one --op)"
)


def outbox_approve_cmd(
    outbox_id: str = _OUTBOX_ID,
    op: list[str] = _OUTBOX_OP,
    publish: list[str] = _OUTBOX_PUBLISH,
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Approve the digest of a dry-run render, minting one single-use grant (operator seats only;
    off unless `intake.external.outbox.enabled`). Approving sends nothing."""
    return work_intake.outbox_approve(outbox_id, op, publish, as_, hive, as_json)


def outbox_backfill_close_cmd(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="read and list what would be drafted; write nothing"
    ),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Draft the issue close a report closed before closes existed never got: for every report
    closed in this hive whose applied triage outcome was fixed, reject or duplicate-of and whose
    bound issue is still open, add a close (completed, not_planned or duplicate) to its latest
    reply; a report that never got its reply gets one drafted from the reviewed text (or apply's
    template), the close attached. One issue listing; nothing is approved or sent (approve with
    `outbox approve --op close:<digest>`, send with `outbox send <id> --kind close` after the reply
    and the labels).
    Off unless `intake.external.outbox.enabled` and the tracker plugin's
    `write.issue_close.enabled`."""
    return work_intake.outbox_backfill_close(dry_run, as_, hive, as_json)


def outbox_reopen_cmd(
    bead: str = _BEAD,
    text: str = typer.Option(
        "", "--text", help="the reply that explains the reopen (default: a short template)"
    ),
    reply_file: str = typer.Option(
        "", "--reply-file", help="read the reply text from this file (instead of --text)"
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="make every check and show the planned reopen and reply; write nothing",
    ),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Draft the reopen of ONE report whose bound issue reads closed (closed by anyone), with the
    reply that explains it. Nothing is approved or sent: review with `outbox show`, approve with
    `outbox approve --op reopen:<digest> --op comment:<digest>`, then `outbox send-approved` (the
    reopen goes before its reply; only that batch guarantees the order across records). Refused
    when a reopen for the issue is already pending. Off unless `intake.external.outbox.enabled`
    and the tracker plugin's `write.issue_reopen.enabled`."""
    return work_intake.outbox_reopen(bead, text, reply_file, dry_run, as_, hive, as_json)


def outbox_discard_cmd(
    outbox_id: str = _OUTBOX_ID,
    reason: str = typer.Option(..., "--reason", help="why the operation is abandoned"),
    accept_unknown: bool = typer.Option(
        False, "--accept-unknown", help="also abandon an operation whose outcome is unknown"
    ),
    kind: str = typer.Option(
        "", "--kind", help="only this operation: comment, labels, close, reopen, type or fields"
    ),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Abandon an unsent operation of a record (an unknown one needs `--accept-unknown`)."""
    return work_intake.outbox_discard(outbox_id, reason, accept_unknown, kind, as_, hive, as_json)


def outbox_draft_cmd(
    bead: str = _BEAD,
    text: str = typer.Option(..., "--text", help="the reply text to post to the report's issue"),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Draft an operator reply to a bound report's issue. Nothing is sent: review it with
    `outbox show`, then `outbox approve` and `outbox send`."""
    return work_intake.outbox_draft(bead, text, as_, hive, as_json)


def outbox_reconcile_cmd(
    outbox_id: str = _OUTBOX_ID_OPT,
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Settle operations whose outcome is unknown by reading the issue back (one record, or every
    report with an outbox). Never sends."""
    return work_intake.outbox_reconcile(outbox_id, as_, hive, as_json)


def outbox_retry_cmd(
    outbox_id: str = _OUTBOX_ID,
    kind: str = _OUTBOX_KIND,
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Return a failed operation to drafted; it needs a new render, digest and approval."""
    return work_intake.outbox_retry(outbox_id, kind, as_, hive, as_json)


def outbox_send_cmd(
    outbox_id: str = _OUTBOX_ID,
    kind: str = _OUTBOX_KIND,
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Send one approved operation (write-ahead, receipt). Needs the adapter's own enable, an
    unspent grant and a clean leak check on the exact bytes; off unless
    `intake.external.outbox.enabled`. `outbox send-approved` sends every approved operation in
    one process."""
    return work_intake.outbox_send(outbox_id, kind, as_, hive, as_json)


_SEND_KIND = typer.Option(
    "",
    "--kind",
    help="only this operation: reopen, comment, labels, type, fields or close (default: all)",
)
_SEND_BEAD = typer.Option(
    [], "--bead", help="only this report's records (repeatable; default: every report)"
)


def outbox_send_approved_cmd(
    kind: str = _SEND_KIND,
    bead: list[str] = _SEND_BEAD,
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Send every approved operation in one process: per report the reopen, the comments (oldest
    first), then the labels, the type and the fields, then the close. Each still spends its own
    single-use grant and writes its own receipt, exactly as `outbox send <id>`; a refused or
    unknown one is reported and the rest still go (a close whose comment did not go out is
    refused). Exits non-zero unless every operation was sent. Off unless
    `intake.external.outbox.enabled`."""
    return work_intake.outbox_send_approved(kind, bead, as_, hive, as_json)


def outbox_show_cmd(
    outbox_id: str = _OUTBOX_ID_OPT,
    bead: str = typer.Option("", "--bead", help="list only this report's records"),
    open_only: bool = typer.Option(
        False, "--open", help="list only records with an operation not yet settled"
    ),
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """The dry-run render of one record exactly as it would be sent, with the digest to approve;
    without an id, the listing of the hive's records. Reads only; no tracker request."""
    return work_intake.outbox_show(outbox_id, bead, open_only, hive, as_json)


_THREAD_REPORT = typer.Argument(..., metavar="<report>", help="the bound report bead")


def thread_show_cmd(
    bead: str = _THREAD_REPORT,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Show a bound report's ingested issue thread: its seq, the comments that are new evidence
    since the last triage read, and anything flagged. Reads the stored thread only (no tracker
    request); off unless `intake.external.comments.enabled`."""
    return work_intake.thread_show(bead, hive, as_json)


def thread_ingest_cmd(
    bead: str = _THREAD_REPORT,
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Read a bound report's issue comments since its checkpoint into its thread, then reconcile
    deletions. Reads the tracker only (needs the tracker plugin's `read.comments.enabled`); off
    unless `intake.external.comments.enabled`."""
    return work_intake.thread_ingest(bead, as_, hive, as_json)


def thread_considered_cmd(
    bead: str = _THREAD_REPORT,
    through: int = typer.Option(
        ..., "--through", help="the thread seq that `thread show` printed when you read it"
    ),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Record that a triage pass read the thread as it stood at `--through`, so only comments
    that arrived later stay new evidence. Idempotent; never changes the report's intake state."""
    return work_intake.thread_considered(bead, through, as_, hive, as_json)


# ---- the label projection (bh work labels ...): off by default --------------------------------


def labels_sync_cmd(
    report: str = typer.Argument(
        "", metavar="<report>", help="one bound report (default: every bound report)"
    ),
    all_: bool = typer.Option(False, "--all", help="every bound report (the default)"),
    include_closed: bool = typer.Option(
        False, "--include-closed", help="also closed reports and issues (a backfill)"
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="report the diffs; write nothing"),
    prune_unmapped: bool = typer.Option(
        False,
        "--prune-unmapped",
        help="also remove owned labels the projection no longer produces (drift)",
    ),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Converge the bound issues' owned labels (under the projection prefix, `bh:` by default)
    on bead state: intake state, delivery, progress, type, priority and component, derived from
    the dimension registry. Reads the issues fresh (one listing per run) and drafts at most one
    `labels` outbox record per issue; nothing is sent until an operator approves the batch
    (`labels review`, `labels approve`). Labels outside the prefix are never touched. With the
    tracker's `write.issue_type` / `write.issue_fields` on, the same record also sets the issue
    type and the Priority field from `intake.external.labels.issue_type.map` /
    `priority_field.map` (an unmapped value is skipped with a note), reported like drift. Off
    unless `intake.external.labels.enabled`; drafting also needs `outbox.enabled` and
    `outbox.labels_enabled`."""
    return work_intake.labels_sync(
        report, all_, include_closed, dry_run, prune_unmapped, as_, hive, as_json
    )


def labels_define_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="show the plan; approve nothing"),
    prune: bool = typer.Option(
        False, "--prune", help="also delete owned labels the registry no longer produces"
    ),
    approve: str = typer.Option(
        "",
        "--approve",
        metavar="<digest>",
        help="apply the plan whose digest you reviewed (full or 12-hex prefix; operator seats)",
    ),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Define the repository's projected labels: the plan (create, update and, with `--prune`,
    delete; a label an open issue still carries is kept) and its digest. `--approve <digest>`
    from an operator seat mints one single-use grant per tracker write and applies it; a converged
    repository needs none and a re-run is harmless. Off unless `intake.external.labels.enabled`;
    needs the tracker's `read.labels` (and `write.label_definitions` to apply)."""
    return work_intake.labels_define(dry_run, prune, approve, as_, hive, as_json)


def labels_review_cmd(hive: str = _HIVE, as_json: bool = _JSON):
    """Render the batch digest to approve and every drafted label, issue type or field change:
    what each adds and removes (or sets), and whether its issue moved since the draft (stale).
    Reads only. Off unless `intake.external.outbox.enabled`."""
    return work_intake.labels_review(hive, as_json)


def labels_approve_cmd(
    batch: str = typer.Option(
        ...,
        "--batch",
        metavar="<digest>",
        help="the batch digest `labels review` printed (full or 12-hex prefix)",
    ),
    as_: str = _AS,
    hive: str = _HIVE,
    as_json: bool = _JSON,
):
    """Approve the reviewed batch of label changes: one single-use grant per operation (operator
    seats only), a type or field change approved as its own operation. A stale item is skipped
    and re-drafted by the next sync. Approving sends nothing: send each with `bh work outbox send
    <id> --kind labels` (or `--kind type` / `--kind fields`). Needs `outbox.labels_enabled`."""
    return work_intake.labels_approve(batch, as_, hive, as_json)


#: Canonical operation identity -> handler, in help order. :mod:`beadhive.work` splices this into
#: its full ``work`` projection; :mod:`beadhive.cli_fastpath` projects it alone.
CLI_HANDLERS = {
    "work.outbox.approve": outbox_approve_cmd,
    "work.outbox.backfill-close": outbox_backfill_close_cmd,
    "work.outbox.discard": outbox_discard_cmd,
    "work.outbox.draft": outbox_draft_cmd,
    "work.outbox.reconcile": outbox_reconcile_cmd,
    "work.outbox.reopen": outbox_reopen_cmd,
    "work.outbox.retry": outbox_retry_cmd,
    "work.outbox.send": outbox_send_cmd,
    "work.outbox.send-approved": outbox_send_approved_cmd,
    "work.outbox.show": outbox_show_cmd,
    "work.thread.considered": thread_considered_cmd,
    "work.thread.ingest": thread_ingest_cmd,
    "work.thread.show": thread_show_cmd,
    "work.labels.approve": labels_approve_cmd,
    "work.labels.define": labels_define_cmd,
    "work.labels.review": labels_review_cmd,
    "work.labels.sync": labels_sync_cmd,
}

#: Help text of the nested groups these handlers mount on.
GROUP_HELP = {
    "work outbox": "Supervised replies to external issues: draft, review, approve, send.",
    "work thread": "Imported reports' issue comments: show, ingest, mark considered.",
    "work labels": "Project bead state onto the bound issues' owned labels: sync, define, "
    "review, approve.",
}
