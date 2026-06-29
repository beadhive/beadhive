"""Unit self-checks for the show/refine pure functions (no git, no bd).

Covers the noise-flag heuristic, squash-plan validation, mode-b auto-message, and rebase-todo
generation — the logic that decides what `ws work refine` will do before any git write."""

from __future__ import annotations

from ws import work


def _row(short, subject, files=(), date="2026-06-01T10:00:00+00:00"):
    """A synthetic commit row (full sha = short padded; only fields the pure fns read matter)."""
    return {
        "sha": short * 8,
        "short": short,
        "parents": [],
        "author": "a",
        "email": "a@x",
        "date": date,
        "subject": subject,
        "files": list(files),
        "sig": "N",
        "signer": "",
    }


# ---- noise flags ------------------------------------------------------------


def test_flag_marker_detected():
    rows = work.flag_rows([_row("aaa", "fixup! feat: x", ["f.py"])])
    assert rows[0]["flags"]["marker"] is True


def test_flag_fixup_subset_points_at_nearest_earlier():
    rows = work.flag_rows(
        [
            _row("aaa", "feat: big", ["a.py", "b.py"]),
            _row("bbb", "feat: other", ["c.py"]),
            _row("ccc", "wip: tweak a", ["a.py"]),  # subset of aaa's files
        ]
    )
    assert rows[2]["flags"]["fixup"] == "aaa"
    assert rows[1]["flags"]["fixup"] is None  # c.py not a subset of any earlier row


def test_flag_run_adjacent_same_type_scope():
    rows = work.flag_rows(
        [
            _row("aaa", "feat(api): one", ["a.py"]),
            _row("bbb", "feat(api): two", ["b.py"]),
            _row("ccc", "fix(api): three", ["c.py"]),
        ]
    )
    assert rows[1]["flags"]["run"] is True  # feat(api) == feat(api)
    assert rows[2]["flags"]["run"] is False  # fix(api) != feat(api)


# ---- plan validation --------------------------------------------------------


def test_validate_plan_accepts_one_group():
    rows = [_row("aaa", "feat: x"), _row("bbb", "wip")]
    ok, errors, groups = work.validate_plan(
        {"groups": [{"keep": "aaa", "fold": ["bbb"]}]}, rows
    )
    assert ok and not errors
    assert groups[0]["keep"] == "aaa" * 8 and groups[0]["fold"] == ["bbb" * 8]


def test_validate_plan_accepts_n_groups():
    rows = [_row("aaa", "f"), _row("bbb", "g"), _row("ccc", "h"), _row("ddd", "i")]
    ok, errors, _ = work.validate_plan(
        {"groups": [{"keep": "aaa", "fold": ["bbb"]}, {"keep": "ccc", "fold": ["ddd"]}]}, rows
    )
    assert ok and not errors


def test_validate_plan_rejects_unknown_hash():
    rows = [_row("aaa", "x")]
    ok, errors, _ = work.validate_plan({"groups": [{"keep": "zzz", "fold": []}]}, rows)
    assert not ok and any("zzz" in e for e in errors)


def test_validate_plan_rejects_commit_in_two_groups():
    rows = [_row("aaa", "x"), _row("bbb", "y")]
    ok, errors, _ = work.validate_plan(
        {"groups": [{"keep": "aaa", "fold": ["bbb"]}, {"keep": "bbb", "fold": []}]}, rows
    )
    assert not ok and any("more than one group" in e for e in errors)


def test_validate_plan_rejects_keep_in_own_fold():
    rows = [_row("aaa", "x")]
    ok, errors, _ = work.validate_plan({"groups": [{"keep": "aaa", "fold": ["aaa"]}]}, rows)
    assert not ok and any("own fold" in e for e in errors)


# ---- auto-message (mode b) --------------------------------------------------


def test_auto_message_subject_is_keep_body_is_bullets():
    keep = _row("aaa", "feat: thing")
    folds = [_row("bbb", "fixup! wip a"), _row("ccc", "tweak b")]
    subject, body = work.auto_message(keep, folds)
    assert subject == "feat: thing"
    assert body == "- wip a\n- tweak b"  # fixup! prefix stripped


# ---- todo generation --------------------------------------------------------


def test_build_todo_reorders_fold_under_keep_with_amend():
    rows = [_row("aaa", "feat: x"), _row("bbb", "mid"), _row("ccc", "wip on x", ["a.py"])]
    # fold the last (non-contiguous) commit into the first
    _, _, groups = work.validate_plan({"groups": [{"keep": "aaa", "fold": ["ccc"]}]}, rows)
    todo = work.build_todo(rows, groups)
    assert todo[0] == f"pick {'aaa' * 8}"
    assert todo[1] == f"fixup {'ccc' * 8}"
    assert todo[2].startswith("exec git commit --amend")  # body bullets => amend
    assert todo[3] == f"pick {'bbb' * 8}"  # passthrough preserved in place


def test_build_todo_passthrough_only_when_no_groups():
    rows = [_row("aaa", "feat: x"), _row("bbb", "fix: y")]
    todo = work.build_todo(rows, [])
    assert todo == [f"pick {'aaa' * 8}", f"pick {'bbb' * 8}"]


def test_build_todo_date_last_sets_date():
    rows = [
        _row("aaa", "feat: x", date="2026-06-01T10:00:00+00:00"),
        _row("bbb", "wip", date="2026-06-05T10:00:00+00:00"),
    ]
    groups = [
        {"keep": "aaa" * 8, "fold": ["bbb" * 8], "subject": None, "body": None, "date": "last"}
    ]
    todo = work.build_todo(rows, groups)
    assert any("--date=2026-06-05T10:00:00+00:00" in ln for ln in todo)


def test_plan_from_since_folds_rest_into_first():
    rows = [_row("aaa", "x"), _row("bbb", "y"), _row("ccc", "z")]
    plan = work.plan_from_since(rows)
    assert plan == {"groups": [{"keep": "aaa" * 8, "fold": ["bbb" * 8, "ccc" * 8]}]}
