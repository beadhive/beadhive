"""bh-judge-haiku: the reference bh-judge/1 command over the Anthropic Messages API.

The HTTP layer is always mocked (an injected ``post`` or an ``httpx.MockTransport``); no test
touches the network. A sentinel API key proves the key never reaches stdout, stderr, logs,
exceptions, judgments or a written plan.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Mapping
from typing import Any

import pytest

import beadlog
from beadlog_sources import (
    ABSTAIN,
    ChoiceQuery,
    CommandJudge,
    CommandResult,
    ScoreQuery,
    haiku,
    reconcile,
    write_plan,
)
from beadlog_sources.judges import choice_request, judge_schema, score_request
from beadlog_sources.reconcile import MATCH_FAMILY

SENTINEL = "sk-ant-api03-SENTINEL-KEY-0123456789abcdef"
QUERY = ChoiceQuery(
    "Which existing bead is the same work?",
    {"present:1": "Add the archive writer", "new": "new work", ABSTAIN: "cannot tell"},
    "Archive writer tests",
    MATCH_FAMILY,
)


def _api_response(probabilities: Mapping[str, float], **extra: Any) -> dict[str, Any]:
    text = json.dumps({"probabilities": dict(probabilities), "rationale": "same subsystem"})
    return {
        "id": "msg_1",
        "type": "message",
        "model": "claude-haiku-5-5",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": text}],
        **extra,
    }


class _FakePost:
    def __init__(self, status: int = 200, payload: Any = None, error: Exception | None = None):
        self.status = status
        self.payload = payload
        self.error = error
        self.calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float
    ) -> tuple[int, Any]:
        self.calls.append((url, dict(headers), dict(body), timeout))
        if self.error is not None:
            raise self.error
        return self.status, self.payload


def _run(request: Any, post: Any, *argv: str, key: str | None = SENTINEL) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    environ = {} if key is None else {haiku.API_KEY_ENV: key}
    code = haiku.main(
        list(argv),
        stdin=io.StringIO(json.dumps(request)),
        stdout=stdout,
        stderr=stderr,
        environ=environ,
        post=post,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_answers_a_choice_with_a_valid_bh_judge_response() -> None:
    post = _FakePost(payload=_api_response({"present:1": 0.85, "new": 0.1, ABSTAIN: 0.05}))
    code, out, err = _run(choice_request(QUERY, model="claude-haiku-5-5"), post)
    assert code == 0 and err == ""
    answer = json.loads(out)
    assert answer["outcome"] == "present:1" and answer["engine"] == "bh-judge-haiku"
    assert answer["model"] == "claude-haiku-5-5"
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator(judge_schema()).validate(answer)

    ((url, headers, body, _),) = post.calls
    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == SENTINEL and headers["anthropic-version"] == "2026-10-21"
    assert body["model"] == "claude-haiku-5-5"
    assert "temperature" not in body
    schema = body["output_config"]["format"]["schema"]
    assert schema["properties"]["probabilities"]["required"] == list(QUERY.options)
    assert "Archive writer tests" in body["messages"][0]["content"]


def test_model_comes_from_the_request_or_the_flag() -> None:
    post = _FakePost(payload=_api_response({"present:1": 1.0, "new": 0.0, ABSTAIN: 0.0}))
    _run(choice_request(QUERY, model=None), post)
    _run(choice_request(QUERY, model="from-request"), post)
    _run(choice_request(QUERY, model="from-request"), post, "--model", "from-flag")
    assert [call[2]["model"] for call in post.calls] == [
        "claude-haiku-5-5",
        "from-request",
        "from-flag",
    ]


def test_mass_on_abstain_is_an_abstention_and_scores_work() -> None:
    post = _FakePost(payload=_api_response({"present:1": 0.2, "new": 0.2, ABSTAIN: 0.6}))
    code, out, _ = _run(choice_request(QUERY, model=None), post)
    assert code == 0 and json.loads(out)["outcome"] is None

    scored = _FakePost(payload=_api_response({"SIMPLE": 0.3, "COMPLEX": 0.7}))
    request = score_request(
        ScoreQuery("how complex?", ("COMPLEX", "SIMPLE"), "a big refactor"), model=None
    )
    code, out, _ = _run(request, scored)
    assert code == 0 and json.loads(out)["outcome"] == "COMPLEX"
    assert scored.calls[0][2]["output_config"]["format"]["schema"]["properties"]["probabilities"][
        "required"
    ] == ["COMPLEX", "SIMPLE"]


@pytest.mark.parametrize(
    "post",
    [
        _FakePost(status=401, payload={"type": "error", "error": {"type": "authentication_error"}}),
        _FakePost(status=200, payload={"content": []}),
        _FakePost(status=200, payload=_api_response({}, stop_reason="refusal")),
        _FakePost(status=200, payload={"content": [{"type": "text", "text": "not json"}]}),
        _FakePost(status=200, payload=_api_response({"present:1": -1.0})),
        _FakePost(error=RuntimeError(f"boom with {SENTINEL} inside")),
    ],
    ids=["http-401", "no-text", "refusal", "not-json", "negative", "transport-error"],
)
def test_failures_exit_non_zero_without_leaking_the_key(post: _FakePost) -> None:
    code, out, err = _run(choice_request(QUERY, model=None), post)
    assert code == 1 and out == "" and err.startswith("bh-judge-haiku:")
    assert SENTINEL not in err


def test_missing_key_and_bad_input_fail_without_a_request() -> None:
    post = _FakePost(payload=_api_response({"new": 1.0}))
    code, _, err = _run(choice_request(QUERY, model=None), post, key=None)
    assert code == 1 and "ANTHROPIC_API_KEY is not set" in err
    code, _, err = _run({"schema": "bh-judge/2"}, post)
    assert code == 1 and "unsupported envelope" in err
    assert post.calls == []


def test_the_key_is_never_accepted_as_an_argument() -> None:
    with pytest.raises(SystemExit):
        haiku.main(["--api-key", SENTINEL], stdin=io.StringIO("{}"), environ={}, post=_FakePost())


def test_sentinel_key_never_reaches_logs_judgments_or_plans(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End to end: CommandJudge -> bh-judge-haiku (in-process runner) -> reconcile -> plan."""
    caplog.set_level(logging.DEBUG)
    post = _FakePost(payload=_api_response({"present:1": 0.1, "new": 0.85, ABSTAIN: 0.05}))
    stderr = io.StringIO()

    def runner(argv: Any, stdin: bytes, timeout: float) -> CommandResult:
        stdout = io.StringIO()
        code = haiku.main(
            [],
            stdin=io.StringIO(stdin.decode()),
            stdout=stdout,
            stderr=stderr,
            environ={haiku.API_KEY_ENV: SENTINEL},
            post=post,
        )
        return CommandResult(code, stdout.getvalue().encode())

    group = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000bb", "bh")

    def create(bead: str, title: str, system: str) -> beadlog.BeadEvent:
        return beadlog.mint_event(
            "create",
            beadlog.BeadRef(group.id, bead),
            {"type": "task", "fields": {"title": title}},
            occurred_at="2026-07-01T00:00:00Z",
            recorded_at=None,
            extracted_at="2026-10-31T00:00:00Z",
            source=(system, f"{system}:{bead}"),
            provenance="inferred",
        )

    judge = CommandJudge("bh-judge-haiku", runner=runner)
    plan = reconcile(
        [create("doc-1", "Archive writer tests", "docs")],
        [create("bh-1", "Add the archive writer", "bd")],
        judge,
    )
    (entry,) = plan.entries
    assert (
        entry.verdict.judgment.engine == "bh-judge-haiku"
        and entry.verdict.judgment.fallback is None
    )
    buffer = io.BytesIO()
    write_plan(buffer, plan, groups=[group], generator=beadlog.Generator("t", "0"))

    assert post.calls and post.calls[0][1]["x-api-key"] == SENTINEL  # it was really used
    for text in (repr(plan), repr(judge.__dict__), stderr.getvalue(), caplog.text):
        assert SENTINEL not in text
    assert SENTINEL.encode() not in buffer.getvalue()


def test_httpx_transport_is_mockable_and_errors_are_scrubbed() -> None:
    httpx = pytest.importorskip("httpx")
    seen: list[Any] = []

    def handler(request: Any) -> Any:
        seen.append(request)
        return httpx.Response(200, json=_api_response({"present:1": 1.0, "new": 0.0, ABSTAIN: 0.0}))

    post = haiku.httpx_post(httpx.MockTransport(handler))
    code, out, _ = _run(choice_request(QUERY, model=None), post)
    assert code == 0 and json.loads(out)["outcome"] == "present:1"
    assert seen[0].headers["x-api-key"] == SENTINEL
    assert json.loads(seen[0].content)["model"] == "claude-haiku-5-5"

    def broken(request: Any) -> Any:
        raise httpx.ConnectError(f"cannot connect, key={SENTINEL}")

    code, _, err = _run(
        choice_request(QUERY, model=None), haiku.httpx_post(httpx.MockTransport(broken))
    )
    assert code == 1 and "ConnectError" in err and SENTINEL not in err
