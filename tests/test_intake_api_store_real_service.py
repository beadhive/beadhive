"""Opt-in conformance: ``ApiReportStore`` against a REAL Beads 1.3.0 service and a real ``bd`` CLI,
on a disposable scratch hive this test creates itself (never a managed hive).

What is proven that the fake service cannot prove: every write that moved to the API (type and
priority, assignment, tracking edges, the three metadata documents, the plain and the held close)
leaves the SAME bead as the CLI route does, field for field (labels, metadata documents, the
dependency edges and the gate's state), and a document that moved between the read and the write is
refused by the service's compare-and-set where ``bd update --metadata`` would overwrite it.

Opt-in via ``BEADS_INTAKE_SCRATCH=1``, the same explicit-env self-skip convention as the other
``real_service`` tests (``bd serve`` needs an OWNED-mode ``bd init --server`` hive)::

    BEADS_INTAKE_SCRATCH=1 uv run pytest tests/test_intake_api_store_real_service.py -m real_service
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from beadhive.adapters.intake import ApiReportStore, BdReportStore
from beadhive.intake_beads import INTAKE_CAPABILITIES, SessionReportApi
from beadhive.modules.intake import (
    HiveTriplet,
    IntakeStoreError,
    IssueBinding,
    IssueStatus,
    ReportDraft,
    SourceSnapshot,
    StoreScope,
    WorkDraft,
)
from beadhive.testing.intake import thread
from beadhive.testing.intake.cases import outbox
from beadhive_bd_cli import CliRoutes, SubprocessBd, err_line
from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint

pytestmark = pytest.mark.real_service

SCOPE = StoreScope("isc", HiveTriplet("github", "o", "r"), frozenset({"pub"}))
ACTOR = "dev/parity"


def _bd(*args: str, cwd: Path) -> Any:
    result = subprocess.run(
        ["bd", *args, "--json"], cwd=cwd, check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def _row(cwd: Path, bead: str) -> dict:
    shown = _bd("show", bead, cwd=cwd)
    return shown[0] if isinstance(shown, list) else shown


def _reap(beads_dir: Path) -> None:
    """Terminate the Dolt SQL server and the detached ``bd db-proxy-child`` an OWNED-mode scratch
    hive leaves behind (the discipline of ``tests/test_work_queue.py``)."""
    for pid_file, is_json in (
        (beads_dir / "dolt-server.pid", False),
        (beads_dir / "dolt" / "proxy.pid", True),
    ):
        if not pid_file.is_file():
            continue
        with contextlib.suppress(OSError, ValueError, json.JSONDecodeError, KeyError):
            text = pid_file.read_text().strip()
            pid = int(json.loads(text)["pid"]) if is_json else int(text)
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                for _ in range(50):
                    time.sleep(0.1)
                    os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)


def _free_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return int(reservation.getsockname()[1])


@pytest.fixture
def scratch(tmp_path):
    if not os.environ.get("BEADS_INTAKE_SCRATCH"):
        pytest.skip("set BEADS_INTAKE_SCRATCH=1 to run the real-bd scratch-hive proof")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["bd", "init", "--server", "--prefix", "isc", "--non-interactive"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    proc = None
    try:
        port = _free_port()
        proc = subprocess.Popen(
            ["bd", "serve", "--addr", f"127.0.0.1:{port}"],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                    break
            time.sleep(0.1)
        else:
            raise RuntimeError("bd serve never became healthy")
        context = _bd("context", cwd=tmp_path)
        expected = ExpectedContext(
            context["project_id"],
            context["database"],
            required_capabilities=INTAKE_CAPABILITIES,
            repo_root=tmp_path,
        )
        with BeadsSession(RemoteEndpoint(base), expected) as session:
            routes = CliRoutes(SubprocessBd(timeout=120), tmp_path)
            cli = BdReportStore(routes, SCOPE, error_line=err_line)
            api = ApiReportStore(
                routes, SCOPE, api=lambda: SessionReportApi(session), error_line=err_line
            )
            yield tmp_path, cli, api
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            if proc.poll() is None:
                proc.kill()
        _reap(tmp_path / ".beads")


_VOLATILE = {"created_at", "updated_at", "closed_at", "started_at", "revision"}


def _normalised(row: dict, ids: dict[str, str]) -> dict:
    row = dict(row)
    for dependency in row.get("dependencies", []):
        if dependency.get("issue_type") == "gate":
            ids = {**ids, dependency["id"]: "GATE"}
    text = json.dumps(row, sort_keys=True)
    for old, new in ids.items():
        text = text.replace(old, new)

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            if "dependencies" in value:
                kinds = sorted(value["dependencies"], key=lambda d: d["issue_type"])
                value = {**value, "dependencies": kinds}
            return {k: strip(v) for k, v in value.items() if k not in _VOLATILE}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    return strip(json.loads(text))


def _scenario(store: BdReportStore, hive: Path, number: int) -> dict[str, dict]:
    url = f"https://github.com/o/r/issues/{number}"
    binding = IssueBinding("", "github", "o/r", url, f"#{number}")
    snapshot = SourceSnapshot("t", "b", IssueStatus.OPEN, "r1")
    report = store.create(
        ReportDraft(f"Parity {number}", "bug", "origin:report", ACTOR, "steps", binding, snapshot)
    )
    store.set_state(report, "origin", "github", reason="imp", actor=ACTOR)
    store.set_state(report, "intake", "untriaged", reason="imp", actor=ACTOR)
    work = store.create_work(WorkDraft("Work", "task", ACTOR, description="d", priority="3"))
    ids = {report: "REPORT", work: "WORK"}
    seen: dict[str, dict] = {}

    def snap(name: str, bead: str = report) -> None:
        seen[name] = _normalised(_row(hive, bead), ids)

    store.update_fields(report, issue_type="feature", priority="P1", actor="dev/u")
    snap("update")
    store.assign(report, "disp/x", actor="dev/u")
    snap("assign")
    store.track(report, work, actor="dev/t")
    snap("track")
    assert store.tracked_work(report) == work and store.trackers_of(work) == (report,)
    store.untrack(report, work, actor="dev/t")
    snap("untrack")
    store.track(report, work, actor="dev/t")
    store.record_snapshot(report, SourceSnapshot("t2", "b2", IssueStatus.CLOSED, "r2"), actor="s")
    snap("snapshot")
    store.write_thread(report, thread(seq=3, url=url), expect_seq=0, actor="dev/s")
    store.write_outbox(report, outbox(url=url), expect_seq=0, actor="dev/s")
    snap("documents")
    assert store.is_held(report)
    assert [h.blocks for h in store.holds() if report in h.blocks] == [(report,)]
    store.close(work, reason="done", actor="dev/c")
    snap("work-closed", work)
    store.close(report, reason="rejected", actor="dev/c")  # the held close
    snap("closed")
    (gate,) = [d["id"] for d in _row(hive, report)["dependencies"] if d["issue_type"] == "gate"]
    seen["gate"] = _normalised(_row(hive, gate), {**ids, gate: "GATE"})
    return seen


def test_every_moved_write_leaves_the_same_bead_as_the_cli_route(scratch) -> None:
    hive, cli, api = scratch

    by_cli = _scenario(cli, hive, 101)
    by_api = _scenario(api, hive, 102)

    # the reads agree too: the queue, the bound reports and a lookup answer the same on both routes
    assert sorted(v.id for v in api.list_open_labeled("intake:untriaged")) == sorted(
        v.id for v in cli.list_open_labeled("intake:untriaged")
    )
    assert sorted(v.id for v in api.bound_reports()) == sorted(v.id for v in cli.bound_reports())
    url = "https://github.com/o/r/issues/101"
    assert [v.id for v in api.find_by_external_ref(url)] == [
        v.id for v in cli.find_by_external_ref(url)
    ]

    # the issue number appears in the external_ref and the document subject: same shape, own number
    def renumbered(seen: dict[str, dict], number: int) -> str:
        return json.dumps(seen, sort_keys=True).replace(str(number), "N")

    assert renumbered(by_api, 102) == renumbered(by_cli, 101)


def test_a_document_that_moved_after_the_read_is_refused_not_overwritten(scratch) -> None:
    hive, _cli, api = scratch
    url = "https://github.com/o/r/issues/201"
    binding = IssueBinding("", "github", "o/r", url, "#201")
    report = api.create(
        ReportDraft(
            "moved",
            "bug",
            "origin:report",
            ACTOR,
            "s",
            binding,
            SourceSnapshot("t", "b", IssueStatus.OPEN, "r1"),
        )
    )
    stale = api._show(report)
    _bd(
        "update",
        report,
        "--metadata",
        json.dumps({"intake.source": {"schema": 1, "moved": 1}}),
        cwd=hive,
    )

    with pytest.raises(IntakeStoreError) as refused:
        api._set_document(
            report,
            "intake.source",
            {"schema": 1},
            row=stale,
            actor=ACTOR,
            operation="record_snapshot",
        )

    assert "changed since it was read" in refused.value.detail
    assert _row(hive, report)["metadata"]["intake.source"] == {"schema": 1, "moved": 1}


def test_a_lookup_finds_a_bead_bound_by_external_ref_alone_through_the_cli_confirm(scratch) -> None:
    hive, _cli, api = scratch
    url = "https://github.com/o/r/issues/301"
    created = _bd("create", "Adopted", "--type", "bug", "--external-ref", url, cwd=hive)

    assert [v.id for v in api.find_by_external_ref(url)] == [created["id"]]
    assert api.find_by_external_ref("https://github.com/o/r/issues/302") == ()
