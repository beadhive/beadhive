from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import duration_order

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import refresh_test_durations as refresh  # noqa: E402


def item(nodeid: str):
    return SimpleNamespace(nodeid=nodeid)


@pytest.mark.parametrize(
    ("nodeid", "key"),
    [
        ("tests/test_a.py::test_x", "tests.test_a::test_x"),
        ("tests/test_a.py::TestC::test_x[p-1]", "tests.test_a.TestC::test_x[p-1]"),
        ("tests/sub/test_a.py::test_x", "tests.sub.test_a::test_x"),
    ],
)
def test_junit_key_matches_report_classname_and_name(nodeid, key):
    assert duration_order.junit_key(nodeid) == key


def test_longest_first_with_unknown_after_known_long_and_stable():
    durations = {"tests.t::slow": 300.0, "tests.t::mid": 5.0, "tests.t::tiny": 0.01}
    items = [item(f"tests/t.py::{n}") for n in ("tiny", "new1", "mid", "new2", "slow")]
    names = [i.nodeid.split("::")[1] for i in duration_order.order_items(items, durations, 0.05)]
    assert names == ["slow", "mid", "new1", "new2", "tiny"]


def test_order_is_deterministic_and_lossless():
    durations = {"tests.t::a": 1.0}
    items = [item(f"tests/t.py::{n}") for n in "abcd"]
    first = duration_order.order_items(items, durations, 0.0)
    assert first == duration_order.order_items(items, durations, 0.0)
    assert sorted(first, key=id) == sorted(items, key=id)


def test_no_durations_keeps_collection_order():
    items = [item("tests/t.py::b"), item("tests/t.py::a")]
    assert duration_order.order_items(items, {}, 0.0) == items


def test_opt_out_env_disables_ordering(tmp_path):
    path = tmp_path / "d.json"
    path.write_text(json.dumps({"median": 0, "tests": {"tests.t::b": 9.0}}))
    items = [item("tests/t.py::a"), item("tests/t.py::b")]
    on = {duration_order.FILE_ENV: str(path)}
    assert duration_order.apply(items, on) == [items[1], items[0]]
    for off in ("0", "false", "OFF"):
        assert duration_order.apply(items, {**on, duration_order.ENABLE_ENV: off}) == items


@pytest.mark.parametrize("content", [None, "not json", "{}", '{"tests": []}'])
def test_missing_or_malformed_file_is_harmless(tmp_path, content):
    path = tmp_path / "d.json"
    if content is not None:
        path.write_text(content)
    assert duration_order.load(path) == ({}, 0.0)


def _report(path: Path, cases: list[tuple[str, str, float, bool]]) -> None:
    body = "".join(
        f'<testcase classname="{c}" name="{n}" time="{t}">{"<skipped/>" if s else ""}</testcase>'
        for c, n, t, s in cases
    )
    path.write_text(f"<testsuites><testsuite>{body}</testsuite></testsuites>")


def test_refresh_takes_median_across_runs_and_skips_skipped(tmp_path):
    runs = tmp_path / "runs"
    for index, slow in enumerate((10.0, 30.0, 20.0)):
        directory = runs / f"run-{index}" / "reports"
        directory.mkdir(parents=True)
        _report(
            directory / "r.xml",
            [
                ("tests.t", "slow", slow, False),
                ("tests.t", "gone", 99.0, True),
                ("tests.t", "fast", 0.01, False),
            ],
        )
    (runs / "run-0" / "reports" / "bad.xml").write_text("<testsuites>")
    out = tmp_path / "out.json"
    assert refresh.main(["--glob", str(runs / "*/reports/*.xml"), "--output", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["tests"] == {"tests.t::slow": 20.0}
    assert duration_order.load(out)[0] == {"tests.t::slow": 20.0}


def test_refresh_fails_without_reports(tmp_path):
    assert (
        refresh.main(["--glob", str(tmp_path / "none*.xml"), "--output", str(tmp_path / "o")]) == 1
    )
