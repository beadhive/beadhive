"""Reader port, discovery (entry points + built-in table) and ``beadlog read``."""

from __future__ import annotations

import io
import tomllib
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from beadlog_sources import (
    BUILTIN_READERS,
    BUILTIN_STRATEGIES,
    Artifact,
    Category,
    DuplicateRegistrationError,
    ReadRequest,
    SourceReader,
    discover_readers,
    discover_strategies,
    read_evidence,
)
from beadlog_sources.cli import main
from beadlog_sources.testing.evidence import change


class _Reader:
    name = "fake"
    version = "1"
    categories = (Category.CHANGE,)

    def read(self, request: ReadRequest) -> Iterable[Artifact]:
        yield change(reader="fake", reader_version="1")


class _Point:
    def __init__(self, name: str, target: Any, value: str | None = None) -> None:
        self.name = name
        self.value = value
        self._target = target

    def load(self) -> Any:
        return self._target


def _cli(*argv: str, **options: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    options.setdefault("environ", {})
    options.setdefault("entry_points", lambda: [])
    code = main(list(argv), stdout=out, stderr=err, **options)
    return code, out.getvalue(), err.getvalue()


def test_protocol_is_runtime_checkable() -> None:
    assert isinstance(_Reader(), SourceReader)
    assert not isinstance(object(), SourceReader)


def test_entry_point_and_builtin_readers_are_both_found() -> None:
    found = discover_readers(
        lambda: [_Point("ep", _Reader, "pkg:Ep")], builtin={"table": "tests.fake:Table"}
    )
    assert list(found) == ["ep", "table"]
    assert found["ep"].origin == "entry-point" and found["table"].origin == "builtin"


def test_same_target_twice_is_one_registration() -> None:
    found = discover_readers(
        lambda: [_Point("x", _Reader, "pkg:X"), _Point("x", _Reader, "pkg:X")],
        builtin={"x": "pkg:X"},
    )
    assert list(found) == ["x"]


def test_duplicate_name_with_different_targets_is_an_error() -> None:
    with pytest.raises(DuplicateRegistrationError, match="'x'"):
        discover_readers(lambda: [_Point("x", _Reader, "pkg:A")], builtin={"x": "pkg:B"})
    with pytest.raises(DuplicateRegistrationError):
        discover_readers(lambda: [_Point("x", _Reader, "a:A"), _Point("x", _Reader, "b:B")])
    with pytest.raises(DuplicateRegistrationError):  # unknown targets compare by loaded object
        discover_readers(lambda: [_Point("x", _Reader), _Point("x", object)])
    assert list(
        discover_readers(lambda: [_Point("x", _Reader), _Point("x", _Reader)], builtin={})
    ) == ["x"]


def test_cli_reports_a_duplicate_as_exit_1() -> None:
    points = lambda: [_Point("bd", object, "elsewhere:Bd")]  # noqa: E731
    code, _, err = _cli("--version", entry_points=points)
    assert code == 1 and "'bd'" in err


def test_builtin_strategies_resolve_without_entry_points() -> None:
    found = discover_strategies(lambda: [])
    assert set(found) == set(BUILTIN_STRATEGIES)
    for registration in found.values():
        assert registration.load() is not None


def test_builtin_table_matches_installed_entry_points() -> None:
    # Where the distribution metadata is present it must agree with the table (no duplicate error).
    installed = {p.name: p.value for p in metadata.entry_points(group="beadlog.sources")}
    assert installed.items() <= dict(BUILTIN_STRATEGIES).items()
    discover_strategies()


def test_read_subcommand_writes_an_evidence_corpus(tmp_path: Path) -> None:
    out = tmp_path / "corpus.jsonl"
    code, text, _ = _cli(
        "read",
        "fake",
        "--root",
        str(tmp_path),
        "--option",
        "depth=3",
        "-o",
        str(out),
        reader_entry_points=lambda: [_Point("fake", _Reader, "pkg:Fake")],
    )
    assert code == 0 and "read 1 artifacts with fake 1" in text
    corpus = read_evidence(out)
    assert [h.name for h in corpus.headers] == ["fake"]
    assert dict(corpus.headers[0].options) == {"depth": "3"}
    assert len(corpus.artifacts) == 1


def test_read_rejects_a_non_reader(tmp_path: Path) -> None:
    code, _, err = _cli(
        "read",
        "bad",
        "-o",
        str(tmp_path / "x.jsonl"),
        reader_entry_points=lambda: [_Point("bad", object(), "pkg:Bad")],
    )
    assert code == 1 and "not a SourceReader" in err


def test_read_help_lists_registered_readers(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["read", "--help"], reader_entry_points=lambda: [_Point("fake", _Reader, "p:F")])
    assert "fake" in capsys.readouterr().out


def _entry_points(pyproject: Path, group: str) -> dict[str, str]:
    data = tomllib.loads(pyproject.read_text())
    return dict(data["project"].get("entry-points", {}).get(group, {}))


def test_pyproject_entry_points_equal_the_builtin_tables() -> None:
    package = Path(__file__).resolve().parents[1] / "pyproject.toml"
    root = Path(__file__).resolve().parents[3] / "pyproject.toml"
    if not root.is_file():
        pytest.skip("repository root pyproject.toml not reachable")
    for pyproject in (root, package):
        assert _entry_points(pyproject, "beadlog.sources") == dict(BUILTIN_STRATEGIES), pyproject
        readers = _entry_points(pyproject, "beadlog.readers")
        if readers:
            assert readers == dict(BUILTIN_READERS), pyproject
