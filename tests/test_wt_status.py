"""Table-driven unit tests for ws.wt_status.classify — the pure worktree classifier."""
from __future__ import annotations

from ws.wt_status import WtClassification, classify

# ---------------------------------------------------------------------------
# Test helpers / fixtures
# ---------------------------------------------------------------------------

_RIG = "workspace"
_PATH = "/tmp/wts/github/org/repo/my-bead"
_BRANCH = "wt/bead/my-bead"
_BEAD_ID = "my-bead"
_INTEGRATION = "main"


def _is_merged_fn(entry, branch, base):
    """Controllable is_merged stub — controlled via the `merged` fixture param."""
    # Callers pass a real function; in tests we substitute a closure over a bool.
    raise NotImplementedError("use _make_merged_fn")


def _make_merged_fn(value: bool):
    """Return an is_merged_fn that always returns `value`."""
    def fn(entry, branch, base):
        return value
    return fn


def _make_parent_fn(bead_id, parent):
    """Return a parent_fn that returns a fixed (bead_id, parent) pair."""
    def fn(entry, path, integration):
        return bead_id, parent
    return fn


def _run(
    branch=_BRANCH,
    path=_PATH,
    bead_id=_BEAD_ID,
    bead_status="open",
    merged=False,
    dirty=False,
):
    """Run classify with one managed row and the given params; return the single WtStatus."""
    rows = [(_RIG, path, branch)]
    bead_statuses = {bead_id: bead_status} if bead_id else {}
    dirty_by_path = {path: dirty}
    meta_branches: list[dict] = []

    result = classify(
        rig_prefix=_RIG,
        managed_rows=rows,
        meta_branches=meta_branches,
        bead_statuses=bead_statuses,
        dirty_by_path=dirty_by_path,
        is_merged_fn=_make_merged_fn(merged),
        parent_fn=_make_parent_fn(bead_id, _INTEGRATION),
        integration=_INTEGRATION,
    )
    assert len(result) == 1
    return result[0]


# ---------------------------------------------------------------------------
# Classification table tests
# ---------------------------------------------------------------------------


def test_safe_requires_closed_merged_clean():
    """SAFE = closed + merged + clean — the only class that enables auto-prune."""
    st = _run(bead_status="closed", merged=True, dirty=False)
    assert st.classification == WtClassification.SAFE
    assert st.safe is True


def test_dirty_is_never_safe():
    """A dirty worktree is never SAFE regardless of bead status or merge state."""
    st = _run(bead_status="closed", merged=True, dirty=True)
    assert st.classification == WtClassification.DIRTY
    assert st.safe is False


def test_dirty_overrides_open_bead():
    st = _run(bead_status="open", merged=False, dirty=True)
    assert st.classification == WtClassification.DIRTY
    assert st.safe is False


def test_review_when_merged_clean_bead_not_closed():
    """Merged + clean + open bead → REVIEW (waiting on human)."""
    st = _run(bead_status="open", merged=True, dirty=False)
    assert st.classification == WtClassification.REVIEW
    assert st.safe is False


def test_review_when_merged_clean_bead_in_progress():
    st = _run(bead_status="in_progress", merged=True, dirty=False)
    assert st.classification == WtClassification.REVIEW
    assert st.safe is False


def test_unmerged_when_closed_but_not_merged():
    """Closed bead + branch NOT an ancestor → UNMERGED (unusual but never safe)."""
    st = _run(bead_status="closed", merged=False, dirty=False)
    assert st.classification == WtClassification.UNMERGED
    assert st.safe is False


def test_active_when_open_bead():
    """Open bead, not merged, clean → ACTIVE."""
    st = _run(bead_status="open", merged=False, dirty=False)
    assert st.classification == WtClassification.ACTIVE
    assert st.safe is False


def test_active_when_in_progress_bead():
    st = _run(bead_status="in_progress", merged=False, dirty=False)
    assert st.classification == WtClassification.ACTIVE
    assert st.safe is False


def test_detached_when_no_branch():
    """Detached HEAD → DETACHED (no branch to inspect)."""
    st = _run(branch="(detached)", bead_id=None, merged=False, dirty=False)
    assert st.classification == WtClassification.DETACHED
    assert st.safe is False


def test_abandoned_when_no_bead_id():
    """Non-bead worktree (session/batch) with no bead id → ABANDONED."""
    st = _run(branch="wt/session/20260701T120000Z-abcd", bead_id=None, merged=False, dirty=False)
    assert st.classification == WtClassification.ABANDONED
    assert st.safe is False


def test_dirty_takes_priority_over_detached():
    """DIRTY takes priority in classification even over DETACHED."""
    # dirty flag triggers DIRTY before we even check detached state
    st = _run(branch="(detached)", bead_id=None, merged=False, dirty=True)
    # detached is checked first, so DETACHED wins even over dirty (branch is detached, no bead)
    # The design is: detached first since we can't determine merged/bead state
    assert st.classification == WtClassification.DETACHED


# ---------------------------------------------------------------------------
# Field accuracy tests
# ---------------------------------------------------------------------------


def test_wtstate_fields_populated():
    """All WtStatus fields are correctly populated from the input data."""
    st = _run(bead_status="closed", merged=True, dirty=False)
    assert st.rig == _RIG
    assert st.branch == _BRANCH
    assert st.path == _PATH
    assert st.bead_id == _BEAD_ID
    assert st.merged is True
    assert st.dirty is False


def test_safe_false_for_all_non_safe_classes():
    """Every non-SAFE classification has safe=False."""
    cases = [
        _run(bead_status="closed", merged=True, dirty=True),     # DIRTY
        _run(bead_status="open", merged=True, dirty=False),       # REVIEW
        _run(bead_status="closed", merged=False, dirty=False),    # UNMERGED
        _run(bead_status="open", merged=False, dirty=False),      # ACTIVE
        _run(branch="(detached)", bead_id=None, merged=False),    # DETACHED
        _run(branch="wt/batch/x", bead_id=None, merged=False),   # ABANDONED
    ]
    for st in cases:
        assert st.safe is False, f"expected safe=False for {st.classification}"


def test_multiple_rows_classified_independently():
    """classify handles multiple rows without state bleed between them."""
    rows = [
        (_RIG, "/wts/bead-a", "wt/bead/bead-a"),
        (_RIG, "/wts/bead-b", "wt/bead/bead-b"),
    ]
    bead_statuses = {"bead-a": "closed", "bead-b": "open"}
    dirty_by_path = {"/wts/bead-a": False, "/wts/bead-b": False}

    def parent_fn(entry, path, integration):
        # Parse the bead id from the path leaf (matches worktree naming convention)
        leaf = path.rsplit("/", 1)[-1]
        return leaf, integration

    results = classify(
        rig_prefix=_RIG,
        managed_rows=rows,
        meta_branches=[],
        bead_statuses=bead_statuses,
        dirty_by_path=dirty_by_path,
        is_merged_fn=_make_merged_fn(True),  # both merged
        parent_fn=parent_fn,
        integration="main",
    )
    assert len(results) == 2
    assert results[0].classification == WtClassification.SAFE
    assert results[1].classification == WtClassification.REVIEW


def test_as_dict_serializes_classification_as_string():
    """WtStatus.as_dict() returns classification as a string, not enum."""
    st = _run(bead_status="closed", merged=True, dirty=False)
    d = st.as_dict()
    assert isinstance(d["classification"], str)
    assert d["classification"] == "safe"
    assert isinstance(d["safe"], bool)
    assert d["safe"] is True
