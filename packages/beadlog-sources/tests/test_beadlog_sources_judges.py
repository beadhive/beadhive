"""Judgment engines: the deterministic judge, the bh-judge/1 command judge (contract cases run
against both), fail-closed fallbacks, call budgets, settings and the envelope schema.

No test depends on wall-clock time: timeouts are injected by a fake runner that raises
``subprocess.TimeoutExpired``; the real-subprocess contract runs answer immediately.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from beadlog_sources import (
    ABSTAIN,
    ChoiceQuery,
    CommandJudge,
    CommandResult,
    DeterministicJudge,
    JudgeSettings,
    Judgment,
    JudgmentPort,
    ScoreQuery,
    build_judge,
    choice_confidence,
    judge_schema,
    settings_from,
)
from beadlog_sources import judges as judges_module
from beadlog_sources.judges import (
    JudgeProtocolError,
    choice_request,
    judgment_response,
    parse_request,
    parse_response,
    score_request,
    subprocess_runner,
)

REPO = Path(__file__).resolve().parents[3]
DOCS_SCHEMA = REPO / "docs" / "schemas" / "bh-judge-v1.schema.json"

FAKE_JUDGE = """\
import json, sys
from beadlog_sources import ChoiceQuery, DeterministicJudge
from beadlog_sources.judges import judgment_response, parse_request

query, model = parse_request(json.load(sys.stdin))
judge = DeterministicJudge()
verdict = judge.choose(query) if isinstance(query, ChoiceQuery) else judge.score(query)
answer = judgment_response(verdict)
answer.update(engine="fake-command", engine_version="9", model=model)
print(json.dumps(answer))
"""

CHOICES = [
    pytest.param(
        ChoiceQuery(
            "which bead?",
            {"present:1": "Add the archive writer", "present:2": "Reader", ABSTAIN: "none"},
            "Add the archive writer\ntype: task",
        ),
        "present:1",
        id="exact-title",
    ),
    pytest.param(
        ChoiceQuery("which bead?", {"a": "zebra census", "b": "lion count"}, "quarterly report"),
        None,
        id="no-overlap-abstains",
    ),
    pytest.param(
        ChoiceQuery(
            "which bead?", {"a": "archive writer", "b": "writer archive"}, "archive writer"
        ),
        None,
        id="tie-abstains",
    ),
]
SCORES = [
    pytest.param(
        ScoreQuery("how complex?", ("COMPLEX", "MEDIUM", "SIMPLE"), "labelled medium effort"),
        "MEDIUM",
        id="criterion-named",
    ),
    pytest.param(
        ScoreQuery("how complex?", ("COMPLEX", "SIMPLE"), "simple, not complex"),
        None,
        id="two-named-abstains",
    ),
    pytest.param(ScoreQuery("how complex?", ("COMPLEX", "SIMPLE"), "unknown"), None, id="none"),
]


def _validator() -> Any:
    jsonschema = pytest.importorskip("jsonschema")
    schema = judge_schema()
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


@pytest.fixture
def command(tmp_path: Path) -> list[str]:
    script = tmp_path / "fake_judge.py"
    script.write_text(FAKE_JUDGE, encoding="utf-8")
    return [sys.executable, str(script)]


class _Recording:
    """Runner wrapper keeping every request and stdout for schema validation."""

    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    def __call__(self, argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
        result = subprocess_runner(argv, stdin, timeout)
        self.envelopes += [json.loads(stdin), json.loads(result.stdout)]
        return result


# -- confidence ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probabilities", "count", "expected"),
    [
        ({"a": 1.0}, 4, 1.0),
        ({"a": 0.25, "b": 0.25, "c": 0.25, "d": 0.25}, 4, 0.0),
        ({"a": 0.5, "b": 0.5}, 4, 1 / 3),
        ({"a": 0.1, "b": 0.9}, 2, 0.8),
        ({"a": 0.6}, 1, 0.6),
        ({}, 3, 0.0),
    ],
)
def test_choice_confidence_follows_the_jev_formula(
    probabilities: dict[str, float], count: int, expected: float
) -> None:
    assert choice_confidence(probabilities, count) == pytest.approx(expected)


# -- the deterministic judge -----------------------------------------------------------------


def test_deterministic_judge_is_one_hot_or_abstains() -> None:
    judge = DeterministicJudge()
    assert isinstance(judge, JudgmentPort)
    hit = judge.choose(ChoiceQuery("q", {"a": "Archive writer", ABSTAIN: "none"}, "archive writer"))
    assert (hit.outcome, dict(hit.probabilities), hit.confidence) == ("a", {"a": 1.0}, 1.0)
    assert hit.option_count == 2 and hit.engine == "deterministic-lexical" and hit.fallback is None
    miss = judge.choose(ChoiceQuery("q", {"a": "x", ABSTAIN: "none"}, "y"))
    assert (miss.outcome, dict(miss.probabilities), miss.confidence) == (None, {ABSTAIN: 1.0}, 0.0)


def test_deterministic_choice_reads_only_the_subject_line() -> None:
    query = ChoiceQuery(
        "q", {"a": "archive writer"}, "archive writer\ndescription: lots more words"
    )
    assert DeterministicJudge().choose(query).outcome == "a"


# -- contract cases: the same queries against both engines -----------------------------------


@pytest.mark.parametrize(("query", "expected"), CHOICES)
def test_choice_contract_deterministic_and_command(
    query: ChoiceQuery, expected: str | None, command: list[str]
) -> None:
    recording = _Recording()
    engines: list[JudgmentPort] = [DeterministicJudge(), CommandJudge(command, runner=recording)]
    verdicts = [engine.choose(query) for engine in engines]
    for verdict in verdicts:
        assert verdict.outcome == expected
        assert verdict.option_count == len(query.options)
        assert verdict.fallback is None
        assert verdict.confidence == (1.0 if expected else 0.0)
    assert dict(verdicts[0].probabilities) == dict(verdicts[1].probabilities)
    assert verdicts[1].engine == "fake-command" and verdicts[1].engine_version == "9"
    validator = _validator()
    for envelope in recording.envelopes:
        validator.validate(envelope)


@pytest.mark.parametrize(("query", "expected"), SCORES)
def test_score_contract_deterministic_and_command(
    query: ScoreQuery, expected: str | None, command: list[str]
) -> None:
    recording = _Recording()
    for engine in (DeterministicJudge(), CommandJudge(command, runner=recording, model=None)):
        verdict = engine.score(query)
        assert verdict.outcome == expected and verdict.option_count == len(query.criteria)
        assert verdict.fallback is None
    validator = _validator()
    for envelope in recording.envelopes:
        validator.validate(envelope)
    assert recording.envelopes[0]["kind"] == "score" and recording.envelopes[0]["model"] is None


def test_requests_carry_the_query_family_and_model() -> None:
    query = ChoiceQuery("q", {"a": "x"}, "c", "reconcile.match")
    request = choice_request(query, model="claude-haiku-5-5")
    assert request["family"] == "reconcile.match" and request["model"] == "claude-haiku-5-5"
    parsed, model = parse_request(request)
    assert parsed == query and model == "claude-haiku-5-5"
    scored, _ = parse_request(score_request(ScoreQuery("q", ("A", "B")), model=None))
    assert scored == ScoreQuery("q", ("A", "B"))


# -- fail closed -----------------------------------------------------------------------------

QUERY = ChoiceQuery(
    "which?", {"a": "archive writer", "b": "reader", ABSTAIN: "none"}, "archive writer"
)


def _answer(**changes: Any) -> bytes:
    answer = judgment_response(
        Judgment("b", {"b": 0.9, "a": 0.1}, 0.8, 3, "because", "fake", "1", "m")
    )
    answer.update(changes)
    return json.dumps(answer).encode()


def _fixed(returncode: int = 0, stdout: bytes = b"") -> Any:
    def run(argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
        return CommandResult(returncode, stdout)

    return run


def _timeout(argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
    raise subprocess.TimeoutExpired(list(argv), timeout)


def _missing(argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
    raise FileNotFoundError(argv[0])


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        pytest.param(_timeout, "timeout", id="timeout"),
        pytest.param(_fixed(returncode=3, stdout=_answer()), "exit", id="non-zero-exit"),
        pytest.param(_fixed(stdout=b"not json"), "malformed", id="not-json"),
        pytest.param(_fixed(stdout=b"\xff\xfe"), "malformed", id="not-utf8"),
        pytest.param(_fixed(stdout=_answer(schema="bh-judge/2")), "version", id="wrong-version"),
        pytest.param(_fixed(stdout=_answer(outcome="zzz")), "malformed", id="unknown-outcome"),
        pytest.param(
            _fixed(stdout=_answer(probabilities={"a": 0.2, "b": 0.2})), "malformed", id="sum"
        ),
        pytest.param(_fixed(stdout=_answer(extra=1)), "malformed", id="unknown-member"),
        pytest.param(_fixed(stdout=b"[]"), "malformed", id="not-an-object"),
        pytest.param(_missing, "spawn", id="command-missing"),
    ],
)
def test_every_failure_falls_back_to_deterministic_with_provenance(
    runner: Any, reason: str
) -> None:
    verdict = CommandJudge("judge-cmd", runner=runner).choose(QUERY)
    expected = DeterministicJudge().choose(QUERY)
    assert verdict.outcome == expected.outcome == "a"
    assert verdict.engine == "deterministic-lexical" and verdict.confidence == 1.0
    assert verdict.fallback is not None
    assert verdict.fallback.engine == "deterministic-lexical"
    assert verdict.fallback.reason == reason


def test_a_valid_answer_is_used_and_confidence_recomputed() -> None:
    verdict = CommandJudge("judge-cmd", runner=_fixed(stdout=_answer())).choose(QUERY)
    assert verdict.outcome == "b" and verdict.fallback is None
    assert verdict.confidence == pytest.approx((3 * 0.9 - 1) / 2)
    assert (verdict.engine, verdict.model, verdict.rationale) == ("fake", "m", "because")


def test_an_abstaining_answer_maps_to_none() -> None:
    answer = _answer(outcome=ABSTAIN, probabilities={ABSTAIN: 1.0})
    verdict = CommandJudge("judge-cmd", runner=_fixed(stdout=answer)).choose(QUERY)
    assert verdict.outcome is None and verdict.confidence == 0.0 and verdict.fallback is None


def test_fallback_details_never_carry_command_output() -> None:
    secret = b"sk-ant-leaked-in-stdout"
    verdict = CommandJudge("judge-cmd", runner=_fixed(returncode=1, stdout=secret)).choose(QUERY)
    assert "leaked" not in repr(verdict)


# -- budget and disablement ------------------------------------------------------------------


def test_max_calls_per_run_is_enforced_by_abstaining() -> None:
    calls: list[bytes] = []

    def runner(argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
        calls.append(stdin)
        return CommandResult(0, _answer())

    judge = CommandJudge("judge-cmd", max_calls_per_run=2, runner=runner)
    first, second, third = (judge.choose(QUERY) for _ in range(3))
    assert first.outcome == second.outcome == "b" and len(calls) == 2
    assert third.outcome is None and third.confidence == 0.0
    assert third.fallback is not None and third.fallback.reason == "max-calls"
    assert (
        CommandJudge("judge-cmd", max_calls_per_run=0, runner=runner)
        .score(ScoreQuery("q", ("A", "B")))
        .fallback.reason
        == "max-calls"
    )
    assert len(calls) == 2


def test_disabled_judge_spawns_no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a disabled judge must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    judge = build_judge(JudgeSettings(command="should-not-run"))
    assert isinstance(judge, DeterministicJudge)
    judge.choose(ChoiceQuery("q", {"a": "x", ABSTAIN: "none"}, "unrelated"))
    judge.score(ScoreQuery("q", ("A", "B"), "nothing"))


def test_enabled_settings_build_a_command_judge() -> None:
    settings = JudgeSettings(
        enabled=True, command="my-judge --flag", timeout_seconds=5, model="m", max_calls_per_run=7
    )
    judge = build_judge(settings, runner=_fixed(stdout=_answer()))
    assert isinstance(judge, CommandJudge)
    assert judge.argv == ("my-judge", "--flag") and judge.timeout_seconds == 5
    assert judge.model == "m" and judge.max_calls_per_run == 7
    assert judge.engine == "bh-judge/1:my-judge"


# -- settings --------------------------------------------------------------------------------


def test_settings_defaults_are_off() -> None:
    settings = JudgeSettings()
    assert settings.enabled is False and dict(settings.auto_accept) == {}
    assert (settings.command, settings.model) == ("bh-judge-haiku", "claude-haiku-5-5")
    assert settings.timeout_seconds == 30.0 and settings.max_calls_per_run == 100


def test_settings_precedence_flags_over_env_over_defaults() -> None:
    environ = {
        "BEADLOG_JUDGE_ENABLED": "true",
        "BEADLOG_JUDGE_MODEL": "env-model",
        "BEADLOG_JUDGE_AUTO_ACCEPT": "reconcile.match=0.9, other=0.5",
        "BEADLOG_JUDGE_MAX_CALLS_PER_RUN": "3",
        "BEADLOG_JUDGE_TIMEOUT_SECONDS": "12.5",
    }
    settings = settings_from({"model": "flag-model", "command": None}, environ)
    assert settings.enabled is True and settings.model == "flag-model"
    assert settings.command == "bh-judge-haiku" and settings.max_calls_per_run == 3
    assert dict(settings.auto_accept) == {"reconcile.match": 0.9, "other": 0.5}
    assert settings.timeout_seconds == 12.5
    assert settings_from({}, {}) == JudgeSettings()


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_seconds": 0},
        {"max_calls_per_run": -1},
        {"auto_accept": {"f": 1.5}},
        {"command": " "},
        {"unknown": 1},
    ],
)
def test_invalid_settings_are_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        settings_from(overrides, {})


def test_invalid_env_values_are_refused() -> None:
    with pytest.raises(ValueError):
        settings_from({}, {"BEADLOG_JUDGE_ENABLED": "maybe"})
    with pytest.raises(ValueError):
        settings_from({}, {"BEADLOG_JUDGE_AUTO_ACCEPT": "no-threshold"})


# -- the envelope schema ---------------------------------------------------------------------


def test_docs_copy_of_the_schema_is_byte_identical() -> None:
    packaged = Path(judges_module.__file__).parent / "schemas" / "bh-judge-v1.schema.json"
    assert DOCS_SCHEMA.read_bytes() == packaged.read_bytes()


@pytest.mark.parametrize(
    "envelope",
    [
        {"schema": "bh-judge/2", "type": "response"},
        {**judgment_response(Judgment("a", {"a": 2.0}, 1, 1, "", "e", "1")), "extra": 1},
        {**choice_request(ChoiceQuery("q", {}), model=None)},
        {**score_request(ScoreQuery("q", ("only",)), model=None)},
    ],
)
def test_schema_and_parser_both_refuse_bad_envelopes(envelope: dict[str, Any]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    with pytest.raises(jsonschema.ValidationError):
        _validator().validate(envelope)
    with pytest.raises(JudgeProtocolError):
        if envelope.get("type") == "response":
            parse_response(envelope, ["a"])
        else:
            parse_request(envelope)


def test_defaults_match_the_root_backfill_judge_config_schema() -> None:
    """The standalone settings and root bh's ``backfill.judge`` section share keys and defaults
    (read from the generated config artifact as data; this package never imports beadhive)."""
    artifacts = sorted(
        REPO.glob("src/beadhive/schemas/contracts/v*/artifacts/config-v1.schema.json"),
        key=lambda path: tuple(int(part) for part in path.parts[-3][1:].split(".")),
    )
    schema = json.loads(artifacts[-1].read_text(encoding="utf-8"))
    judge = schema["$defs"]["BackfillJudgeConfig"]["properties"]
    # bh-only keys (bh backfill judge setup, bh backfill history)
    host_only = {"cost_per_call_usd", "credential", "resume"}
    assert set(judge) - host_only == set(judges_module.SETTING_KEYS)
    assert judge["cost_per_call_usd"]["default"] is None
    defaults = JudgeSettings()
    for key in judges_module.SETTING_KEYS:
        if key == "auto_accept":
            assert dict(defaults.auto_accept) == {} and "default" not in judge[key]
        else:
            assert judge[key]["default"] == getattr(defaults, key), key
