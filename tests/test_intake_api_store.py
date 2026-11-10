"""``ApiReportStore``: the intake report store served from the Beads service (bh-9o12j).

A real :class:`BeadsSession` talks to :class:`FakeBeadsService` over ``httpx.MockTransport``, over
the SAME issue and edge tables a ``FakeBd`` keeps, so the operations that stay on the CLI (the
creates, gates, ``set-state``, ``find-duplicates``) and the ones on the API see one world and every
``bd`` call is recorded. Three things are pinned:

* the shared ``ReportStore`` port suite passes against the API-backed store, and against the same
  class with the CLI route selected;
* with the API route no read, and no api-ready write, spawns ``bd`` (recorded argv, never a
  duration), and the writes that moved leave the same bead state as the CLI route (parity);
* the route decision: ``api`` fails closed with the work verbs' message, ``api+cli-fallback``
  selects the CLI before the first operation, ``cli`` never resolves a session.

No clock, no sleep, no process, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from tests.test_intake_bead_store import FakeBd

from beadhive import beads_routing, intake_beads, intake_services
from beadhive.adapters.intake import ApiReportStore, BdReportStore, ReportApiError
from beadhive.modules.intake import (
    IntakeRefused,
    IntakeStoreError,
    IssueStatus,
    RefusalReason,
    ReportStore,
    SourceSnapshot,
    WorkDraft,
)
from beadhive.testing.intake import (
    REPORT_STORE_CASES,
    SCOPE,
    URL,
    draft,
    run_report_store_case,
    thread,
)
from beadhive.testing.intake.cases import outbox
from beadhive_bd_cli import CliRoutes, err_line
from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint
from beadhive_beads_client.service import ServiceUnavailable
from harness.beads_service_fake import CAPABILITIES, FakeBeadsService

ACTOR = "dev/test"


def _session(service: FakeBeadsService) -> BeadsSession:
    return BeadsSession(
        RemoteEndpoint("http://127.0.0.1:9"),
        ExpectedContext("p", "db", required_capabilities=intake_beads.INTAKE_CAPABILITIES),
        transport=httpx.MockTransport(service.handle),
    )


def _rig(
    bd: FakeBd | None = None, *, api: bool = True
) -> tuple[ApiReportStore, FakeBd, FakeBeadsService]:
    """An API-backed store, its fake ``bd`` and its fake service (all over one world). With
    ``api=False`` the route decision is the CLI: the same class, selected away from the service."""
    bd = bd or FakeBd()
    service = FakeBeadsService(bd)
    session = _session(service).open()
    served = intake_beads.SessionReportApi(session)
    store = ApiReportStore(
        CliRoutes(bd, "/hive"), SCOPE, api=lambda: served if api else None, error_line=err_line
    )
    return store, bd, service


def _verbs(bd: FakeBd) -> list[str]:
    names = []
    for call in bd.calls:
        args = [a for a in call["args"] if a != "--json"]
        names.append(" ".join(args[:2]) if args[0] in {"dep", "gate"} else args[0])
    return names


# -- the port suite, on both routes -------------------------------------------------------------


@pytest.mark.parametrize("case", REPORT_STORE_CASES, ids=lambda case: case.case_id)
def test_the_report_store_port_suite_passes_over_the_api(case) -> None:
    run_report_store_case(lambda: _rig()[0], case, subject="ApiReportStore over the service")


@pytest.mark.parametrize("case", REPORT_STORE_CASES, ids=lambda case: case.case_id)
def test_the_report_store_port_suite_passes_with_the_cli_route_selected(case) -> None:
    run_report_store_case(
        lambda: _rig(api=False)[0], case, subject="ApiReportStore with the CLI route selected"
    )


def test_it_satisfies_the_port_and_is_a_bd_report_store() -> None:
    store = _rig()[0]
    assert isinstance(store, ReportStore) and isinstance(store, BdReportStore)


# -- the API route spawns no bd for what moved --------------------------------------------------


def _bound_report(store: ApiReportStore) -> tuple[str, str]:
    report = store.create(draft(bound=True))
    store.set_state(report, "origin", "github", reason="imp", actor=ACTOR)
    store.set_state(report, "intake", "untriaged", reason="imp", actor=ACTOR)
    work = store.create_work(WorkDraft("Work", "task", ACTOR, description="d"))
    return report, work


def test_every_read_and_every_api_ready_write_spawns_no_bd_process() -> None:
    store, bd, service = _rig()
    report, work = _bound_report(store)
    bd.calls.clear()
    service.requests.clear()

    assert store.get(report) is not None
    assert [v.id for v in store.find_by_external_ref(URL)] == [report]
    assert store.is_held(report)
    assert store.tracked_work(report) is None and store.trackers_of(work) == ()
    assert [v.id for v in store.list_open_labeled("intake:untriaged")] == [report]
    assert [v.id for v in store.bound_reports()] == [report]
    assert store.reports_with_outbox() == ()
    assert store.read_thread(report) is None and store.read_outbox(report) is None
    store.update_fields(report, issue_type="feature", priority="P1", actor=ACTOR)
    store.assign(report, "disp/x", actor=ACTOR)
    store.track(report, work, actor=ACTOR)
    assert store.tracked_work(report) == work and store.trackers_of(work) == (report,)
    store.untrack(report, work, actor=ACTOR)
    store.investigate(report, work, actor=ACTOR)
    assert store.investigations_of(report) == (work,) and store.tracked_work(report) is None
    store.uninvestigate(report, work, actor=ACTOR)
    assert store.investigations_of(report) == ()
    snapshot = SourceSnapshot("t2", "b2", IssueStatus.CLOSED, "r2")
    store.record_snapshot(report, snapshot, actor=ACTOR)
    store.write_thread(report, thread(seq=3), expect_seq=0, actor=ACTOR)
    store.write_outbox(report, outbox(), expect_seq=0, actor=ACTOR)
    store.close(work, reason="done", actor=ACTOR)

    assert bd.calls == []  # not one subprocess: every one of these is served by the service
    assert not any(op.startswith("POST /v0/beads/issues:create") for op in service.operations())


def test_the_held_close_forces_through_the_service_and_resolves_the_hold_on_the_cli() -> None:
    store, bd, service = _rig()
    report, _work = _bound_report(store)
    bd.calls.clear()
    service.requests.clear()

    store.close(report, reason="rejected", actor=ACTOR)

    assert _verbs(bd) == ["gate resolve"]  # the hold's bookkeeping resolve: CLI only
    closes = [body for _m, path, _q, body in service.requests if path.endswith(":close")]
    assert [bool(body.get("force")) for body in closes] == [False, True]  # plain first, then held
    assert bd.issues[report]["status"] == "closed"


def test_a_report_with_another_live_blocker_is_refused_not_forced() -> None:
    store, bd, service = _rig()
    report, work = _bound_report(store)
    bd.edges.append((report, work, "blocks"))

    with pytest.raises(IntakeRefused) as refused:
        store.close(report, reason="rejected", actor=ACTOR)

    assert refused.value.reason is RefusalReason.LIVE_BLOCKER
    assert bd.issues[report]["status"] == "open"
    assert not any(
        bool(b.get("force")) for _m, p, _q, b in service.requests if p.endswith(":close")
    )


def test_only_the_gates_set_state_and_the_duplicate_scan_stay_on_the_cli() -> None:
    store, bd, service = _rig()
    report, _work = _bound_report(store)
    # the creates are CLI: the service cannot record created_by (see the module docstring)
    assert _verbs(bd) == ["create", "gate create", "set-state", "set-state", "create"]

    bd.calls.clear()
    service.requests.clear()
    store.duplicate_pairs(threshold=0.5)
    assert _verbs(bd) == ["find-duplicates"]
    assert service.operations() == []  # a duplicate scan never even touches the service

    bd.calls.clear()
    assert [h.gate for h in store.holds() if report in h.blocks]  # the gate list stays on the CLI
    assert _verbs(bd) == ["gate list"]
    assert any(op.startswith("GET /v0/beads/issues/") for op in service.operations())


# -- the lookup by external_ref -----------------------------------------------------------------


def test_a_lookup_that_hits_the_bound_report_index_makes_no_bd_call() -> None:
    store, bd, _service = _rig()
    report, _ = _bound_report(store)
    bd.calls.clear()

    assert [v.id for v in store.find_by_external_ref(URL)] == [report]
    assert bd.calls == []


def test_a_miss_is_confirmed_on_the_cli_so_a_bead_bound_by_external_ref_alone_is_found() -> None:
    store, bd, _service = _rig()
    report, _ = _bound_report(store)
    bd.issues[report].pop("metadata")  # bound only by external_ref: not in the intake.source index
    bd.calls.clear()

    assert [v.id for v in store.find_by_external_ref(URL)] == [report]
    assert _verbs(bd) == ["list"] and "--external-ref" in bd.calls[0]["args"]

    bd.calls.clear()
    assert store.find_by_external_ref("https://tracker.example/acme/widgets/issues/99") == ()
    assert _verbs(bd) == ["list"]  # one confirming read, nothing found


def test_a_create_drops_the_index_so_a_racing_duplicate_shows_up() -> None:
    store, bd, _service = _rig()
    first = store.create(draft(bound=True))
    assert [v.id for v in store.find_by_external_ref(URL)] == [first]  # the index is built

    store.create(draft("a racing twin", bound=True))  # a second bound report of the same URL
    found = store.find_by_external_ref(URL)

    assert len(found) == 2  # the lookup after a create re-read the index, as the CLI lookup would


def test_an_empty_url_is_never_looked_up() -> None:
    store, bd, service = _rig()
    assert store.find_by_external_ref("") == ()
    assert bd.calls == [] and service.operations() == []


# -- the priority grammar -----------------------------------------------------------------------


@pytest.mark.parametrize(("given", "sent"), [("P1", 1), ("p2", 2), ("3", 3), ("P0", 0)])
def test_bds_priority_grammar_is_sent_as_the_services_integer(given, sent) -> None:
    store, bd, service = _rig()
    report = store.create(draft())
    service.requests.clear()

    store.update_fields(report, issue_type="", priority=given, actor=ACTOR)

    (_method, _path, _query, body) = service.requests[-1]
    assert body["patch"] == {"priority": sent} and body["actor"] == ACTOR


def test_a_priority_outside_the_grammar_is_left_to_bd_to_refuse() -> None:
    store, bd, service = _rig()
    report = store.create(draft())
    bd.calls.clear()
    service.requests.clear()

    store.update_fields(report, issue_type="", priority="9", actor=ACTOR)  # not 0-4 / P0-P4

    assert _verbs(bd) == ["update"] and service.operations() == []  # exactly bd's own path


def test_a_new_work_priority_is_set_through_the_service_after_the_cli_create() -> None:
    store, bd, service = _rig()
    bd.calls.clear()
    service.requests.clear()

    work = store.create_work(WorkDraft("W", "task", ACTOR, description="d", priority="P2"))

    assert _verbs(bd) == ["create"]
    assert [b["patch"] for m, p, q, b in service.requests if m == "PATCH"] == [{"priority": 2}]
    assert bd.issues[work]["priority"] == 2


# -- failures are never silent ------------------------------------------------------------------


def test_a_failed_read_is_a_store_failure_not_an_empty_answer() -> None:
    store, bd, service = _rig()
    report = store.create(draft(bound=True))
    service.fail["GET /v0/beads/issues"] = 500

    with pytest.raises(IntakeStoreError) as failed:
        store.bound_reports()  # the CLI route reads an empty list here; the API route must not

    assert failed.value.operation == "list" and "Beads" in failed.value.detail
    assert [v.id for v in store.bound_reports()] == [report]


def test_a_refused_write_names_the_operation_the_bead_and_the_refusal() -> None:
    store, bd, service = _rig()
    report = store.create(draft())
    service.fail[f"PATCH {report}"] = 400

    with pytest.raises(IntakeStoreError) as failed:
        store.assign(report, "disp/x", actor=ACTOR)

    assert failed.value.bead == report and failed.value.operation == "assign"
    assert "refused by Beads" in failed.value.detail and "invalid_argument" in failed.value.detail
    assert "assignee" not in bd.issues[report]


def test_a_transport_failure_on_close_is_reported_and_never_read_as_a_refusal() -> None:
    store, bd, service = _rig()
    report, _ = _bound_report(store)
    service.requests.clear()
    service.fail[f"POST {report}:close"] = 503

    with pytest.raises(IntakeStoreError) as failed:
        store.close(report, reason="x", actor=ACTOR)

    assert failed.value.operation == "close"
    assert not any(m == "GET" for m, *_ in service.requests)  # no blocker analysis on a 5xx
    assert bd.issues[report]["status"] == "open"


def test_a_document_that_moved_between_the_read_and_the_write_is_not_overwritten() -> None:
    store, bd, service = _rig()
    report = store.create(draft(bound=True))
    seen = store._show(report)
    bd.issues[report]["metadata"]["intake.source"] = {"schema": 1, "moved": True}

    with pytest.raises(IntakeStoreError) as failed:
        store._set_document(
            report,
            "intake.source",
            {"schema": 1},
            row=seen,
            actor=ACTOR,
            operation="record_snapshot",
        )

    assert "changed since it was read" in failed.value.detail
    assert bd.issues[report]["metadata"]["intake.source"] == {"schema": 1, "moved": True}


def test_the_session_api_reports_a_missing_bead_as_none_and_pages_to_the_end() -> None:
    store, bd, service = _rig()
    for n in range(5):
        store.create(draft(f"report {n}"))
    api = intake_beads.SessionReportApi(_session(service).open())

    assert api.issue("acme-404") is None
    assert api.dependents("acme-404") == []
    service.requests.clear()
    intake_beads._PAGE, saved = 2, intake_beads._PAGE
    try:
        rows = api.issues()
    finally:
        intake_beads._PAGE = saved
    assert len(rows) == 5
    assert len([op for op in service.operations() if op == "GET /v0/beads/issues"]) == 3


def test_a_service_error_that_is_not_a_problem_is_a_report_api_error() -> None:
    bd = FakeBd()
    service = FakeBeadsService(bd)
    api = intake_beads.SessionReportApi(_session(service).open())
    service.fail["GET acme-1"] = 500
    bd.issues["acme-1"] = {"id": "acme-1", "title": "t", "status": "open", "labels": []}

    with pytest.raises(ReportApiError) as failed:
        api.issue("acme-1")

    assert not failed.value.refused  # a 5xx is not the service saying no


# -- parity: the moved writes leave the same bead state as the CLI route --------------------------


def _normalised(bd: FakeBd) -> dict:
    """The world with its ids replaced by their order of creation."""
    ids = {bead: f"B{index}" for index, bead in enumerate(sorted(bd.issues))}
    text = json.dumps(
        {
            "issues": {ids[k]: v for k, v in bd.issues.items()},
            "edges": sorted(map(list, bd.edges)),
        },
        sort_keys=True,
    )
    for old, new in ids.items():
        text = text.replace(old, new)
    return json.loads(text)


def _scenario(store: BdReportStore) -> None:
    report, work = _bound_report(store)
    store.update_fields(report, issue_type="feature", priority="P1", actor="dev/u")
    store.assign(report, "disp/x", actor="dev/u")
    store.track(report, work, actor="dev/t")
    store.untrack(report, work, actor="dev/t")
    store.track(report, work, actor="dev/t")
    store.record_snapshot(report, SourceSnapshot("t2", "b2", IssueStatus.CLOSED, "r2"), actor="s")
    store.write_thread(report, thread(seq=3), expect_seq=0, actor="dev/s")
    store.write_outbox(report, outbox(), expect_seq=0, actor="dev/s")
    store.close(work, reason="done", actor="dev/c")
    store.close(report, reason="rejected", actor="dev/c")


def test_the_moved_writes_leave_the_same_bead_state_as_the_cli_route() -> None:
    cli_store = BdReportStore(CliRoutes(cli_bd := FakeBd(), "/hive"), SCOPE, error_line=err_line)
    api_store, api_bd, _service = _rig()

    _scenario(cli_store)
    _scenario(api_store)

    assert _normalised(api_bd) == _normalised(cli_bd)


# -- the route decision -------------------------------------------------------------------------

ENTRY = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"}
HIVE = "github/acme/widget"


def _entry(route: str | None) -> dict:
    return ENTRY if route is None else {**ENTRY, "work": {"beads": {"route": route}}}


@pytest.fixture
def checkout(tmp_path) -> Path:
    """The hive's own checkout: the directory the fake service says it serves."""
    return tmp_path


def _factory(monkeypatch, outcome):
    calls = []

    def make(main, entry):
        calls.append((main, entry))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome()

    monkeypatch.setattr(intake_beads, "session_factory", make)
    return calls


def _absent() -> ServiceUnavailable:
    return ServiceUnavailable("no service", state="absent", start_command="bh host beads start")


def test_the_default_route_fails_closed_with_the_work_verbs_message(
    monkeypatch, checkout, capsys
) -> None:
    _factory(monkeypatch, _absent())
    select = intake_beads.ApiSelector(checkout, _entry(None))

    with pytest.raises(beads_routing.BeadsServiceRequired) as refused:
        select()

    message = str(refused.value)
    assert refused.value.exit_code == 1
    assert f"no capable Beads session for {HIVE}" in message
    assert f"`bh host beads start --hive {HIVE}`" in message and "work.beads.route" in message
    assert capsys.readouterr().err == f"✗ {message}\n"
    with pytest.raises(beads_routing.BeadsServiceRequired) as seam:  # the very same refusal
        beads_routing.allow_cli_route(_entry(None), _absent())
    assert str(seam.value) == message


def test_the_store_fails_closed_before_it_writes_through_the_cli(
    monkeypatch, checkout, tmp_path
) -> None:
    _factory(monkeypatch, _absent())
    bd = FakeBd()
    bd.issues["acme-001"] = {"id": "acme-001", "title": "t", "status": "open", "labels": []}
    store = ApiReportStore(
        CliRoutes(bd, "/hive"),
        SCOPE,
        api=intake_beads.ApiSelector(checkout, _entry(None)),
        error_line=err_line,
    )

    with pytest.raises(beads_routing.BeadsServiceRequired):
        store.get("acme-001")  # the first read decides the route; nothing ran on bd first

    assert bd.calls == []


def test_the_fallback_route_selects_the_cli_when_no_service_is_there(monkeypatch, checkout) -> None:
    calls = _factory(monkeypatch, _absent())
    bd = FakeBd()
    bd.issues["acme-001"] = {"id": "acme-001", "title": "t", "status": "open", "labels": []}
    store = ApiReportStore(
        CliRoutes(bd, "/hive"),
        SCOPE,
        api=intake_beads.ApiSelector(checkout, _entry("api+cli-fallback")),
        error_line=err_line,
    )

    assert store.get("acme-001").id == "acme-001"
    assert store.get("acme-001") is not None
    assert _verbs(bd) == ["show", "show"]  # exactly the CLI store's calls
    assert len(calls) == 1  # decided once, held for the command


def test_the_cli_route_never_resolves_a_session(monkeypatch, checkout) -> None:
    calls = _factory(monkeypatch, AssertionError("a cli-route hive must not open a session"))
    # hive_session itself refuses a cli-route hive before resolving anything
    monkeypatch.setattr(intake_beads, "session_factory", intake_beads.hive_session)
    resolved = []
    monkeypatch.setattr(
        "beadhive.host_beads.resolve_session", lambda *a, **k: resolved.append(a) or "session"
    )

    assert intake_beads.ApiSelector(checkout, _entry("cli"))() is None
    assert resolved == [] and calls == []


def test_a_directory_the_service_does_not_serve_stays_on_the_cli(monkeypatch, checkout) -> None:
    """A clone-on-demand cache is another checkout of the data: the service names the checkout it
    serves, and a store over any other directory keeps the CLI (and closes the session)."""
    service = FakeBeadsService(FakeBd(), repo_root=checkout)
    sessions = []

    def make(main, entry):
        sessions.append(_session(service))
        return sessions[-1]

    monkeypatch.setattr(intake_beads, "session_factory", make)

    assert intake_beads.ApiSelector(checkout / "cache", _entry(None))() is None
    assert any(path == "/v0/beads/context" for _m, path, _q, _b in service.requests)  # asked
    assert sessions[0].context is None  # ... then closed
    assert isinstance(
        intake_beads.ApiSelector(checkout, _entry(None))(), intake_beads.SessionReportApi
    )


def test_a_directory_no_hive_owns_stays_on_the_cli_and_looks_its_entry_up_lazily(
    monkeypatch, checkout
) -> None:
    calls = _factory(monkeypatch, AssertionError("must not open"))
    looked = []
    monkeypatch.setattr(
        intake_beads, "entry_of_directory", lambda directory: looked.append(directory)
    )
    store = intake_services.directory_store(checkout)
    assert looked == []  # constructing a store resolves nothing

    store.duplicate_pairs(threshold=0.5)  # reads bd's find-duplicates, answered empty here
    assert looked == [] and calls == []  # the duplicate scan never needs the route


def test_the_selected_session_is_opened_once_with_the_intake_capabilities(
    monkeypatch, checkout
) -> None:
    bd = FakeBd()
    service = FakeBeadsService(bd, repo_root=checkout)
    requested = []

    def make(main, entry):
        requested.append((main, entry))
        return _session(service)

    monkeypatch.setattr(intake_beads, "session_factory", make)
    select = intake_beads.ApiSelector(checkout, _entry(None))

    first, second = select(), select()

    assert first is second and isinstance(first, intake_beads.SessionReportApi)
    assert requested == [(checkout, _entry(None))]
    assert set(intake_beads.INTAKE_CAPABILITIES) <= set(CAPABILITIES)


def test_the_composition_root_builds_the_api_store_with_the_entry_it_holds(checkout) -> None:
    store = intake_services.report_store(checkout, lambda: SCOPE, entry=ENTRY)
    assert isinstance(store, ApiReportStore)
    assert isinstance(intake_services.directory_store(checkout), ApiReportStore)


def test_timestamps_are_spelled_the_way_bd_spells_them() -> None:
    row = {
        "created_at": "2026-11-04T08:31:44+00:00",
        "title": "t+00:00",
        "dependencies": [{"created_at": "2026-11-04T08:31:46+00:00"}],
    }
    assert intake_beads._spelled_as_bd(row) == {
        "created_at": "2026-11-04T08:31:44Z",
        "title": "t+00:00",
        "dependencies": [{"created_at": "2026-11-04T08:31:46Z"}],
    }


def test_a_metadata_document_is_data_and_keeps_its_own_timestamp_spelling() -> None:
    """A thread comment's ``created_at`` inside ``intake.thread`` is the document's, not a bead
    field: respelling it would change what the compare-and-set expects and what is read back."""
    document = {"comments": {"c1": {"created_at": "2026-01-01T00:00:00+00:00"}}}
    row = {"created_at": "2026-01-01T00:00:00+00:00", "metadata": {"intake.thread": document}}

    spelled = intake_beads._spelled_as_bd(row)

    assert spelled["created_at"] == "2026-01-01T00:00:00Z"
    assert spelled["metadata"] == {"intake.thread": document}


def test_every_outbox_document_comes_from_one_listing_over_the_api() -> None:
    """``read_outboxes`` (bh-pc9rf): the service's rows carry their metadata, so a scan of N
    reports is ONE listing, not a listing plus N reads, and no ``bd`` at all."""
    store, bd, service = _rig()
    reports = [store.create(draft(f"report {i}", bound=True)) for i in range(4)]
    for i, report in enumerate(reports):
        store.write_outbox(report, outbox(2 + i, texts=(f"t{i}",)), expect_seq=0, actor=ACTOR)
    bd.calls.clear()
    service.requests.clear()

    docs = dict(store.read_outboxes())
    requests = list(service.operations())

    assert bd.calls == [] and len(requests) == 1  # one listing request, nothing per report
    assert docs == {report: store.read_outbox(report) for report in reports}


# -- the unit-of-work session (ReportSession, bh-25fvb) -----------------------------------------


def _reads(service: FakeBeadsService, report: str) -> int:
    return sum(1 for op in service.operations() if op == f"GET /v0/beads/issues/{report}")


def _plain(bd: FakeBd | None = None) -> tuple[BdReportStore, FakeBd, FakeBeadsService]:
    """``work.beads.route=cli``: the plain ``bd`` store, no service and no compare-and-set."""
    bd = bd or FakeBd()
    store = BdReportStore(CliRoutes(bd, "/hive"), SCOPE, error_line=err_line)
    return store, bd, FakeBeadsService(bd)


ROUTES = {
    "api": lambda: _rig(),
    "cli-fallback": lambda: _rig(api=False),
    "cli": lambda: _plain(),
}


@pytest.mark.parametrize(
    ("route", "within", "after"), [("api", 1, 2), ("cli-fallback", 3, 4), ("cli", 3, 4)]
)
def test_inside_a_session_a_report_is_read_once_and_its_own_writes_are_kept(
    route, within, after
) -> None:
    store, bd, service = ROUTES[route]()
    report = store.create(draft(bound=True))
    store.write_outbox(report, outbox(2), expect_seq=0, actor=ACTOR)
    bd.calls.clear()
    service.requests.clear()

    def reads() -> int:
        return _reads(service, report) if route == "api" else _verbs(bd).count("show")

    with store.session():
        first = store.read_outbox(report)
        assert store.get(report) is not None and store.read_thread(report) is None
        store.write_outbox(report, outbox(3), expect_seq=first.seq, actor=ACTOR)  # write-ahead
        store.write_outbox(report, outbox(4), expect_seq=3, actor=ACTOR)  # receipt
        assert store.read_outbox(report).seq == 4  # the session keeps what it wrote

    # over the API ONE read serves the unit of work (each write is a compare-and-set); with no
    # compare-and-set each write's seq re-read reads the store (bh-25fvb B3): 1 + 2 writes
    assert reads() == within
    assert store.read_outbox(report).seq == 4  # the store holds it: the session was not a cache
    assert reads() == after  # outside the session every read reads


@pytest.mark.parametrize("route", ["cli-fallback", "cli"])
def test_without_compare_and_set_the_seq_re_read_never_comes_from_the_session(route) -> None:
    store, bd, _service = ROUTES[route]()
    report = store.create(draft(bound=True))
    store.write_outbox(report, outbox(2), expect_seq=0, actor=ACTOR)
    bd.calls.clear()

    with store.session():
        assert store.read_outbox(report).seq == 2
        assert store.read_outbox(report).seq == 2  # an ordinary read is served
        assert _verbs(bd).count("show") == 1
        store.write_outbox(report, outbox(3), expect_seq=2, actor=ACTOR)
        assert _verbs(bd).count("show") == 2  # the write's seq re-read read the store
        assert store.read_outbox(report).seq == 3  # and the session keeps what it wrote
        assert _verbs(bd).count("show") == 2


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_a_session_still_refuses_a_document_moved_underneath(route) -> None:
    store, bd, _service = ROUTES[route]()
    report = store.create(draft(bound=True))
    store.write_outbox(report, outbox(2), expect_seq=0, actor=ACTOR)

    with store.session():
        doc = store.read_outbox(report)
        bd.issues[report]["metadata"]["intake.outbox"]["seq"] = 7  # another writer, unlocked
        if route == "api":  # the service's compare-and-set refuses the write
            with pytest.raises(IntakeStoreError) as failed:
                store.write_outbox(report, outbox(3), expect_seq=doc.seq, actor=ACTOR)
            assert "changed since it was read" in failed.value.detail
        else:  # no compare-and-set: the seq re-read reads the store, never the session
            with pytest.raises(IntakeRefused) as refused:
                store.write_outbox(report, outbox(3), expect_seq=doc.seq, actor=ACTOR)
            assert refused.value.reason is RefusalReason.OUTBOX_MOVED
        assert store.read_outbox(report).seq == 7  # the session no longer holds the stale row

    assert bd.issues[report]["metadata"]["intake.outbox"]["seq"] == 7  # nothing overwritten


def test_any_other_write_in_a_session_drops_what_it_read() -> None:
    store, _bd, service = _rig()
    report = store.create(draft(bound=True))
    service.requests.clear()

    with store.session():
        assert "intake:untriaged" not in store.get(report).labels
        store.set_state(report, "intake", "untriaged", reason="imp", actor=ACTOR)
        assert "intake:untriaged" in store.get(report).labels  # read again, not served stale
        assert _reads(service, report) == 2
