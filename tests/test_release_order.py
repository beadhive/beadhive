"""`beadhive.release_order` self-checks — the advisory stable-versioning merge-order scorer.

Pure unit tests over in-memory bead dicts shaped like `bd list --json` (id + `release:`/`wave:`
labels). Each case isolates one decision: fix/feature/breaking tier ordering, the `fix_churn_budget`
cap, wave grouping, and the strategy registry (default + unknown-name error). AAA structure.
"""

from __future__ import annotations

import pytest

from beadhive import release_order


def _bead(bead_id, *, release=None, wave=None):
    """A molecule child carrying the release-order labels: `release:<impact>` and `wave:<name>`."""
    labels = []
    if release:
        labels.append(f"release:{release}")
    if wave:
        labels.append(f"wave:{wave}")
    return {"id": bead_id, "labels": labels}


def test_tiers_ordered_fixes_then_waves_then_breaking():
    # Arrange: one of each impact, interleaved so ordering can't come from input position.
    beads = [
        _bead("brk", release="breaking"),
        _bead("feat", release="feature", wave="one"),
        _bead("fix", release="fix"),
    ]

    # Act
    result = release_order.order_beads(beads)

    # Assert: fix flushed first, feature next, breaking last.
    assert result.order == ("fix", "feat", "brk")


def test_fix_churn_budget_caps_the_leading_fix_block():
    # Arrange: four fixes, budget of 2.
    beads = [_bead(f"fix{n}", release="fix") for n in range(1, 5)]

    # Act
    result = release_order.order_beads(beads, fix_churn_budget=2)

    # Assert: first two flush ahead; the overflow is deferred, in input order.
    assert result.fixes == ("fix1", "fix2")
    assert result.deferred_fixes == ("fix3", "fix4")
    assert result.order == ("fix1", "fix2", "fix3", "fix4")


def test_deferred_fixes_land_behind_features_but_before_breaking():
    # Arrange: over-budget fixes plus a feature and a breaking change.
    beads = [
        _bead("fix1", release="fix"),
        _bead("fix2", release="fix"),
        _bead("feat", release="feature", wave="one"),
        _bead("brk", release="breaking"),
    ]

    # Act: budget 1 pushes fix2 into the deferred tier.
    result = release_order.order_beads(beads, fix_churn_budget=1)

    # Assert: fix1 first, feature cohort next, then the deferred fix, breaking last.
    assert result.order == ("fix1", "feat", "fix2", "brk")


def test_features_grouped_one_group_per_wave_in_first_appearance_order():
    # Arrange: features across two waves, interleaved.
    beads = [
        _bead("b1", release="feature", wave="beta"),
        _bead("a1", release="feature", wave="alpha"),
        _bead("b2", release="feature", wave="beta"),
        _bead("a2", release="feature", wave="alpha"),
    ]

    # Act
    result = release_order.order_beads(beads)

    # Assert: one Wave per name, waves in first-appearance order, ids in input order.
    assert [(w.name, w.ids) for w in result.waves] == [
        ("beta", ("b1", "b2")),
        ("alpha", ("a1", "a2")),
    ]
    assert result.order == ("b1", "b2", "a1", "a2")


def test_unwaved_features_collect_under_the_empty_wave():
    # Arrange: a waved and an unwaved feature.
    beads = [
        _bead("w", release="feature", wave="one"),
        _bead("u", release="feature"),
    ]

    # Act
    result = release_order.order_beads(beads)

    # Assert: the unwaved feature forms the empty-string wave.
    assert [(w.name, w.ids) for w in result.waves] == [("one", ("w",)), ("", ("u",))]


def test_unlabeled_beads_are_dropped_from_the_order():
    # Arrange: a release-labeled bead and a bare one.
    beads = [_bead("fix", release="fix"), _bead("bare")]

    # Act
    result = release_order.order_beads(beads)

    # Assert: only the classified bead is ordered; the bare one is recorded, not merged.
    assert result.order == ("fix",)
    assert result.unlabeled == ("bare",)


def test_zero_budget_defers_all_fixes():
    # Arrange: two fixes, budget 0.
    beads = [_bead("fix1", release="fix"), _bead("fix2", release="fix")]

    # Act
    result = release_order.order_beads(beads, fix_churn_budget=0)

    # Assert: nothing flushed ahead; both deferred.
    assert result.fixes == ()
    assert result.deferred_fixes == ("fix1", "fix2")


def test_default_strategy_is_stable_versioning():
    # Arrange / Act
    names = release_order.available_strategies()

    # Assert
    assert names == ["stable-versioning"]


def test_unknown_strategy_raises_valueerror_listing_available():
    # Arrange
    beads = [_bead("fix", release="fix")]

    # Act / Assert: the error names the bad strategy and the available one.
    with pytest.raises(ValueError, match="unknown release strategy 'rolling'"):
        release_order.order_beads(beads, strategy="rolling")
    with pytest.raises(ValueError, match="stable-versioning"):
        release_order.order_beads(beads, strategy="rolling")


def test_empty_bead_set_orders_nothing():
    # Arrange / Act
    result = release_order.order_beads([])

    # Assert
    assert result.order == ()
    assert result.waves == ()
