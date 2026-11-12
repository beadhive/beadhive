"""The ``docs`` reader and the DocFormat port: markdown adapter with and without markdown-it-py.

Every fixture tree is built under ``tmp_path``; git is real (a throwaway ``git init``). Nothing
depends on elapsed time.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from beadlog_sources import (
    BUILTIN_READERS,
    DocFormat,
    DocsReader,
    MarkdownFormat,
    ReadRequest,
    discover_readers,
)
from beadlog_sources import _sources as sources
from beadlog_sources.cli import main
from beadlog_sources.commits import candidate_pattern
from beadlog_sources.doc_formats import markdown_it_available, parse_frontmatter
from beadlog_sources.docs import discover_docs, frontmatter_candidate
from beadlog_sources.evidence import Category, read_evidence
from beadlog_sources.readers import load_reader
from beadlog_sources.tokens import GENERIC_BEAD_TOKEN

HAS_MD = markdown_it_available()
needs_md = pytest.mark.skipif(not HAS_MD, reason="markdown-it-py ([md] extra) not installed")

CORPUS = {
    "docs/decisions/0001-use-beads.md": (
        "---\ntitle: Use beads\nstatus: accepted\ndate: 2026-01-02\nBeads: bh-ab1c2, bh-zz9\n---\n"
        "# Heading ignored\n\nBody mentions bh-q7x1 here.\n\n## Status\n\nAccepted.\n"
    ),
    "docs/decisions/nested/0002-deep.md": "# Deep decision\n\nNo refs here.\n",
    "docs/design/feature.md": (
        "# The feature\n\n- Bead: bh-feat1\n\n```sh\n# not a heading\n```\n\n### Details\ntext\n"
    ),
    "docs/design/untitled.md": "just prose, no heading and no bead\n",
    "docs/adr/0003-adr.md": "---\nstatus: 'proposed'\n---\n\n# ADR three\n\nSee bh-adr33.\n",
    "docs/architecture/overview.md": "# Overview\n",
    "docs/other/ignored.md": "# Ignored\n",
    "README.md": "# Readme\n",
}


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = {**os.environ}
    if when:
        env.update(GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    ).stdout.strip()


def _write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    _write(root, CORPUS)
    return root


@pytest.fixture
def repo(tree: Path) -> Path:
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "t@example.com")
    _git(tree, "config", "user.name", "T")
    _git(tree, "config", "commit.gpgsign", "false")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "add docs", when="2026-03-04T05:06:07+00:00")
    return tree


def _read(root: Path, **options: str) -> list:
    return list(DocsReader().read(ReadRequest(root=root, options=options)))


def _normal(artifacts: list) -> list[dict]:
    return [a.to_json() for a in artifacts]


# -- registration and the port -------------------------------------------------------------


def test_registered_builtin_and_a_valid_source_reader() -> None:
    assert BUILTIN_READERS["docs"] == "beadlog_sources.docs_reader:DocsReader"
    reader = DocsReader()
    assert reader.name == "docs" and reader.categories == (Category.DESIGN_RECORD,)
    assert isinstance(MarkdownFormat(use_markdown_it=False), DocFormat)


def test_markdown_detect_is_by_name() -> None:
    fmt = MarkdownFormat(use_markdown_it=False)
    assert fmt.detect("a/b.md") and fmt.detect("A.MARKDOWN")
    assert not fmt.detect("a/b.html") and not fmt.detect("notes.txt")


# -- parsing: both heading paths -----------------------------------------------------------


def _formats() -> list[MarkdownFormat]:
    return [MarkdownFormat(use_markdown_it=False), *([MarkdownFormat(True)] if HAS_MD else [])]


@pytest.mark.parametrize("fmt", _formats(), ids=lambda f: "md-it" if f.tokenized else "regex")
def test_title_metadata_sections_and_backrefs(fmt: MarkdownFormat) -> None:
    parsed = fmt.parse(CORPUS["docs/decisions/0001-use-beads.md"])
    assert parsed.title == "Use beads"
    assert dict(parsed.metadata) == {
        "title": "Use beads",
        "status": "accepted",
        "date": "2026-01-02",
        "beads": "bh-ab1c2, bh-zz9",
    }
    assert [(s.level, s.heading) for s in parsed.sections] == [
        (1, "Heading ignored"),
        (2, "Status"),
    ]
    assert parsed.sections[1].text.strip() == "Accepted."
    assert parsed.backref_lines == ("Beads: bh-ab1c2, bh-zz9",)


@pytest.mark.parametrize("fmt", _formats(), ids=lambda f: "md-it" if f.tokenized else "regex")
def test_headings_inside_code_fences_are_not_headings(fmt: MarkdownFormat) -> None:
    parsed = fmt.parse(CORPUS["docs/design/feature.md"])
    assert [s.heading for s in parsed.sections] == ["The feature", "Details"]
    assert fmt.parse("no heading\n").title is None


def test_frontmatter_subset() -> None:
    assert parse_frontmatter(["Title: 'Q'", "  indented: x", "- item", "# c", "a: 1", "a: 2"]) == {
        "title": "Q",
        "a": "1",
    }


def test_forcing_markdown_it_without_it_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("beadlog_sources.doc_formats.markdown_it_available", lambda: False)
    with pytest.raises(ImportError):
        MarkdownFormat(use_markdown_it=True)
    assert MarkdownFormat().tokenized is False


def test_regex_fallback_when_the_module_cannot_be_imported(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object):
        if name == "markdown_it" or name.startswith("markdown_it."):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert not markdown_it_available()
    fmt = MarkdownFormat()
    assert fmt.tokenized is False
    assert fmt.parse("# T\n").title == "T"


@needs_md
def test_artifacts_are_identical_with_and_without_markdown_it(tree: Path) -> None:
    def run(use: bool) -> list[dict]:
        reader = DocsReader()
        pattern = GENERIC_BEAD_TOKEN
        out = []
        for path in sorted(CORPUS):
            text = (tree / path).read_text()
            out.append(reader.artifact(path, MarkdownFormat(use), text, pattern, None).to_json())
        return out

    assert run(True) == run(False)


# -- the reader ----------------------------------------------------------------------------


def test_every_md_under_the_layouts_is_read_recursively(tree: Path) -> None:
    keys = [a.key for a in _read(tree)]
    assert keys == [
        "doc:docs/adr/0003-adr.md",
        "doc:docs/architecture/overview.md",
        "doc:docs/decisions/0001-use-beads.md",
        "doc:docs/decisions/nested/0002-deep.md",
        "doc:docs/design/feature.md",
        "doc:docs/design/untitled.md",
    ]


def test_artifact_shape_refs_and_raw_attrs(tree: Path) -> None:
    by_key = {a.key: a for a in _read(tree, first_add="false")}
    record = by_key["doc:docs/decisions/0001-use-beads.md"]
    assert record.category is Category.DESIGN_RECORD and record.provenance == "observed"
    assert record.title == "Use beads" and record.occurred_at is None
    assert dict(record.attrs) == {
        "declared_status": "accepted",
        "declared_date": "2026-01-02",
        "owner": None,
        "supersedes": (),
        "superseded_by": (),
    }
    assert record.raw_attrs["frontmatter"]["status"] == "accepted"
    assert {(r.kind, r.raw, r.where) for r in record.refs} == {
        ("bead-token", "bh-ab1c2", "back-ref"),
        ("bead-token", "bh-zz9", "back-ref"),
        ("bead-token", "bh-ab1c2", "frontmatter"),
        ("bead-token", "bh-zz9", "frontmatter"),
        ("bead-token", "bh-q7x1", "body"),
    }
    # a doc with no heading takes its file stem; a sourceless tree has no time
    assert by_key["doc:docs/design/untitled.md"].title == "untitled"


def test_first_add_commit_time_and_sha_ref(repo: Path) -> None:
    sha = _git(repo, "rev-parse", "HEAD")
    record = {a.key: a for a in _read(repo)}["doc:docs/adr/0003-adr.md"]
    assert record.occurred_at == "2026-03-04T05:06:07Z"
    assert record.raw_attrs["first_add"]["sha"] == sha
    assert any(r.kind == "sha" and r.raw == sha for r in record.refs)


def test_no_git_means_no_time(tree: Path) -> None:
    assert all(a.occurred_at is None for a in _read(tree))


def test_deterministic(repo: Path) -> None:
    assert _normal(_read(repo)) == _normal(_read(repo))


def test_configurable_layouts_globs_and_files(tree: Path) -> None:
    assert [a.key for a in _read(tree, layouts="adr")] == ["doc:docs/adr/0003-adr.md"]
    assert _read(tree, layouts="none") == []
    keys = {a.key for a in _read(tree, layouts="none", globs="docs/other/*.md,*.md")}
    assert keys == {"doc:docs/other/ignored.md", "doc:README.md"}
    keys = {a.key for a in _read(tree, layouts="none", files="docs/other/ignored.md")}
    assert keys == {"doc:docs/other/ignored.md"}
    with pytest.raises(sources.SourceError, match="unknown docs layout"):
        _read(tree, layouts="nope")


def test_an_explicit_file_must_be_detected_by_a_format(tree: Path) -> None:
    (tree / "page.html").write_text("<title>x</title>")
    with pytest.raises(sources.SourceError, match="no enabled doc format"):
        _read(tree, layouts="none", files="page.html")
    with pytest.raises(sources.SourceError, match="does not exist"):
        _read(tree, layouts="none", files="missing.md")
    with pytest.raises(sources.SourceError, match="unknown doc format"):
        _read(tree, formats="rtf")
    # a glob only takes what a format detects
    assert _read(tree, layouts="none", globs="*.html") == []


def test_namespaces_anchor_the_token_pattern(tree: Path) -> None:
    refs = {r.raw for a in _read(tree, namespaces="zz") for r in a.refs if r.kind == "bead-token"}
    assert refs == set()


# -- parity with today's docs strategy -----------------------------------------------------


def test_titles_and_back_refs_equal_the_docs_strategy(tree: Path) -> None:
    pattern = candidate_pattern(["bh"])
    today = {}
    for path in discover_docs(tree, ["docs/decisions", "docs/design"]):
        text = (tree / path).read_text()
        today[f"doc:{path}"] = (
            sources.doc_title(text, Path(path).stem),
            frontmatter_candidate(text, pattern),
        )
    ours = {}
    for a in _read(tree, layouts="decisions,design", namespaces="bh", first_add="false"):
        backrefs = [r.raw for r in a.refs if r.where == "back-ref"]
        ours[a.key] = (a.title, backrefs[0 if not backrefs else -1] if backrefs else None)
    assert set(ours) == set(today)
    for key, (title, ref) in today.items():
        assert ours[key][0] == title, key
        # the strategy reads the first Beads: line and its LAST id; the reader lists every id,
        # so that id is the last token of the first back-ref line
        assert ref is None or ref in {r.raw for r in _artifact(tree, key).refs}, key


def _artifact(tree: Path, key: str):
    return next(a for a in _read(tree, namespaces="bh", first_add="false") if a.key == key)


# -- the CLI and the generic token ---------------------------------------------------------


def test_beadlog_read_docs_writes_an_evidence_corpus(tree: Path, tmp_path: Path) -> None:
    out = tmp_path / "docs.bdlg"
    assert main(["read", "docs", "--root", str(tree), "--output", str(out)]) == 0
    corpus = read_evidence(out)
    assert len(corpus.artifacts) == 6
    assert corpus.headers[0].options["token_mode"] == "generic"
    main(["read", "docs", "--root", str(tree), "--output", str(out), "--option", "namespaces=bh"])
    assert read_evidence(out).headers[0].options["token_mode"] == "namespaces"
    assert load_reader(discover_readers()["docs"]).name == "docs"


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("see bh-4r0uh.", ["bh-4r0uh"]),
        ("well-known words", []),
        ("x bh-ab1.2 y", ["bh-ab1.2"]),
        ("on 2026-01-02 we", []),
    ],
)
def test_generic_bead_token(text: str, found: list[str]) -> None:
    assert [m.group(0) for m in GENERIC_BEAD_TOKEN.finditer(text)] == found


# --- html format (bh-rqshf) ---------------------------------------------------------------------

HTML_CORPUS = {
    "docs/design/one.html": (
        "<html><head><title> Design One </title>"
        '<meta name="Status" content="accepted"><meta name="date" content="2026-02-03">'
        '<meta name="author" content="ann"><style>.x{content:"bh-style1"}</style>'
        "<script>var t = 'bh-script1';</script></head>"
        "<body><h1>Ignored h1</h1><p>Intro with bh-body1 ref.</p>"
        "<h2>Details</h2><p>More text</p><p>Beads: bh-back1</p></body></html>"
    ),
    "docs/design/two.html": "<h1>Only h1</h1><p>Status: proposed</p><h3>Deep</h3>x",
    "docs/adr/0001.htm": "<title>ADR</title><p>See bh-adr77",
    "docs/arch.html": "<title>Top level</title>",
    "docs/guides/nested.html": "<title>Not a preset</title>",
    "docs/reviews/export.html": "<title>Review export</title><p>bh-rev11</p>",
    "docs/design/plain.md": "# Markdown doc\n\nbh-md111\n",
}


def _html_tree(tmp_path: Path) -> Path:
    for name, text in HTML_CORPUS.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def test_html_format_title_meta_sections_and_ignored_content() -> None:
    from beadlog_sources.doc_formats import BUILTIN_FORMATS, HtmlFormat

    assert BUILTIN_FORMATS["html"] is HtmlFormat
    fmt = HtmlFormat()
    assert fmt.detect("a/B.HTML") and fmt.detect("x.htm") and not fmt.detect("x.md")
    one = fmt.parse(HTML_CORPUS["docs/design/one.html"])
    assert one.title == "Design One"
    assert dict(one.metadata) == {"status": "accepted", "date": "2026-02-03", "author": "ann"}
    assert [(s.level, s.heading) for s in one.sections] == [(1, "Ignored h1"), (2, "Details")]
    assert "bh-body1" in one.sections[0].text and "More text" in one.sections[1].text
    assert "bh-script1" not in one.body and "bh-style1" not in one.body
    assert one.backref_lines == ("Beads: bh-back1",)
    two = fmt.parse(HTML_CORPUS["docs/design/two.html"])
    assert two.title == "Only h1"
    assert two.metadata["status"] == "proposed"  # the visible Status: line
    assert fmt.parse("<p>no title").title is None
    assert fmt.parse("<h1>unclosed <b>bold<p>x</div></h1").title == "unclosed bold"


def test_html_status_meta_beats_status_line() -> None:
    from beadlog_sources.doc_formats import HtmlFormat

    doc = HtmlFormat().parse('<meta name="status" content="accepted"><p>Status: draft</p>')
    assert doc.metadata["status"] == "accepted"


def test_html_decode_declared_charset() -> None:
    from beadlog_sources.doc_formats import HtmlFormat

    raw = '<meta charset="latin-1"><title>café</title>'.encode("latin-1")
    assert HtmlFormat.decode(raw).endswith("café</title>")
    assert "�" in HtmlFormat.decode(b"<title>\xff</title>")


def _read_html(root: Path, **options: str) -> dict:
    request = ReadRequest(root=str(root), options={"first_add": "false", **options})
    return {a.key: a for a in DocsReader().read(request)}


def test_html_docs_discovered_end_to_end_with_markdown(tmp_path: Path) -> None:
    root = _html_tree(tmp_path)
    assert set(_read_html(root)) == {"doc:docs/design/plain.md"}  # html is opt-in
    found = _read_html(root, formats="markdown,html", namespaces="bh")
    assert set(found) == {
        "doc:docs/design/one.html",
        "doc:docs/design/two.html",
        "doc:docs/adr/0001.htm",
        "doc:docs/arch.html",
        "doc:docs/reviews/export.html",
        "doc:docs/design/plain.md",
    }
    assert found["doc:docs/design/plain.md"].raw_attrs["format"] == "markdown"
    one = found["doc:docs/design/one.html"]
    assert one.raw_attrs["format"] == "html" and one.title == "Design One"
    assert one.raw_attrs["frontmatter"]["status"] == "accepted"
    tokens = {(r.raw, r.where) for r in one.refs if r.kind == "bead-token"}
    assert tokens == {("bh-back1", "back-ref"), ("bh-back1", "body"), ("bh-body1", "body")}


def test_html_layout_presets_and_globs(tmp_path: Path) -> None:
    root = _html_tree(tmp_path)
    only = _read_html(root, formats="html", layouts="docs-html")
    assert set(only) == {"doc:docs/arch.html"}  # non-recursive glob preset
    reviews = _read_html(root, formats="html", layouts="reviews")
    assert set(reviews) == {"doc:docs/reviews/export.html"}
    extra = _read_html(root, formats="html", layouts="none", globs="docs/guides/*.html")
    assert set(extra) == {"doc:docs/guides/nested.html"}
