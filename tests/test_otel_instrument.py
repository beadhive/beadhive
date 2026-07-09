"""cit.3 — lifecycle instrumentation (traces + metrics) on the gated otel surface.

The OTel SDK is an optional extra and is NOT installed in the default test env, so the "otel on"
assertions drive a **mocked provider** (``get_tracer`` / ``get_meter`` return MagicMocks) rather
than the real SDK — mirroring test_otel.py's fake-surface approach. The "otel off" assertions
prove the default path is a zero-overhead, import-free no-op that leaves the patch-``run`` test
seam untouched.
"""

from __future__ import annotations

import inspect
import sys
from unittest.mock import MagicMock

import pytest

from beadhive import otel, run


@pytest.fixture(autouse=True)
def _reset_otel():
    """Each test starts with otel off and an empty instrument cache; restore afterward so a
    forced-on test never leaks ``_initialized`` into the rest of the suite."""
    otel._initialized = False
    otel._instruments.clear()
    yield
    otel._initialized = False
    otel._instruments.clear()


def _mock_provider(monkeypatch):
    """Force otel 'on' with a mocked tracer + meter; return (tracer, meter) for assertions."""
    tracer = MagicMock(name="tracer")
    meter = MagicMock(name="meter")
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    monkeypatch.setattr(otel, "get_meter", lambda *a, **k: meter)
    return tracer, meter


# ---- gating: off by default = cheap, import-free no-ops ----------------------


def test_off_by_default_returns_noop_surface():
    assert otel.is_active() is False
    assert otel.get_tracer() is otel._NOOP_TRACER
    assert otel.get_meter() is otel._NOOP_METER
    # the no-op span doubles as its own (zero-cost) context manager
    with otel.span("anything", {"k": "v"}) as sp:
        assert sp is otel._NOOP_SPAN


def test_metric_helpers_are_noops_when_off():
    # No raise, no instrument creation, and crucially no opentelemetry import (the extra is absent).
    otel.record_merge_duration(1.5, {"x": "y"})
    otel.count_bead_transition("merged", {"ws.bead": "mr-1"})
    otel.count_validation(True)
    otel.count_validation(False)
    assert otel._instruments == {}  # nothing cached on the off-path


def test_trace_verb_is_a_passthrough_when_off(monkeypatch):
    # Off ⇒ the decorator must not even reach for a tracer; it just calls through.
    monkeypatch.setattr(
        otel, "get_tracer", MagicMock(side_effect=AssertionError("no tracer when off"))
    )

    @otel.trace_verb("work.demo")
    def verb(a, b=2):
        return a + b

    assert verb(1) == 3
    assert verb(10, b=5) == 15


def test_trace_verb_preserves_signature_for_typer():
    # Typer introspects the registered callback; functools.wraps must keep the original params.
    def verb(bead: str, rig: str = "r"):
        return (bead, rig)

    wrapped = otel.trace_verb("work.demo")(verb)
    assert list(inspect.signature(wrapped).parameters) == ["bead", "rig"]


# ---- run() subprocess seam: zero-overhead off, span on ----------------------


def test_run_off_path_does_not_build_a_span(monkeypatch):
    # otel off ⇒ run() must take the nullcontext branch and never touch otel.span.
    monkeypatch.setattr(otel, "span", MagicMock(side_effect=AssertionError("no span when off")))
    res = run.run([sys.executable, "-c", "pass"])
    assert res.returncode == 0


def test_run_emits_subprocess_span_when_on(monkeypatch):
    tracer, _meter = _mock_provider(monkeypatch)
    res = run.run([sys.executable, "-c", "print('hi')"], capture=True)
    assert res.returncode == 0 and res.stdout.strip() == "hi"
    tracer.start_as_current_span.assert_called_once()
    name, kwargs = (
        tracer.start_as_current_span.call_args.args[0],
        tracer.start_as_current_span.call_args.kwargs,
    )
    assert name.startswith("python")  # span named after the tool
    assert kwargs["attributes"]["ws.subprocess.tool"].startswith("python")


def test_patched_run_bypasses_instrumentation(monkeypatch):
    # The test seam: patching a module's `run` replaces the whole function, so a fake never hits
    # the span — even with otel forced on. This is why fakes keep working unchanged.
    _mock_provider(monkeypatch)
    calls = []
    monkeypatch.setattr("beadhive.bd.run", lambda cmd, **k: calls.append(cmd) or "faked")
    from beadhive import bd

    assert bd.run(["bd", "ready"]) == "faked"
    assert calls == [["bd", "ready"]]


# ---- secret-safe span naming ------------------------------------------------


def test_safe_op_stops_at_first_flag_so_secrets_never_leak():
    # dolt passes --password <secret>; the span name must include only the leading verb tokens.
    name = run._safe_op(["dolt", "--host", "h", "--password", "S3CRET", "sql"])
    assert name == "dolt"
    assert "S3CRET" not in name
    assert run._safe_op(["git", "merge", "mr-1"]) == "git merge"
    assert run._tool(["/usr/bin/git", "status"]) == "git"


# ---- metrics emitted when on ------------------------------------------------


def test_merge_duration_histogram_recorded_when_on(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.record_merge_duration(2.5, {"ws.merge.kind": "bead"})
    meter.create_histogram.assert_called_once()
    assert meter.create_histogram.call_args.args[0] == "ws.work.merge.duration"
    meter.create_histogram.return_value.record.assert_called_once_with(
        2.5, {"ws.merge.kind": "bead"}
    )


def test_bead_transition_counter_added_when_on(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.count_bead_transition("merged", {"ws.bead": "mr-1"})
    assert meter.create_counter.call_args.args[0] == "ws.work.bead.transitions"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.bead.transition": "merged", "ws.bead": "mr-1"}
    )


def test_worktree_event_is_noop_when_off():
    # Off path: no instrument created, no opentelemetry import, no allocation.
    otel.record_worktree_event("create", "ok", {"ws.rig": "mr"})
    assert otel._instruments == {}  # nothing cached on the off-path


def test_worktree_event_counter_added_when_on(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.record_worktree_event("create", "ok", {"ws.rig": "mr", "ws.worktree": "ag-1"})
    assert meter.create_counter.call_args.args[0] == "ws.worktree.events"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.worktree.op": "create", "ws.worktree.outcome": "ok",
            "ws.rig": "mr", "ws.worktree": "ag-1"},
    )


def test_worktree_event_outcome_defaults_ok_and_tags_op(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.record_worktree_event("remove")  # outcome defaults to ok, no extra attrs
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.worktree.op": "remove", "ws.worktree.outcome": "ok"}
    )


def test_validation_counter_tags_pass_and_fail_when_on(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.count_validation(True, {"ws.bead": "mr-1"})
    otel.count_validation(False, {"ws.bead": "mr-1"})
    counter = meter.create_counter.return_value
    assert counter.add.call_count == 2
    results = [c.args[1]["ws.validation.result"] for c in counter.add.call_args_list]
    assert results == ["pass", "fail"]
    # one cached instrument reused across both samples (not re-created per call)
    assert meter.create_counter.call_count == 1


def test_instruments_cached_per_name(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.record_merge_duration(1.0)
    otel.record_merge_duration(2.0)
    assert meter.create_histogram.call_count == 1  # created once, reused


# ---- record_mcp_invocation --------------------------------------------------


def test_record_mcp_invocation_is_noop_when_off():
    # Off path: no instrument created, no opentelemetry import, no allocation.
    otel.record_mcp_invocation("plan_check", "ok", 0.5)
    assert otel._instruments == {}  # nothing cached on the off-path


def test_record_mcp_invocation_emits_counter_and_histogram_when_on(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.record_mcp_invocation("plan_check", "ok", 1.23)
    # Counter: ws.mcp.tool.invocations with tool + outcome tags.
    assert meter.create_counter.call_args.args[0] == "ws.mcp.tool.invocations"
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.mcp.tool": "plan_check", "ws.mcp.outcome": "ok"}
    )
    # Histogram: ws.mcp.tool.duration with the same tags.
    assert meter.create_histogram.call_args.args[0] == "ws.mcp.tool.duration"
    meter.create_histogram.return_value.record.assert_called_once_with(
        1.23, {"ws.mcp.tool": "plan_check", "ws.mcp.outcome": "ok"}
    )


def test_record_mcp_invocation_error_outcome_tags_correctly(monkeypatch):
    _tracer, meter = _mock_provider(monkeypatch)
    otel.record_mcp_invocation("plan_file", "error", 0.05)
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"ws.mcp.tool": "plan_file", "ws.mcp.outcome": "error"}
    )
    meter.create_histogram.return_value.record.assert_called_once_with(
        0.05, {"ws.mcp.tool": "plan_file", "ws.mcp.outcome": "error"}
    )
