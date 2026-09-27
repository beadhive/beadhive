"""The durable per-worktree binding record in Git per-worktree config (bh-cb4jo)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from beadhive.worktree_bindings import BindingRecord, BindingStore


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def linked(tmp_path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", "-b", "main", cwd=main)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=main,
    )
    path = tmp_path / "wts" / "a-1"
    _git("worktree", "add", "-q", "-b", "wt/bead/issue/a-1", str(path), cwd=main)
    return main, path


def test_intent_then_reference_round_trip(linked) -> None:
    main, path = linked
    store = BindingStore()

    assert store.read(path) == {}
    assert store.record_intent(path, "herdr", "default")
    assert store.read(path) == {"herdr": BindingRecord("herdr", "default", "")}
    assert store.read(path)["herdr"].pending

    assert store.record(path, "herdr", "default", "w4")
    assert store.read(path) == {"herdr": BindingRecord("herdr", "default", "w4")}

    handle = store.handle(main, path, "wt/bead/issue/a-1")
    assert handle.bindings == {"herdr": "w4"}
    assert handle.path == path and handle.main == main


def test_a_new_intent_drops_a_stale_reference(linked) -> None:
    _main, path = linked
    store = BindingStore()
    store.record(path, "herdr", "default", "w4")

    store.record_intent(path, "herdr", "other")

    assert store.read(path) == {"herdr": BindingRecord("herdr", "other", "")}


def test_the_record_is_per_worktree_and_never_leaks_to_the_main_checkout(linked) -> None:
    main, path = linked
    store = BindingStore()
    store.record(path, "herdr", "default", "w4")

    assert store.read(main) == {}
    assert store.handle(main, main).bindings == {}


def test_clear_forgets_the_binding(linked) -> None:
    _main, path = linked
    store = BindingStore()
    store.record(path, "herdr", "default", "w4")

    assert store.clear(path, "herdr")
    assert store.read(path) == {}
    assert store.clear(path, "herdr")  # already absent is success


def test_a_pending_record_gives_the_handle_an_empty_reference_to_re_derive(linked) -> None:
    main, path = linked
    store = BindingStore()
    store.record_intent(path, "herdr", "default")

    assert store.handle(main, path).bindings == {"herdr": ""}


def test_non_checkouts_and_missing_paths_read_as_unbound(tmp_path) -> None:
    store = BindingStore()
    plain = tmp_path / "plain"
    plain.mkdir()

    assert store.read(plain) == {}
    assert store.read(tmp_path / "missing") == {}
    assert store.record(plain, "herdr", "default", "w1") is False
