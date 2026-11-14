"""beadlog-sources: a docs-only ``create = propose`` run never creates a bead from nothing
(bh-uysl1): a unit with no evidence moment is skipped and reported, in every status mode."""

from __future__ import annotations

import pytest

import beadlog
from beadlog_sources import (
    PROJECT_NOTE_NO_MOMENT,
    ClusterOptions,
    IdentifyOptions,
    StatusOptions,
    collate,
    has_no_moment,
    lifecycle_of,
    project,
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


def projected(artifacts, mode: str, baseline=()):
    col = collation(artifacts, mode, baseline)
    return col, project(
        col,
        ev.corpus(*artifacts),
        baseline,
        prefix="bh",
        group=GROUP,
        extracted_at="2026-02-01T00:00:00Z",
    )


@pytest.mark.parametrize("mode", MODES)
def test_a_doc_with_no_moment_is_skipped_and_reported_in_every_mode(mode: str) -> None:
    for status in (None, "draft", "accepted"):
        col, projection = projected([doc("docs/n.md", status)], mode)
        assert has_no_moment(life(col, "doc:docs/n.md"))
        record = projection.unit("doc:docs/n.md")
        assert record is not None and record.decision == "NEW"
        assert PROJECT_NOTE_NO_MOMENT in record.notes
        assert not projection.events and record.bead is None


@pytest.mark.parametrize("mode", MODES)
def test_a_dated_doc_is_created_by_its_mode_never_silently_open_from_nothing(mode: str) -> None:
    arts = [doc("docs/d.md", "draft", date="2026-03-04")]
    col, projection = projected(arts, mode)
    record = projection.unit("doc:docs/d.md")
    assert record is not None and PROJECT_NOTE_NO_MOMENT not in record.notes
    state = beadlog.fold(projection.events)[beadlog.BeadRef(GROUP, record.bead)]
    # closed mode closes a dated doc at its date; from-doc and open keep a draft open
    assert state.phase == ("closed" if mode == "closed" else "open")
    assert state.fields["metadata.lifecycle.created_at"] == "2026-03-04"


def test_an_open_doc_bead_has_no_closed_event_and_a_closed_one_does() -> None:
    arts = [
        commit(1, paths=["docs/a.md"]),
        doc("docs/a.md", "draft"),
        commit(2, paths=["docs/b.md"]),
        doc("docs/b.md", "accepted"),
    ]
    _, projection = projected(arts, "from-doc")
    phases = {e.source.ref: e for e in projection.events if str(e.kind) == "phase"}
    assert "doc:docs/a.md" not in phases and "doc:docs/b.md" in phases
    _, legacy = projected(arts, "closed")
    assert {e.source.ref for e in legacy.events if str(e.kind) == "phase"} == {
        "doc:docs/a.md",
        "doc:docs/b.md",
    }


@pytest.mark.parametrize("mode", MODES)
def test_no_mode_changes_an_existing_bead(mode: str) -> None:
    arts = [commit(1, paths=["docs/a.md"]), doc("docs/a.md", "draft")]
    _, first = projected(arts, mode)
    baseline = tuple(first.events)
    _, again = projected(arts, mode, baseline)
    record = again.unit("doc:docs/a.md")
    assert record is not None and record.decision == "PRESENT"
    assert not [e for e in again.events if str(e.kind) in ("phase", "create")]
    # a second run over the same corpus is a no-op under every mode
    assert not again.events
