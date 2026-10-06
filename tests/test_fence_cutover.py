"""Pure pieces of the per-hive cutover (bh-oarxp): C1/C2 agreement, the R2 floor, the record
trailer, and the R3 statement shape. The engine-backed C1–C6 / R1–R5 runs are in
``tests/test_fence_cutover_int.py``."""

from __future__ import annotations

import pytest

from beadhive import fence_cutover as fc
from beadhive import fence_schema as fs
from beadhive.host_lease_contracts import EpochFence
from beadhive.writer_adopt import PlacementView, WriterRow


def test_c1_agreement_seeds_at_the_shared_epoch_without_a_bump():
    assert fc._agree("bh", "a", EpochFence(221, "a", 0), PlacementView("a", 221)) == 221


@pytest.mark.parametrize(
    ("fence", "placed", "match"),
    [
        (None, PlacementView("a", 3), "never been adopted"),
        (EpochFence(3, "a"), None, "no live placement"),
        (EpochFence(3, "a"), PlacementView("a", 4), r"disagree \(placement_ahead\)"),
        (EpochFence(4, "a"), PlacementView("a", 3), "disagree"),
        (EpochFence(3, "b"), PlacementView("a", 3), "disagree"),
        (EpochFence(3, "b"), PlacementView("b", 3), "only the current holder"),
    ],
)
def test_c1_refuses_every_disagreement_with_a_legacy_adopt_pointer(fence, placed, match):
    with pytest.raises(fc.CutoverRefused, match=match) as info:
        fc._agree("bh", "a", fence, placed)
    assert "bh host lease adopt bh" in str(info.value)


class _History:
    def __init__(self, top: int):
        self.top = top

    def history_max_epoch(self) -> int:
        return self.top


def test_r2_floor_is_the_max_of_every_carrier_and_history():
    floor = fc._floor(
        _History(7),
        WriterRow("a", 7),
        EpochFence(7, "a", 3),
        PlacementView("a", 7),
        prefix="bh",
        host_id="a",
    )
    assert floor == 7


@pytest.mark.parametrize(
    ("history", "writer", "fence", "placed"),
    [
        (9, WriterRow("a", 7), EpochFence(7, "a"), PlacementView("a", 7)),  # epoch_regressed
        (7, WriterRow("a", 7), EpochFence(8, "a"), PlacementView("a", 8)),  # legacy adopt ahead
        (7, WriterRow("a", 7), EpochFence(6, "a"), PlacementView("a", 7)),  # ref below
        (7, WriterRow("a", 7), None, PlacementView("a", 7)),
        (7, WriterRow("a", 7), EpochFence(7, "a"), None),
        (7, WriterRow("a", 7), EpochFence(7, "b"), PlacementView("a", 7)),
    ],
)
def test_r2_refuses_a_below_floor_state(history, writer, fence, placed):
    with pytest.raises(fc.CutoverRefused, match="below-floor.*bh host lease adopt bh"):
        fc._floor(_History(history), writer, fence, placed, prefix="bh", host_id="a")


def test_the_record_trailer_round_trips():
    line = fc._trailer(hive="bh", epoch=221, holder="host-1", ref_sha="abc123")
    message = f"{fc.CUTOVER_COMMIT_PREFIX} at 221\n\n{line}\n"
    assert fc.parse_trailer(message) == {
        "hive": "bh",
        "epoch": "221",
        "holder": "host-1",
        "ref": "abc123",
    }
    assert fc.parse_trailer("routine commit") == {}


def test_rollback_drops_all_44_triggers_and_the_tables_but_keeps_the_ignore_row():
    statements = fc.fence_schema.rollback_statements()
    dropped = {s.split()[-1] for s in statements if s.startswith("DROP TRIGGER")}
    assert dropped == set(fs.trigger_names()) and len(dropped) == fs.TRIGGER_COUNT == 44
    tables = [s.split()[-1] for s in statements if s.startswith("DROP TABLE")]
    assert tables == ["bh_write_mark", "bh_epoch_live", "bh_writer"]  # FK child first
    assert f"DROP PROCEDURE IF EXISTS {fs.GUARD_PROCEDURE}" in statements
    assert not any("dolt_ignore" in s for s in statements)
    assert fs.drop_ident_statements() == ["DROP TABLE IF EXISTS bh_local_ident"]


def test_the_cutover_sentinel_marks_the_seed_epoch():
    assert fs.cutover_sentinel_statements(221) == [
        "INSERT INTO bh_write_mark (id, epoch, tbl) VALUES ('cutover-221', 221, 'bh_writer')"
    ]


def test_status_findings_include_a_short_guard_and_unreadable_parts():
    from beadhive.fence_data import GuardReport

    short = GuardReport(present=frozenset({"bh_writer_monotonic"}), procedure=False)
    st = fc.FenceStatus("bh", cut_over=True, guard=short, errors=("refs/bh/epoch unreadable",))
    findings = st.findings()
    assert findings[0] == "refs/bh/epoch unreadable"
    assert findings[1].startswith("guard: 1 of 44")
    assert st.as_dict()["trigger_count"] == 1


def test_the_verb_is_hidden_from_help_and_validates_its_action():
    from typer.testing import CliRunner

    from beadhive.cli import app

    runner = CliRunner()
    env = {"COLUMNS": "200", "BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1"}
    for args in ([], ["hive"]):
        shown = runner.invoke(app, [*args, "--help"], env=env)
        assert shown.exit_code == 0 and "fence" not in shown.output.split("Commands")[-1]
    bogus = runner.invoke(app, ["hive", "fence", "adopt", "bh"], env=env)
    assert bogus.exit_code == 2 and "unknown action" in bogus.output
    misplaced = runner.invoke(app, ["hive", "fence", "status", "bh", "--others-published"], env=env)
    assert misplaced.exit_code == 2 and "cutover only" in misplaced.output


# ---- bh doctor (the writer-fence section and its warnings) ----------------------------------


def _audit(**kw):
    from beadhive.fence_audit import FenceAudit

    base = dict(ref="origin/main", head="h" * 32, cut_over=True, writer_frame="a", writer_epoch=7)
    return FenceAudit(**{**base, "live_epoch": 7, **kw})


def test_doctor_reports_the_fence_audit_and_trigger_count_for_a_cut_over_hive(
    monkeypatch, tmp_path, capsys
):
    from beadhive import doctor, fence_data, host_adopt
    from beadhive.fence_data import GuardReport

    node = fence_data.FenceNode(engine=None)  # type: ignore[arg-type] - status is stubbed
    monkeypatch.setattr(host_adopt, "fence_data_for", lambda prefix, path: node)
    monkeypatch.setattr(doctor.guard, "primary_state", lambda **kw: None)
    seen = {}

    def fake_status(n, *, prefix, placement=None, ref=None):
        seen["args"] = (n, prefix, placement, ref)
        return fc.FenceStatus(
            prefix,
            cut_over=True,
            writer=WriterRow("a", 7),
            record=fc.CutoverRecord(prefix, 7, "a", "c" * 32, "r" * 40),
            audit=_audit(stale_mark_count=2, placement_epoch=8, placement_frame="b"),
            guard=GuardReport(present=frozenset(fs.trigger_names()[:43]), procedure=True),
        )

    monkeypatch.setattr(fc, "status", fake_status)
    doctor._fence_status_cache.clear()
    status = doctor._writer_fence_status({}, {"prefix": "bh"}, tmp_path)
    assert seen["args"] == (node, "bh", None, None)
    assert status["epoch"] == 7 and status["trigger_count"] == 43
    assert status["cutover"]["ref_sha"] == "r" * 40
    audit = status["fence_audit"]
    assert (audit["stale_marks"], audit["epoch_regressed"], audit["placement_ahead"]) == (
        2,
        False,
        True,
    )
    warns = doctor._writer_fence_warnings(status)
    assert any("stale_marks: 2" in w for w in warns)
    assert any("guard: 43 of 44" in w for w in warns)
    assert not any("placement_ahead" in w for w in warns)  # the adopt-incomplete warning owns it
    assert doctor._writer_fence_status({}, {"prefix": "bh"}, tmp_path) is status  # one read

    doctor._render_writer_fence([status])
    out = capsys.readouterr().out
    assert "# Writer fence" in out and "triggers        43 of 44" in out
    assert "stale_marks=2" in out and "placement_ahead=True" in out


def test_doctor_is_silent_for_a_legacy_hive(monkeypatch, tmp_path, capsys):
    from beadhive import doctor, host_adopt

    monkeypatch.setattr(host_adopt, "fence_data_for", lambda prefix, path: None)
    doctor._fence_status_cache.clear()
    assert doctor._writer_fence_status({}, {"prefix": "bh"}, tmp_path) is None
    assert doctor._writer_fence_warnings(None) == []
    doctor._render_writer_fence([])
    assert capsys.readouterr().out == ""
