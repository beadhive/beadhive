"""A furnished hive's tracked `.beads/` scaffolding is not a store (bh-ky918).

Hermetic: the Engine and bd are faked (the real-bd proof is
tests/test_onboard_tracked_store_int.py). The skip predicate is exercised through
`_act_bd_init`; the shared server's database is a fake `<server dir>/dolt/<db>/.dolt`
directory, the on-disk marker `has_direct_server_database` reads.
"""

from __future__ import annotations

import json

import pytest

from beadhive import hive, hive_ready, onboard, store_locator
from test_onboard_hq_remote import URL, FakeEngine, _cfg, _ctx, wired  # noqa: F401

REAL_CLEANUP = hive.cleanup_failed_bd_init  # the `wired` fixture swaps in a recorder
SERVER = {"dolt_mode": "server", "dolt_database": "widget"}


@pytest.fixture
def host(tmp_path, monkeypatch):
    server = tmp_path / "shared"
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server))
    monkeypatch.delenv("BEADS_DOLT_SERVER_HOST", raising=False)
    checkout = tmp_path / "checkout"
    (checkout / ".beads").mkdir(parents=True)
    return checkout, server


def _scaffold(checkout, **meta):
    (checkout / ".beads" / "metadata.json").write_text(json.dumps(meta or SERVER))


def _database(server, name="widget"):
    (server / "dolt" / name / ".dolt").mkdir(parents=True)


@pytest.fixture(autouse=True)
def _record_scaffold(wired, monkeypatch):  # noqa: F811
    fake = wired[0]
    fake.tracked_scaffold = []
    inner = fake.clone_remote

    def clone(cwd, url, *, env=None, extra_env=None, tracked_scaffold=False):
        fake.tracked_scaffold.append(tracked_scaffold)
        return inner(cwd, url, env=env, extra_env=extra_env)

    monkeypatch.setattr(fake, "clone_remote", clone)


def _run(checkout, descriptor=True):
    onboard._act_bd_init(_ctx(checkout, _cfg({"url": URL} if descriptor else None)))


def test_tracked_scaffold_without_database_clones_from_the_descriptor(host, wired):  # noqa: F811
    checkout, _ = host
    _scaffold(checkout)
    fake, bd_calls, _ = wired
    _run(checkout)
    assert [c[:2] for c in fake.calls[:1]] == [("clone", URL)]
    assert fake.tracked_scaffold == [True]
    assert bd_calls == []  # never bd init / bd bootstrap (origin path)


def test_existing_database_keeps_the_skip(host, wired, capsys):  # noqa: F811
    checkout, server = host
    _scaffold(checkout)
    _database(server)
    fake, _, _ = wired
    _run(checkout)
    assert fake.calls == []
    assert "already initialized" in capsys.readouterr().out


def test_no_descriptor_keeps_the_skip(host, wired):  # noqa: F811
    checkout, _ = host
    _scaffold(checkout)
    fake, bd_calls, _ = wired
    _run(checkout, descriptor=False)
    assert fake.calls == [] and bd_calls == []


def test_embedded_mode_keeps_the_skip(host, wired):  # noqa: F811
    checkout, _ = host
    _scaffold(checkout, dolt_mode="embedded", dolt_database="widget")
    fake, _, _ = wired
    _run(checkout)
    assert fake.calls == []


def test_bare_beads_dir_without_a_recorded_mode_keeps_the_skip(host, wired):  # noqa: F811
    checkout, _ = host
    fake, _, _ = wired
    _run(checkout)
    assert fake.calls == []


def test_remote_bd_server_is_assumed_present(host, wired, monkeypatch):  # noqa: F811
    checkout, _ = host
    _scaffold(checkout)
    monkeypatch.setenv("BEADS_DOLT_SERVER_HOST", "dolt.example.net")
    fake, _, _ = wired
    _run(checkout)
    assert fake.calls == []


def test_identity_stamp_leaves_matching_tracked_metadata_byte_identical(host):
    checkout, _ = host
    path = checkout / ".beads" / "metadata.json"
    path.write_text('{"dolt_mode":"server","dolt_database":"widget","extra":1}')
    before = path.read_text()
    assert onboard._stamp_clone_identity(_ctx(checkout, _cfg())) == "widget"
    assert path.read_text() == before


def test_failed_clone_restores_tracked_scaffold_and_keeps_it(tmp_path):
    from harness.world import git

    git("init", "-q", "-b", "main", cwd=tmp_path)
    git("config", "user.email", "t@t.dev", cwd=tmp_path)
    git("config", "user.name", "T", cwd=tmp_path)
    beads = tmp_path / ".beads"
    beads.mkdir()
    (beads / "metadata.json").write_text("{}\n")
    git("add", ".", cwd=tmp_path)
    git("commit", "-q", "-m", "init", cwd=tmp_path)
    (beads / "metadata.json").write_text('{"dirty": 1}\n')
    (beads / "debris").write_text("x")
    REAL_CLEANUP(tmp_path)
    assert (beads / "metadata.json").read_text() == "{}\n"
    assert not (beads / "debris").exists()


# ---- hive ready ------------------------------------------------------------

_ENTRY = {"prefix": "widget", "dolt_remote": {"name": "hq", "url": "file:///hq/widget"}}


def test_hive_ready_is_not_applicable_without_an_hq_descriptor(host):
    checkout, _ = host
    _scaffold(checkout)
    assert store_locator.dolt_mode(checkout) == "server"
    assert hive_ready._internal_store_check({"prefix": "widget"}, checkout) is None
    checks = hive_ready.scan({"managed_repos": []}, ("github", "acme", "widget"),
                             {"prefix": "widget", "kind": "personal"}, checkout)  # fmt: skip
    assert all(c.label != "internal store" for c in checks)


def test_hive_ready_fails_when_the_server_database_is_missing(host):
    checkout, _ = host
    _scaffold(checkout)
    check = hive_ready._internal_store_check(_ENTRY, checkout)
    assert check is not None and check.required and check.state == "missing"
    assert "hive onboard" in check.detail
    assert "do NOT run `bd bootstrap`" in check.detail


def test_hive_ready_passes_when_the_database_exists(host):
    checkout, server = host
    _scaffold(checkout)
    _database(server)
    assert hive_ready._internal_store_check(_ENTRY, checkout) is None


def test_hive_ready_is_not_applicable_for_embedded_or_unknown_mode(host):
    checkout, _ = host
    _scaffold(checkout, dolt_mode="embedded", dolt_database="widget")
    assert hive_ready._internal_store_check(_ENTRY, checkout) is None
    (checkout / ".beads" / "metadata.json").unlink()
    assert hive_ready._internal_store_check(_ENTRY, checkout) is None


def test_missing_store_reaches_the_scan_as_a_required_failure(host):
    checkout, _ = host
    _scaffold(checkout)
    assert store_locator.dolt_mode(checkout) == "server"
    checks = hive_ready.scan({"managed_repos": []}, ("github", "acme", "widget"),
                             {**_ENTRY, "kind": "personal"}, checkout)  # fmt: skip
    line = next(c for c in checks if c.label == "internal store")
    assert line.required and line.state == "missing"
