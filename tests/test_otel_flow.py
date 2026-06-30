"""hqfy.1 — commit-flow metric helpers on the gated otel surface.

Mirrors test_otel_instrument.py: the OTel SDK extra is absent in the default test env, so the
"otel on" assertions drive a mocked meter (``get_meter`` returns a MagicMock), and the "off"
assertions prove each helper is a zero-overhead, import-free no-op (nothing cached).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ws import otel


@pytest.fixture(autouse=True)
def _reset_otel():
    otel._initialized = False
    otel._instruments.clear()
    yield
    otel._initialized = False
    otel._instruments.clear()


def _mock_meter(monkeypatch):
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    return meter


# ---- off-path: every helper is a cheap, import-free no-op -------------------


def test_flow_helpers_are_noops_when_off():
    otel.record_cycle_time(1.0, {"ws.rig": "mr"})
    otel.record_cycle_time_active(1.0)
    otel.record_stage("coding", 2.0)
    otel.record_rework(3)
    otel.record_merge_slot_wait(0.5)
    otel.record_merge_slot_hold(0.5)
    otel.record_validation_duration(4.0)
    otel.count_merge_outcome({"ws.merge.how": "ff"})
    otel.record_worktree_op_duration(0.1, {"ws.worktree.op": "create"})
    assert otel._instruments == {}  # nothing cached on the off-path


# ---- histograms: right instrument name + unit + recorded value --------------


@pytest.mark.parametrize(
    "call,name,value",
    [
        (lambda: otel.record_cycle_time(12.0, {"ws.rig": "mr"}), "ws.work.cycle_time", 12.0),
        (lambda: otel.record_cycle_time_active(8.0), "ws.work.cycle_time.active", 8.0),
        (lambda: otel.record_rework(2), "ws.work.rework.count", 2),
        (lambda: otel.record_merge_slot_wait(1.5), "ws.work.merge_slot.wait", 1.5),
        (lambda: otel.record_merge_slot_hold(0.25), "ws.work.merge_slot.hold", 0.25),
        (lambda: otel.record_validation_duration(9.0), "ws.work.validation.duration", 9.0),
        (lambda: otel.record_worktree_op_duration(0.3), "ws.worktree.op.duration", 0.3),
    ],
)
def test_histogram_helpers_record_named_instrument(monkeypatch, call, name, value):
    meter = _mock_meter(monkeypatch)
    call()
    assert meter.create_histogram.call_args.args[0] == name
    unit = meter.create_histogram.call_args.kwargs["unit"]
    assert unit == ("1" if name == "ws.work.rework.count" else "s")
    rec = meter.create_histogram.return_value.record
    assert rec.call_args.args[0] == value


def test_record_stage_validates_and_names_per_stage(monkeypatch):
    meter = _mock_meter(monkeypatch)
    for stage in ("coding", "review_wait", "merge_latency"):
        otel.record_stage(stage, 1.0, {"ws.rig": "mr"})
    names = [c.args[0] for c in meter.create_histogram.call_args_list]
    assert names == [
        "ws.work.stage.coding",
        "ws.work.stage.review_wait",
        "ws.work.stage.merge_latency",
    ]
    assert all(c.kwargs["unit"] == "s" for c in meter.create_histogram.call_args_list)


def test_record_stage_rejects_unknown_stage(monkeypatch):
    _mock_meter(monkeypatch)
    with pytest.raises(ValueError, match="stage must be one of"):
        otel.record_stage("bogus", 1.0)


def test_record_stage_validates_before_init_even_when_off():
    # Validation happens on the off-path too — a typo is a programming error, not telemetry.
    with pytest.raises(ValueError):
        otel.record_stage("nope", 1.0)


# ---- counter: merge outcome -------------------------------------------------


def test_count_merge_outcome_is_noop_when_off():
    otel.count_merge_outcome({"ws.merge.how": "ff"})
    assert otel._instruments == {}


def test_count_merge_outcome_adds_one_with_attrs(monkeypatch):
    meter = _mock_meter(monkeypatch)
    otel.count_merge_outcome({"ws.merge.kind": "bead", "ws.merge.how": "conflict", "ws.rig": "mr"})
    assert meter.create_counter.call_args.args[0] == "ws.work.merge.outcome"
    assert meter.create_counter.call_args.kwargs["unit"] == "1"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.merge.kind": "bead", "ws.merge.how": "conflict", "ws.rig": "mr"}
    )


def test_flow_instruments_cached_per_name(monkeypatch):
    meter = _mock_meter(monkeypatch)
    otel.record_cycle_time(1.0)
    otel.record_cycle_time(2.0)
    assert meter.create_histogram.call_count == 1  # created once, reused
