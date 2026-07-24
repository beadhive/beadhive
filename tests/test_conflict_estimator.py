"""`beadhive.conflict_estimator` self-checks — the advisory start-verdict conflict floor.

Pure unit tests over in-memory bead dicts shaped like `bd list --json` (id + `path:<p>` labels).
Two acceptance shapes: the bundled `file-overlap` floor (HIGH on overlapping expected paths, LOW on
disjoint) and the registry/protocol indirection (a stub estimator swapped in by name, proving the
`release.conflict_estimator` seam works without a real plugin). AAA structure.
"""

from __future__ import annotations

import pytest

from beadhive import conflict_estimator, release_order
from beadhive.conflict_estimator import ConflictEstimate


def _bead(bead_id, *paths):
    """A molecule child declaring its expected files as `path:<p>` labels."""
    return {"id": bead_id, "labels": [f"path:{p}" for p in paths]}


@pytest.fixture
def clean_registry():
    """Snapshot the estimator registry and restore it, so a test that registers a stub estimator
    doesn't leak the name into other tests."""
    snapshot = dict(conflict_estimator._ESTIMATORS)
    yield conflict_estimator
    conflict_estimator._ESTIMATORS.clear()
    conflict_estimator._ESTIMATORS.update(snapshot)


def test_file_overlap_reports_high_likelihood_for_overlapping_paths():
    # Arrange: this bead touches a file a bead queued ahead also touches.
    bead = _bead("me", "src/x.py", "src/z.py")
    queue_ahead = [_bead("ahead", "src/x.py")]
    estimator = conflict_estimator.get_estimator("file-overlap")

    # Act
    verdict = estimator.estimate(bead, queue_ahead)

    # Assert: overlap => HIGH likelihood, reason names the colliding bead and the shared file.
    assert verdict.likelihood == conflict_estimator.HIGH_LIKELIHOOD
    assert verdict.likelihood > 0.5
    assert "ahead (src/x.py)" in verdict.reason


def test_file_overlap_reports_low_likelihood_for_disjoint_paths():
    # Arrange: this bead's files don't intersect any bead queued ahead.
    bead = _bead("me", "src/q.py")
    queue_ahead = [_bead("ahead", "src/x.py"), _bead("other", "src/y.py")]
    estimator = conflict_estimator.get_estimator("file-overlap")

    # Act
    verdict = estimator.estimate(bead, queue_ahead)

    # Assert: disjoint => LOW likelihood.
    assert verdict.likelihood == conflict_estimator.LOW_LIKELIHOOD
    assert verdict.likelihood < 0.5


def test_file_overlap_low_when_bead_declares_no_paths():
    # Arrange: an unlabeled bead has nothing to overlap on.
    bead = {"id": "me", "labels": []}
    queue_ahead = [_bead("ahead", "src/x.py")]
    estimator = conflict_estimator.get_estimator("file-overlap")

    # Act
    verdict = estimator.estimate(bead, queue_ahead)

    # Assert
    assert verdict.likelihood == conflict_estimator.LOW_LIKELIHOOD


def test_stub_estimator_swapped_via_name_is_honored(clean_registry):
    # Arrange: a stub estimator with a sentinel verdict, registered under a config-style name and
    # nothing to do with file-overlap — proving selection is by name through the registry.
    class StubEstimator:
        def estimate(self, bead, queue_ahead):
            return ConflictEstimate(0.42, f"stub saw {bead['id']} vs {len(queue_ahead)} ahead")

    clean_registry.register_estimator("stub-structural", StubEstimator())

    # Act: resolve by name — the same path a real config `release.conflict_estimator` drives.
    verdict = clean_registry.get_estimator("stub-structural").estimate(_bead("me"), [_bead("a")])

    # Assert: the stub's sentinel came back, not file-overlap's floor.
    assert verdict == ConflictEstimate(0.42, "stub saw me vs 1 ahead")
    assert "stub-structural" in clean_registry.available_estimators()


def test_start_verdict_seam_resolves_estimator_by_name(clean_registry):
    # Arrange: release_order.start_verdict is the wired-in seam; register a stub and select it.
    class StubEstimator:
        def estimate(self, bead, queue_ahead):
            return ConflictEstimate(1.0, "stub")

    clean_registry.register_estimator("stub", StubEstimator())
    bead = _bead("me", "src/x.py")
    queue_ahead = [_bead("ahead", "src/x.py")]

    # Act: default resolves the file-overlap floor; the named stub overrides it.
    default_verdict = release_order.start_verdict(bead, queue_ahead)
    stub_verdict = release_order.start_verdict(bead, queue_ahead, estimator="stub")

    # Assert: the seam honors both the default and the swapped name.
    assert default_verdict.likelihood == conflict_estimator.HIGH_LIKELIHOOD
    assert stub_verdict == ConflictEstimate(1.0, "stub")


def test_unknown_estimator_raises_valueerror_listing_available():
    # Arrange / Act / Assert: an unregistered name errors, naming the bad name and the bundled one.
    with pytest.raises(ValueError, match="unknown conflict estimator 'structural'"):
        conflict_estimator.get_estimator("structural")
    with pytest.raises(ValueError, match="file-overlap"):
        conflict_estimator.get_estimator("structural")


def test_default_estimator_matches_config_default():
    # Arrange: the config default (bh-k2j8.1) must resolve through the registry — proving the
    # config key and the registry agree on the bundled name.
    from beadhive import config

    # Act
    configured = config.release_conflict_estimator({}, {})

    # Assert: config default name is registered and is the module default.
    assert configured == conflict_estimator.DEFAULT_ESTIMATOR
    assert configured in conflict_estimator.available_estimators()
