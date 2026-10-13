"""Fixture proof of the "add a CLI verb" wire release procedure (bh-bwnys.5).

Adding a verb needs only: register the operation, run the documented publish command
(`just wire-publish`, whose wire step is ``scripts/publish_wire_release.py``) to cut the next
minor release, and the gates pass.  Removing or changing an operation that a supported
(>= 1.5.0) release already published still fails, in both the wire gate and the package
contract-release gate.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from beadhive import contract_release, transport_inventory
from beadhive.kernel import operations

ROOT = Path(__file__).resolve().parents[1]
WIRE = Path("docs") / "schemas" / "wire"
# Sorts among the existing `hive.*` operations, so the new catalog member lands mid-list.
FIXTURE_VERB = "hive fixture-probe|as_json:boolean:o"
FIXTURE_OPERATION = "hive.fixture-probe"


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_{name}", ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PUBLISH = _script("publish_wire_release")
COMPAT = _script("check_wire_schema_compat")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """An isolated integration branch carrying the real wire releases and gate scripts."""
    root = tmp_path / "hive"
    shutil.copytree(ROOT / WIRE, root / WIRE)
    (root / "scripts").mkdir()
    for name in ("check_wire_schema_compat.py", "publish_wire_release.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Wire Publish Test")
    _git(root, "config", "user.email", "wire-publish@example.invalid")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "integration base")
    return root


@pytest.fixture
def catalog_rows(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Replace the authoritative CLI declaration the way a bead edits it."""

    def declare(rows: str) -> None:
        monkeypatch.setattr(operations, "_CLI_ROWS", rows)
        transport_inventory._catalog_index.cache_clear()

    yield declare
    transport_inventory._catalog_index.cache_clear()


def _index(repo: Path) -> dict:
    return json.loads((repo / WIRE / "index.json").read_text())


def _catalog_is_current(repo: Path) -> bool:
    target = PUBLISH.latest_release_dir(repo) / PUBLISH.CATALOG
    return target.read_text(encoding="utf-8") == PUBLISH.render_catalog()


@dataclass(frozen=True)
class GateResult:
    returncode: int
    stdout: str
    stderr: str


def _wire_gate(repo: Path) -> GateResult:
    """Run the gate in-process; the baseline is still read from the repo's real git history."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = COMPAT.run(repo, "main")
    return GateResult(status, out.getvalue(), err.getvalue())


def _contract_errors() -> list[str]:
    return contract_release.compatibility_errors(
        contract_release.load_published_baseline(), contract_release.build_release()
    )


def _without_row(prefix: str) -> str:
    rows = operations._CLI_ROWS.splitlines()
    assert sum(row.startswith(prefix) for row in rows) == 1
    return "\n".join(row for row in rows if not row.startswith(prefix))


def test_adding_a_cli_verb_publishes_the_next_minor_release_and_gates_pass(
    repo: Path, catalog_rows: Callable[[str], None]
) -> None:
    latest = _index(repo)["latest"]
    major, minor, _patch = (int(part) for part in latest.split("."))
    next_minor = f"{major}.{minor + 1}.0"
    published_before = _git(repo, "ls-files", "-s", str(WIRE / f"v{latest}"))
    assert _catalog_is_current(repo)
    assert _wire_gate(repo).returncode == 0

    # 1. Register the operation.
    catalog_rows(operations._CLI_ROWS + "\n" + FIXTURE_VERB)
    assert not _catalog_is_current(repo)
    names = [row["name"] for row in operations.document()["operations"]]
    assert FIXTURE_OPERATION in names[:-1], "the fixture verb must land mid-list"

    # 2. Run the documented publish command.
    result = PUBLISH.publish(repo)
    assert (result.version, result.action) == (next_minor, "cut")

    # 3. Gates pass: the catalog check, the wire compatibility gate, the contract gate.
    index = _index(repo)
    assert index["latest"] == next_minor
    assert index["releases"][-1] == {
        "version": next_minor,
        "major": major,
        "manifest": f"v{next_minor}/release.json",
    }
    assert _catalog_is_current(repo)
    gate = _wire_gate(repo)
    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert f"{latest} -> {next_minor} is fully compatible" in gate.stdout
    assert _contract_errors() == []
    manifest = json.loads((repo / WIRE / f"v{next_minor}" / "release.json").read_text())
    assert manifest["release_version"] == next_minor and "deprecated" not in manifest
    conformance = json.loads((repo / WIRE / f"v{next_minor}" / "conformance.json").read_text())
    assert conformance["release_version"] == next_minor

    # The published release is untouched, and re-running the command is idempotent.
    assert _git(repo, "status", "--porcelain", str(WIRE / f"v{latest}")) == ""
    assert _git(repo, "ls-files", "-s", str(WIRE / f"v{latest}")) == published_before
    assert (PUBLISH.publish(repo).version, PUBLISH.publish(repo).action) == (
        next_minor,
        "current",
    )


def test_iterating_on_an_unpublished_release_refreshes_it_in_place(
    repo: Path, catalog_rows: Callable[[str], None]
) -> None:
    catalog_rows(operations._CLI_ROWS + "\n" + FIXTURE_VERB)
    cut = PUBLISH.publish(repo)
    catalog_rows(operations._CLI_ROWS.replace(FIXTURE_VERB, "hive fixture-probe|verbose:boolean:o"))

    refreshed = PUBLISH.publish(repo)

    assert (refreshed.version, refreshed.action) == (cut.version, "refreshed")
    assert _index(repo)["latest"] == cut.version
    assert _catalog_is_current(repo)
    assert _wire_gate(repo).returncode == 0


def test_changing_an_existing_operation_can_publish_an_explicit_major_release(
    repo: Path,
    catalog_rows: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = _index(repo)["latest"]
    major = int(latest.split(".")[0])
    next_major = major + 1
    submit = next(
        row for row in operations._CLI_ROWS.splitlines() if row.startswith("work submit|")
    )
    catalog_rows(operations._CLI_ROWS.replace(submit, submit + ",fixture_override:string:o"))
    monkeypatch.setattr(operations, "CATALOG_VERSION", f"{next_major}.0.0")

    result = PUBLISH.publish(repo, major_release=True)

    assert (result.version, result.action) == (f"{next_major}.0.0", "cut")
    manifest = json.loads((repo / WIRE / f"v{next_major}.0.0" / "release.json").read_text())
    assert all(row["contract_version"] == next_major for row in manifest["artifacts"])
    gate = _wire_gate(repo)
    assert gate.returncode == 0, gate.stdout + gate.stderr


@pytest.mark.parametrize(
    "mutation, diagnostic",
    [
        ("remove", "$.operations[name='hive.classify']: canonical operation was removed"),
        ("change", "$.operations[name='hive.classify'].parameters[0].required: value changed"),
    ],
)
def test_removing_or_changing_a_published_operation_still_fails(
    repo: Path, catalog_rows: Callable[[str], None], mutation: str, diagnostic: str
) -> None:
    classify = "hive classify|provider:string:r,org:string:r,repo:string:r"
    assert classify in operations._CLI_ROWS
    if mutation == "remove":
        catalog_rows(_without_row("hive classify|"))
    else:
        catalog_rows(
            operations._CLI_ROWS.replace(
                classify, "hive classify|provider:string:o,org:string:r,repo:string:r"
            )
        )

    assert PUBLISH.publish(repo).action == "cut"
    gate = _wire_gate(repo)

    assert gate.returncode == 1, gate.stdout + gate.stderr
    assert diagnostic in gate.stderr
    assert any("hive.classify" in error or "hive classify" in error for error in _contract_errors())


def test_an_operation_added_after_the_baseline_is_immutable_once_published(
    repo: Path, catalog_rows: Callable[[str], None]
) -> None:
    declaration = operations._CLI_ROWS + "\n" + FIXTURE_VERB
    catalog_rows(declaration)
    first = PUBLISH.publish(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", f"publish wire release {first.version}")

    catalog_rows(declaration.replace(FIXTURE_VERB, "hive fixture-probe|verbose:boolean:o"))
    second = PUBLISH.publish(repo)
    gate = _wire_gate(repo)

    assert second.action == "cut" and second.version != first.version
    assert gate.returncode == 1, gate.stdout + gate.stderr
    assert f"$.operations[name='{FIXTURE_OPERATION}'].parameters[0].name" in gate.stderr


def test_editing_a_published_supported_release_in_place_fails(repo: Path) -> None:
    catalog = repo / WIRE / "v1.5.0" / PUBLISH.CATALOG
    document = json.loads(catalog.read_text())
    document["operations"].pop()
    catalog.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")

    gate = _wire_gate(repo)

    assert gate.returncode != 0
    assert "published release file was modified in place" in gate.stderr


def test_releases_below_the_supported_floor_must_be_marked_deprecated(repo: Path) -> None:
    reader = COMPAT.FilesystemReader(repo)
    repository = COMPAT.load_repository(reader)
    assert repository.latest is not None
    assert all(COMPAT._semver_tuple(version) >= (1, 5, 0) for version in repository.releases)

    index = _index(repo)
    unmarked = next(row for row in index["releases"] if row["version"] == "1.4.0")
    del unmarked["deprecated"]
    (repo / WIRE / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    with pytest.raises(ValueError, match="1.4.0 is below the supported floor"):
        COMPAT.load_repository(reader)
    # A baseline from before the marks existed only skips pre-floor history.
    assert COMPAT.load_repository(reader, require_deprecation_marks=False).latest is not None


def test_a_base_without_a_supported_release_validates_the_new_baseline(repo: Path) -> None:
    index = _index(repo)
    supported = [
        row["version"]
        for row in index["releases"]
        if tuple(map(int, row["version"].split("."))) >= (1, 5, 0)
    ]
    index["releases"] = [row for row in index["releases"] if row["version"] not in supported]
    for row in index["releases"]:
        row.pop("deprecated", None)
    index["latest"] = index["releases"][-1]["version"]
    staged = repo / "baseline-index.json"
    staged.write_text(json.dumps(index, indent=2) + "\n")
    current = (repo / WIRE / "index.json").read_bytes()
    shutil.copy2(staged, repo / WIRE / "index.json")
    staged.unlink()
    _git(repo, "commit", "-qam", "pre-1.5.0 integration base")
    _git(repo, "switch", "-qc", "candidate")
    (repo / WIRE / "index.json").write_bytes(current)

    gate = _wire_gate(repo)

    assert gate.returncode == 0, gate.stdout + gate.stderr
    assert f"{supported[-1]} validated as the new supported baseline" in gate.stdout


def test_publish_reads_base_versions_from_the_real_git_merge_base(tmp_path: Path) -> None:
    """The one real-git proof of publish's base lookup; the other tests use a snapshot."""
    root = tmp_path / "real"
    (root / WIRE).mkdir(parents=True)
    shutil.copy2(ROOT / WIRE / "index.json", root / WIRE / "index.json")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Wire Publish Test")
    git("config", "user.email", "wire-publish@example.invalid")
    assert PUBLISH._base_versions(root, "main") is None
    git("add", ".")
    git("commit", "-qm", "integration base")

    expected = {row["version"] for row in _index(root)["releases"]}
    assert PUBLISH._base_versions(root, "main") == expected
