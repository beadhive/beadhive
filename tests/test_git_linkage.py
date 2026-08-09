"""Unit tests for `beadhive.git_linkage` — the `git.commits` accumulate/idempotent-write
algorithm (bh-1b0rc.2, docs/design/bead-commit-linkage-contract.md).

Patches `ws.bd._run` directly (the seam `bd.run`/`bd.show` both funnel through), same pattern as
`test_bd_json_seam.py` — no real `bd` binary or git repo needed for this module's own contract.
"""

from __future__ import annotations

import json
from collections import namedtuple

import pytest

from beadhive import bd as bd_mod
from beadhive import git_linkage

_CP = namedtuple("CP", "returncode stdout stderr")

_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40


class FakeBdStore:
    """Minimal stand-in for `bd show` / `bd update --set-metadata`, recording every call."""

    def __init__(self):
        self.beads: dict[str, dict] = {}
        self.calls: list[list[str]] = []
        self.update_rc = 0
        self.update_err = ""

    def seed(self, bead_id: str, metadata: dict | None = None) -> None:
        self.beads[bead_id] = {"id": bead_id, "metadata": dict(metadata or {})}

    def __call__(self, cmd, **_kw):
        args = list(cmd)
        assert args[0] == "bd"
        args = args[1:]
        if args[:1] == ["-C"]:
            args = args[2:]
        if args[:1] == ["--actor"]:
            args = args[2:]
        self.calls.append(args)
        sub = args[0] if args else ""
        if sub == "show":
            bead = self.beads.get(args[1])
            return _CP(0 if bead else 1, json.dumps(bead) if bead else "", "")
        if sub == "update" and "--set-metadata" in args:
            if self.update_rc != 0:
                return _CP(self.update_rc, "", self.update_err)
            kv = args[args.index("--set-metadata") + 1]
            key, _, val = kv.partition("=")
            bead = self.beads.setdefault(args[1], {"id": args[1], "metadata": {}})
            bead.setdefault("metadata", {})[key] = val
            return _CP(0, "", "")
        return _CP(1, "", f"unexpected call: {args}")

    def set_metadata_calls(self):
        return [c for c in self.calls if c[0] == "update" and "--set-metadata" in c]


@pytest.fixture
def store(monkeypatch):
    fb = FakeBdStore()
    monkeypatch.setattr(bd_mod, "_run", fb)
    return fb


# ---- read_commits ------------------------------------------------------------------------


def test_read_commits_missing_bead_is_empty(store):
    assert git_linkage.read_commits("nope", "/hive") == []


def test_read_commits_missing_key_is_empty(store):
    store.seed("mr-1")
    assert git_linkage.read_commits("mr-1", "/hive") == []


def test_read_commits_unparseable_value_is_empty(store):
    store.seed("mr-1", metadata={"git.commits": "not json at all"})
    assert git_linkage.read_commits("mr-1", "/hive") == []


def test_read_commits_non_list_value_is_empty(store):
    store.seed("mr-1", metadata={"git.commits": json.dumps({"not": "a list"})})
    assert git_linkage.read_commits("mr-1", "/hive") == []


def test_read_commits_list_of_non_strings_is_empty(store):
    store.seed("mr-1", metadata={"git.commits": json.dumps([1, 2, 3])})
    assert git_linkage.read_commits("mr-1", "/hive") == []


def test_read_commits_valid_array_parses(store):
    store.seed("mr-1", metadata={"git.commits": json.dumps([_SHA_A, _SHA_B])})
    assert git_linkage.read_commits("mr-1", "/hive") == [_SHA_A, _SHA_B]


def test_read_commits_never_reads_the_nested_shape(store):
    """The nested `{"git": {"commits": [...]}}` shape is a DIFFERENT, incompatible
    representation per the contract doc — read_commits must not look at it."""
    store.seed("mr-1", metadata={"git": {"commits": [_SHA_A]}})
    assert git_linkage.read_commits("mr-1", "/hive") == []


# ---- record_commits: first write / accumulate / idempotent --------------------------------


def test_record_commits_first_write(store):
    store.seed("mr-1")

    wrote = git_linkage.record_commits("mr-1", "/hive", [_SHA_A, _SHA_B])

    assert wrote is True
    assert git_linkage.read_commits("mr-1", "/hive") == [_SHA_A, _SHA_B]
    # the flat key, literally "git.commits", as a serialized JSON-string value
    (call,) = store.set_metadata_calls()
    kv = call[call.index("--set-metadata") + 1]
    assert kv.startswith("git.commits=")
    assert json.loads(kv[len("git.commits=") :]) == [_SHA_A, _SHA_B]


def test_record_commits_appends_only_new_preserving_existing_order(store):
    store.seed("mr-1", metadata={"git.commits": json.dumps([_SHA_A])})

    wrote = git_linkage.record_commits("mr-1", "/hive", [_SHA_A, _SHA_B, _SHA_C])

    assert wrote is True
    assert git_linkage.read_commits("mr-1", "/hive") == [_SHA_A, _SHA_B, _SHA_C]


def test_record_commits_noop_skips_the_bd_update_call_entirely(store):
    """Step 3 of the contract's algorithm is load-bearing: when nothing is new, `bd update` must
    never even be called — not just 'called with an unchanged value'."""
    store.seed("mr-1", metadata={"git.commits": json.dumps([_SHA_A, _SHA_B])})

    wrote = git_linkage.record_commits("mr-1", "/hive", [_SHA_A, _SHA_B])

    assert wrote is False
    assert store.set_metadata_calls() == []
    assert git_linkage.read_commits("mr-1", "/hive") == [_SHA_A, _SHA_B]


def test_record_commits_reruns_idempotently_across_multiple_calls(store):
    """Re-running against already-recorded SHAs changes nothing, however many times."""
    store.seed("mr-1")
    git_linkage.record_commits("mr-1", "/hive", [_SHA_A])
    git_linkage.record_commits("mr-1", "/hive", [_SHA_A])
    git_linkage.record_commits("mr-1", "/hive", [_SHA_A])

    assert git_linkage.read_commits("mr-1", "/hive") == [_SHA_A]
    assert len(store.set_metadata_calls()) == 1  # only the first call actually wrote


def test_record_commits_dedupes_within_the_input_list(store):
    store.seed("mr-1")

    wrote = git_linkage.record_commits("mr-1", "/hive", [_SHA_A, _SHA_B, _SHA_A])

    assert wrote is True
    assert git_linkage.read_commits("mr-1", "/hive") == [_SHA_A, _SHA_B]


def test_record_commits_empty_input_is_a_noop(store):
    store.seed("mr-1")

    wrote = git_linkage.record_commits("mr-1", "/hive", [])

    assert wrote is False
    assert store.set_metadata_calls() == []


def test_record_commits_raises_on_bd_failure(store):
    store.seed("mr-1")
    store.update_rc = 1
    store.update_err = "Error: dolt write failed"

    with pytest.raises(RuntimeError, match="mr-1"):
        git_linkage.record_commits("mr-1", "/hive", [_SHA_A])
