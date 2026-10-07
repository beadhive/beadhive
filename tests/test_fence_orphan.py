"""Unit tests for the managed divert and the orphan merge (bh-4z3oz) — no Dolt, no bd.

The E5 test drives :func:`beadhive.fence_orphan.divert_if_superseded` through a real
:class:`beadhive.fence_data.BdServerEngine` whose ``bd`` seam is recorded: the divert's reset
must go through ``reset_to_remote``, so every forwarder session is killed after the orphan is
published and before ``DOLT_RESET`` — an in-flight forwarded write is refused, not acknowledged
and then dropped (``bh-uhx2r`` E5, M12 ``bh-g7dlo``).
"""

from __future__ import annotations

import json
import subprocess
from collections import namedtuple

import pytest

from beadhive import bd, engine, fence_data, fence_orphan, guard, host_fence
from beadhive import fence_data_port as port
from beadhive import hive_forward as hf
from beadhive.writer_adopt import DataUnreachable, WriterRow

Completed = namedtuple("Completed", "returncode stdout stderr")
DB = "fx"
MAIN = "a" * 32
REMOTE = "b" * 32


def _hive(tmp_path):
    hive = tmp_path / "hive"
    (hive / ".beads").mkdir(parents=True)
    meta = {"database": "dolt", "backend": "dolt", "dolt_mode": "server", "dolt_database": DB}
    (hive / ".beads" / "metadata.json").write_text(json.dumps(meta))
    return hive


class Recording(fence_data.BdServerEngine):
    """A server-mode engine whose ``bd`` seam answers from a script and records every call."""

    def __init__(self, hive, *, held=1, remote=2, published=False, push_fails=False):
        super().__init__(hive)
        self.calls: list[str] = []
        self.held, self.remote_epoch = held, remote
        self.published, self.push_fails = published, push_fails
        self.pushed: str | None = None
        self.processes = [(11, "fwd-e1", "10.0.0.21"), (12, "beads", "localhost")]

    def _bd(self, *args):
        statement = args[-1]
        self.calls.append(" ".join(args) if args[0] == "dolt" else statement)
        out: object = []
        if "processlist" in statement:
            out = [{"id": i, "user": u, "host": h} for i, u, h in self.processes]
        elif "FROM bh_writer AS OF 'origin/main'" in statement:
            out = [{"frame": "new", "epoch": self.remote_epoch}]
        elif "FROM bh_writer AS OF 'main'" in statement:
            out = [{"frame": "old", "epoch": self.held}]
        elif "AS published" in statement:
            out = [{"published": 1 if self.published else 0}]
        elif "hashof('main')" in statement:
            out = [{"h": MAIN}]
        elif "dolt_remote_branches" in statement:
            out = [{"name": "remotes/origin/main", "hash": REMOTE}]
            if self.pushed:
                out.append({"name": f"remotes/origin/{self.pushed}", "hash": MAIN})
        elif statement.startswith("CALL DOLT_PUSH"):
            if self.push_fails:
                return subprocess.CompletedProcess(["bd", *args], 1, "", "remote unreachable")
            self.pushed = statement.split("main:")[1].split("'")[0]
            out = {"rows_affected": 0}
        elif "dolt_status" in statement:
            out = []
        return subprocess.CompletedProcess(["bd", *args], 0, json.dumps(out), "")


@pytest.fixture
def quiesce_on(monkeypatch):
    monkeypatch.delenv(hf.QUIESCE_ENV, raising=False)
    monkeypatch.setenv("BEADS_DOLT_SERVER_USER", "beads")
    defaults = hf.serve_settings({})
    monkeypatch.setattr(hf, "serve_settings", lambda cfg=None: defaults)


def _first(calls, prefix):
    return next(i for i, c in enumerate(calls) if c.startswith(prefix))


# ---- E5: the divert's reset kills forwarder sessions first ------------------------------------


def test_divert_publishes_the_orphan_then_kills_forwarders_before_the_reset(tmp_path, quiesce_on):
    eng = Recording(_hive(tmp_path))
    out = fence_orphan.divert_if_superseded(fence_data.FenceNode(eng), frame="old")

    assert out.superseded and out.reset and out.branch == "frame/old/orphan-1-1"
    calls = eng.calls
    push, kill, reset = (
        _first(calls, "CALL DOLT_PUSH"),
        _first(calls, "KILL "),
        _first(calls, "CALL DOLT_RESET"),
    )
    assert push < kill < reset  # orphan on the remote, then quiesce, then the reset
    assert [c for c in calls if c.startswith("KILL")] == ["KILL 11"]  # bd's own login spared
    assert "main:frame/old/orphan-1-1" in calls[push]
    # main itself is never pushed: no `bd dolt push`, no DOLT_PUSH of main to main
    assert not [c for c in calls if c.startswith("dolt push")]
    assert not [c for c in calls if "DOLT_PUSH" in c and "main:frame/" not in c]


def test_a_failed_orphan_push_neither_quiesces_nor_resets(tmp_path, quiesce_on):
    eng = Recording(_hive(tmp_path), push_fails=True)
    with pytest.raises(fence_orphan.DivertFailed, match="NOT reset; nothing was lost"):
        fence_orphan.divert_if_superseded(fence_data.FenceNode(eng), frame="old")
    assert not [c for c in eng.calls if c.startswith(("KILL", "CALL DOLT_RESET"))]


def test_superseded_with_nothing_unpublished_still_quiesces_before_reset(tmp_path, quiesce_on):
    eng = Recording(_hive(tmp_path), published=True)
    out = fence_orphan.divert_if_superseded(fence_data.FenceNode(eng), frame="old")
    assert out.superseded and out.branch is None and out.reset
    assert _first(eng.calls, "KILL ") < _first(eng.calls, "CALL DOLT_RESET")
    assert not [c for c in eng.calls if c.startswith("CALL DOLT_PUSH")]


def test_a_current_frame_touches_nothing(tmp_path, quiesce_on):
    eng = Recording(_hive(tmp_path), held=2, remote=2)
    out = fence_orphan.divert_if_superseded(fence_data.FenceNode(eng), frame="old")
    assert not out.superseded
    assert not [c for c in eng.calls if c.startswith(("KILL", "CALL DOLT_RESET", "CALL DOLT_PUSH"))]
    assert not [c for c in eng.calls if c.startswith("dolt commit")]


# ---- names ------------------------------------------------------------------------------------


def test_orphan_names_round_trip_and_refuse_odd_frames():
    assert fence_orphan.orphan_branch("f-1.x", 3, 2) == "frame/f-1.x/orphan-3-2"
    assert fence_orphan.parse_orphan("frame/f-1.x/orphan-3-2") == ("f-1.x", 3, "2")
    assert fence_orphan.parse_orphan("frame/f/orphan") is None
    assert fence_orphan.parse_orphan("main") is None
    assert fence_orphan.parse_orphan("frame/a'b/orphan-1-1") is None
    with pytest.raises(fence_orphan.OrphanError):
        fence_orphan.orphan_branch("a b", 1, 1)


def test_merge_message_is_the_sanctioned_subject():
    msg = fence_orphan.merge_message("frame/a/orphan-1-1", 2)
    assert msg.startswith(fence_orphan.MERGE_PREFIX)
    assert ";" not in msg and "'" not in msg


# ---- managed_preflight: dormant on legacy hives, fail closed on cut-over ones ------------------


def _cut_over(monkeypatch, node, frame="old"):
    monkeypatch.setattr(guard, "writer_state", lambda **_k: ("fx", frame, WriterRow(frame, 1)))
    port.set_fence_data_resolver(lambda _p, _d: node)


@pytest.fixture(autouse=True)
def _restore_resolver():
    yield
    port.set_fence_data_resolver(None)


def test_preflight_is_a_no_op_on_a_legacy_hive(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "writer_state", lambda **_k: None)
    port.set_fence_data_resolver(lambda *_a: pytest.fail("legacy hives never resolve a node"))
    assert fence_orphan.managed_preflight(tmp_path, commit_result=Completed(1, "", "boom")) is None


def test_preflight_refuses_when_the_server_mode_commit_failed(monkeypatch, tmp_path):
    eng = Recording(_hive(tmp_path), held=2, remote=2)
    _cut_over(monkeypatch, fence_data.FenceNode(eng))
    failed = Completed(1, "", "Error: dolt commit: lock held")
    refused = fence_orphan.managed_preflight(tmp_path, commit_result=failed)
    assert refused and "commit-before-push failed" in refused and "lock held" in refused
    assert eng.calls == []  # refused before any fetch
    nothing = Completed(0, "Nothing to commit.\n", "")
    assert fence_orphan.managed_preflight(tmp_path, commit_result=nothing) is None


def test_preflight_reports_the_divert(monkeypatch, tmp_path, quiesce_on):
    eng = Recording(_hive(tmp_path))
    _cut_over(monkeypatch, fence_data.FenceNode(eng))
    refused = fence_orphan.managed_preflight(tmp_path, commit_result=Completed(0, "", ""))
    assert "superseded" in refused and "main was NOT pushed" in refused
    assert "frame/old/orphan-1-1" in refused and "orphan-merge" in refused


def test_preflight_fails_closed_when_the_remote_epoch_cannot_be_read(monkeypatch, tmp_path):
    class Unreachable(fence_data.FenceNode):
        def fetch(self):
            raise DataUnreachable("partitioned")

    _cut_over(monkeypatch, Unreachable(Recording(_hive(tmp_path))))
    refused = fence_orphan.managed_preflight(tmp_path, commit_result=Completed(0, "", ""))
    assert "fail closed" in refused and "partitioned" in refused


def test_preflight_refuses_on_an_unreadable_local_writer(monkeypatch, tmp_path):
    def unreadable(**_k):
        raise guard.WriterUnreadable("fx: cannot read the local bh_writer")

    monkeypatch.setattr(guard, "writer_state", unreadable)
    refused = fence_orphan.managed_preflight(tmp_path)
    assert refused and "cannot read the local bh_writer" in refused


# ---- Engine.push_state wiring --------------------------------------------------------------


def test_push_state_never_pushes_main_when_the_preflight_refuses(monkeypatch):
    calls = []
    monkeypatch.setattr(bd, "_run", lambda cmd, **_k: calls.append(cmd) or Completed(0, "", ""))
    monkeypatch.setattr(
        fence_orphan, "managed_preflight", lambda *_a, **_k: "fx: superseded — diverted"
    )
    monkeypatch.setattr(
        host_fence,
        "reserve_managed_push",
        lambda *_a, **_k: pytest.fail("a refused preflight never reserves the fence"),
    )
    result = engine.BdEngine().push_state("/hive", message="m")
    assert result.returncode == 1 and "diverted" in result.stderr
    assert [c[-3:-1] for c in calls] == [["commit", "-m"]]  # committed, never pushed


def test_push_state_passes_the_commit_result_to_the_preflight(monkeypatch):
    seen = {}

    def fake_run(cmd, **_k):
        if "commit" in cmd:
            return Completed(1, "", "commit exploded")
        return Completed(0, "", "")

    def preflight(cwd, *, commit_result=None, cfg=None):
        seen["commit"] = commit_result
        return None

    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(fence_orphan, "managed_preflight", preflight)
    monkeypatch.setattr(host_fence, "reserve_managed_push", lambda *_a, **_k: None)
    engine.BdEngine().push_state("/hive", message="m")
    assert seen["commit"].stderr == "commit exploded"


def test_a_non_fast_forward_push_re_checks_and_reports_the_divert(monkeypatch):
    checks = []

    def fake_run(cmd, **_k):
        if cmd[-2:] == ["dolt", "push"]:
            return Completed(1, "", "! [rejected] main -> main (non-fast-forward)")
        return Completed(0, "", "")

    def preflight(cwd, *, commit_result=None, cfg=None):
        checks.append(commit_result is not None)
        return None if len(checks) == 1 else "fx: superseded — diverted to frame/old/orphan-1-1"

    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(fence_orphan, "managed_preflight", preflight)
    monkeypatch.setattr(host_fence, "reserve_managed_push", lambda *_a, **_k: None)
    result = engine.BdEngine().push_state("/hive", message="m")
    assert checks == [True, False]
    assert result.returncode == 1 and "orphan-1-1" in result.stderr


def test_an_unreachable_push_is_not_re_checked(monkeypatch):
    checks = []

    def fake_run(cmd, **_k):
        if cmd[-2:] == ["dolt", "push"]:
            return Completed(1, "", "fatal: could not read from remote")
        return Completed(0, "", "")

    monkeypatch.setattr(bd, "_run", fake_run)
    monkeypatch.setattr(
        fence_orphan, "managed_preflight", lambda *_a, **_k: checks.append(1) or None
    )
    monkeypatch.setattr(host_fence, "reserve_managed_push", lambda *_a, **_k: None)
    result = engine.BdEngine().push_state("/hive", message="m")
    assert checks == [1] and result.returncode == 1
