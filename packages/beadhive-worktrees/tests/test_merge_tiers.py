"""Pure fragments of the integration-boundary merge tiers (bh-qdezo.8).

New package-local coverage: these pure functions were previously exercised only indirectly,
through ``tests/test_worktree.py``'s real-git ``try_merge_rebase`` integration tests — there
was no prior root unit test isolating just the parsing/eligibility/rendering/zero-delta logic,
so nothing here replaces a deleted root test (see bh-qdezo.8's NOTES for the accounting).
"""

from __future__ import annotations

from beadhive_worktrees import (
    all_union_eligible,
    is_zero_delta_rebase,
    parse_conflict_paths,
    union_attributes_text,
)

# ---- parse_conflict_paths ----------------------------------------------------


def test_parse_conflict_paths_splits_nonblank_lines() -> None:
    assert parse_conflict_paths("a.txt\nb/c.txt\n") == ["a.txt", "b/c.txt"]


def test_parse_conflict_paths_drops_blank_lines() -> None:
    assert parse_conflict_paths("a.txt\n\n   \nb.txt\n") == ["a.txt", "b.txt"]


def test_parse_conflict_paths_empty_or_none_yields_empty_list() -> None:
    assert parse_conflict_paths("") == []
    assert parse_conflict_paths(None) == []  # type: ignore[arg-type]


# ---- all_union_eligible ------------------------------------------------------


def test_all_union_eligible_true_when_every_path_matches_a_glob() -> None:
    assert all_union_eligible(["notes.txt", "CHANGELOG.md"], ["*.txt", "*.md"]) is True


def test_all_union_eligible_false_when_any_path_is_unwhitelisted() -> None:
    assert all_union_eligible(["notes.txt", "src/app.py"], ["*.txt"]) is False


def test_all_union_eligible_false_on_empty_paths() -> None:
    """Nothing for the union driver to resolve — never eligible even with a wide-open glob."""
    assert all_union_eligible([], ["*"]) is False


def test_all_union_eligible_false_when_globs_are_empty() -> None:
    assert all_union_eligible(["notes.txt"], []) is False


# ---- union_attributes_text -------------------------------------------------


def test_union_attributes_text_one_line_per_glob_with_trailing_newline() -> None:
    assert union_attributes_text(["*.txt", "CHANGELOG*"]) == (
        "*.txt merge=union\nCHANGELOG* merge=union\n"
    )


def test_union_attributes_text_empty_globs_is_just_a_newline() -> None:
    assert union_attributes_text([]) == "\n"


# ---- is_zero_delta_rebase -----------------------------------------------------


def test_is_zero_delta_rebase_true_when_no_commits_remain() -> None:
    assert is_zero_delta_rebase([]) is True


def test_is_zero_delta_rebase_false_when_commits_remain() -> None:
    assert is_zero_delta_rebase(["deadbeef"]) is False
