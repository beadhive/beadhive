"""``BdReportStore`` over the real ``beadhive_bd_cli`` routes and an in-process fake ``bd``.

The fake understands exactly the argv the intake routes shape (create, list, show, set-state,
update, assign, close, dep, find-duplicates) and nothing else, so an unexpected argv fails loudly.
It lets the ReportStore contract cases run on every machine and pins the adapter's own behaviour:
the failures it raises, the ``intake.source`` document it writes, and the rows it hands back
untouched. The same cases run against a real bd store in ``test_intake_bead_store_int.py``.

No clock, no sleep, no process, no network.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import pytest

from beadhive.adapters.intake import SOURCE_KEY, THREAD_KEY, BdReportStore
from beadhive.modules.intake import (
    BeadView,
    IntakeRefused,
    IntakeStoreError,
    IssueBinding,
    IssueStatus,
    RefusalReason,
    ReportDraft,
    ReportStore,
    SourceSnapshot,
    StorePosture,
    ThreadRequest,
    ThreadService,
)
from beadhive.testing.intake import (
    REPORT_STORE_CASES,
    SCOPE,
    URL,
    InProcessIntakeLock,
    draft,
    run_report_store_case,
    thread,
)
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at
from beadhive_bd_cli import CliRoutes, err_line


class FakeBd:
    """A bd stand-in: an ordered dict of issue dicts plus the exact argv it was asked to run."""

    def __init__(self, prefix: str = "acme") -> None:
        self.prefix = prefix
        self.issues: dict[str, dict[str, Any]] = {}
        self.edges: list[tuple[str, str, str]] = []
        self.calls: list[dict[str, Any]] = []
        self.fail_next: tuple[int, str] | None = None
        self._n = 0

    # -- the BdTransport the routes run through ----------------------------------------------

    def run(self, args, cwd, actor="", capture=False, text_input=None, **_kw):
        self.calls.append({"args": list(args), "actor": actor, "stdin": text_input})
        if self.fail_next is not None:
            code, message = self.fail_next
            self.fail_next = None
            return subprocess.CompletedProcess(args, code, "", message)
        try:
            out = self._dispatch(list(args), text_input)
        except _Missing as exc:
            return subprocess.CompletedProcess(args, 1, "", f"Error: no issue found: {exc}")
        except _BadState as exc:
            return subprocess.CompletedProcess(args, 1, "", f"Error: {exc}")
        return subprocess.CompletedProcess(args, 0, out, "")

    def json(self, args, cwd, *, strict=False):
        res = self.run([*args, "--json"], cwd, capture=True)
        return json.loads(res.stdout or "null") if res.returncode == 0 else None

    # -- the argv the intake routes shape -----------------------------------------------------

    def _dispatch(self, args: list[str], stdin: str | None) -> str:
        if args[0] == "--json":
            args = args[1:]
        if args[-1] == "--json":
            args = args[:-1]
        verb, rest = args[0], args[1:]
        return getattr(self, f"_{verb.replace('-', '_')}")(rest, stdin)

    @staticmethod
    def _flags(rest: list[str]) -> dict[str, str]:
        """``--flag value`` pairs; the bare switches bd takes here carry no value."""
        flags: dict[str, str] = {}
        index = 0
        while index < len(rest):
            token = rest[index]
            if token.startswith("-") and token not in {"--all", "--include-infra", "--force"}:
                flags[token] = rest[index + 1]
                index += 2
            else:
                index += 1
        return flags

    def _create(self, rest, stdin):
        title, flags = rest[0], self._flags(rest[1:])
        self._n += 1
        bead = f"{self.prefix}-{self._n:03d}"
        issue: dict[str, Any] = {
            "id": bead,
            "title": title,
            "status": "open",
            "priority": 2,
            "issue_type": flags["--type"],
            "labels": sorted(flags["-l"].split(",")),
        }
        if "-d" in flags:
            issue["description"] = flags["-d"]
        if "--external-ref" in flags:
            issue["external_ref"] = flags["--external-ref"]
        if "--metadata" in flags:
            assert flags["--metadata"] == "@/dev/stdin"
            issue["metadata"] = json.loads(stdin or "{}")
        self.issues[bead] = issue
        return json.dumps({"id": bead, "title": title})

    def _row(self, bead: str) -> dict[str, Any]:
        if bead not in self.issues:
            raise _Missing(bead)
        return self.issues[bead]

    def _list(self, rest, stdin):
        flags = self._flags(rest)
        rows = list(self.issues.values())
        if "--all" not in rest:
            rows = [r for r in rows if r["status"] != "closed"]
        if "--status" in flags:
            rows = [r for r in rows if r["status"] == flags["--status"]]
        if "--label" in flags:
            rows = [r for r in rows if flags["--label"] in r["labels"]]
        if "--external-ref" in flags:
            rows = [r for r in rows if r.get("external_ref") == flags["--external-ref"]]
        if "--has-metadata-key" in flags:
            rows = [r for r in rows if flags["--has-metadata-key"] in r.get("metadata", {})]
        return json.dumps(rows)

    def _show(self, rest, stdin):
        return json.dumps([self._row(rest[0])])

    def _set_state(self, rest, stdin):
        row = self._row(rest[0])
        dimension, _, value = rest[1].partition("=")
        if not dimension or not value:  # real bd 1.3.0: no way to clear a dimension (bh-cokbi)
            raise _BadState(f'invalid state format "{rest[1]}", expected <dimension>=<value>')
        row["labels"] = sorted(
            [x for x in row["labels"] if not x.startswith(f"{dimension}:")]
            + [f"{dimension}:{value}"]
        )
        return ""

    def _update(self, rest, stdin):
        row, flags = self._row(rest[0]), self._flags(rest[1:])
        if "--type" in flags:
            row["issue_type"] = flags["--type"]
        if "--priority" in flags:
            row["priority"] = int(flags["--priority"])
        if "--metadata" in flags:
            assert flags["--metadata"] == "@/dev/stdin"
            row.setdefault("metadata", {}).update(json.loads(stdin or "{}"))
        return ""

    def _assign(self, rest, stdin):
        self._row(rest[0])["assignee"] = rest[1]
        return ""

    def _close(self, rest, stdin):
        self._row(rest[0])["status"] = "closed"
        return ""

    def _dep(self, rest, stdin):
        action, source = rest[0], rest[1]
        self._row(source)
        if action == "list":  # dep list <bead> --direction up|down --type T
            flags = self._flags(rest[2:])
            kind, direction = flags["--type"], flags["--direction"]
            wanted = [
                dst if direction == "down" else src
                for src, dst, edge in self.edges
                if edge == kind and (src if direction == "down" else dst) == source
            ]
            return json.dumps([{**self.issues[b], "dependency_type": kind} for b in wanted])
        target = rest[2]
        self._row(target)
        if action == "add":
            self.edges.append((source, target, rest[4]))
        else:  # remove
            self.edges = [e for e in self.edges if e[:2] != (source, target)]
        return ""

    def _find_duplicates(self, rest, stdin):
        flags = self._flags(rest)
        threshold = float(flags["--threshold"])
        open_rows = [r for r in self.issues.values() if r["status"] != "closed"]
        pairs = []
        for index, first in enumerate(open_rows):
            for second in open_rows[index + 1 :]:
                a, b = _tokens(first["title"]), _tokens(second["title"])
                similarity = len(a & b) / len(a | b) if a | b else 0.0
                if similarity >= threshold:
                    pairs.append(
                        {
                            "issue_a_id": first["id"],
                            "issue_b_id": second["id"],
                            "issue_a_title": first["title"],
                            "similarity": similarity,
                        }
                    )
        return json.dumps({"count": len(pairs), "method": flags["--method"], "pairs": pairs})


class _BadState(Exception):
    """``bd set-state`` refuses an empty dimension or value."""


class _Missing(Exception):
    pass


def _tokens(title: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", title.lower()))


def _store(bd: FakeBd | None = None) -> tuple[BdReportStore, FakeBd]:
    bd = bd or FakeBd()
    return BdReportStore(CliRoutes(bd, "/hive"), SCOPE, error_line=err_line), bd


# -- the contract ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", REPORT_STORE_CASES, ids=lambda case: case.case_id)
def test_bd_report_store_passes_the_report_store_contract_on_a_fake_bd(case) -> None:
    run_report_store_case(lambda: _store()[0], case, subject="BdReportStore over fake bd")


def test_bd_report_store_satisfies_the_port() -> None:
    assert isinstance(_store()[0], ReportStore)


# -- the argv it asks for (the same shapes the legacy report/triage code ran) ----------------


def test_create_asks_for_the_hive_triplet_labels_and_nothing_else_for_a_native_report() -> None:
    store, bd = _store()

    store.create(draft("login is broken"))

    (call,) = bd.calls
    assert call["args"] == [
        "--json",
        "create",
        "login is broken",
        "--type",
        "bug",
        "-l",
        "provider:github,org:acme,repo:widgets",
        "-d",
        "steps",
    ]
    assert call["actor"] == "dev/test" and call["stdin"] is None


def test_a_bound_report_is_created_with_its_url_and_source_document_atomically() -> None:
    store, bd = _store()

    bead = store.create(draft(bound=True))

    (call,) = bd.calls
    assert call["args"][-4:] == ["--external-ref", URL, "--metadata", "@/dev/stdin"]
    document = json.loads(call["stdin"])[SOURCE_KEY]
    assert document["schema"] == 1
    assert document["binding"] == {
        "tracker": "memory",
        "project": "acme/widgets",
        "identifier": "#7",
    }
    view = store.get(bead)
    assert view is not None and view.binding == IssueBinding(
        bead, "memory", "acme/widgets", URL, "#7"
    )


def test_set_state_update_close_assign_and_tracks_run_the_legacy_argv() -> None:
    store, bd = _store()
    bead, work = store.create(draft()), store.create(draft("w"))
    bd.calls.clear()

    store.set_state(bead, "intake", "untriaged", reason="filed via bh report", actor="a/1")
    store.update_fields(bead, issue_type="feature", priority="1", actor="a/2")
    store.update_fields(bead, issue_type="", priority="", actor="a/2")  # nothing to ask bd for
    store.assign(bead, "super/anna", actor="a/3")
    store.close(bead, reason="works as intended", actor="a/4")
    store.track(bead, work, actor="a/5")
    store.untrack(bead, work, actor="a/5")

    assert [(c["args"], c["actor"]) for c in bd.calls] == [
        (["set-state", bead, "intake=untriaged", "--reason", "filed via bh report"], "a/1"),
        (["update", bead, "--type", "feature", "--priority", "1"], "a/2"),
        (["assign", bead, "super/anna"], "a/3"),
        (["close", bead, "--reason", "works as intended"], "a/4"),
        (["dep", "add", bead, work, "-t", "tracks"], "a/5"),
        (["dep", "remove", bead, work], "a/5"),
    ]


def test_reads_ask_for_the_legacy_queue_and_duplicate_argv() -> None:
    store, bd = _store()
    bd.calls.clear()

    store.list_open_labeled("intake:untriaged")
    store.find_by_external_ref(URL)
    store.find_by_external_ref("")
    store.duplicate_pairs(threshold=0.4)
    store.duplicate_pairs(threshold=0.4, method="ai")
    store.get("acme-001")

    assert [c["args"] for c in bd.calls] == [
        ["list", "--label", "intake:untriaged", "--status", "open", "--json"],
        ["list", "--all", "--limit", "0", "--external-ref", URL, "--json"],
        ["find-duplicates", "--threshold", "0.4", "--method", "mechanical", "--json"],
        ["find-duplicates", "--threshold", "0.4", "--method", "ai", "--json"],
        ["show", "acme-001", "--json"],
    ]


# -- failures ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "operation", "prefix"),
    [
        (lambda s, b: s.create(draft()), "create", "bd create failed: "),
        (lambda s, b: s.set_state(b, "intake", "x", reason="r", actor="a"), "set_state", ""),
        (
            lambda s, b: s.update_fields(b, issue_type="bug", priority="", actor="a"),
            "update_fields",
            "bd update failed: ",
        ),
        (lambda s, b: s.close(b, reason="r", actor="a"), "close", "bd close failed: "),
        (lambda s, b: s.assign(b, "seat", actor="a"), "assign", ""),
        (lambda s, b: s.track(b, b, actor="a"), "track", ""),
        (lambda s, b: s.untrack(b, b, actor="a"), "untrack", ""),
    ],
)
def test_a_failed_bd_write_carries_the_step_the_bead_the_status_and_the_failure_line(
    call, operation, prefix
) -> None:
    store, bd = _store()
    bead = store.create(draft())
    bd.fail_next = (3, "Error: database is read-only")

    with pytest.raises(IntakeStoreError) as raised:
        call(store, bead)

    error = raised.value
    assert error.operation == operation and error.returncode == 3
    assert error.detail == f"{prefix}Error: database is read-only"
    assert error.bead == ("" if operation == "create" else bead)


def test_a_create_that_prints_no_id_is_a_store_error_not_a_blank_bead() -> None:
    store, bd = _store()
    bd._create = lambda rest, stdin: "{}"  # type: ignore[method-assign]

    with pytest.raises(IntakeStoreError, match="bd create reported no new bead id"):
        store.create(draft())


def test_reads_bd_cannot_answer_degrade_to_the_empty_result() -> None:
    store, bd = _store()
    bd.fail_next = (1, "Error: no database")
    assert store.get("acme-001") is None
    bd.fail_next = (1, "Error: no database")
    assert store.list_open_labeled("intake:untriaged") == ()
    bd.fail_next = (1, "Error: no database")
    assert store.duplicate_pairs(threshold=0.5) == ()
    bd.fail_next = (1, "Error: no database")
    assert store.trackers_of("acme-001") == ()


# -- the intake.source document and the passthrough rows ----------------------------------


def test_record_snapshot_rewrites_the_whole_document_and_keeps_the_binding() -> None:
    store, bd = _store()
    bead = store.create(draft(bound=True))
    bd.calls.clear()
    newer = SourceSnapshot(
        "t2",
        "b2",
        IssueStatus.CLOSED,
        "r2",
        closed_reason="completed",
        labels=("bug", "p1"),
        author_id="u1",
        author_kind="user",
        source_missing=True,
    )

    store.record_snapshot(bead, newer, actor="importer/a")

    show, write = bd.calls
    assert show["args"] == ["show", bead, "--json"]
    assert (
        write["args"] == ["update", bead, "--metadata", "@/dev/stdin"]
        and write["actor"] == "importer/a"
    )
    document = json.loads(write["stdin"])[SOURCE_KEY]
    assert document["binding"]["identifier"] == "#7" and document["snapshot"]["revision"] == "r2"
    view = store.get(bead)
    assert view is not None and view.snapshot == newer and view.binding is not None
    assert view.binding.tracker == "memory"


def test_a_snapshot_for_an_unknown_bead_is_a_store_error() -> None:
    store, bd = _store()
    bd.fail_next = (1, "Error: no issue found")
    with pytest.raises(IntakeStoreError) as raised:
        store.record_snapshot(
            "acme-ghost", SourceSnapshot("t", "b", IssueStatus.OPEN, "r"), actor="a"
        )
    assert raised.value.operation == "record_snapshot"


def test_rows_pass_through_untouched_and_never_take_part_in_equality() -> None:
    store, bd = _store()
    bead = store.create(draft("login is broken"))
    store.set_state(bead, "intake", "untriaged", reason="r", actor="a")
    bd.issues[bead]["extra"] = {"nested": [1, 2]}

    (view,) = store.list_open_labeled("intake:untriaged")

    assert view.raw == bd.issues[bead] and view.raw["extra"] == {"nested": [1, 2]}
    assert isinstance(view, BeadView) and view.priority == "2"
    store.create(draft("login is broken too"))
    (pair,) = store.duplicate_pairs(threshold=0.4)
    assert pair.raw["issue_a_title"] == "login is broken"


def test_a_bead_with_an_external_ref_but_no_source_document_still_has_a_binding() -> None:
    """A row bd imported from a tracker (native ``source_system`` + ``external_ref``) predates any
    ``intake.source`` document: the URL and the native tracker name are enough to bind."""
    store, bd = _store()
    bd.issues["acme-900"] = {
        "id": "acme-900",
        "title": "imported",
        "status": "open",
        "priority": 1,
        "issue_type": "bug",
        "labels": [],
        "source_system": "github",
        "external_ref": URL,
    }

    (view,) = store.find_by_external_ref(URL)

    assert view.binding == IssueBinding("acme-900", "github", "", URL, "")
    assert view.snapshot is None


def test_a_lazy_scope_is_resolved_once_and_only_when_asked() -> None:
    resolved: list[int] = []

    def scope():
        resolved.append(1)
        return SCOPE

    bd = FakeBd()
    store = BdReportStore(CliRoutes(bd, "/hive"), scope, error_line=err_line)
    store.list_open_labeled("intake:untriaged")
    store.get("acme-001")
    assert resolved == []  # reads and dispositions never need the hive entry
    store.create(draft())
    store.create(draft())
    assert resolved == [1]


# -- the intake.thread document -------------------------------------------------------------


def _bound(store: BdReportStore, bd: FakeBd) -> str:
    bead = store.create(draft(bound=True))
    bd.issues[bead]["metadata"]["git.commits"] = {"commits": ["abc123"]}
    bd.calls.clear()
    return bead


def test_a_thread_write_rereads_the_seq_then_rewrites_only_its_own_key_on_stdin() -> None:
    store, bd = _store()
    bead = _bound(store, bd)

    store.write_thread(bead, thread(3), expect_seq=0, actor="intake/ingest")

    show, write = bd.calls
    assert show["args"] == ["show", bead, "--json"]
    assert write["args"] == ["update", bead, "--metadata", "@/dev/stdin"]
    assert write["actor"] == "intake/ingest"
    assert list(json.loads(write["stdin"])) == [THREAD_KEY]  # one key, the whole document
    metadata = bd.issues[bead]["metadata"]
    assert metadata["git.commits"] == {"commits": ["abc123"]}  # other writers' keys survive
    assert metadata[SOURCE_KEY]["binding"]["identifier"] == "#7"
    assert store.read_thread(bead) == thread(3)


def test_a_stale_thread_write_is_refused_without_an_update() -> None:
    store, bd = _store()
    bead = _bound(store, bd)
    store.write_thread(bead, thread(3), expect_seq=0, actor="a")
    bd.calls.clear()

    with pytest.raises(IntakeRefused) as refused:
        store.write_thread(bead, thread(4), expect_seq=1, actor="a")

    assert refused.value.reason is RefusalReason.THREAD_MOVED
    assert [c["args"][0] for c in bd.calls] == ["show"]


def test_a_failed_thread_update_is_a_store_error_naming_the_step() -> None:
    store, bd = _store()
    bead = _bound(store, bd)
    original = bd._update

    def failing(rest, stdin):
        raise _Missing(rest[0])

    bd._update = failing  # type: ignore[method-assign]
    with pytest.raises(IntakeStoreError) as raised:
        store.write_thread(bead, thread(3), expect_seq=0, actor="a")
    bd._update = original  # type: ignore[method-assign]

    assert raised.value.operation == "write_thread" and raised.value.bead == bead
    assert store.read_thread(bead) is None


def test_a_malformed_thread_is_a_read_error_and_never_overwritten() -> None:
    store, bd = _store()
    bead = _bound(store, bd)
    bd.issues[bead]["metadata"][THREAD_KEY] = {"schema": 99}

    for call in (
        lambda: store.read_thread(bead),
        lambda: store.write_thread(bead, thread(3), expect_seq=0, actor="a"),
    ):
        with pytest.raises(IntakeStoreError) as raised:
            call()
        assert raised.value.operation == "read_thread"
    assert bd.issues[bead]["metadata"][THREAD_KEY] == {"schema": 99}


def test_queue_and_lookup_reads_never_decode_a_thread() -> None:
    """``bd list --json`` carries every row's metadata (bd 1.3.0 has no metadata-free list), so a
    queue row holds its thread document; the queue path passes it through and never parses it."""
    store, bd = _store()
    bead = _bound(store, bd)
    store.set_state(bead, "intake", "untriaged", reason="r", actor="a")
    bd.issues[bead]["metadata"][THREAD_KEY] = "not a document"

    (queued,) = store.list_open_labeled("intake:untriaged")
    (found,) = store.find_by_external_ref(URL)

    assert queued.id == found.id == bead and store.get(bead) is not None
    assert queued.raw["metadata"][THREAD_KEY] == "not a document"


def test_comment_ingestion_runs_end_to_end_over_the_bd_store() -> None:
    world = MemoryWorld()
    issue = world.add_issue("login is broken", at=at(1))
    tracker = MemoryTracker(world)
    store, bd = _store()
    binding = IssueBinding("", "memory", world.locator, issue.url, issue.identifier)
    bead = store.create(
        ReportDraft("login is broken", "bug", "origin:github", "importer/a", binding=binding)
    )
    bd.issues[bead]["metadata"]["git.commits"] = {"commits": ["abc123"]}
    first = world.add_comment(issue, "it crashes", at=at(2))
    world.add_comment(issue, "me too", at=at(3))
    service = ThreadService(
        store, tracker, InProcessIntakeLock(), posture=StorePosture(published=False)
    )
    request = ThreadRequest(bead, "intake/ingest")

    assert service.ingest_thread(request).written
    writes = [c for c in bd.calls if c["args"][0] == "update"]
    assert len(writes) == 1  # records and checkpoint in ONE bd update
    assert not service.ingest_thread(request).written  # a boundary repeat: no update at all
    world.edit_comment(first, "it crashes on save", at=at(4))
    assert service.ingest_thread(request).written

    stored = store.read_thread(bead)
    assert stored is not None and stored.records[first.id].rev == 2
    assert bd.issues[bead]["metadata"]["git.commits"] == {"commits": ["abc123"]}
    assert len([c for c in bd.calls if c["args"][0] == "update"]) == 2
    assert world.write_requests() == 0
