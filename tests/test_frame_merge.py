"""The writer's merge of ``open``-policy frame refs (bh-sfp9o.5): unit level, fakes only.

No real hive, no Dolt, no network; the clock is injected. The real merge against real bd and
Dolt is ``tests/test_frame_merge_int.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from beadhive import (
    frame_merge,
    hive_frames_cli,
    hq_why,
    hq_why_layer_write_policy,
    write_policy,
)
from beadhive.frame_merge import ConflictRow, FrameMergeOutcome, Status
from beadhive.writer_adopt import DataUnreachable, WriterRow

HIVE = "fm"
HEAD = "a" * 32
MAIN = "b" * 32


class Clock:
    def __init__(self, at: float = 100.0):
        self.at = at

    def __call__(self) -> float:
        return self.at


def _conflict(bead="fm-1", main="b", frame="c", table="issues"):
    return ConflictRow(table, bead, bead, {"assignee": main}, {"assignee": frame})


def _held(frame="c", rows=None, **kw) -> FrameMergeOutcome:
    rows = tuple(rows if rows is not None else [_conflict()])
    return FrameMergeOutcome(
        HIVE,
        frame,
        Status.CONFLICT,
        10.0,
        frame_head=HEAD,
        main_before=MAIN,
        conflicts=rows,
        double_claims=frame_merge.double_claims(rows, frame),
        **kw,
    )


# ---- the typed outcome and its record -------------------------------------------------------


def test_outcome_round_trips_through_its_record(tmp_path):
    state = tmp_path / "frame-merge.json"
    held = _held(resolutions={"fm-1": "main"})
    frame_merge.record(held, path=state)
    frame_merge.record(
        FrameMergeOutcome(HIVE, "d", Status.MERGED, 11.0, commit="c" * 32), path=state
    )
    loaded = frame_merge.load(path=state)
    assert set(loaded[HIVE]) == {"c", "d"}
    assert loaded[HIVE]["c"] == held
    assert loaded[HIVE]["c"].ref == "refs/dolt/frame/c"
    assert loaded[HIVE]["c"].unresolved == ()
    assert frame_merge.recorded(HIVE, "nope", path=state) is None


def test_an_unknown_status_is_refused():
    with pytest.raises(ValueError, match="unknown frame merge status"):
        FrameMergeOutcome(HIVE, "c", "strategy_ours", 0.0)


def test_a_double_claim_names_both_holders():
    rows = [
        _conflict("fm-1", "b", "c"),
        _conflict("fm-2", "b", "b"),  # same holder: no double claim
        _conflict("fm-3", "", "c"),  # main unclaimed: a plain conflict
        ConflictRow("issues", "fm-4", "fm-4", {"priority": 1}, {"priority": 3}),
    ]
    [claim] = frame_merge.double_claims(rows, "c")
    assert (claim.bead, claim.holders, claim.frame) == ("fm-1", ("b", "c"), "c")
    assert "claimed twice: by b on main and by c on refs/dolt/frame/c" in claim.describe()


def test_doctor_and_why_report_held_refs_per_hive_and_frame(tmp_path):
    state = tmp_path / "frame-merge.json"
    frame_merge.record(_held(), path=state)
    frame_merge.record(FrameMergeOutcome(HIVE, "d", Status.UP_TO_DATE, 1.0), path=state)
    data = frame_merge.doctor_data(path=state)
    assert (data["level"], data["held"]) == ("warn", 1)
    lines = frame_merge.doctor_lines(data)
    assert any("⚠ fm: refs/dolt/frame/c is held unmerged" in line for line in lines)
    assert any("fm-1 is claimed twice: by b on main and by c" in line for line in lines)
    assert any("issues fm-1: main {assignee='b'} | frame {assignee='c'}" in ln for ln in lines)
    assert any("hive frames resolve fm --frame c --bead <bead>" in line for line in lines)
    assert any("✓ fm: refs/dolt/frame/d is already in main" in line for line in lines)

    text, held = frame_merge.why_summary(HIVE, path=state)
    assert "refs/dolt/frame/c" in text and "refs/dolt/frame/d" not in text
    assert [item["frame"] for item in held] == ["c"]
    assert frame_merge.why_summary("other", path=state) == ("", [])


def test_the_write_policy_doctor_section_carries_frame_merges(tmp_path):
    state = tmp_path / "frame-merge.json"
    frame_merge.record(_held(), path=state)
    data = write_policy.doctor_data(
        {"managed_repos": [{"prefix": HIVE}]},
        profile=write_policy.trust_profile({}),
        host={},
        events=tmp_path / "events.jsonl",
        hive_dir=lambda _e: None,
        frame_state=state,
    )
    assert data["level"] == "warn" and data["frame_merges"]["held"] == 1
    assert any("refs/dolt/frame/c is held" in line for line in write_policy.doctor_lines(data))


def test_why_names_a_held_frame_ref_without_refusing(monkeypatch):
    ctx = hq_why.WhyContext(HIVE, "author", cfg={})
    ctx.memo.update(entry={"prefix": HIVE})
    plain = hq_why.Verdict("write_policy", hq_why.OK, "writer", "open; this host writes")
    monkeypatch.setattr(hq_why_layer_write_policy, "_check", lambda _ctx: plain)
    monkeypatch.setattr(frame_merge, "why_summary", lambda hive: ("", []))
    assert hq_why_layer_write_policy.check_write_policy(ctx) == plain

    held = _held().as_dict()
    monkeypatch.setattr(frame_merge, "why_summary", lambda hive: ("c is held", [held]))
    verdict = hq_why_layer_write_policy.check_write_policy(ctx)
    assert verdict.status == hq_why.OK and verdict.code == "writer"
    assert "Frame refs held unmerged by the writer: c is held" in verdict.summary
    assert "hive frames status fm" in verdict.summary
    assert verdict.detail["frame_merges"] == [held]


# ---- listing frame refs ---------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self.rows = rows
        self.remote, self.branch = "origin", "main"
        self.engine = type("E", (), {"env": {"HOME": "/h"}})()

    def query(self, _sql):
        return self.rows


def test_frame_refs_are_listed_from_the_git_remote():
    node = _Rows([{"name": "origin", "url": "git+file:///srv/hive.git"}])
    seen = {}

    def runner(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"))
        out = (
            f"{HEAD}\trefs/dolt/frame/b\n{HEAD}\trefs/dolt/frame/c\n{HEAD}\trefs/dolt/frame/../x\n"
        )
        return subprocess.CompletedProcess(cmd, 0, out, "")

    assert frame_merge.list_frames(node, runner=runner) == ["b", "c"]
    assert seen["cmd"] == ["git", "ls-remote", "file:///srv/hive.git", "refs/dolt/frame/*"]
    assert seen["env"] == {"HOME": "/h"}


def test_a_non_git_remote_has_no_frame_refs_and_a_failed_listing_raises():
    assert frame_merge.list_frames(_Rows([{"name": "origin", "url": "file:///x"}])) == []
    node = _Rows([{"name": "origin", "url": "git+ssh://h/x.git"}])
    failing = lambda cmd, **_kw: subprocess.CompletedProcess(cmd, 128, "", "denied")  # noqa: E731
    with pytest.raises(DataUnreachable, match="denied"):
        frame_merge.list_frames(node, runner=failing)


# ---- the merge (fake node) ------------------------------------------------------------------


class FakeEngine:
    def __init__(self):
        self.remote = "origin"
        self.sessions: list[list[str]] = []
        self.executed: list[list[str]] = []
        self.env = None
        self.reset = False

    def fetch(self):
        return None

    def commit(self, _message):
        return False

    def execute(self, statements):
        self.executed.append(list(statements))

    def execute_session(self, statements):
        self.sessions.append(list(statements))

    def reset_hard(self, _rev):
        self.reset = True


class FakeNode:
    """Answers the merge's reads; `conflicts` is the preview, `merged` flips after a session."""

    remote, branch = "origin", "main"

    def __init__(self, conflicts=(), *, contained=False):
        self.engine = FakeEngine()
        self.conflicts = list(conflicts)
        self.contained = contained

    def require_writer_ready(self):
        return None

    def writer(self):
        return WriterRow("a", 4)

    def query(self, sql):
        merged = bool(self.engine.sessions) and not self.engine.reset
        if "FROM dolt_remotes" in sql:
            return [{"name": "origin", "url": "git+file:///srv/hive.git", "params": "{}"}]
        if "bh_epoch_live" in sql:
            return [{"epoch": 4}]
        if sql.startswith("SELECT hashof('frame-c/main')"):
            return [{"h": HEAD}]
        if sql.startswith("SELECT hashof('main')"):
            return [{"h": "m" * 32 if merged else MAIN}]
        if "dolt_merge_base" in sql:
            return [{"m": self.contained}]
        if "PREVIEW_MERGE_CONFLICTS_SUMMARY" in sql:
            tables = sorted({row["table"] for row in self.conflicts})
            return [{"t": t, "d": 1, "s": 0} for t in tables]
        if "PREVIEW_MERGE_CONFLICTS(" in sql:
            return [row["row"] for row in self.conflicts if f"'{row['table']}')" in sql]
        if "key_column_usage" in sql:
            return [{"c": "key"}] if "'metadata'" in sql else [{"c": "id"}]
        if "information_schema.columns" in sql:
            return [{"c": "id"}, {"c": "assignee"}]
        if "dolt_log('--parents'" in sql:
            message = self.engine.sessions[-1][-1].split("IF(@bh_merging > 0, '")[1]
            return [{"p": f"{MAIN}, {HEAD}", "m": message.rsplit("', NULL))", 1)[0]}]
        if "COUNT(*)" in sql:
            return [{"n": 0}]
        return []


def _issue_row(bead, ours, theirs):
    return {
        "table": "issues",
        "row": {
            "our_id": bead,
            "their_id": bead,
            "base_id": bead,
            "our_assignee": ours,
            "their_assignee": theirs,
            "base_assignee": "",
            "our_updated_at": "t1",
            "their_updated_at": "t2",
            "our_diff_type": "modified",
            "their_diff_type": "modified",
            "dolt_conflict_id": "x",
        },
    }


CLONE_ID = {
    "table": "metadata",
    "row": {
        "our_key": "clone_id",
        "their_key": "clone_id",
        "our_value": "aaa",
        "their_value": "ccc",
        "our_diff_type": "modified",
        "their_diff_type": "modified",
    },
}


@pytest.fixture
def writer(monkeypatch):
    monkeypatch.setattr(
        frame_merge.importlib.import_module("beadhive.write_policy_publish"),
        "ensure_remote",
        lambda _engine, frame: f"frame-{frame}",
    )


def _merge(node, **kw):
    return frame_merge.merge_frame(node, "c", hive=HIVE, writer_frame="a", now=Clock(), **kw)


def test_a_conflicting_ref_is_held_unmerged_and_nothing_is_written(writer):
    node = FakeNode([_issue_row("fm-1", "b", "c"), CLONE_ID])
    outcome = _merge(node)
    assert outcome.status == Status.CONFLICT
    assert node.engine.sessions == []  # no merge ran
    [row] = outcome.conflicts  # clone_id is bookkeeping, never a conflict
    assert (row.table, row.bead, row.main, row.frame) == (
        "issues",
        "fm-1",
        {"assignee": "b"},
        {"assignee": "c"},
    )  # updated_at differs on every write and is not reported
    assert [c.holders for c in outcome.double_claims] == [("b", "c")]
    assert (outcome.frame_head, outcome.main_before, outcome.at) == (HEAD, MAIN, 100.0)


def test_a_clean_ref_merges_in_one_session_without_strategy(writer):
    node = FakeNode([CLONE_ID])
    outcome = _merge(node)
    assert outcome.status == Status.MERGED, outcome.error
    assert outcome.kept_main == ("metadata.clone_id",)
    [session] = node.engine.sessions
    text = "\n".join(session)
    assert "--strategy" not in text and "--theirs" not in text and "--ours" not in text
    assert "CALL DOLT_MERGE('--no-ff', '--no-commit', 'frame-c/main')" in session
    assert "DELETE FROM dolt_conflicts_metadata WHERE COALESCE(our_key" in text
    assert "UPDATE metadata SET value = (SELECT value FROM metadata AS OF 'HEAD'" in text
    assert session[-1].startswith("CALL DOLT_COMMIT('-Am', IF(@bh_merging > 0, 'bh: merge frame/c")


def test_an_already_merged_ref_is_up_to_date(writer):
    node = FakeNode([_issue_row("fm-1", "b", "c")], contained=True)
    assert _merge(node).status == Status.UP_TO_DATE
    assert node.engine.sessions == []


def test_resolutions_drop_mains_rows_and_take_the_frames(writer):
    node = FakeNode([_issue_row("fm-1", "b", "c"), _issue_row("fm-2", "b", "d")])
    partial = _merge(node, resolutions={"fm-1": "frame"})
    assert partial.status == Status.CONFLICT and partial.unresolved == ("fm-2",)
    assert node.engine.sessions == []
    outcome = _merge(node, resolutions={"fm-1": "frame", "fm-2": "main", "gone": "main"})
    assert outcome.status == Status.MERGED, outcome.error
    assert outcome.resolutions == {"fm-1": "frame", "fm-2": "main"}
    [session] = node.engine.sessions
    drop = "DELETE FROM dolt_conflicts_issues WHERE COALESCE(our_id, their_id, base_id) = 'fm-2'"
    assert drop in session
    assert session.index(drop) < session.index("CALL DOLT_CONFLICTS_RESOLVE('--theirs', 'issues')")
    assert not any("UPDATE issues" in s for s in session)  # never DML on a guarded table
    assert "resolved fm-1=frame, fm-2=main" in session[-1]


def test_a_merge_that_did_not_land_is_reported_failed(writer, monkeypatch):
    node = FakeNode([])
    monkeypatch.setattr(
        frame_merge, "_postflight", lambda *_a: ["HEAD is not the merge of main and the ref"]
    )
    outcome = _merge(node)
    assert outcome.status == Status.FAILED
    assert "did not land" in outcome.error and "main was restored to bbbbbbbbbbbb" in outcome.error


def test_only_the_writer_merges(writer):
    node = FakeNode([])
    with pytest.raises(frame_merge.FrameMergeRefused, match="only the hive's writer"):
        frame_merge.merge_frame(node, "c", hive=HIVE, writer_frame="b", now=Clock())
    with pytest.raises(frame_merge.FrameMergeRefused, match="cannot name"):
        frame_merge.merge_frame(node, "../x", hive=HIVE, writer_frame="a", now=Clock())


# ---- the hive: isolation, the sync seam, resolve -------------------------------------------


def _ctx():
    return frame_merge.WriterContext(HIVE, FakeNode(), "a")


def test_one_failing_ref_never_stops_the_others(tmp_path, monkeypatch):
    state = tmp_path / "frame-merge.json"

    def merge(_node, frame, **kw):
        if frame == "b":
            raise RuntimeError("boom")
        if frame == "c":
            return _held()
        return FrameMergeOutcome(HIVE, frame, Status.MERGED, kw["now"](), commit="x")

    monkeypatch.setattr(frame_merge, "merge_frame", merge)
    outcomes = frame_merge.merge_hive(
        _ctx(), now=Clock(), path=state, lister=lambda _n: ["a", "b", "c", "d"]
    )
    assert [(o.frame, o.status) for o in outcomes] == [
        ("b", Status.FAILED),
        ("c", Status.CONFLICT),
        ("d", Status.MERGED),
    ]  # the writer's own ref is skipped
    assert "boom" in outcomes[0].error
    assert set(frame_merge.load(path=state)[HIVE]) == {"b", "c", "d"}


def test_recorded_sides_are_reapplied_on_the_next_pass(tmp_path, monkeypatch):
    state = tmp_path / "frame-merge.json"
    frame_merge.record(_held(resolutions={"fm-1": "frame"}), path=state)
    seen = {}

    def merge(_node, frame, **kw):
        seen[frame] = kw["resolutions"]
        return _held(resolutions=kw["resolutions"])

    monkeypatch.setattr(frame_merge, "merge_frame", merge)
    frame_merge.merge_hive(_ctx(), frames=["c"], now=Clock(), path=state)
    assert seen == {"c": {"fm-1": "frame"}}


def test_after_pull_never_raises(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise frame_merge.FrameMergeRefused("not the writer")

    monkeypatch.setattr(frame_merge, "writer_context", refuse)
    assert frame_merge.after_pull(tmp_path) == []
    monkeypatch.setattr(frame_merge, "writer_context", lambda *_a, **_k: _ctx())

    def unlistable(*_a, **_k):
        raise DataUnreachable("remote down")

    monkeypatch.setattr(frame_merge, "merge_hive", unlistable)
    assert frame_merge.after_pull(tmp_path) == []
    done = [FrameMergeOutcome(HIVE, "c", Status.MERGED, 1.0)]
    monkeypatch.setattr(frame_merge, "merge_hive", lambda *_a, **_k: done)
    assert frame_merge.after_pull(tmp_path) == done


def test_resolve_refuses_without_an_explicit_side_or_a_recorded_conflict(tmp_path):
    state = tmp_path / "frame-merge.json"
    frame_merge.record(_held(), path=state)
    for take in (None, "", "ours", "theirs"):
        with pytest.raises(frame_merge.FrameMergeRefused, match="explicit side"):
            frame_merge.resolve(_ctx(), "c", "fm-1", take, path=state)
    with pytest.raises(frame_merge.FrameMergeRefused, match="no recorded conflict on fm-9"):
        frame_merge.resolve(_ctx(), "c", "fm-9", "main", path=state)
    assert frame_merge.recorded(HIVE, "c", path=state).resolutions == {}


def test_resolve_records_a_side_then_completes_the_merge(tmp_path, monkeypatch):
    state = tmp_path / "frame-merge.json"
    frame_merge.record(_held(rows=[_conflict("fm-1"), _conflict("fm-2")]), path=state)
    calls = []

    def merge(_node, frame, **kw):
        calls.append(kw["resolutions"])
        return FrameMergeOutcome(HIVE, frame, Status.MERGED, 2.0, resolutions=kw["resolutions"])

    monkeypatch.setattr(frame_merge, "merge_frame", merge)
    pending = frame_merge.resolve(_ctx(), "c", "fm-1", "main", path=state)
    assert pending.status == Status.CONFLICT and pending.unresolved == ("fm-2",)
    assert calls == []
    assert frame_merge.recorded(HIVE, "c", path=state).resolutions == {"fm-1": "main"}
    done = frame_merge.resolve(_ctx(), "c", "fm-2", "frame", path=state)
    assert done.status == Status.MERGED
    assert calls == [{"fm-1": "main", "fm-2": "frame"}]
    assert frame_merge.recorded(HIVE, "c", path=state).status == Status.MERGED


def test_writer_context_refuses_off_the_writer(monkeypatch, tmp_path):
    fence_data = frame_merge.importlib.import_module("beadhive.fence_data")
    guard = frame_merge.importlib.import_module("beadhive.guard")
    monkeypatch.setattr(fence_data, "node_for", lambda _d: None)
    with pytest.raises(frame_merge.FrameMergeRefused, match="not cut over"):
        frame_merge.writer_context(tmp_path)
    monkeypatch.setattr(fence_data, "node_for", lambda _d: FakeNode())
    monkeypatch.setattr(guard, "writer_state", lambda *_a, **_k: (HIVE, "b", WriterRow("a", 4)))
    monkeypatch.setattr(write_policy, "off_primary", lambda **_k: object())
    with pytest.raises(frame_merge.FrameMergeRefused, match=r"only the hive's writer \(a\)"):
        frame_merge.writer_context(tmp_path)
    monkeypatch.setattr(guard, "writer_state", lambda *_a, **_k: (HIVE, "a", WriterRow("a", 4)))
    monkeypatch.setattr(write_policy, "off_primary", lambda **_k: None)
    ctx = frame_merge.writer_context(tmp_path)
    assert (ctx.hive, ctx.writer_frame) == (HIVE, "a")


# ---- bh hive frames -------------------------------------------------------------------------


def _run(action, **kw):
    hive_frames_cli.run(action, cfg={}, entry={"prefix": HIVE}, hive_dir=Path("/h"), **kw)


def test_cli_resolve_refuses_without_take(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(frame_merge, "_state_path", lambda: tmp_path / "frame-merge.json")
    frame_merge.record(_held())
    monkeypatch.setattr(frame_merge, "writer_context", lambda *_a, **_k: _ctx())
    with pytest.raises(typer.Exit) as exit_:
        _run("resolve", frame="c", bead="fm-1")
    assert exit_.value.exit_code == 1
    assert "needs an explicit side" in capsys.readouterr().err
    with pytest.raises(typer.Exit):
        _run("resolve", bead="fm-1", take="main")
    assert "needs --frame" in capsys.readouterr().err


def test_cli_merge_exits_1_while_a_ref_is_held(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(frame_merge, "_state_path", lambda: tmp_path / "frame-merge.json")
    monkeypatch.setattr(frame_merge, "writer_context", lambda *_a, **_k: _ctx())
    monkeypatch.setattr(frame_merge, "merge_hive", lambda *_a, **_k: [_held()])
    with pytest.raises(typer.Exit) as exit_:
        _run("merge")
    assert exit_.value.exit_code == 1
    assert "refs/dolt/frame/c is held unmerged" in capsys.readouterr().out
    monkeypatch.setattr(frame_merge, "merge_hive", lambda *_a, **_k: [])
    _run("merge", as_json=True)
    assert '"frames": []' in capsys.readouterr().out


def test_cli_status_is_read_only(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(frame_merge, "_state_path", lambda: tmp_path / "frame-merge.json")
    _run("status")
    assert "fm: no frame refs" in capsys.readouterr().out
    frame_merge.record(_held())
    _run("status")
    assert "refs/dolt/frame/c is held unmerged" in capsys.readouterr().out


# ---- bh-xwy5x condition 3: an id collision never takes a side ------------------------------


def _added_row(bead, ours, theirs):
    row = _issue_row(bead, "", "")
    row["row"].update(
        our_title=ours,
        their_title=theirs,
        base_id=None,
        base_assignee=None,
        our_diff_type="added",
        their_diff_type="added",
    )
    return row


def test_an_id_collision_is_held_naming_both_beads_and_no_side_applies(writer):
    node = FakeNode([_added_row("fm-dup", "from main", "from frame")])
    outcome = _merge(node, resolutions={"fm-dup": "frame"})
    assert outcome.status == Status.CONFLICT and node.engine.sessions == []
    [row] = outcome.collisions
    assert row.collision and (row.main["title"], row.frame["title"]) == ("from main", "from frame")
    assert outcome.resolutions == {}  # a recorded side for a collision is never applied
    assert "id collision on fm-dup" in outcome.describe() and "drop one" in outcome.error
    assert not outcome.double_claims
    assert any(
        "[id collision" in ln
        for ln in frame_merge.doctor_lines({"hives": {HIVE: [outcome.as_dict()]}})
    )


def test_resolve_refuses_an_id_collision_either_way(tmp_path):
    state = tmp_path / "frame-merge.json"
    row = ConflictRow(
        "issues",
        "fm-dup",
        "fm-dup",
        {"title": "A"},
        {"title": "B"},
        "added",
        "added",
        collision=True,
    )
    frame_merge.record(_held(rows=[row]), path=state)
    for take in ("main", "frame"):
        with pytest.raises(frame_merge.FrameMergeRefused, match="re-create its bead 'B'"):
            frame_merge.resolve(_ctx(), "c", "fm-dup", take, path=state)
    assert frame_merge.recorded(HIVE, "c", path=state).resolutions == {}
    with pytest.raises(frame_merge.FrameMergeRefused, match="No side is taken"):
        frame_merge._resolution_statements(FakeNode(), [row], {"fm-dup": "main"})
