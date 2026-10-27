"""``CheckpointStore``: the contract both implementations meet, and the file store's safety.

The checkpoint is an opaque, local, non-secret resume token keyed per binding. The recording
in-memory store and the hive's file-backed store are held to the same behaviour; the file store
additionally never lets a key leave its directory, writes atomically and reads a damaged file as
"no checkpoint" (a full pass, which the upsert makes idempotent) rather than as an error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from beadhive.adapters.intake import FileCheckpointStore
from beadhive.modules.intake import Checkpoint, CheckpointStore
from beadhive.testing.intake import MemoryCheckpointStore

KEY = "issues:memory:acme/widgets"


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path: Path) -> CheckpointStore:
    if request.param == "memory":
        return MemoryCheckpointStore()
    return FileCheckpointStore(tmp_path / "checkpoints")


def test_an_unknown_key_has_no_checkpoint(store) -> None:
    assert store.get(KEY) is None


def test_put_then_get_round_trips_and_the_latest_put_wins(store) -> None:
    store.put(KEY, Checkpoint("memory:2026-01-02T00:00:00+00:00"))
    store.put(KEY, Checkpoint("memory:2026-01-03T00:00:00+00:00"))

    assert store.get(KEY) == Checkpoint("memory:2026-01-03T00:00:00+00:00")


def test_keys_are_independent(store) -> None:
    store.put(KEY, Checkpoint("a"))
    store.put("comments:memory:acme/widgets", Checkpoint("b"))
    store.put("issues:memory:acme/gadgets", Checkpoint("c"))

    assert [store.get(KEY), store.get("comments:memory:acme/widgets")] == [
        Checkpoint("a"),
        Checkpoint("b"),
    ]
    assert store.get("issues:memory:acme/gadgets") == Checkpoint("c")


def test_it_satisfies_the_port(store) -> None:
    assert isinstance(store, CheckpointStore)


# -- the file store --


def test_the_directory_is_created_on_first_put_and_given_lazily(tmp_path: Path) -> None:
    root = tmp_path / ".bh" / "intake" / "checkpoints"
    calls = []

    def where() -> Path:
        calls.append(1)
        return root

    store = FileCheckpointStore(where)
    assert calls == []  # building the store resolves nothing
    assert store.get(KEY) is None and not root.exists()

    store.put(KEY, Checkpoint("t"))

    assert root.is_dir() and store.get(KEY) == Checkpoint("t")


def test_a_key_can_never_escape_the_directory(tmp_path: Path) -> None:
    root = tmp_path / "checkpoints"
    store = FileCheckpointStore(root)

    store.put("../../escape", Checkpoint("t"))
    store.put("a/b/../../c", Checkpoint("u"))

    assert {p.parent for p in tmp_path.rglob("*.json")} == {root}
    assert store.get("../../escape") == Checkpoint("t")


def test_a_file_holds_only_the_key_and_the_token(tmp_path: Path) -> None:
    store = FileCheckpointStore(tmp_path)
    store.put(KEY, Checkpoint("tok"))

    (path,) = tmp_path.glob("*.json")

    assert json.loads(path.read_text()) == {"key": KEY, "schema": 1, "token": "tok"}


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[]",
        '{"schema": 1}',
        '{"schema": 2, "token": "t"}',
        '{"schema": 1, "token": 7}',
    ],
)
def test_a_damaged_file_reads_as_no_checkpoint_never_as_an_error(
    tmp_path: Path, content: str
) -> None:
    store = FileCheckpointStore(tmp_path)
    store.put(KEY, Checkpoint("tok"))
    (path,) = tmp_path.glob("*.json")
    path.write_text(content)

    assert store.get(KEY) is None

    store.put(KEY, Checkpoint("fresh"))  # and the next put heals it
    assert store.get(KEY) == Checkpoint("fresh")


def test_a_write_that_fails_leaves_the_previous_checkpoint_and_no_debris(
    tmp_path: Path, monkeypatch
) -> None:
    store = FileCheckpointStore(tmp_path)
    store.put(KEY, Checkpoint("old"))

    def boom(src, dst):
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.put(KEY, Checkpoint("new"))
    monkeypatch.undo()

    assert store.get(KEY) == Checkpoint("old")
    assert sorted(p.suffix for p in tmp_path.iterdir()) == [".json"]
