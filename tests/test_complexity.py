"""Provider-neutral complexity contract and Bifrost-compatible local scorer regressions."""

from __future__ import annotations

import pytest

from beadhive import complexity


def test_tier_enum_has_exact_ordered_vocabulary():
    tiers = list(complexity.ComplexityTier)

    assert [tier.name for tier in tiers] == ["SIMPLE", "MEDIUM", "COMPLEX", "REASONING"]
    assert tiers == sorted(tiers)
    assert complexity.ComplexityTier.SIMPLE < complexity.ComplexityTier.MEDIUM
    assert complexity.ComplexityTier.MEDIUM < complexity.ComplexityTier.COMPLEX
    assert complexity.ComplexityTier.COMPLEX < complexity.ComplexityTier.REASONING


@pytest.mark.parametrize("tier", complexity.ComplexityTier)
def test_canonical_tier_and_label_helpers_round_trip(tier):
    assert complexity.ComplexityTier.parse(tier.name) is tier
    assert complexity.complexity_label(tier) == f"complexity:{tier.name}"
    assert complexity.parse_complexity_label(tier.label) is tier
    assert str(tier) == tier.name


@pytest.mark.parametrize(
    "value",
    ["simple", "Simple", "medium", "unknown", "UNKNOWN", "", " SIMPLE", "SIMPLE "],
)
def test_tier_parsing_is_case_sensitive_and_closed(value):
    with pytest.raises(ValueError, match="invalid complexity tier"):
        complexity.ComplexityTier.parse(value)


@pytest.mark.parametrize("label", ["complexity:simple", "complexity:UNKNOWN", "SIMPLE", ""])
def test_label_parsing_requires_the_exact_canonical_form(label):
    with pytest.raises(ValueError):
        complexity.parse_complexity_label(label)


@pytest.mark.parametrize(
    "value",
    [
        "openai/gpt-9.1",
        "anthropic/claude-opus-9",
        "future_provider/model_v2",
        "amazon-bedrock/anthropic.claude-4-7",
    ],
)
def test_model_preference_accepts_open_future_provider_and_model_names(value):
    assert complexity.valid_model_preference(value)


@pytest.mark.parametrize(
    "value",
    [
        "sonnet",
        "/model",
        "provider/",
        "provider/model/extra",
        "provider//model",
        "provider/model name",
        "provider/model,other",
        "provider:model/name",
        "provider/model:name",
        " provider/model",
        "provider/model ",
        "",
        None,
    ],
)
def test_model_preference_rejects_malformed_structure(value):
    assert not complexity.valid_model_preference(value)


def test_classifier_satisfies_stable_protocol_and_result_contract():
    classifier = complexity.BifrostLocalClassifier()

    result = classifier.classify("Implement a Python function and unit test")

    assert isinstance(classifier, complexity.ComplexityClassifier)
    assert result.tier is complexity.ComplexityTier.MEDIUM
    assert 0.0 <= result.score <= 1.0
    assert result.source == complexity.LOCAL_SCORER_SOURCE
    assert result.version == complexity.LOCAL_SCORER_VERSION
    assert not result.fallback_used


def test_stable_bead_text_uses_only_planning_fields_in_fixed_order():
    bead = {
        "title": "Add a retry policy",
        "description": "Implement bounded retry behavior.",
        "design": "Keep fallback selection deterministic.",
        "acceptance_criteria": "A unit test covers the timeout path.",
        "issue_type": "feature",
        "status": "open",
        "assignee": "dev/a",
        "comments": ["mutable"],
        "dependencies": [{"id": "other", "status": "open"}],
        "labels": ["model:anything", "size:xl"],
        "updated_at": "today",
    }
    changed_operations = dict(
        bead,
        status="closed",
        assignee="dev/b",
        comments=["different"],
        dependencies=[],
        labels=["model:changed"],
        updated_at="tomorrow",
    )

    rendered = complexity.stable_bead_text(bead)

    assert rendered == complexity.stable_bead_text(changed_operations)
    assert rendered.startswith("Issue type:\nfeature\n\nTitle:\nAdd a retry policy")
    assert "Acceptance criteria:\nA unit test covers the timeout path." in rendered
    assert "dev/a" not in rendered
    assert "model:anything" not in rendered


def test_stable_bead_text_accepts_molecule_spec_field_names():
    spec_issue = {
        "type": "feature",
        "title": "Add a retry policy",
        "description": "Implement bounded retry behavior.",
        "design": "Keep fallback selection deterministic.",
        "acceptance": "A unit test covers the timeout path.",
        "complexity": "REASONING",
        "model": "provider/model-preference",
    }
    bead = {
        "issue_type": spec_issue["type"],
        "title": spec_issue["title"],
        "description": spec_issue["description"],
        "design": spec_issue["design"],
        "acceptance_criteria": spec_issue["acceptance"],
    }

    assert complexity.stable_bead_text(spec_issue) == complexity.stable_bead_text(bead)


def test_classify_bead_uses_stable_render_and_requires_a_tier_by_default():
    classifier = complexity.BifrostLocalClassifier()
    bead = {"issue_type": "chore", "title": "Polish wording", "status": "open"}

    result = classifier.classify_bead(bead)

    assert result.tier is complexity.ComplexityTier.MEDIUM
    assert result.fallback_used
    assert result.fallback is not None
    assert result.fallback.from_value == complexity.UNKNOWN


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What is a webhook?", complexity.ComplexityTier.SIMPLE),
        ("Implement a Python function and unit test", complexity.ComplexityTier.MEDIUM),
        (
            "Implement and debug an API database architecture with Kubernetes authentication",
            complexity.ComplexityTier.COMPLEX,
        ),
        (
            "Think step by step and explain why this design has better tradeoffs",
            complexity.ComplexityTier.REASONING,
        ),
    ],
)
def test_regression_fixture_covers_all_four_tiers(text, expected):
    result = complexity.BifrostLocalClassifier().classify(text)

    assert result.tier is expected, (result.score, result)


def test_weighted_dimensions_and_simple_penalty_are_deterministic():
    classifier = complexity.BifrostLocalClassifier()

    code = classifier.classify("function class api")
    reasoning = classifier.classify("step by step")
    technical = classifier.classify("architecture kubernetes latency")
    code_with_simple_nudge = classifier.classify("brief function class api")

    assert code.score == pytest.approx(0.306)
    assert reasoning.score == pytest.approx(0.131)
    assert technical.score == pytest.approx(0.256)
    assert code_with_simple_nudge.score < code.score


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (0, 0.0),
        (14, 0.28),
        (15, 0.30),
        (400, 0.70),
        (700, 0.85),
        (1000, 1.0),
        (1600, 1.0),
    ],
)
def test_bifrost_word_count_curve(words, expected):
    assert complexity._word_count_score(words) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, complexity.ComplexityTier.SIMPLE),
        (0.149999, complexity.ComplexityTier.SIMPLE),
        (0.15, complexity.ComplexityTier.MEDIUM),
        (0.349999, complexity.ComplexityTier.MEDIUM),
        (0.35, complexity.ComplexityTier.COMPLEX),
        (0.599999, complexity.ComplexityTier.COMPLEX),
        (0.60, complexity.ComplexityTier.REASONING),
        (1.0, complexity.ComplexityTier.REASONING),
    ],
)
def test_default_tier_boundary_values_enter_the_higher_tier(score, expected):
    assert complexity.BifrostLocalClassifier()._tier_for_score(score) is expected


def test_tier_thresholds_are_configurable_and_strictly_ordered():
    config = complexity.BifrostScorerConfig(boundaries=complexity.TierBoundaries(0.05, 0.10, 0.20))
    classifier = complexity.BifrostLocalClassifier(config)

    assert classifier._tier_for_score(0.18) is complexity.ComplexityTier.COMPLEX
    with pytest.raises(ValueError, match="boundaries must satisfy"):
        complexity.TierBoundaries(0.35, 0.15, 0.60)


def test_two_strong_reasoning_signals_override_numeric_tier():
    result = complexity.BifrostLocalClassifier().classify("step by step and explain why")

    assert result.score < 0.60
    assert result.tier is complexity.ComplexityTier.REASONING


def test_one_reasoning_signal_plus_strong_code_overrides_numeric_tier():
    result = complexity.BifrostLocalClassifier().classify("explain why the function and API fail")

    assert result.score < 0.60
    assert result.tier is complexity.ComplexityTier.REASONING


def test_no_signal_preserves_unknown_without_required_fallback():
    result = complexity.BifrostLocalClassifier().classify("Polish the wording")

    assert result.tier is None
    assert result.is_unknown
    assert result.score == 0.0
    assert not result.fallback_used


def test_required_unknown_uses_explicit_medium_fallback_provenance():
    result = complexity.BifrostLocalClassifier().classify("Polish the wording", required=True)

    assert result.tier is complexity.ComplexityTier.MEDIUM
    assert not result.is_unknown
    assert result.fallback_used
    assert result.fallback == complexity.FallbackProvenance(
        fallback_tier=complexity.ComplexityTier.MEDIUM,
        reason="no configured complexity signal matched stable bead text",
    )


def test_required_unknown_fallback_tier_is_configurable():
    result = complexity.BifrostLocalClassifier().classify(
        "Polish the wording", required=True, fallback_tier=complexity.ComplexityTier.COMPLEX
    )

    assert result.tier is complexity.ComplexityTier.COMPLEX
    assert result.fallback is not None
    assert result.fallback.fallback_tier is complexity.ComplexityTier.COMPLEX


@pytest.mark.parametrize(
    ("bead", "expected"),
    [
        (
            {
                "issue_type": "chore",
                "title": "Write a brief definition for the contributor guide",
            },
            complexity.ComplexityTier.SIMPLE,
        ),
        (
            {
                "issue_type": "feature",
                "title": "Add unit tests for the config parser",
                "acceptance_criteria": "The error case and valid schema are tested.",
            },
            complexity.ComplexityTier.MEDIUM,
        ),
        (
            {
                "issue_type": "feature",
                "title": "Implement a distributed scheduler",
                "design": (
                    "Use concurrency, idempotency, telemetry, and failover while preserving the "
                    "API schema and database migration."
                ),
            },
            complexity.ComplexityTier.COMPLEX,
        ),
        (
            {
                "issue_type": "bug",
                "title": "Reconstruct a production incident",
                "design": (
                    "Perform root cause analysis step by step, explain why failover broke, and "
                    "weigh the tradeoffs of each remediation."
                ),
            },
            complexity.ComplexityTier.REASONING,
        ),
    ],
)
def test_representative_beadhive_engineering_tasks(bead, expected):
    result = complexity.BifrostLocalClassifier().classify_bead(bead)

    assert result.tier is expected, (complexity.stable_bead_text(bead), result)
