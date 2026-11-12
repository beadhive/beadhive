"""Doc metadata normalization: status synonyms, MADR / Nygard sections, HTML meta, dates."""

from __future__ import annotations

import pytest

from beadlog_sources import DocsReader, HtmlFormat, MarkdownFormat
from beadlog_sources.doc_metadata import (
    STATUS_SYNONYMS,
    normalize_date,
    normalize_doc_metadata,
    normalize_status,
)
from beadlog_sources.evidence import DECLARED_STATUSES, Category
from beadlog_sources.tokens import bead_token_pattern

MD = MarkdownFormat(use_markdown_it=False)
HTML = HtmlFormat()


def test_synonym_table_targets_the_closed_vocabulary() -> None:
    assert set(STATUS_SYNONYMS.values()) == set(DECLARED_STATUSES)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("proposed", "proposed"),
        ("Proposed", "proposed"),
        ("RFC", "proposed"),
        ("Draft", "draft"),
        ("WIP", "draft"),
        ("accepted", "accepted"),
        ("**Accepted**", "accepted"),
        ("Approved.", "accepted"),
        ("Accepted (2026-01-02)", "accepted"),
        ("Accepted - see bh-12", "accepted"),
        ("accepted by the team", "accepted"),
        ("Implemented", "delivered"),
        ("Done", "delivered"),
        ("Shipped in 1.2", "delivered"),
        ("Superseded", "superseded"),
        ("Rejected", "rejected"),
        ("Declined", "rejected"),
        ("Deprecated", "deprecated"),
        ("`deprecated`", "deprecated"),
    ],
)
def test_status_forms_and_synonyms(raw: str, expected: str) -> None:
    assert normalize_status(raw) == (expected, ())


@pytest.mark.parametrize(
    "raw", ["", "maybe", "Accepted and then some", "in flux", "Proposed-ish", "tbd", "5"]
)
def test_unknown_status_is_never_coerced(raw: str) -> None:
    assert normalize_status(raw) == (None, ())


@pytest.mark.parametrize(
    ("raw", "refs"),
    [
        ("Superseded by ADR-12", ("ADR-12",)),
        ("superseded by [ADR-0012](0012-new.md)", ("0012-new.md",)),
        ("Superseded by ADR-12, ADR-13 (2026-02-01).", ("ADR-12", "ADR-13")),
        ("Replaced by docs/design/new.md", ("docs/design/new.md",)),
    ],
)
def test_superseded_by_carries_the_reference(raw: str, refs: tuple[str, ...]) -> None:
    assert normalize_status(raw) == ("superseded", refs)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-02", "2026-01-02"),
        ("2026/1/2", "2026-01-02"),
        ("January 2, 2026", "2026-01-02"),
        ("2 Jan 2026", "2026-01-02"),
        ("2026-01-02T10:30:00Z", "2026-01-02T10:30:00Z"),
        ("2026-01-02 10:30:00+02:00", "2026-01-02T08:30:00Z"),
        ("2026-01-02T10:30+0200", "2026-01-02T08:30:00Z"),
        ("2026-01-02T10:30:00.5Z", "2026-01-02T10:30:00.5Z"),
        ("2026-01-02T10:30:00 UTC", "2026-01-02T10:30:00Z"),
        # day-only never becomes a timestamp; a time with no zone is not guessed at
        ("2026-01-02T10:30:00", None),
        ("2026-02-30", None),
        ("soon", None),
        ("", None),
        ("2026-13-01T00:00:00Z", None),
    ],
)
def test_date_forms(raw: str, expected: str | None) -> None:
    assert normalize_date(raw) == expected


def test_day_only_date_is_exactly_the_day() -> None:
    assert "T" not in (normalize_date("2026-01-02") or "T")


def _meta(text: str, fmt=MD):
    return normalize_doc_metadata(fmt.parse(text))


def test_frontmatter_fields_and_raw() -> None:
    meta = _meta(
        "---\nstatus: Implemented\ndate: 2026-01-02\nauthor: Ada Lovelace\n"
        "supersedes: docs/old.md, bh-ab12\n---\n# T\n"
    )
    assert meta.attrs() == {
        "declared_status": "delivered",
        "declared_date": "2026-01-02",
        "owner": "Ada Lovelace",
        "supersedes": ["docs/old.md", "bh-ab12"],
        "superseded_by": [],
    }
    assert [(r.kind, r.raw, r.where) for r in meta.refs] == [
        ("bead-token", "bh-ab12", "supersedes")
    ]
    assert meta.raw["status"] == {"raw": "Implemented", "source": "frontmatter"}
    assert meta.raw["date"] == "2026-01-02" and meta.raw["owner"] == "Ada Lovelace"


def test_owner_key_precedence_and_brackets() -> None:
    assert _meta("---\nauthor: A\nowner: O\n---\n").owner == "O"
    assert _meta("---\ndeciders: [Ann, Bob]\n---\n").owner == "Ann, Bob"
    assert _meta("---\ndecision-makers: Cy\n---\n").owner == "Cy"


def test_nygard_status_section() -> None:
    meta = _meta("# 1. Use beads\n\nDate: 2026-01-02\n\n## Status\n\nAccepted\n\n## Context\n\nx\n")
    assert meta.declared_status == "accepted"
    assert meta.raw["status"]["source"] == "section"


def test_nygard_superseded_section() -> None:
    meta = _meta("# T\n\n## Status\n\nSuperseded by [ADR 12](0012.md)\n\n## Context\n")
    assert (meta.declared_status, meta.supersedes) == ("superseded", ())
    assert meta.superseded_by == ("0012.md",)


def test_madr_bullet_status_and_bold_lines() -> None:
    assert _meta("# T\n\n* Status: accepted\n* Deciders: Ann\n").declared_status == "accepted"
    assert _meta("# T\n\n**Status:** Proposed\n").declared_status == "proposed"
    assert _meta("# T\n\n**Status**: Draft\n").declared_status == "draft"
    assert _meta("# T\n\nStatus: deprecated\n").declared_status == "deprecated"


def test_status_line_inside_a_fence_is_ignored() -> None:
    assert _meta("# T\n\n```\nStatus: accepted\n```\n").raw == {}


def test_precedence_frontmatter_then_section_then_line() -> None:
    text = "---\nstatus: draft\n---\n# T\n\nStatus: accepted\n\n## Status\n\nRejected\n"
    assert _meta(text).declared_status == "draft"
    assert _meta("# T\n\nStatus: accepted\n\n## Status\n\nRejected\n").declared_status == "rejected"


def test_unknown_status_keeps_raw_and_does_not_fall_through() -> None:
    meta = _meta("---\nstatus: it depends\n---\n# T\n\n## Status\n\nAccepted\n")
    assert meta.declared_status is None
    assert meta.raw["status"] == {"raw": "it depends", "source": "frontmatter"}


def test_garbage_date_is_none_with_raw_kept() -> None:
    meta = _meta("---\ndate: sometime in spring\n---\n")
    assert meta.declared_date is None and meta.raw["date"] == "sometime in spring"


def test_no_metadata_is_empty() -> None:
    meta = _meta("# Just a title\n\nBody.\n")
    assert meta.attrs() == {
        "declared_status": None,
        "declared_date": None,
        "owner": None,
        "supersedes": [],
        "superseded_by": [],
    }
    assert meta.raw == {} and meta.refs == ()


def test_html_meta_and_visible_status_line() -> None:
    page = (
        '<html><head><title>T</title><meta name="Status" content="Implemented">'
        '<meta name="date" content="2026-01-02T10:30:00Z"><meta name="author" content="Ada">'
        "</head><body><h1>T</h1><p>Status: Rejected</p></body></html>"
    )
    meta = _meta(page, HTML)
    assert meta.attrs() == {
        "declared_status": "delivered",
        "declared_date": "2026-01-02T10:30:00Z",
        "owner": "Ada",
        "supersedes": [],
        "superseded_by": [],
    }
    assert _meta("<h1>T</h1><p>Status: proposed</p>", HTML).declared_status == "proposed"
    sect = _meta("<h1>T</h1><h2>Status</h2><p>Accepted</p><h2>Context</h2>", HTML)
    assert sect.declared_status == "accepted"


def test_markdown_and_html_normalize_identically() -> None:
    md = "---\nstatus: Superseded by ADR-12\ndate: 2026-01-02\nowner: Ada\n---\n# T\n"
    html = (
        '<meta name="status" content="Superseded by ADR-12"><meta name="date" content="2026-01-02">'
        '<meta name="owner" content="Ada"><h1>T</h1>'
    )
    assert _meta(md).attrs() == _meta(html, HTML).attrs()
    assert _meta(md).attrs()["supersedes"] == []
    assert _meta(md).attrs()["superseded_by"] == ["ADR-12"]


def test_superseded_by_is_not_supersedes() -> None:
    old = _meta("---\nstatus: Superseded by bh-b1\n---\n# A\n")
    assert (old.declared_status, old.supersedes, old.superseded_by) == (
        "superseded",
        (),
        ("bh-b1",),
    )
    assert [(r.raw, r.where) for r in old.refs] == [("bh-b1", "superseded-by")]
    new = _meta("---\nstatus: accepted\nreplaces: A\n---\n# B\n")
    assert (new.supersedes, new.superseded_by) == (("A",), ())
    assert _meta("---\nsupersedes: A\n---\n").supersedes == ("A",)


def test_namespace_pattern_limits_supersedes_refs() -> None:
    parsed = MD.parse("---\nsupersedes: adr-12, bh-ab12\n---\n")
    generic = normalize_doc_metadata(parsed)
    scoped = normalize_doc_metadata(parsed, bead_token_pattern(["bh"]))
    assert [r.raw for r in generic.refs] == ["adr-12", "bh-ab12"]
    assert [r.raw for r in scoped.refs] == ["bh-ab12"]
    assert scoped.supersedes == ("adr-12", "bh-ab12")


def test_reader_fills_the_design_record_and_passes_shape_validation() -> None:
    text = (
        "---\nstatus: Implemented\ndate: 2026-01-02\nauthor: Ada\nsupersedes: bh-ab12\n---\n# T\n"
    )
    record = DocsReader().artifact("docs/d.md", MD, text, bead_token_pattern(["bh"]))
    assert record.category is Category.DESIGN_RECORD
    assert record.attrs["declared_status"] == "delivered"
    assert record.attrs["declared_date"] == "2026-01-02"
    assert record.attrs["owner"] == "Ada" and tuple(record.attrs["supersedes"]) == ("bh-ab12",)
    assert record.raw_attrs["declared"]["status"]["raw"] == "Implemented"
    assert ("bead-token", "bh-ab12", "supersedes") in {
        (r.kind, r.raw, r.where) for r in record.refs
    }
    assert DocsReader.version == "0.2.0"
