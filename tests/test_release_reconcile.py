"""Submit-time release-hint reconcile (bh-k2j8.5).

Pure unit tests for the non-blocking cross-check `bh work submit` runs: a bead's planner-declared
`release:` hint (breaking|feature|fix) vs what its branch actually landed. A `release:feature` /
`release:fix` bead that ships a breaking commit (`feat!` / `BREAKING CHANGE:`) warns; nothing else
does. The check is advisory — these tests assert the warning STRING is produced, never that submit
fails (the caller emits it as a warning).
"""

from __future__ import annotations

from beadhive import work_logic

# ---- release_hint: read the release:<value> label ---------------------------


def test_release_hint_reads_release_label():
    # Arrange
    data = {"labels": ["provider:github", "release:feature", "component:runtime"]}
    # Act
    hint = work_logic.release_hint(data)
    # Assert
    assert hint == "feature"


def test_release_hint_empty_when_no_release_label():
    # Arrange
    data = {"labels": ["provider:github", "component:runtime"]}
    # Act / Assert
    assert work_logic.release_hint(data) == ""


def test_release_hint_tolerates_missing_labels():
    # Arrange / Act / Assert — no labels key, and None, both yield ''.
    assert work_logic.release_hint({}) == ""
    assert work_logic.release_hint(None) == ""


# ---- commit_is_breaking: subject bang + BREAKING CHANGE: footer --------------


def test_commit_is_breaking_on_subject_bang():
    # Arrange / Act / Assert
    assert work_logic.commit_is_breaking("feat!: drop the old api")
    assert work_logic.commit_is_breaking("fix(api)!: remove deprecated field")


def test_commit_is_breaking_on_footer():
    # Arrange
    message = "feat(api): add v2 endpoint\n\nBREAKING CHANGE: v1 is removed\n"
    # Act / Assert
    assert work_logic.commit_is_breaking(message)


def test_commit_is_not_breaking_for_plain_feat():
    # Arrange / Act / Assert — an additive feat with no bang and no footer is not breaking.
    assert not work_logic.commit_is_breaking("feat(api): add a v2 endpoint")
    assert not work_logic.commit_is_breaking("")


# ---- reconcile_release_hint: warn only on hint-vs-reality drift --------------


def test_reconcile_warns_feature_hint_lands_breaking_bang():
    # Arrange
    messages = ["fix(x): tidy", "feat!: rip out the old path"]
    # Act
    warn = work_logic.reconcile_release_hint("feature", messages)
    # Assert
    assert warn is not None
    assert "release:feature" in warn and "breaking" in warn


def test_reconcile_warns_fix_hint_lands_breaking_footer():
    # Arrange
    messages = ["fix(db): reindex\n\nBREAKING CHANGE: schema changed\n"]
    # Act
    warn = work_logic.reconcile_release_hint("fix", messages)
    # Assert
    assert warn is not None and "release:fix" in warn


def test_reconcile_silent_when_feature_hint_matches_reality():
    # Arrange — a feature hint that lands only additive commits does not warn.
    messages = ["feat(api): add endpoint", "test: cover it"]
    # Act / Assert
    assert work_logic.reconcile_release_hint("feature", messages) is None


def test_reconcile_silent_for_breaking_hint():
    # Arrange — a breaking hint already matches a breaking commit; never warns.
    messages = ["feat!: intended breaking change"]
    # Act / Assert
    assert work_logic.reconcile_release_hint("breaking", messages) is None


def test_reconcile_silent_for_absent_hint():
    # Arrange — an unlabeled bead (no release: hint) opts out of the reconcile entirely.
    messages = ["feat!: breaking, but no hint to reconcile against"]
    # Act / Assert
    assert work_logic.reconcile_release_hint("", messages) is None
