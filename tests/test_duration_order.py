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


# -- worksteal packing (bh-xqtp8) ---------------------------------------------------------------


def _marked(nodeid: str, *, marker: bool = False, fixtures: tuple[str, ...] = ()):
    return SimpleNamespace(
        nodeid=nodeid,
        fixturenames=list(fixtures),
        get_closest_marker=lambda name: object() if marker and name == "dolt_server" else None,
    )


def test_chunk_sizes_mirror_worksteal_initial_distribution():
    assert duration_order.chunk_sizes(279, 16) == [17] * 9 + [18] * 7
    assert sum(duration_order.chunk_sizes(5, 4)) == 5


def test_slot_holders_are_the_marker_and_the_rewound_fixtures():
    assert duration_order.holds_dolt_slot(_marked("t::a", marker=True))
    assert duration_order.holds_dolt_slot(_marked("t::b", fixtures=("tmp_path", "rewound_world")))
    assert not duration_order.holds_dolt_slot(_marked("t::c", fixtures=("tmp_path",)))


def _chunks(order, workers):
    out, start = [], 0
    for size in duration_order.chunk_sizes(len(order), workers):
        out.append(order[start : start + size])
        start += size
    return out


def test_pack_balances_each_worksteal_chunk_longest_first():
    seconds = [100, 90, 80, 70, 10, 10, 10, 10]
    items = [item(f"tests/t.py::n{i}") for i in range(len(seconds))]
    durations = {f"tests.t::n{i}": float(s) for i, s in enumerate(seconds)}
    packed = duration_order.pack(items, durations, 0.0, 2, 0, lambda _: False)

    assert sorted(packed, key=id) == sorted(items, key=id)
    chunks = _chunks(packed, 2)
    loads = [sum(durations[duration_order.junit_key(i.nodeid)] for i in c) for c in chunks]
    assert loads == [190.0, 190.0]
    for chunk in chunks:
        weights = [durations[duration_order.junit_key(i.nodeid)] for i in chunk]
        assert weights == sorted(weights, reverse=True)
    # A plain longest-first sort would have handed the first chunk all four long tests.
    assert loads != [340.0, 40.0]


def test_pack_bins_slot_work_onto_as_many_chunks_as_there_are_slots():
    durations = {f"tests.t::s{i}": 50.0 for i in range(4)} | {
        f"tests.t::f{i}": 5.0 for i in range(4)
    }
    items = [item(f"tests/t.py::s{i}") for i in range(4)] + [
        item(f"tests/t.py::f{i}") for i in range(4)
    ]
    holds = {i.nodeid: i.nodeid.split("::")[1].startswith("s") for i in items}
    packed = duration_order.pack(items, durations, 0.0, 4, 2, lambda i: holds[i.nodeid])
    chunks = _chunks(packed, 4)
    assert [all(holds[i.nodeid] for i in c) for c in chunks] == [True, True, False, False]
    # No split when the slot ceiling does not bind.
    unsplit = _chunks(
        duration_order.pack(items, durations, 0.0, 4, 4, lambda i: holds[i.nodeid]), 4
    )
    assert all(any(holds[i.nodeid] for i in c) for c in unsplit)


def test_pack_is_deterministic_and_off_by_default():
    items = [item(f"tests/t.py::n{i}") for i in range(7)]
    durations = {f"tests.t::n{i}": float(i % 3) for i in range(7)}
    first = duration_order.pack(items, durations, 0.0, 3, 2, lambda _: False)
    assert first == duration_order.pack(items, durations, 0.0, 3, 2, lambda _: False)
    assert duration_order.pack(items, durations, 0.0, 1, 2, lambda _: False) == items
    assert not duration_order.pack_enabled({})
    assert not duration_order.pack_enabled({duration_order.PACK_ENV: "0"})
    assert duration_order.pack_enabled({duration_order.PACK_ENV: "1"})


def test_pack_hook_only_acts_on_a_worksteal_worker():
    from harness import worksteal_pack

    def config(workerinput):
        cfg = SimpleNamespace()
        if workerinput is not None:
            cfg.workerinput = workerinput
        return cfg

    # xdist resets a worker's own `dist` option to "no", so the controller's mode travels in env.
    steal = {worksteal_pack.DIST_ENV: "worksteal"}
    assert worksteal_pack.worksteal_workers(config({"workercount": 16}), steal) == 16
    load = {worksteal_pack.DIST_ENV: "load"}
    assert worksteal_pack.worksteal_workers(config({"workercount": 16}), load) == 0
    assert worksteal_pack.worksteal_workers(config({"workercount": 16}), {}) == 0
    assert worksteal_pack.worksteal_workers(config(None), steal) == 0
