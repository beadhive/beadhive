"""beadlog-sources: ``status.doc_default`` (bh-f4t6r) and docs-only creation (bh-uysl1).

A backfill writes a timeline: a draft or proposed doc's add commit is the unit's creation, never
its close. These tests cover a git-history run and a docs-only run in each mode, the reporter's
three example docs (#153), evidence outranking the declaration, the options' round trip and the
rule that no mode ever changes an existing bead.
"""

from __future__ import annotations

import pytest

from beadlog_sources import (
    ClusterOptions,
    IdentifyOptions,
    Moment,
    StatusOptions,
    StatusOptionsError,
    collate,
    conflicts_of,
    lifecycle_of,
)
from beadlog_sources.testing import evidence as ev
from beadlog_sources.testing.ledger import InMemoryLedgerDirectory

GROUP = "urn:uuid:00000000-0000-4000-8000-0000000000aa"
MODES = ("closed", "open", "from-doc")


def sha(n: int) -> str:
    return f"{n:040x}"


def ckey(n: int) -> str:
    return f"git:commit/{sha(n)}"


def stamp(n: int) -> str:
    return f"2026-01-01T00:00:{n:02d}Z"


def commit(n: int, *, cc_type: str = "docs", paths=()):
    return ev.change(sha(n), occurred_at=stamp(n), parents=[], cc_type=cc_type, paths=list(paths))


def doc(path: str, status=None, raw=None, date=None):
    raw_attrs = {} if raw is None else {"declared_status": raw}
    return ev.design_record(path, declared_status=status, declared_date=date, raw_attrs=raw_attrs)


def collation(artifacts, mode: str, baseline=(), **status):
    return collate(
        ev.corpus(*artifacts),
        baseline,
        InMemoryLedgerDirectory({}),
        local="bh",
        options=ClusterOptions(),
        identify_options=IdentifyOptions(new_threshold=100),
        status_options=StatusOptions(doc_default=mode, **status),
    )


def life(col, unit: str):
    found = lifecycle_of(col, unit)
    assert found is not None, unit
    return found


# The reporter's three example docs (#153): draft, proposed and "design only" stay open; the
# accepted, superseded and implemented ones close.
REPORTER = {
    "draft": ("draft", None, True),
    "proposed": ("proposed", None, True),
    "design-only": (None, "Design only", True),
    "accepted": ("accepted", None, False),
    "superseded": ("superseded", None, False),
    "implemented": ("delivered", "Implemented", False),
}


@pytest.mark.parametrize("name", sorted(REPORTER))
def test_from_doc_maps_the_reporters_statuses_with_a_git_history(name: str) -> None:
    status, raw, opens = REPORTER[name]
    path = f"docs/{name}.md"
    col = collation([commit(1, paths=[path]), doc(path, status, raw)], "from-doc")
    got = life(col, f"doc:{path}")
    assert got.basis == "doc"
    assert got.created == Moment(stamp(1), "second", ckey(1))
    if opens:
        assert (got.status, got.closed) == ("open", None)  # the add commit is only a creation
    else:
        assert (got.status, got.closed) == ("closed", Moment(stamp(1), "second", ckey(1)))


@pytest.mark.parametrize("name", sorted(REPORTER))
def test_from_doc_maps_the_reporters_statuses_docs_only(name: str) -> None:
    status, raw, opens = REPORTER[name]
    path = f"docs/{name}.md"
    col = collation([doc(path, status, raw, date="2026-03-04")], "from-doc")
    got = life(col, f"doc:{path}")
    day = Moment("2026-03-04", "day", f"doc:{path}")
    assert got.created == day
    assert got.status == ("open" if opens else "closed")
    assert got.closed == (None if opens else day)  # a closed doc closes at its declared date


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_with_a_git_history(mode: str) -> None:
    arts = [commit(1, paths=["docs/a.md"]), doc("docs/a.md", "accepted")]
    got = life(collation(arts, mode), "doc:docs/a.md")
    assert got.status == ("open" if mode == "open" else "closed")
    drafts = [commit(2, paths=["docs/b.md"]), doc("docs/b.md", "draft")]
    got = life(collation(drafts, mode), "doc:docs/b.md")
    assert got.status == ("closed" if mode == "closed" else "open")
    assert got.created == Moment(stamp(2), "second", ckey(2))
    assert (got.closed is None) == (mode != "closed")


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_docs_only(mode: str) -> None:
    day = "2026-03-04"
    got = life(collation([doc("docs/a.md", "accepted", date=day)], mode), "doc:docs/a.md")
    # a declared-date-only doc closes at its date in closed and from-doc, whatever it declares
    assert got.status == ("open" if mode == "open" else "closed")
    got = life(collation([doc("docs/b.md", "draft", date=day)], mode), "doc:docs/b.md")
    if mode == "closed":
        assert (got.status, got.closed) == ("closed", Moment(day, "day", "doc:docs/b.md"))
    else:
        assert (got.status, got.closed) == ("open", None)
    undeclared = life(collation([doc("docs/c.md", None, date=day)], mode), "doc:docs/c.md")
    assert undeclared.status == ("open" if mode == "open" else "closed")  # M1: undeclared closes
    assert got.created == Moment(day, "day", "doc:docs/b.md")


def test_closed_mode_reproduces_the_0_36_lifecycle_for_a_git_history() -> None:
    arts = [commit(1, paths=["docs/d.md"]), doc("docs/d.md", "draft", date="2026-01-01")]
    got = life(collation(arts, "closed"), "doc:docs/d.md")
    assert (got.status, got.basis) == ("closed", "doc")
    assert got.closed == Moment(stamp(1), "second", ckey(1))


def test_evidence_wins_over_a_draft_declaration_and_the_conflict_still_fires() -> None:
    arts = [
        commit(1, cc_type="feat", paths=["src/x.py", "docs/x.md"]),
        doc("docs/x.md", "draft", raw="Draft"),
    ]
    col = collation(arts, "from-doc")
    got = life(col, ckey(1))
    assert (got.status, got.basis) == ("closed", "committed")
    assert [c.rule for c in conflicts_of(col)] == ["doc-draft-code-landed"]


def test_a_release_that_does_not_belong_to_the_unit_does_not_close_a_draft_doc() -> None:
    released = ev.release_marker("v1", occurred_at="2026-01-02T00:00:00Z")
    arts = [commit(1, paths=["docs/d.md"]), doc("docs/d.md", "draft"), released]
    col = collation(arts, "from-doc")
    assert life(col, "doc:docs/d.md").status == "open"  # a release elsewhere is not its closure


def test_doc_open_is_configurable() -> None:
    arts = [commit(1, paths=["docs/a.md"]), doc("docs/a.md", "accepted")]
    col = collation(arts, "from-doc", doc_open=("accepted",))
    assert life(col, "doc:docs/a.md").status == "open"
    col = collation(arts, "from-doc", doc_open=())
    assert life(col, "doc:docs/a.md").status == "closed"


def test_the_options_round_trip_and_old_records_read_back_as_closed() -> None:
    opts = StatusOptions(doc_default="open", doc_open=("accepted",))
    assert StatusOptions.from_json(opts.to_json()) == opts
    assert StatusOptions().doc_default == "from-doc"
    assert StatusOptions().doc_open == ("draft", "proposed")
    legacy = StatusOptions().to_json()
    del legacy["doc_default"], legacy["doc_open"]
    assert StatusOptions.from_json(legacy).doc_default == "closed"
    with pytest.raises(StatusOptionsError):
        StatusOptions(doc_default="sometimes")
    with pytest.raises(StatusOptionsError):
        StatusOptions(doc_open=("nonsense",))
