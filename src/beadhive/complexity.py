"""Provider-neutral complexity tiers and the bundled local classifier.

The public boundary in this module is deliberately smaller than the scoring implementation:
callers depend on :class:`ComplexityTier`, :class:`ComplexityResult`, and the one-method
:class:`ComplexityClassifier` protocol.  Keyword lists and score mechanics belong to the bundled
backend and can be replaced without changing that contract.

The local scorer is a modified Python port of Maxim Bifrost's Apache-2.0 complexity analyzer at
commit ``c1b84fdc5a85176975c2943e8a5f965705dbeb16`` (Copyright 2025 H3 Labs Inc.).  See
``docs/upstream/bifrost-complexity-scorer.md`` for source paths, obligations, and deviations.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable

COMPLEXITY_LABEL_PREFIX = "complexity:"
UNKNOWN = "UNKNOWN"

LOCAL_SCORER_SOURCE = "beadhive/bifrost-compatible-local"
LOCAL_SCORER_VERSION = "1.0.0+bifrost.c1b84fdc5a85"


class ComplexityTier(IntEnum):
    """The durable capability vocabulary, ordered from least to most demanding."""

    SIMPLE = 0
    MEDIUM = 1
    COMPLEX = 2
    REASONING = 3

    @classmethod
    def parse(cls, value: str) -> ComplexityTier:
        """Parse an exact canonical tier name; lower/mixed-case spellings are invalid."""
        if not isinstance(value, str) or value not in cls.__members__:
            raise ValueError(f"invalid complexity tier {value!r}; expected one of {tier_names()}")
        return cls[value]

    @classmethod
    def from_label(cls, label: str) -> ComplexityTier:
        """Parse one canonical ``complexity:<TIER>`` label."""
        if not isinstance(label, str) or not label.startswith(COMPLEXITY_LABEL_PREFIX):
            raise ValueError(
                f"invalid complexity label {label!r}; expected {COMPLEXITY_LABEL_PREFIX}<TIER>"
            )
        return cls.parse(label[len(COMPLEXITY_LABEL_PREFIX) :])

    @property
    def label(self) -> str:
        """The canonical bead label for this tier."""
        return f"{COMPLEXITY_LABEL_PREFIX}{self.name}"

    def __str__(self) -> str:
        return self.name


def tier_names() -> tuple[str, ...]:
    """Canonical tier names in capability order."""
    return tuple(tier.name for tier in ComplexityTier)


def complexity_label(tier: ComplexityTier) -> str:
    """Return the canonical bead label for ``tier`` with a strict type boundary."""
    if not isinstance(tier, ComplexityTier):
        raise TypeError(f"tier must be ComplexityTier, got {type(tier).__name__}")
    return tier.label


def parse_complexity_label(label: str) -> ComplexityTier:
    """Parse a canonical complexity label (case-sensitive)."""
    return ComplexityTier.from_label(label)


@dataclass(frozen=True)
class FallbackProvenance:
    """Why a required classification replaced an UNKNOWN backend result."""

    fallback_tier: ComplexityTier
    reason: str
    from_value: str = UNKNOWN

    def __post_init__(self) -> None:
        if self.from_value != UNKNOWN:
            raise ValueError(f"fallback source must be {UNKNOWN!r}")
        if not self.reason:
            raise ValueError("fallback reason must not be empty")


@dataclass(frozen=True)
class ComplexityResult:
    """Backend-neutral classification result.

    ``tier is None`` explicitly represents UNKNOWN.  Keeping UNKNOWN outside
    :class:`ComplexityTier` preserves the exact four-tier ordered public vocabulary.  A required
    classification instead carries a concrete tier plus ``fallback`` provenance.
    """

    tier: ComplexityTier | None
    score: float
    source: str
    version: str
    fallback: FallbackProvenance | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("complexity score must be normalized to 0..1")
        if not self.source or not self.version:
            raise ValueError("complexity source and version must not be empty")
        if self.fallback is not None and self.tier != self.fallback.fallback_tier:
            raise ValueError("fallback tier must match the result tier")

    @property
    def is_unknown(self) -> bool:
        return self.tier is None

    @property
    def fallback_used(self) -> bool:
        return self.fallback is not None


@runtime_checkable
class ComplexityClassifier(Protocol):
    """Stable classifier seam; implementations inspect text and never mutate bead state."""

    def classify(
        self,
        text: str,
        *,
        required: bool = False,
        fallback_tier: ComplexityTier = ComplexityTier.MEDIUM,
    ) -> ComplexityResult: ...


@dataclass(frozen=True)
class TierBoundaries:
    """Inclusive lower bounds for MEDIUM, COMPLEX, and REASONING."""

    simple_medium: float = 0.15
    medium_complex: float = 0.35
    complex_reasoning: float = 0.60

    def __post_init__(self) -> None:
        values = (self.simple_medium, self.medium_complex, self.complex_reasoning)
        if not 0.0 < values[0] < values[1] < values[2] < 1.0:
            raise ValueError("complexity boundaries must satisfy 0 < simple < medium < complex < 1")


_CODE_KEYWORDS = (
    "function",
    "class",
    "api",
    "database",
    "algorithm",
    "code",
    "implement",
    "debug",
    "error",
    "syntax",
    "compile",
    "runtime",
    "library",
    "framework",
    "variable",
    "loop",
    "array",
    "object",
    "method",
    "interface",
    "regex",
    "deploy",
    "docker",
    "sql",
    "query",
    "schema",
    "endpoint",
    "refactor",
    "bug",
    "parse",
    "async",
    "webhook",
    "migration",
    "ci/cd",
    "pipeline",
    "rest",
    "graphql",
    "test",
    "unit test",
    "python",
    "javascript",
    "typescript",
    "golang",
    "java",
    "ruby",
    "github actions",
    "monorepo",
    "aws cli",
    "config rule",
    "config rules",
    "retry",
    "fallback",
    "middleware",
    "patch",
    "diff",
    "pr",
    "pull request",
    "commit",
    "commit message",
    "behavior change",
    "cel",
    "auto-routing",
    "rwmutex",
    "goroutine",
)

_STRONG_REASONING_KEYWORDS = (
    "step by step",
    "think through",
    "tradeoffs",
    "pros and cons",
    "justify",
    "critique",
    "implications",
    "explain why",
    "root cause analysis",
    "reconstruct the sequence",
    "reconstruct the most likely sequence",
    "what should have happened instead",
    "explain your reasoning",
    "weigh the tradeoffs",
    "recommend a design",
)

_TECHNICAL_KEYWORDS = (
    "architecture",
    "distributed",
    "encryption",
    "authentication",
    "scalability",
    "microservices",
    "kubernetes",
    "infrastructure",
    "protocol",
    "latency",
    "throughput",
    "concurrency",
    "optimization",
    "load balancer",
    "caching",
    "sharding",
    "replication",
    "consensus",
    "mutex",
    "deadlock",
    "race condition",
    "api gateway",
    "terraform",
    "observability",
    "access token",
    "refresh token",
    "rbac",
    "sso",
    "oidc",
    "saml",
    "tenant",
    "multi-tenant",
    "audit log",
    "failover",
    "idempotency",
    "zero downtime",
    "incident",
    "outage",
    "postmortem",
    "root cause",
    "telemetry",
    "metrics",
    "configmap",
    "connection pool",
    "payment processing",
    "saas",
    "feature flag",
    "operational risk",
    "vendor lock-in",
    "s3 bucket",
    "misconfiguration",
    "remediation",
    "oltp",
    "olap",
    "ledger",
    "metering",
    "aggregation",
    "proration",
    "credits",
    "dunning",
    "invoice",
    "invoice generation",
    "double-entry",
    "reconciliation",
    "chart of accounts",
    "hipaa",
    "quarantine workflow",
    "retention policy",
    "audit trail",
    "pre-signed url",
    "entitlements",
    "seat limits",
    "usage quotas",
    "deprovisioning",
    "permission drift",
    "role mapping",
    "fraud detection",
    "manual review",
    "feedback loop",
    "model serving",
    "a/b testing",
    "identity resolution",
    "deterministic replay",
    "tamper evidence",
    "hash chain",
    "approval workflow",
    "vpc",
    "soc 2",
    "data residency",
    "disaster recovery",
    "data race",
    "struct copy",
    "hybrid search",
)

_SIMPLE_KEYWORDS = (
    "what is",
    "define",
    "hello",
    "hi",
    "thanks",
    "how do i spell",
    "translate",
    "what does",
    "who is",
    "when was",
    "tell me about",
    "good morning",
    "good night",
    "how are you",
    "simple",
    "brief",
    "short",
    "quick",
    "beginner",
    "basic",
    "concise",
)


@dataclass(frozen=True)
class BifrostScorerConfig:
    """Configuration owned by the replaceable local backend."""

    boundaries: TierBoundaries = TierBoundaries()
    code_keywords: tuple[str, ...] = _CODE_KEYWORDS
    reasoning_keywords: tuple[str, ...] = _STRONG_REASONING_KEYWORDS
    technical_keywords: tuple[str, ...] = _TECHNICAL_KEYWORDS
    simple_keywords: tuple[str, ...] = _SIMPLE_KEYWORDS

    def __post_init__(self) -> None:
        for name in (
            "code_keywords",
            "reasoning_keywords",
            "technical_keywords",
            "simple_keywords",
        ):
            if not tuple(keyword.strip() for keyword in getattr(self, name) if keyword.strip()):
                raise ValueError(f"{name} must contain at least one non-empty keyword")


@dataclass(frozen=True)
class _SignalCounts:
    words: int
    code: int
    reasoning: int
    technical: int
    simple: int


def _keyword_matches(text: str, keywords: tuple[str, ...]) -> int:
    """Count configured keyword identities at most once each, case-insensitively."""
    count = 0
    for keyword in dict.fromkeys(k.strip().lower() for k in keywords if k.strip()):
        if " " in keyword:
            matched = keyword in text
        else:
            matched = re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text) is not None
        count += int(matched)
    return count


def _score_count(count: int, cap_at: int) -> float:
    return min(1.0, count / cap_at) if cap_at > 0 else 0.0


def _word_count_score(words: int) -> float:
    """Bifrost's three-segment word-count curve, before its 10% dimension weight."""
    if words < 15:
        return words / 15.0 * 0.3
    if words <= 400:
        return 0.3 + (words - 15) / 385.0 * 0.4
    return 0.7 + min(0.3, (words - 400) / 600.0 * 0.3)


def _stable_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def stable_bead_text(bead: Mapping[str, Any]) -> str:
    """Render only immutable planning fields in one fixed order for repeatable scoring.

    Operational fields such as status, assignee, comments, dependencies, labels, and timestamps
    are intentionally ignored.  Changing them cannot perturb a bead's classification.
    """
    fields = (
        ("Issue type", "issue_type"),
        ("Title", "title"),
        ("Description", "description"),
        ("Design", "design"),
        ("Acceptance criteria", "acceptance_criteria"),
    )
    sections = []
    for heading, key in fields:
        value = _stable_value(bead.get(key))
        if value:
            sections.append(f"{heading}:\n{value}")
    return "\n\n".join(sections)


class BifrostLocalClassifier:
    """Deterministic, dependency-free, best-effort Bifrost-compatible classifier."""

    source = LOCAL_SCORER_SOURCE
    version = LOCAL_SCORER_VERSION

    def __init__(self, config: BifrostScorerConfig | None = None) -> None:
        self._config = config or BifrostScorerConfig()

    def _signals(self, text: str) -> _SignalCounts:
        lowered = text.lower()
        return _SignalCounts(
            words=len(text.split()),
            code=_keyword_matches(lowered, self._config.code_keywords),
            reasoning=_keyword_matches(lowered, self._config.reasoning_keywords),
            technical=_keyword_matches(lowered, self._config.technical_keywords),
            simple=_keyword_matches(lowered, self._config.simple_keywords),
        )

    def _tier_for_score(self, score: float) -> ComplexityTier:
        boundaries = self._config.boundaries
        if score < boundaries.simple_medium:
            return ComplexityTier.SIMPLE
        if score < boundaries.medium_complex:
            return ComplexityTier.MEDIUM
        if score < boundaries.complex_reasoning:
            return ComplexityTier.COMPLEX
        return ComplexityTier.REASONING

    def classify(
        self,
        text: str,
        *,
        required: bool = False,
        fallback_tier: ComplexityTier = ComplexityTier.MEDIUM,
    ) -> ComplexityResult:
        if not isinstance(text, str):
            raise TypeError(f"complexity text must be str, got {type(text).__name__}")
        if not isinstance(fallback_tier, ComplexityTier):
            raise TypeError("fallback_tier must be a ComplexityTier")

        signals = self._signals(text)
        has_signal = bool(signals.code or signals.reasoning or signals.technical or signals.simple)
        if not has_signal:
            if required:
                provenance = FallbackProvenance(
                    fallback_tier=fallback_tier,
                    reason="no configured complexity signal matched stable bead text",
                )
                return ComplexityResult(
                    fallback_tier, 0.0, self.source, self.version, fallback=provenance
                )
            return ComplexityResult(None, 0.0, self.source, self.version)

        code_score = _score_count(signals.code, 3)
        reasoning_score = _score_count(signals.reasoning, 2)
        technical_score = _score_count(signals.technical, 3)
        simple_score = _score_count(signals.simple, 2)
        word_score = _word_count_score(signals.words)

        score = (
            code_score * 0.30
            + reasoning_score * 0.25
            + technical_score * 0.25
            - simple_score * 0.05
            + word_score * 0.10
        )
        score = max(0.0, min(1.0, score))

        tier = self._tier_for_score(score)
        if signals.reasoning >= 2 or (
            signals.reasoning >= 1 and (code_score > 0.5 or technical_score > 0.5)
        ):
            tier = ComplexityTier.REASONING

        return ComplexityResult(tier, score, self.source, self.version)

    def classify_bead(
        self,
        bead: Mapping[str, Any],
        *,
        required: bool = True,
        fallback_tier: ComplexityTier = ComplexityTier.MEDIUM,
    ) -> ComplexityResult:
        """Classify a bead through the stable planning-field renderer."""
        return self.classify(stable_bead_text(bead), required=required, fallback_tier=fallback_tier)


DEFAULT_CLASSIFIER: ComplexityClassifier = BifrostLocalClassifier()
