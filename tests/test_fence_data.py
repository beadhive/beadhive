"""The product fence adapter with fakes (bh-uz46l): install, guard check, identity, data switch.

The same paths on the pinned Dolt/bd are in ``tests/test_fence_data_int.py``.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from beadhive import fence_data as fd
from beadhive import fence_data_port, host_adopt, writer_adopt
from beadhive import fence_schema as fs


class FakeEngine:
    """Just enough of a Dolt node to drive :class:`fd.FenceNode` without an engine."""

    def __init__(self, *, triggers=None, procedure=False, writer=None, ignore=(False, False)):
        self.triggers = set(triggers or ())
        self.procedure = procedure
        self.writer = writer
        self.ignore_head, self.ignore_work = ignore
        self.status: list[str] = []
        self.executed: list[list[str]] = []
        self.commits: list[str] = []
        self.complete_on_install = True

    @property
    def store(self):
        return "fake"

    def query(self, sql):
        s = sql.lower()
        if "information_schema.tables" in s:
            return [{"n": int(self.writer is not None)}]
        if "information_schema.triggers" in s:
            return [{"TRIGGER_NAME": t} for t in sorted(self.triggers)]
        if "information_schema.routines" in s:
            return [{"ROUTINE_NAME": fs.GUARD_PROCEDURE}] if self.procedure else []
        if "from bh_writer" in s:
            if self.writer is None:
                raise fd.SqlFailed("table not found: bh_writer")
            frame, epoch = self.writer
            return [{"frame": frame, "epoch": epoch, "revision": "r"}]
        if "dolt_ignore as of 'head'" in s:
            return [{"ignored": 1}] if self.ignore_head else []
        if "from dolt_ignore" in s:
            return [{"ignored": 1}] if self.ignore_work else []
        if "dolt_status" in s:
            return [{"table_name": t} for t in self.status]
        raise AssertionError(f"unexpected query {sql}")

    def execute(self, statements):
        self.executed.append(list(statements))
        joined = " ".join(statements).lower()
        if "insert into bh_writer values" in joined:
            seed = statements[
                [s.lower().startswith("insert into bh_writer") for s in statements].index(True)
            ]
            frame = seed.split("'")[1]
            epoch = int(seed.split(",")[2])
            self.writer = (frame, epoch)
        if "create procedure" in joined and self.complete_on_install:
            self.procedure = True
            self.triggers |= set(fs.trigger_names())
        if "replace into dolt_ignore" in joined:
            self.ignore_work = True

    def commit(self, message):
        self.commits.append(message)
        self.ignore_head = self.ignore_work
        return True

    def fetch(self):
        pass

    def reset_to_remote(self):
        pass

    def push(self):
        return True


# ---- guard report ------------------------------------------------------------------------


def test_guard_report_counts_only_the_expected_triggers():
    names = fs.trigger_names()
    full = fd.GuardReport(present=frozenset(names), procedure=True)
    assert full.complete and full.count == full.total == 44
    short = fd.GuardReport(present=frozenset(names[1:]) | {"bh_guard_bogus_ins"}, procedure=True)
    assert short.count == 43 and short.total == 44 and not short.complete
    assert short.missing == (names[0],) and short.extra == ("bh_guard_bogus_ins",)
    assert "43 of 44" in short.describe()
    assert not fd.GuardReport(present=frozenset(names), procedure=False).complete


def test_a_node_short_of_44_refuses_to_act_as_writer():
    engine = FakeEngine(triggers=fs.trigger_names()[:-1], procedure=True, writer=("a", 1))
    node = fd.FenceNode(engine)
    assert node.trigger_count() == 43
    with pytest.raises(fd.GuardIncomplete, match="43 of 44"):
        node.require_writer_ready()


# ---- install -----------------------------------------------------------------------------


def test_first_install_seeds_then_reinstall_only_rebuilds_the_guard():
    engine = FakeEngine()
    node = fd.FenceNode(engine)
    first = node.install("a", 7, revision="r7")
    assert first.seeded and first.writer == writer_adopt.WriterRow("a", 7, "r")
    assert first.guard.complete
    assert engine.executed[0] == fs.install_statements(("a", 7, "r7"))
    assert engine.commits == [fs.INSTALL_COMMIT_MESSAGE]

    again = node.install("z", 99)
    assert not again.seeded and again.writer.frame == "a" and again.writer.epoch == 7
    assert engine.executed[1] == fs.install_statements(None)


def test_install_that_leaves_the_guard_short_raises():
    engine = FakeEngine()
    engine.complete_on_install = False
    with pytest.raises(fd.GuardIncomplete):
        fd.FenceNode(engine).install("a", 1)


# ---- identity ----------------------------------------------------------------------------


def test_ident_provisioning_refuses_until_the_ignore_row_is_committed():
    engine = FakeEngine(ignore=(False, True))  # in the working set, not yet on HEAD
    node = fd.FenceNode(engine)
    with pytest.raises(fd.IdentNotIgnored, match="not committed on HEAD"):
        node.provision_ident("a")
    assert engine.executed == []
    engine.ignore_head = True
    node.provision_ident("a", "replica")
    assert engine.executed == [fs.ident_statements("a", "replica")]


def test_ident_that_shows_in_dolt_status_is_refused():
    engine = FakeEngine(ignore=(True, True))
    engine.status = [fs.LOCAL_IDENT_TABLE]
    with pytest.raises(fd.IdentStaged):
        fd.FenceNode(engine).provision_ident("a")


# ---- FenceData port ----------------------------------------------------------------------


def test_node_implements_the_adopt_and_reclaim_ports():
    from beadhive import failover_reclaim

    node = fd.FenceNode(FakeEngine())
    assert isinstance(node, failover_reclaim.ReclaimData)
    for method in ("sync_to_remote", "writer", "remote_writer", "history_max_epoch"):
        assert callable(getattr(node, method))
    for method in ("trigger_count", "commit_bump", "push"):
        assert callable(getattr(node, method))
    assert node.writer() is None


def test_push_maps_a_rejection_to_false_and_anything_else_to_unreachable(monkeypatch, tmp_path):
    outputs = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "! [rejected] main -> main (non-fast-forward)"),
            subprocess.CompletedProcess([], 1, "", "could not read from remote repository"),
        ]
    )
    monkeypatch.setattr(fd, "run", lambda *a, **k: next(outputs))
    engine = fd.DoltCliEngine(tmp_path)
    assert engine.push() is True
    assert engine.push() is False
    with pytest.raises(writer_adopt.DataUnreachable):
        engine.push()


# ---- the data switch ---------------------------------------------------------------------


def _hive(tmp_path, mode="embedded"):
    hive = tmp_path / "hive"
    (hive / ".beads").mkdir(parents=True)
    (hive / ".beads" / "metadata.json").write_text(
        json.dumps({"dolt_mode": mode, "dolt_database": "fx"})
    )
    noms = hive / ".beads" / "embeddeddolt" / "fx" / ".dolt" / "noms"
    noms.mkdir(parents=True)
    (noms / "manifest").write_text("1")
    return hive, noms


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    fd.reset_probe_cache()
    yield
    fd.reset_probe_cache()


def test_a_hive_without_a_local_store_is_legacy_without_a_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(fd, "run", lambda *a, **k: pytest.fail("no subprocess for a legacy hive"))
    assert fd.resolve_cut_over("fx", tmp_path) is None
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "metadata.json").write_text(json.dumps({"dolt_mode": "embedded"}))
    assert fd.resolve_cut_over("fx", tmp_path) is None  # embedded mode, no store directory


def test_only_a_hive_whose_data_carries_bh_writer_gets_the_adapter(monkeypatch, tmp_path):
    hive, noms = _hive(tmp_path)
    answers = iter([False, True])
    calls = []

    def probe(self):
        calls.append(1)
        return next(answers)

    monkeypatch.setattr(fd.FenceNode, "is_cut_over", probe)
    assert fd.resolve_cut_over("fx", hive) is None
    assert fd.resolve_cut_over("fx", hive) is None  # memoised: same store fingerprint
    assert len(calls) == 1
    (noms / "journal").write_text("write")  # the store moved (a pull brought the cutover)
    node = fd.resolve_cut_over("fx", hive)
    assert isinstance(node, fd.FenceNode) and len(calls) == 2
    assert isinstance(node.engine, fd.DoltCliEngine)
    assert node.engine.db_dir == hive / ".beads" / "embeddeddolt" / "fx"


def test_a_failing_probe_is_legacy_unless_the_store_was_seen_cut_over(monkeypatch, tmp_path):
    hive, noms = _hive(tmp_path)

    def broken(self):
        raise fd.SqlFailed("dolt exploded")

    monkeypatch.setattr(fd.FenceNode, "is_cut_over", broken)
    assert fd.resolve_cut_over("fx", hive) is None  # never seen cut over: today's behaviour
    monkeypatch.setattr(fd.FenceNode, "is_cut_over", lambda self: True)
    (noms / "journal").write_text("1")
    assert fd.resolve_cut_over("fx", hive) is not None
    monkeypatch.setattr(fd.FenceNode, "is_cut_over", broken)
    (noms / "journal").write_text("22")
    os.utime(noms / "journal", ns=(1, 1))
    assert fd.resolve_cut_over("fx", hive) is not None  # fails CLOSED through the node's reads


def test_server_mode_uses_bd_and_unknown_mode_is_legacy(tmp_path, monkeypatch):
    hive, _ = _hive(tmp_path, mode="server")
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(tmp_path / "shared"))
    engine = fd.engine_for(hive)
    assert isinstance(engine, fd.BdServerEngine) and engine.hive_dir == hive
    (hive / ".beads" / "metadata.json").write_text(json.dumps({"dolt_mode": "weird"}))
    assert fd.engine_for(hive) is None


def test_importing_fence_data_registers_the_product_default_idempotently(tmp_path):
    assert fence_data_port.default_resolver() is fd.resolve_cut_over
    fd.register()
    fd.register()
    assert fence_data_port.default_resolver() is fd.resolve_cut_over
    assert host_adopt.fence_data_for("fx", tmp_path) is None  # no store: legacy
    host_adopt.set_fence_data_resolver(lambda _p, _d: "custom")
    try:
        fd.register()  # a re-registration never clobbers an explicit override
        assert fence_data_port.fence_data_for("fx", tmp_path) == "custom"
    finally:
        host_adopt.set_fence_data_resolver(None)
    assert fence_data_port._fence_data_resolver is fd.resolve_cut_over  # None: the default


def test_a_process_that_never_imports_fence_data_treats_every_hive_as_legacy(tmp_path):
    """The port's built-in default is "no adapter": without the product module loaded, adopt
    and the guard take the legacy path, whatever the hive's store holds."""
    hive, _ = _hive(tmp_path)
    code = (
        "import sys\n"
        "from beadhive import fence_data_port, guard, host_adopt, host_lease\n"
        "assert 'beadhive.fence_data' not in sys.modules, 'fence_data was imported'\n"
        f"print(fence_data_port.fence_data_for('fx', {str(hive)!r}),"
        f" host_adopt.fence_data_for('fx', {str(hive)!r}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["None", "None"]


def test_the_port_stays_a_leaf_at_module_load():
    tree = ast.parse(Path(fence_data_port.__file__).read_text())
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert all(getattr(n, "level", 0) == 0 for n in top)
    assert not any("beadhive" in (getattr(n, "module", "") or "") for n in top)
