"""cit.5 — agentic GenAI spans (EXPERIMENTAL) on the gated otel surface.

The coordinator->developer dispatch is emitted as an OTel GenAI semconv ``invoke_agent`` span.
The contract under test, beyond span/attribute shape:

  * low-cardinality control-plane facts (operation, system, model, agent, tokens) ride as
    ``gen_ai.*`` *attributes*; and
  * brief / feedback *content* rides as span **EVENTS**, never attributes — so the Collector can
    drop it (PII / size) without touching the queryable span.

As in test_otel_instrument.py the OTel SDK isn't installed in the default test env, so "otel on"
drives a **mocked provider** (a MagicMock tracer/span); "otel off" proves the zero-cost no-op path.
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


def _mock_span_provider(monkeypatch):
    """Force otel 'on' with a mocked tracer whose span is inspectable. Returns (tracer, span)."""
    span = MagicMock(name="span")
    cm = MagicMock(name="span_cm")
    cm.__enter__.return_value = span
    cm.__exit__.return_value = False
    tracer = MagicMock(name="tracer")
    tracer.start_as_current_span.return_value = cm
    monkeypatch.setattr(otel, "_initialized", True)
    monkeypatch.setattr(otel, "get_tracer", lambda *a, **k: tracer)
    return tracer, span


def _span_attrs(tracer):
    """The attributes dict passed to start_as_current_span on the dispatch span."""
    return tracer.start_as_current_span.call_args.kwargs["attributes"]


def _event_calls(span):
    """(name, attributes) for every add_event call on the span."""
    return [(c.args[0], c.args[1]) for c in span.add_event.call_args_list]


# ---- gating: off by default = cheap, import-free no-op ----------------------


def test_dispatch_is_a_noop_span_when_off():
    # otel off ⇒ the dispatch yields the shared no-op span; content events are zero-cost no-ops.
    assert otel.is_active() is False
    with otel.record_agent_dispatch(agent="crew/cit-5", model="opus", brief="secret brief") as span:
        assert span is otel._NOOP_SPAN
        # add_event exists on the no-op span and never raises (no opentelemetry import on this path)
        otel.record_feedback_event(span, "more secret feedback")


# ---- attributes: low-cardinality gen_ai.* control-plane facts ----------------


def test_dispatch_emits_invoke_agent_gen_ai_attributes(monkeypatch):
    tracer, _span = _mock_span_provider(monkeypatch)
    with otel.record_agent_dispatch(
        agent="crew/cit-5", model="opus", system="claude", attributes={"ws.bead": "mr-1"}
    ):
        pass

    name = tracer.start_as_current_span.call_args.args[0]
    assert name == "invoke_agent crew/cit-5"  # semconv span name: `invoke_agent {agent}`

    attrs = _span_attrs(tracer)
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.system"] == "claude"
    assert attrs["gen_ai.request.model"] == "opus"
    assert attrs["gen_ai.agent.name"] == "crew/cit-5"
    assert attrs["ws.bead"] == "mr-1"  # caller extras pass through


def test_system_defaults_and_blank_model_is_omitted(monkeypatch):
    tracer, _span = _mock_span_provider(monkeypatch)
    with otel.record_agent_dispatch(agent="crew/cit-6"):  # no model, default system
        pass
    attrs = _span_attrs(tracer)
    assert attrs["gen_ai.system"] == otel._GEN_AI_SYSTEM  # defaulted harness
    assert "gen_ai.request.model" not in attrs  # omitted rather than blank


# ---- content: briefs & feedback are EVENTS, never attributes -----------------


def test_brief_and_feedback_are_events_not_attributes(monkeypatch):
    tracer, span = _mock_span_provider(monkeypatch)
    brief = "Implement the gen_ai dispatch span — may contain PII"
    feedback = "Please carry content as events, not attributes"
    with otel.record_agent_dispatch(
        agent="crew/cit-5", model="opus", brief=brief, feedback=feedback
    ):
        pass

    # content rode as span EVENTS ...
    events = _event_calls(span)
    names = [n for n, _a in events]
    assert names == ["gen_ai.user.message", "gen_ai.user.message"]
    kinds = {a["ws.genai.content_kind"]: a["content"] for _n, a in events}
    assert kinds == {"brief": brief, "feedback": feedback}

    # ... and NOT as span attributes (the whole point: Collector can drop the events).
    attrs = _span_attrs(tracer)
    assert brief not in attrs.values()
    assert feedback not in attrs.values()
    assert not any("content" in k for k in attrs)


def test_blank_content_emits_no_event(monkeypatch):
    _tracer, span = _mock_span_provider(monkeypatch)
    with otel.record_agent_dispatch(agent="crew/cit-5", brief="", feedback=None):
        pass
    span.add_event.assert_not_called()  # nothing to carry ⇒ no empty events


# ---- token usage: gen_ai.usage.* when available ------------------------------


def test_token_usage_sets_gen_ai_usage_attributes(monkeypatch):
    _tracer, span = _mock_span_provider(monkeypatch)
    with otel.record_agent_dispatch(agent="crew/cit-5", model="opus") as s:
        otel.set_token_usage(s, input_tokens=1200, output_tokens=350)
    span.set_attribute.assert_any_call("gen_ai.usage.input_tokens", 1200)
    span.set_attribute.assert_any_call("gen_ai.usage.output_tokens", 350)


def test_token_usage_omits_unknown_counts(monkeypatch):
    _tracer, span = _mock_span_provider(monkeypatch)
    with otel.record_agent_dispatch(agent="crew/cit-5") as s:
        otel.set_token_usage(s)  # nothing known at dispatch
    span.set_attribute.assert_not_called()
