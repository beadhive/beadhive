"""Pure status-tag string building (bh-qdezo.7).

No root test exercised ``impl__status_tags``'s tag combinations directly before this move — only
indirectly, through the ``_render_status`` rendering tests in ``tests/test_worktree.py``, which
stay in root (they prove the tree-rendering wiring, not this string builder). This file is a
net-new direct test of the moved logic.
"""

from __future__ import annotations

from types import SimpleNamespace

from beadhive_worktrees import ordered_statuses, status_tags


def _st(**overrides):
    base = dict(
        merged=False,
        dirty=False,
        underlying=None,
        safe=False,
        precious=(),
        legacy_root=False,
        disposition_reason="",
        citing_bead="",
        binding_gaps=(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_status_tags_combines_every_flag_in_the_established_order() -> None:
    item = SimpleNamespace(path=".env", bytes=8)
    st = _st(
        merged=True,
        dirty=True,
        underlying="safe",
        safe=False,
        precious=(item,),
        legacy_root=True,
        disposition_reason="pivot",
        citing_bead="bh-9",
        binding_gaps=("herdr:stale",),
    )

    assert status_tags(st) == (
        "  merged  dirty  (under: SAFE)  precious=.env(8B)  legacy-root  reason=pivot"
        "  citing=bh-9  binding-gap=herdr:stale"
    )


def test_status_tags_shows_safe_instead_of_precious_when_safe() -> None:
    st = _st(safe=True, precious=(SimpleNamespace(path=".env", bytes=8),))

    assert status_tags(st) == "  SAFE"


def test_status_tags_is_empty_for_a_plain_row() -> None:
    assert status_tags(_st()) == ""


def test_ordered_statuses_flattens_in_managed_repository_order() -> None:
    entries = [{"prefix": "a"}, {"prefix": "b"}]
    statuses_by_prefix = {"b": ["b1"], "a": ["a1", "a2"]}

    assert ordered_statuses(entries, statuses_by_prefix) == ["a1", "a2", "b1"]


def test_ordered_statuses_skips_entries_with_no_completed_classification() -> None:
    entries = [{"prefix": "a"}, {"prefix": "missing"}]
    statuses_by_prefix = {"a": ["a1"]}

    assert ordered_statuses(entries, statuses_by_prefix) == ["a1"]
