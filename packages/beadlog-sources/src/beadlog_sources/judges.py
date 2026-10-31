"""Judgment engines behind :class:`~beadlog_sources.JudgmentPort` (backfill platform ADR §4).

- :class:`DeterministicJudge` — the default: lexical, a one-hot answer at confidence 1.0 or an
  abstention. No I/O.
- :class:`CommandJudge` — speaks the versioned ``bh-judge/1`` JSON envelope to a configured
  subprocess over stdin/stdout (the ``semantic_selection`` precedent). It **fails closed**: a
  timeout, a non-zero exit, malformed output or a wrong envelope version each fall back to the
  deterministic engine, with the fallback recorded on the returned :class:`Judgment`. Once
  ``max_calls_per_run`` calls are spent it abstains without spawning anything.
- :func:`build_judge` picks the engine from :class:`JudgeSettings`; with the judge disabled
  (the default) it returns the deterministic engine and no subprocess is ever spawned.

Choice confidence follows the Jev derivation ``clamp((n * p_max - 1) / (n - 1), 0, 1)`` where
``n`` is the option count (abstain included), so ``option_count`` is always recorded next to
``confidence``; a score uses the same formula over its criteria. An abstention (outcome
``None``) always carries confidence 0.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from types import MappingProxyType
from typing import Any

from .contracts import ABSTAIN, ChoiceQuery, Fallback, Judgment, ScoreQuery

SCHEMA = "bh-judge/1"
DETERMINISTIC_ENGINE = "deterministic-lexical"
DETERMINISTIC_VERSION = "1"
DEFAULT_COMMAND = "bh-judge-haiku"
DEFAULT_MODEL = "claude-haiku-5-5"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_CALLS_PER_RUN = 100
# Lexical match bar of the deterministic choice: token Jaccard of option text and subject.
LEXICAL_THRESHOLD = 0.8
_MAX_OUTPUT_BYTES = 1024 * 1024
_PROBABILITY_SLACK = 0.01
_TOKEN = re.compile(r"[a-z0-9]+")
_EMPTY: Mapping[str, float] = MappingProxyType({})


class JudgeProtocolError(ValueError):
    """A ``bh-judge/1`` envelope that does not conform (the caller falls back)."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        super().__init__(detail)


def judge_schema() -> dict[str, Any]:
    """The packaged ``bh-judge/1`` JSON Schema (draft 2020-12)."""
    text = (resources.files("beadlog_sources") / "schemas" / "bh-judge-v1.schema.json").read_text(
        encoding="utf-8"
    )
    return json.loads(text)


def choice_confidence(probabilities: Mapping[str, float], option_count: int) -> float:
    """``clamp((n * p_max - 1) / (n - 1), 0, 1)``; with a single option, ``p_max`` itself."""
    p_max = max(probabilities.values(), default=0.0)
    if option_count <= 1:
        return max(0.0, min(1.0, p_max))
    return max(0.0, min(1.0, (option_count * p_max - 1) / (option_count - 1)))


def tokens(text: str) -> frozenset[str]:
    """Lower-cased alphanumeric tokens of ``text`` (the deterministic engine's vocabulary)."""
    return frozenset(_TOKEN.findall(text.lower()))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Token-set Jaccard similarity (0 when either side is empty)."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _subject(context: str) -> str:
    return context.split("\n", 1)[0]


def _abstain(option_count: int, rationale: str, offered: Sequence[str]) -> Judgment:
    probabilities = {ABSTAIN: 1.0} if ABSTAIN in offered else {}
    return Judgment(
        outcome=None,
        probabilities=probabilities,
        confidence=0.0,
        option_count=option_count,
        rationale=rationale,
        engine=DETERMINISTIC_ENGINE,
        engine_version=DETERMINISTIC_VERSION,
    )


def _one_hot(key: str, option_count: int, rationale: str) -> Judgment:
    return Judgment(
        outcome=key,
        probabilities={key: 1.0},
        confidence=1.0,
        option_count=option_count,
        rationale=rationale,
        engine=DETERMINISTIC_ENGINE,
        engine_version=DETERMINISTIC_VERSION,
    )


class DeterministicJudge:
    """Lexical judge: one-hot at confidence 1.0, or abstain.

    ``choose`` reads the **first line** of ``query.context`` as the subject and compares its
    token set with each non-abstain option's text (Jaccard). It picks the single best option
    when that score is at least :data:`LEXICAL_THRESHOLD` and strictly above the runner-up;
    otherwise it abstains. ``score`` picks the one criterion whose token sequence appears in
    the context as a phrase; zero or several such criteria abstain.
    """

    engine = DETERMINISTIC_ENGINE
    engine_version = DETERMINISTIC_VERSION
    model: str | None = None

    def choose(self, query: ChoiceQuery) -> Judgment:
        subject = tokens(_subject(query.context))
        count = len(query.options)
        scored = sorted(
            (
                (jaccard(subject, tokens(text)), key)
                for key, text in query.options.items()
                if key != ABSTAIN
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if not scored or scored[0][0] < LEXICAL_THRESHOLD:
            return _abstain(count, "no option matches the subject lexically", list(query.options))
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            return _abstain(count, "several options match the subject equally", list(query.options))
        best, key = scored[0]
        return _one_hot(key, count, f"lexical match {best:.2f} with option {key!r}")

    def score(self, query: ScoreQuery) -> Judgment:
        context = " " + " ".join(_TOKEN.findall(query.context.lower())) + " "
        hits = [
            criterion
            for criterion in query.criteria
            if (phrase := " ".join(_TOKEN.findall(criterion.lower()))) and f" {phrase} " in context
        ]
        count = len(query.criteria)
        if len(hits) != 1:
            why = "no criterion" if not hits else "several criteria"
            return _abstain(count, f"{why} named in the context", ())
        return _one_hot(hits[0], count, f"criterion {hits[0]!r} named in the context")


# -- the bh-judge/1 envelope -----------------------------------------------------------------


def choice_request(query: ChoiceQuery, *, model: str | None) -> dict[str, Any]:
    """The ``bh-judge/1`` request object for a choice."""
    return {
        "schema": SCHEMA,
        "type": "request",
        "kind": "choice",
        "family": query.family,
        "question": query.question,
        "options": dict(query.options),
        "context": query.context,
        "model": model,
    }


def score_request(query: ScoreQuery, *, model: str | None) -> dict[str, Any]:
    """The ``bh-judge/1`` request object for a score."""
    return {
        "schema": SCHEMA,
        "type": "request",
        "kind": "score",
        "family": query.family,
        "question": query.question,
        "criteria": list(query.criteria),
        "context": query.context,
        "model": model,
    }


def _string(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise JudgeProtocolError("malformed", f"{name} must be a string")
    return value


def parse_request(payload: Any) -> tuple[ChoiceQuery | ScoreQuery, str | None]:
    """Validate a request object; return ``(query, model)``."""
    if not isinstance(payload, Mapping):
        raise JudgeProtocolError("malformed", "request must be a JSON object")
    if payload.get("schema") != SCHEMA:
        raise JudgeProtocolError("version", f"unsupported envelope {payload.get('schema')!r}")
    if payload.get("type") != "request":
        raise JudgeProtocolError("malformed", "not a request envelope")
    kind = payload.get("kind")
    family = _string(payload.get("family"), "family") or ""
    question = _string(payload.get("question"), "question") or ""
    context = _string(payload.get("context"), "context") or ""
    model = _string(payload.get("model"), "model", nullable=True)
    if not family:
        raise JudgeProtocolError("malformed", "family must not be empty")
    query: ChoiceQuery | ScoreQuery
    if kind == "choice":
        options = payload.get("options")
        if (
            not isinstance(options, Mapping)
            or not options
            or not all(isinstance(k, str) and k and isinstance(v, str) for k, v in options.items())
        ):
            raise JudgeProtocolError("malformed", "options must map non-empty keys to text")
        query = ChoiceQuery(question, dict(options), context, family)
    elif kind == "score":
        criteria = payload.get("criteria")
        if (
            not isinstance(criteria, list)
            or not 2 <= len(criteria) <= 10
            or not all(isinstance(c, str) and c for c in criteria)
            or len(set(criteria)) != len(criteria)
        ):
            raise JudgeProtocolError("malformed", "criteria must be 2..10 distinct strings")
        query = ScoreQuery(question, tuple(criteria), context, family)
    else:
        raise JudgeProtocolError("malformed", f"unknown request kind {kind!r}")
    return query, model


def judgment_response(judgment: Judgment) -> dict[str, Any]:
    """The ``bh-judge/1`` response object for ``judgment``."""
    return {
        "schema": SCHEMA,
        "type": "response",
        "outcome": judgment.outcome,
        "probabilities": dict(judgment.probabilities),
        "rationale": judgment.rationale,
        "engine": judgment.engine,
        "engine_version": judgment.engine_version,
        "model": judgment.model,
    }


def parse_response(payload: Any, keys: Sequence[str]) -> Judgment:
    """Validate a response object against the offered ``keys`` (option keys or criteria)."""
    if not isinstance(payload, Mapping):
        raise JudgeProtocolError("malformed", "response must be a JSON object")
    if payload.get("schema") != SCHEMA:
        raise JudgeProtocolError("version", f"unsupported envelope {payload.get('schema')!r}")
    if payload.get("type") != "response":
        raise JudgeProtocolError("malformed", "not a response envelope")
    extra = set(payload) - {
        "schema",
        "type",
        "outcome",
        "probabilities",
        "rationale",
        "engine",
        "engine_version",
        "model",
    }
    if extra:
        raise JudgeProtocolError("malformed", f"unknown response members {sorted(extra)}")
    offered = set(keys)
    outcome = _string(payload.get("outcome"), "outcome", nullable=True)
    if outcome is not None and outcome not in offered:
        raise JudgeProtocolError("malformed", "outcome is not one of the offered keys")
    if outcome == ABSTAIN:
        outcome = None
    raw = payload.get("probabilities")
    if not isinstance(raw, Mapping):
        raise JudgeProtocolError("malformed", "probabilities must be an object")
    probabilities: dict[str, float] = {}
    for key, value in raw.items():
        if key not in offered:
            raise JudgeProtocolError("malformed", "probabilities name a key that was not offered")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise JudgeProtocolError("malformed", "every probability must be within 0..1")
        probabilities[key] = float(value)
    if probabilities and abs(sum(probabilities.values()) - 1.0) > _PROBABILITY_SLACK:
        raise JudgeProtocolError("malformed", "probabilities must sum to 1")
    rationale = _string(payload.get("rationale"), "rationale") or ""
    engine = _string(payload.get("engine"), "engine") or ""
    engine_version = _string(payload.get("engine_version"), "engine_version") or ""
    if not engine or not engine_version:
        raise JudgeProtocolError("malformed", "engine and engine_version must not be empty")
    model = _string(payload.get("model"), "model", nullable=True)
    return Judgment(
        outcome=outcome,
        probabilities=probabilities,
        confidence=0.0 if outcome is None else choice_confidence(probabilities, len(keys)),
        option_count=len(keys),
        rationale=rationale[:4096],
        engine=engine,
        engine_version=engine_version,
        model=model,
    )


# -- the command judge -----------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """What a judge command returned: exit status and stdout bytes."""

    returncode: int
    stdout: bytes


Runner = Callable[[Sequence[str], bytes, float], CommandResult]


def subprocess_runner(argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
    """Run ``argv`` with ``stdin`` and a timeout; raises :class:`subprocess.TimeoutExpired`.

    The child inherits the environment (a reference command reads its API key from there) and
    its stderr goes to the parent's stderr; nothing it prints is ever recorded.
    """
    completed = subprocess.run(  # noqa: S603 - the operator configures the command
        list(argv), input=stdin, stdout=subprocess.PIPE, timeout=timeout, check=False
    )
    return CommandResult(completed.returncode, completed.stdout)


class CommandJudge:
    """A ``bh-judge/1`` subprocess judge that fails closed to :class:`DeterministicJudge`.

    ``runner`` executes the command (inject a fake in tests; the default spawns a subprocess).
    ``max_calls_per_run`` bounds the calls this instance makes; once spent it abstains without
    running anything. Fallback provenance is recorded on every judgment that did not come from
    the command.
    """

    engine_version = "1"

    def __init__(
        self,
        command: str | Sequence[str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        model: str | None = DEFAULT_MODEL,
        max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN,
        runner: Runner | None = None,
        fallback: DeterministicJudge | None = None,
    ) -> None:
        self.argv = tuple(shlex.split(command) if isinstance(command, str) else command)
        if not self.argv:
            raise ValueError("a judge command must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_calls_per_run < 0:
            raise ValueError("max_calls_per_run must not be negative")
        self.timeout_seconds = timeout_seconds
        self.model = model
        self.max_calls_per_run = max_calls_per_run
        self.calls = 0
        self._runner = runner or subprocess_runner
        self._fallback = fallback or DeterministicJudge()

    @property
    def engine(self) -> str:
        return f"{SCHEMA}:{os.path.basename(self.argv[0])}"

    def choose(self, query: ChoiceQuery) -> Judgment:
        request = choice_request(query, model=self.model)
        return self._ask(request, list(query.options), lambda: self._fallback.choose(query))

    def score(self, query: ScoreQuery) -> Judgment:
        request = score_request(query, model=self.model)
        return self._ask(request, list(query.criteria), lambda: self._fallback.score(query))

    def _ask(
        self, request: Mapping[str, Any], keys: Sequence[str], fallback: Callable[[], Judgment]
    ) -> Judgment:
        if self.calls >= self.max_calls_per_run:
            return Judgment(
                outcome=None,
                probabilities=_EMPTY,
                confidence=0.0,
                option_count=len(keys),
                rationale="judge call budget spent; abstaining",
                engine=self.engine,
                engine_version=self.engine_version,
                model=self.model,
                fallback=Fallback(
                    "abstain", "max-calls", f"max_calls_per_run={self.max_calls_per_run}"
                ),
            )
        self.calls += 1
        payload = json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            result = self._runner(self.argv, payload, self.timeout_seconds)
            if result.returncode != 0:
                raise JudgeProtocolError("exit", f"judge command exited {result.returncode}")
            if len(result.stdout) > _MAX_OUTPUT_BYTES:
                raise JudgeProtocolError("malformed", "judge output is too large")
            try:
                answer = json.loads(result.stdout.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise JudgeProtocolError("malformed", "judge output is not JSON") from exc
            return parse_response(answer, keys)
        except subprocess.TimeoutExpired:
            reason, detail = "timeout", f"no answer within {self.timeout_seconds:g}s"
        except JudgeProtocolError as exc:
            reason, detail = exc.reason, str(exc)
        except OSError as exc:
            reason, detail = "spawn", f"cannot run the judge command ({type(exc).__name__})"
        verdict = fallback()
        return Judgment(
            outcome=verdict.outcome,
            probabilities=verdict.probabilities,
            confidence=verdict.confidence,
            option_count=verdict.option_count,
            rationale=verdict.rationale,
            engine=verdict.engine,
            engine_version=verdict.engine_version,
            model=verdict.model,
            fallback=Fallback(verdict.engine, reason, detail[:300]),
        )


# -- settings --------------------------------------------------------------------------------


def _frozen(mapping: Mapping[str, float]) -> Mapping[str, float]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class JudgeSettings:
    """The ``backfill.judge`` keys (root bh config) as the standalone tools take them.

    Every default is off: ``enabled=False`` means the deterministic engine only and no
    subprocess. ``auto_accept`` maps a judgment family to the confidence at or above which a
    judged verdict needs no human review (empty: every judged verdict is reviewed).
    """

    enabled: bool = False
    command: str = DEFAULT_COMMAND
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    model: str = DEFAULT_MODEL
    auto_accept: Mapping[str, float] = field(default_factory=dict)
    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise ValueError("backfill.judge.command must not be empty")
        if not self.model.strip():
            raise ValueError("backfill.judge.model must not be empty")
        if not self.timeout_seconds > 0:
            raise ValueError("backfill.judge.timeout_seconds must be positive")
        if self.max_calls_per_run < 0:
            raise ValueError("backfill.judge.max_calls_per_run must not be negative")
        for family, threshold in self.auto_accept.items():
            if not family or not 0 <= threshold <= 1:
                raise ValueError(f"backfill.judge.auto_accept[{family!r}] must be within 0..1")
        object.__setattr__(self, "auto_accept", _frozen(self.auto_accept))


ENV_PREFIX = "BEADLOG_JUDGE_"
SETTING_KEYS = (
    "enabled",
    "command",
    "timeout_seconds",
    "model",
    "auto_accept",
    "max_calls_per_run",
)


def parse_auto_accept(text: str) -> dict[str, float]:
    """``family=threshold[,family=threshold...]`` → mapping (the env/flag spelling)."""
    out: dict[str, float] = {}
    for item in text.split(","):
        if not item.strip():
            continue
        family, separator, threshold = item.partition("=")
        if not separator or not family.strip():
            raise ValueError(f"auto_accept entry {item!r} is not family=threshold")
        out[family.strip()] = float(threshold)
    return out


def parse_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"not a boolean: {text!r}")


def settings_from(
    overrides: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None
) -> JudgeSettings:
    """Settings from defaults < ``BEADLOG_JUDGE_<KEY>`` env < explicit ``overrides`` (flags).

    ``None`` override values are ignored, so unset flags fall through to the environment.
    """
    environ = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    converters: dict[str, Callable[[str], Any]] = {
        "enabled": parse_bool,
        "command": str,
        "timeout_seconds": float,
        "model": str,
        "auto_accept": parse_auto_accept,
        "max_calls_per_run": int,
    }
    for key in SETTING_KEYS:
        raw = environ.get(ENV_PREFIX + key.upper())
        if raw is not None:
            values[key] = converters[key](raw)
    for key, value in (overrides or {}).items():
        if key not in SETTING_KEYS:
            raise ValueError(f"unknown judge setting {key!r}")
        if value is not None:
            values[key] = value
    return JudgeSettings(**values)


def build_judge(
    settings: JudgeSettings, *, runner: Runner | None = None
) -> DeterministicJudge | CommandJudge:
    """The configured engine: the deterministic judge unless ``settings.enabled``."""
    if not settings.enabled:
        return DeterministicJudge()
    return CommandJudge(
        settings.command,
        timeout_seconds=settings.timeout_seconds,
        model=settings.model,
        max_calls_per_run=settings.max_calls_per_run,
        runner=runner,
    )


__all__ = [
    "DEFAULT_COMMAND",
    "DEFAULT_MAX_CALLS_PER_RUN",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "DETERMINISTIC_ENGINE",
    "ENV_PREFIX",
    "LEXICAL_THRESHOLD",
    "SCHEMA",
    "SETTING_KEYS",
    "CommandJudge",
    "CommandResult",
    "DeterministicJudge",
    "JudgeProtocolError",
    "JudgeSettings",
    "Runner",
    "build_judge",
    "choice_confidence",
    "choice_request",
    "jaccard",
    "judge_schema",
    "judgment_response",
    "parse_auto_accept",
    "parse_bool",
    "parse_request",
    "parse_response",
    "score_request",
    "settings_from",
    "subprocess_runner",
    "tokens",
]
