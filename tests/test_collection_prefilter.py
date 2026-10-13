"""The marker-index collection pre-filter never drops a selected test (bh-l1jje).

Each plugin case runs a real pytest over a small project, in this process through
``pytester.runpytest_inprocess`` (pytester restores ``sys.modules``/``sys.path`` after each run).
Every test module appends its name to an import log when imported, so a case can tell a skipped
file from a deselected one. The xdist-worker case is the one that keeps a real subprocess: the
controller/worker hand-off of the skip set only exists across processes.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from harness import collection_prefilter as prefilter

pytest_plugins = ["pytester"]

HARNESS_ROOT = Path(prefilter.__file__).resolve().parents[1]
LOG_IMPORT = (
    'import os, pathlib\npathlib.Path(os.environ["IMPORT_LOG"]).open("a").write(__name__ + "\\n")\n'
)
INTEGRATION_TEST = "@pytest.mark.integration\ndef test_{}():\n    pass\n"
FILES = {
    "pytest.ini": (
        "[pytest]\ntestpaths = tests\nmarkers =\n    integration: real\n    unit_only: x\n"
    ),
    "tests/conftest.py": "# root conftest\n",
    "tests/helpers.py": "import pytest\nMARK = pytest.mark.unit_only\n",
    "tests/test_int.py": LOG_IMPORT
    + "import pytest\n\n@pytest.mark.integration\ndef test_a():\n    pass\n",
    "tests/test_unit.py": LOG_IMPORT + "def test_b():\n    pass\n",
    "tests/test_via_helper.py": LOG_IMPORT
    + "from helpers import MARK\npytestmark = MARK\n\ndef test_c():\n    pass\n",
    "tests/test_empty.py": LOG_IMPORT
    + "import os, pytest\n"
    + 'if not os.environ.get("EMPTY_HAS_ITEMS"):\n'
    + '    pytest.skip("nothing here yet", allow_module_level=True)\n\n'
    + "@pytest.mark.integration\ndef test_d():\n    pass\n",
}
INDEX = "index.json"


_SESSIONS: dict[Path, tuple[pytest.Pytester, pytest.MonkeyPatch]] = {}


@pytest.fixture
def project(pytester, monkeypatch):
    for rel, text in FILES.items():
        path = pytester.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _SESSIONS[pytester.path] = (pytester, monkeypatch)
    yield pytester.path
    _SESSIONS.pop(pytester.path, None)


def _outcome(project: Path, stdout: str):
    ids = set(re.findall(r"PASSED (tests/\S+::\w+)", stdout))
    imported = set((project / "imports.log").read_text().split())
    header = [line for line in stdout.splitlines() if "collection prefilter" in line]
    return ids, imported, header


def run(project: Path, *args: str, prefilter_on: bool = True, record: bool = False, **env):
    """Run pytest in ``project`` in-process; return (selected node ids, imported modules, header
    lines)."""
    pytester, monkeypatch = _SESSIONS[project]
    log = project / "imports.log"
    log.write_text("")
    for key in list(os.environ):
        if key.startswith(("BH_TEST_", "PYTEST_ADDOPTS", "PYTEST_PLUGINS")):
            monkeypatch.delenv(key)
    monkeypatch.delenv(prefilter.ENABLE_ENV, raising=False)
    monkeypatch.delenv(prefilter.RECORD_ENV, raising=False)
    monkeypatch.setenv("IMPORT_LOG", str(log))
    monkeypatch.setenv("BH_TEST_PREFILTER_INDEX", str(project / INDEX))
    if prefilter_on:
        monkeypatch.setenv(prefilter.ENABLE_ENV, "1")
    if record:
        monkeypatch.setenv(prefilter.RECORD_ENV, str(project / INDEX))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    result = pytester.runpytest_inprocess(
        "-p", "harness.collection_prefilter", "-p", "no:cacheprovider", "-rA", "-v", *args
    )
    assert result.ret in (0, 5), result.stdout.str() + result.stderr.str()
    return _outcome(project, result.stdout.str())


def run_subprocess(project: Path, *args: str, prefilter_on: bool = True, **env):
    """The same project run in a real child interpreter (the xdist controller/worker hand-off)."""
    log = project / "imports.log"
    log.write_text("")
    environ = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BH_TEST_", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"))
    }
    environ.update(
        PYTHONPATH=os.pathsep.join([str(HARNESS_ROOT), environ.get("PYTHONPATH", "")]),
        IMPORT_LOG=str(log),
        BH_TEST_PREFILTER_INDEX=str(project / INDEX),
    )
    if prefilter_on:
        environ[prefilter.ENABLE_ENV] = "1"
    environ.update(env)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "harness.collection_prefilter"]
        + ["-p", "no:cacheprovider", "-rA", "-v", *args],
        cwd=project,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 5), result.stdout + result.stderr
    return _outcome(project, result.stdout)


def record(project: Path) -> dict:
    run(project, "-p", "no:xdist", "--collect-only", prefilter_on=False, record=True)
    return json.loads((project / INDEX).read_text())


def test_record_writes_marker_sets_and_skips_files_with_no_selected_item(project):
    index = record(project)
    assert index["files"]["tests/test_int.py"]["marks"] == [["integration"]]
    assert index["files"]["tests/test_via_helper.py"]["marks"] == [["unit_only"]]
    assert "tests/test_empty.py" not in index["files"]  # zero items: never skippable

    off = run(project, "-p", "no:xdist", "-m", "integration", prefilter_on=False)
    on = run(project, "-p", "no:xdist", "-m", "integration")
    assert on[0] == off[0] == {"tests/test_int.py::test_a"}
    assert off[1] == {"test_int", "test_unit", "test_via_helper", "test_empty"}
    assert on[1] == {"test_int", "test_empty"}
    assert on[2] == ["collection prefilter: skipping 2 of 3 indexed files for -m 'integration'"]

    off = run(project, "-p", "no:xdist", "-m", "not integration", prefilter_on=False)
    on = run(project, "-p", "no:xdist", "-m", "not integration")
    assert on[0] == off[0] == {"tests/test_unit.py::test_b", "tests/test_via_helper.py::test_c"}
    assert "test_int" not in on[1]


def test_xdist_workers_receive_the_controllers_skip_set(project):
    record(project)
    off = run_subprocess(project, "-n", "2", "-m", "integration", prefilter_on=False)
    on = run_subprocess(project, "-n", "2", "-m", "integration")
    assert on[0] == off[0] == {"tests/test_int.py::test_a"}
    assert on[1] == {"test_int", "test_empty"}


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (lambda p: (p / "tests/conftest.py").write_text("# changed\n"), "global digest mismatch"),
        (lambda p: (p / INDEX).write_text("{"), "cannot read"),
        (lambda p: (p / INDEX).unlink(), "cannot read"),
        (
            lambda p: (p / INDEX).write_text(json.dumps({"version": 0, "files": {}})),
            "index version mismatch",
        ),
        (
            lambda p: (p / "pytest.ini").write_text(FILES["pytest.ini"] + "python_files = *.py\n"),
            "global digest mismatch",
        ),
    ],
    ids=["conftest-changed", "corrupt", "missing", "old-version", "ini-changed"],
)
def test_any_index_problem_falls_back_to_full_collection(project, damage, reason):
    record(project)
    damage(project)
    ids, imported, header = run(project, "-p", "no:xdist", "-m", "integration")
    assert ids == {"tests/test_int.py::test_a"}
    assert {"test_int", "test_unit", "test_via_helper", "test_empty"} <= imported
    assert len(header) == 1
    assert "FALLBACK to full collection" in header[0] and reason in header[0]


def test_keyword_argument_marker_expression_falls_back(project):
    record(project)
    ids, imported, header = run(
        project, "-p", "no:xdist", "-m", "integration(x=1) or not unit_only"
    )
    off = run(
        project, "-p", "no:xdist", "-m", "integration(x=1) or not unit_only", prefilter_on=False
    )
    assert ids == off[0]
    assert "FALLBACK" in header[0] and "keyword arguments" in header[0]
    assert imported == off[1]


def test_a_changed_file_is_collected_and_its_new_selected_test_kept(project):
    record(project)
    path = project / "tests/test_unit.py"
    path.write_text(
        path.read_text() + "\nimport pytest\n\n@pytest.mark.integration\ndef test_e():\n    pass\n"
    )
    ids, imported, _ = run(project, "-p", "no:xdist", "-m", "integration")
    assert ids == {"tests/test_int.py::test_a", "tests/test_unit.py::test_e"}
    assert "test_unit" in imported and "test_via_helper" not in imported


def test_a_marker_changed_in_an_imported_helper_invalidates_the_importing_file(project):
    record(project)
    (project / "tests/helpers.py").write_text("import pytest\nMARK = pytest.mark.integration\n")
    ids, imported, _ = run(project, "-p", "no:xdist", "-m", "integration")
    assert ids == {"tests/test_int.py::test_a", "tests/test_via_helper.py::test_c"}
    assert "test_via_helper" in imported


def test_a_file_with_no_items_at_index_time_is_always_collected(project):
    record(project)
    ids, imported, _ = run(project, "-p", "no:xdist", "-m", "integration", EMPTY_HAS_ITEMS="1")
    assert ids == {"tests/test_int.py::test_a", "tests/test_empty.py::test_d"}


def test_opt_out_and_no_marker_expression_leave_collection_alone(project):
    record(project)
    everything = {"test_int", "test_unit", "test_via_helper", "test_empty"}
    ids, imported, header = run(project, "-p", "no:xdist", "-m", "integration", prefilter_on=False)
    assert imported == everything and header == []
    ids, imported, header = run(
        project, "-p", "no:xdist", "-m", "integration", **{prefilter.ENABLE_ENV: "0"}
    )
    assert imported == everything and header == []
    ids, imported, header = run(project, "-p", "no:xdist")
    assert imported == everything
    assert header == ["collection prefilter: inactive (no -m expression)"]


def test_selects_matches_pytest_marker_expressions():
    expression = prefilter._compile("integration and not pants_profile")
    assert prefilter.selects(expression, ["integration"])
    assert not prefilter.selects(expression, ["integration", "pants_profile"])
    assert not prefilter.selects(expression, [])
    with pytest.raises(prefilter.Fallback):
        prefilter.selects(prefilter._compile("integration(x=1)"), ["integration"])


def test_enabled_reads_the_opt_in_switch():
    assert not prefilter.enabled({})
    for on in ("1", "true", "ON"):
        assert prefilter.enabled({prefilter.ENABLE_ENV: on})
    for off in ("0", "false", "", "off"):
        assert not prefilter.enabled({prefilter.ENABLE_ENV: off})


def test_checked_in_index_is_well_formed():
    index = json.loads(prefilter.DEFAULT_INDEX.read_text())
    assert index["version"] == prefilter.VERSION
    assert index["files"]
    for rel, entry in index["files"].items():
        assert rel.startswith("tests/") and rel.endswith(".py")
        assert re.fullmatch(r"[0-9a-f]{64}", entry["digest"])
        assert entry["marks"] and all(isinstance(names, list) for names in entry["marks"])
