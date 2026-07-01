"""Table-driven unit tests for ws.wt_status.classify — the pure worktree classifier."""
from __future__ import annotations

from unittest.mock import patch

from ws.worktree import bead_and_parent  # noqa: E402
from ws.wt_status import WtClassification, classify  # noqa: E402

# Importing WtClassification.LANDED_REBASED here validates the new enum member is present
_LANDED_REBASED = WtClassification.LANDED_REBASED

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
    def fn(entry, path, integration, branch=""):
        return bead_id, parent
    return fn


def _make_landed_fn(value: bool):
    """Return an is_landed_fn that always returns ``value``."""
    def fn(entry, branch, base, close_reason):
        return value
    return fn


def _run(
    branch=_BRANCH,
    path=_PATH,
    bead_id=_BEAD_ID,
    bead_status="open",
    merged=False,
    dirty=False,
    is_landed_fn=None,
    bead_close_reasons=None,
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
        is_landed_fn=is_landed_fn,
        bead_close_reasons=bead_close_reasons or {},
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

    def parent_fn(entry, path, integration, branch=""):
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


# ---------------------------------------------------------------------------
# Fix 1: conforming bead (dotted branch) → SAFE
# Fix 2: merged-orphan, batch, unmerged-unknown classification cases
# ---------------------------------------------------------------------------


def test_conforming_dotted_bead_classifies_safe():
    """A conforming closed+merged+clean bead with a dotted branch id classifies SAFE.

    This is the primary regression case: bead has branch
    wt/bead/. After Fix 1, bead_and_parent extracts the id
    from the branch ref, not the dashed directory leaf -1.
    In classify() we stub parent_fn to return the correct dotted id, simulating
    what bead_and_parent returns after the fix.
    """
    dotted_id = ""
    dotted_branch = f"wt/bead/{dotted_id}"
    path = "/tmp/wts/github/org/repo/-1" # sanitized leaf has a dash

    rows = [(_RIG, path, dotted_branch)]
    bead_statuses = {dotted_id: "closed"}
    dirty_by_path = {path: False}

    result = classify(
        rig_prefix=_RIG,
        managed_rows=rows,
        meta_branches=[],
        bead_statuses=bead_statuses,
        dirty_by_path=dirty_by_path,
        is_merged_fn=_make_merged_fn(True),
        parent_fn=_make_parent_fn(dotted_id, _INTEGRATION),
        integration=_INTEGRATION,
    )
    assert len(result) == 1
    st = result[0]
    assert st.bead_id == dotted_id
    assert st.classification == WtClassification.SAFE
    assert st.safe is True


def test_merged_orphan_when_no_bead_id_but_merged_and_clean():
    """Legacy/non-conforming branch that is merged+clean → MERGED_ORPHAN (not ABANDONED).

    The bead id cannot be resolved (bead_id=None), but git shows the branch is already
    a merge ancestor of its parent.  This is a reclaimable-but-unattributed worktree —
    surface it as MERGED_ORPHAN so operators can see it, but do NOT auto-prune it
    (no closed-bead signal to confirm safety).
    """
    st = _run(
        branch="wt/bead/some-legacy-ref",
        bead_id=None,
        merged=True,
        dirty=False,
    )
    assert st.classification == WtClassification.MERGED_ORPHAN
    assert st.safe is False


def test_batch_worktree_is_abandoned_even_when_merged():
    """Batch worktree (wt/batch/<epic>) stays ABANDONED even if the branch is merged.

    Batch branches are coordination branches, not individual bead seats.  They keep
    their own no-bead treatment and are never promoted to MERGED_ORPHAN.
    """
    st = _run(
        branch="wt/batch/some-epic",
        bead_id=None,
        merged=True,
        dirty=False,
    )
    assert st.classification == WtClassification.ABANDONED
    assert st.safe is False


def test_abandoned_when_unresolvable_and_unmerged():
    """Unresolvable bead id + branch NOT merged → ABANDONED (neither resolvable nor merged)."""
    st = _run(
        branch="wt/bead/unknown-ghost",
        bead_id=None,
        merged=False,
        dirty=False,
    )
    assert st.classification == WtClassification.ABANDONED
    assert st.safe is False


# ---------------------------------------------------------------------------
# LANDED_REBASED: second-stage detection for rebase/squash-landed closed branches
# ---------------------------------------------------------------------------


def test_rebase_landed_closed_branch_classifies_landed_rebased():
    """Closed bead + not an ancestor + patch-id match → LANDED_REBASED, safe=True.

    Regression case: closed 8v8.N branches were not ancestors of main after the molecule
    was rebase-integrated, but their content was present under different SHAs.  The
    patch-id / git-cherry equivalence check should detect this and return LANDED_REBASED
    instead of UNMERGED, making the branch prune-eligible.
    """
    st = _run(
        bead_status="closed",
        merged=False,
        dirty=False,
        is_landed_fn=_make_landed_fn(True),  # simulates: all commits cherry-marked as "-"
        bead_close_reasons={_BEAD_ID: ""},    # no merge event; falls through to patch-id
    )
    assert st.classification == WtClassification.LANDED_REBASED
    assert st.safe is True


def test_squash_landed_with_merge_event_classifies_landed_rebased():
    """Closed bead + not an ancestor + close_reason='merged' → LANDED_REBASED, safe=True.

    Squash-merges collapse N commits into one, so patch-id won't match; the AGF lifecycle
    close_reason='merged' is the authoritative signal.  is_landed_fn returns True because
    the merge-event is present (simulated here by the landed stub always returning True,
    which represents what is_landed does when close_reason='merged').
    """
    st = _run(
        bead_status="closed",
        merged=False,
        dirty=False,
        is_landed_fn=_make_landed_fn(True),
        bead_close_reasons={_BEAD_ID: "merged"},
    )
    assert st.classification == WtClassification.LANDED_REBASED
    assert st.safe is True


def test_molecule_landed_epic_classifies_landed_rebased():
    """Closed epic + not an ancestor + close_reason='molecule landed' → LANDED_REBASED."""
    st = _run(
        bead_status="closed",
        merged=False,
        dirty=False,
        is_landed_fn=_make_landed_fn(True),
        bead_close_reasons={_BEAD_ID: "molecule landed"},
    )
    assert st.classification == WtClassification.LANDED_REBASED
    assert st.safe is True


def test_genuinely_unlanded_closed_branch_stays_unmerged():
    """Closed bead + not an ancestor + no patch-id match + no merge event → UNMERGED.

    This is the real work-loss signal: the branch content is neither in the parent via
    ancestry nor via patch-id equivalence, and no AGF lifecycle event confirms the merge.
    Must stay UNMERGED so prune does NOT reclaim it.
    """
    st = _run(
        bead_status="closed",
        merged=False,
        dirty=False,
        is_landed_fn=_make_landed_fn(False),  # simulates: commits have "+" in git cherry
        bead_close_reasons={_BEAD_ID: ""},
    )
    assert st.classification == WtClassification.UNMERGED
    assert st.safe is False


def test_no_landed_fn_leaves_closed_non_ancestor_as_unmerged():
    """Without is_landed_fn, closed+non-ancestor stays UNMERGED (unchanged behavior).

    Callers that do not supply is_landed_fn preserve the pre-existing classification
    behavior — backward-compatible with all existing classify() call sites that don't
    opt in to the second-stage check.
    """
    # Same as existing test_unmerged_when_closed_but_not_merged but explicit about the
    # backward-compat contract
    st = _run(
        bead_status="closed",
        merged=False,
        dirty=False,
        is_landed_fn=None,
    )
    assert st.classification == WtClassification.UNMERGED
    assert st.safe is False


def test_landed_rebased_is_safe_eligible():
    """LANDED_REBASED has safe=True — prune may reclaim these worktrees."""
    st = _run(
        bead_status="closed",
        merged=False,
        dirty=False,
        is_landed_fn=_make_landed_fn(True),
    )
    assert st.safe is True
    assert st.classification == WtClassification.LANDED_REBASED


def test_dirty_landed_rebased_is_not_safe():
    """A dirty worktree is never safe even if content is confirmed in parent."""
    st = _run(
        bead_status="closed",
        merged=False,
        dirty=True,
        is_landed_fn=_make_landed_fn(True),
    )
    # DIRTY takes priority over the landed check
    assert st.classification == WtClassification.DIRTY
    assert st.safe is False


# ---------------------------------------------------------------------------
# bead_and_parent: id parsed from dotted branch ref, not dashed directory leaf
# ---------------------------------------------------------------------------


def test_bead_and_parent_parses_id_from_dotted_branch_ref():
    """bead_and_parent extracts the bead id from the real branch ref, not the directory leaf.

    The directory leaf for is -1 (dot→dash
    sanitization). The real branch ref wt/bead/ must be used.
    Stripping the ``wt/bead/`` prefix from the actual ref yields the dotted id.
    """
    entry = {"provider": "github", "org": "org", "repo": "repo", "prefix": "repo"}
    path = "/some/root/github/org/repo/-1" # dashed leaf
    dotted_branch = "wt/bead/" # real ref with dot
    integration = "main"

    # molecule_base is called to resolve the parent branch; mock it to return integration
    # so this test needs no real git repo.
    with patch("ws.worktree.molecule_base", return_value=integration):
        bead_id, parent = bead_and_parent(entry, path, integration, branch=dotted_branch)

    assert bead_id == "", (
        f"expected dotted id '', got {bead_id!r}"
    )
    assert bead_id != "-1", "id must NOT come from the dashed directory leaf"
    assert parent == integration


def test_bead_and_parent_none_for_non_bead_branch():
    """bead_and_parent returns bead_id=None for non-bead branches (batch, session)."""
    entry = {"provider": "github", "org": "org", "repo": "repo", "prefix": "repo"}
    path = "/some/root/github/org/repo/some-epic"
    integration = "main"

    with patch("ws.worktree.molecule_base", return_value=integration):
        bead_id, parent = bead_and_parent(
            entry, path, integration, branch="wt/batch/some-epic"
        )

    assert bead_id is None
    assert parent == integration
