"""The ``github`` snapshot fetch and reader, offline: a fake ``get`` serves checked-in API pages
(``fixtures/github-api-pages.json``) and the reader replays the golden snapshot
(``fixtures/github-snapshot.json``). No network (the package conftest refuses sockets)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from beadhive_tracker_github.adapter.transport import Reply
from beadhive_tracker_github.reader import GitHubReader, read_snapshot
from beadhive_tracker_github.snapshot import (
    SnapshotError,
    closing_numbers,
    dumps_snapshot,
    fetch_snapshot,
    load_snapshot,
    validate_snapshot,
    write_snapshot,
)
from beadlog_sources import Category, ReadRequest, SourceReader, discover_readers

FIXTURES = Path(__file__).parent / "fixtures"
PAGES = json.loads((FIXTURES / "github-api-pages.json").read_text())
GOLDEN = FIXTURES / "github-snapshot.json"


class FakeGet:
    """``Session.get`` over the fixture pages; records every target; can fail on request."""

    def __init__(self, fail_at: int | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at

    def __call__(self, target, params=None):
        self.calls.append(target)
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise RuntimeError("boom")
        page = PAGES[f"GET {target}"]
        headers = {"link": page["link"]} if "link" in page else {}
        return Reply(200, headers, page["body"])


def _fetch(**kw):
    return fetch_snapshot(kw.pop("get", None) or FakeGet(), "o/r", **kw)


def test_fetch_matches_the_golden_snapshot_byte_for_byte() -> None:
    assert dumps_snapshot(_fetch()) == GOLDEN.read_text()


def test_fetch_is_paginated_and_skips_pull_requests_in_the_issue_listing() -> None:
    get = FakeGet()
    snap = _fetch(get=get)
    assert len(get.calls) == 8  # 2 pull pages, 4 commit listings (one paged), 2 issue pages
    assert [r["number"] for r in snap["pulls"]] == [1, 2, 3]
    assert [r["number"] for r in snap["issues"]] == [1, 3]  # issue 2 is a pull request


def test_fetch_is_deterministic() -> None:
    assert dumps_snapshot(_fetch()) == dumps_snapshot(_fetch())


def test_resume_after_a_failure_completes_to_the_same_snapshot() -> None:
    saved: list[dict] = []
    for fail_at in range(2, 9):
        saved.clear()
        with pytest.raises(RuntimeError):
            _fetch(get=FakeGet(fail_at=fail_at), on_progress=saved.append)
        partial = saved[-1]
        assert partial["complete"] is False
        get = FakeGet()
        done = _fetch(get=get, previous=partial)
        assert dumps_snapshot(done) == GOLDEN.read_text(), fail_at
        assert len(get.calls) < 8  # it did not start over


def test_resume_by_page_budget_follows_the_saved_cursor(tmp_path: Path) -> None:
    path = tmp_path / "snap.json"
    snap, rounds = None, 0
    while snap is None or not snap["complete"]:
        snap = _fetch(previous=None if snap is None else load_snapshot(path), max_pages=1)
        write_snapshot(path, snap)
        rounds += 1
        assert rounds < 20
    assert rounds == 8
    assert path.read_text() == GOLDEN.read_text()


def test_a_page_budget_records_where_to_continue() -> None:
    snap = _fetch(max_pages=1)
    assert snap["complete"] is False
    assert snap["resume"]["stage"] == "pulls"
    assert snap["resume"]["next"].endswith("page=2")


def test_a_complete_snapshot_is_returned_without_a_request() -> None:
    get = FakeGet()
    assert _fetch(get=get, previous=load_snapshot(GOLDEN))["complete"] is True
    assert get.calls == []


def test_a_next_link_off_the_api_origin_is_refused() -> None:
    def get(target, params=None):
        return Reply(200, {"link": '<https://evil.example/x?page=2>; rel="next"'}, [])

    with pytest.raises(SnapshotError, match="left the API origin"):
        fetch_snapshot(get, "o/r")


def test_a_snapshot_of_another_repository_is_refused() -> None:
    with pytest.raises(SnapshotError, match="not 'x/y'"):
        fetch_snapshot(FakeGet(), "x/y", previous=load_snapshot(GOLDEN))


def test_validate_refuses_a_malformed_snapshot() -> None:
    with pytest.raises(SnapshotError):
        validate_snapshot({"schema": "nope"})
    bad = json.loads(GOLDEN.read_text())
    bad["pulls"][0].pop("title")
    with pytest.raises(SnapshotError, match="pulls row"):
        validate_snapshot(bad)


def test_closing_references() -> None:
    assert closing_numbers("Closes #4, fixed o/r#2", "resolves: #4 and see #9") == [2, 4]


def test_the_closing_references_come_from_the_title_and_body() -> None:
    pulls = {r["number"]: r for r in load_snapshot(GOLDEN)["pulls"]}
    assert pulls[2]["closes"] == [1, 3] and pulls[1]["closes"] == []


# -- the reader ----------------------------------------------------------------------------


def _read(**options):
    return list(GitHubReader().read(ReadRequest(FIXTURES, {"snapshot": GOLDEN.name, **options})))


def test_the_reader_is_a_source_reader_of_review_and_tracker_items() -> None:
    reader = GitHubReader()
    assert isinstance(reader, SourceReader)
    assert reader.categories == (Category.REVIEW, Category.TRACKER_ITEM)
    keys = [a.key for a in _read()]
    assert keys == ["gh:pr/1", "gh:pr/2", "gh:pr/3", "gh:issue/1", "gh:issue/3"]


def test_pull_request_mapping_state_merge_and_closing_refs() -> None:
    by_key = {a.key: a for a in _read()}
    merged, plain, draft = by_key["gh:pr/2"], by_key["gh:pr/1"], by_key["gh:pr/3"]
    assert merged.category is Category.REVIEW
    assert merged.attrs["state"] == "merged"
    assert merged.attrs["merged_at"] == "2026-01-03T09:30:00Z"
    assert merged.attrs["closed_at"] == "2026-01-03T09:30:00Z"  # +02:00 normalised to UTC
    assert merged.occurred_at == "2026-01-03T09:30:00Z"
    assert merged.attrs["commits"] == ("a" * 40, "b" * 40)
    assert merged.attrs["closes"] == ("gh:issue/1", "gh:issue/3")
    assert ("issue", "gh:issue/1", "closes") in {(r.kind, r.raw, r.where) for r in merged.refs}
    assert plain.attrs["state"] == "closed" and plain.attrs["merged_at"] is None
    assert plain.occurred_at == "2026-01-01T09:00:00Z" and plain.attrs["commits"] == ()
    assert draft.attrs["state"] == "open" and draft.attrs["draft"] is True
    assert draft.occurred_at == "2026-01-05T00:00:00Z"


def test_issue_mapping_state_labels_and_assignee() -> None:
    by_key = {a.key: a for a in _read()}
    closed, opened = by_key["gh:issue/1"], by_key["gh:issue/3"]
    assert closed.category is Category.TRACKER_ITEM
    assert closed.attrs["id"] == "gh:issue/1" and closed.attrs["state"] == "closed"
    assert closed.attrs["labels"] == ("area:z", "bug")
    assert closed.attrs["assignee"] == "alice" and closed.attrs["assignees"] == ("alice", "bob")
    assert closed.attrs["closed_at"] == "2026-01-03T09:30:00Z"
    assert opened.attrs["state"] == "open" and opened.attrs["assignee"] is None
    assert opened.attrs["closed_at"] is None and opened.attrs["labels"] == ("bug", "p2")


def test_bead_tokens_of_title_and_body_become_refs_and_mode_is_recorded() -> None:
    by_key = {a.key: a for a in _read()}
    tokens = {(r.raw, r.where) for r in by_key["gh:pr/2"].refs if r.kind == "bead-token"}
    assert tokens == {("bh-abc12", "title")}
    assert {(r.raw, r.where) for r in by_key["gh:issue/3"].refs if r.kind == "bead-token"} == {
        ("bh-abc12", "title"),
        ("bh-q7", "body"),
    }
    anchored = {a.key: a for a in _read(namespaces="bh")}
    assert any(r.kind == "bead-token" for r in anchored["gh:pr/3"].refs)
    assert GitHubReader.header_options({})["token_mode"] == "generic"
    assert GitHubReader.header_options({"namespaces": "bh"})["token_mode"] == "namespaces"


def test_the_reader_is_pure_and_deterministic() -> None:
    assert [a.to_json() for a in _read()] == [a.to_json() for a in _read()]
    assert [a.key for a in read_snapshot(load_snapshot(GOLDEN))] == [a.key for a in _read()]


def test_an_unparsable_timestamp_is_null_not_a_crash() -> None:
    snap = load_snapshot(GOLDEN)
    snap["pulls"][0]["opened_at"] = "yesterday"
    assert read_snapshot(snap)[0].attrs["opened_at"] is None


def test_the_reader_registers_as_an_entry_point_of_this_plugin_only() -> None:
    import tomllib

    package = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    points = package["project"]["entry-points"]["beadlog.readers"]
    assert points == {"github": "beadhive_tracker_github.reader:GitHubReader"}

    class Point:
        name, value = "github", points["github"]

        @staticmethod
        def load():
            return GitHubReader

    found = discover_readers(lambda: [Point()])
    assert found["github"].origin == "entry-point" and {"docs", "git"} <= set(found)
