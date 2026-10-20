"""Dolt-level remote/backup registration on the Engine seam (bh-ksmt0.3).

Pure snapshot tests, ``BdEngine`` unit tests with the registry read and bd calls faked, and
integration tests that drive the real pinned ``bd``/``dolt`` against ``file://`` stores only (no
network, no cloud, never the shared server).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from beadhive import dolt_remote, engine
from beadhive.dolt_remote import (
    RegistryEntry,
    RegistryKind,
    RegistrySnapshot,
    RemoteFamily,
    RemoteOp,
)
from beadhive.run import run
from harness.beads import embedded_env, skip_if_no_bd

R, B = RegistryKind.REMOTE, RegistryKind.BACKUP
SECRET = "hunter2-secret"


def _snap(remotes=(), backups=()):
    return RegistrySnapshot(
        remotes=tuple(RegistryEntry(R, n, u, True) for n, u in remotes),
        backups=tuple(RegistryEntry(B, n, u) for n, u in backups),
    )


# ---- pure registry model ---------------------------------------------------------------------


def test_conflict_is_a_backup_at_an_address_a_remote_claims():
    snap = _snap(
        remotes=[("backup_export", "file:///srv/bk"), ("origin", "file:///srv/other")],
        backups=[("default", "file:///srv/bk/")],  # trailing slash: same address
    )
    assert [(c.remote.name, c.backup.name) for c in snap.conflicts] == [
        ("backup_export", "default")
    ]
    assert snap.claimants("file:///srv/bk", kind=R)[0].name == "backup_export"
    assert snap.claimants("file:///srv/bk", kind=B)[0].name == "default"
    assert not _snap(remotes=[("o", "file:///a")], backups=[("default", "file:///b")]).conflicts


def test_redacted_listing_never_contains_a_credential():
    url = f"git+https://user:{SECRET}@host.example/org/repo.git"
    s3 = f"s3://b/db?endpoint=http://h:3900&path-style=true&token={SECRET}"
    snap = _snap(remotes=[("origin", url)], backups=[("default", s3)])
    rendered = json.dumps(snap.redacted())
    assert SECRET not in rendered
    assert SECRET not in snap.remote("origin").describe()
    assert "endpoint=http://h:3900" in rendered  # the addressing allowlist survives
    assert SECRET in json.dumps(snap.to_raw())  # the raw form is for exact restore only


def test_snapshot_round_trips_and_rejects_foreign_documents():
    snap = _snap(remotes=[("a", "file:///x")], backups=[("default", "file:///y")])
    again = RegistrySnapshot.from_raw(json.loads(json.dumps(snap.to_raw())))
    assert snap.diff(again).empty
    with pytest.raises(ValueError):
        RegistrySnapshot.from_raw({"schema": "other/1"})


def test_diff_names_added_missing_and_changed_registrations():
    base = _snap(
        remotes=[("a", "file:///1"), ("b", "file:///2")], backups=[("default", "file:///k")]
    )
    now = _snap(remotes=[("a", "file:///CHANGED"), ("c", "file:///3")])
    delta = base.diff(now)
    assert delta.changed == ((R, "a"),)
    assert delta.removed == ((B, "default"), (R, "b"))
    assert delta.added == ((R, "c"),)
    assert "missing backup 'default'" in delta.describe()


def test_hidden_from_bd_is_reported():
    snap = RegistrySnapshot(
        remotes=(
            RegistryEntry(R, "origin", "file:///a", True),
            RegistryEntry(R, "backup_export", "file:///b", False),
        )
    )
    assert [e.name for e in snap.hidden_from_bd] == ["backup_export"]
    assert snap.redacted()["remotes"][0]["hidden_from_bd"] is True


# ---- transport capability contracts (git / file / S3 / hosted) -------------------------------


@pytest.mark.parametrize(
    ("url", "family", "channel_git", "clone_url", "transfer"),
    [
        ("git+ssh://git@h/o/r.git", RemoteFamily.GIT, True, "ssh://git@h/o/r.git", True),
        ("git+file:///srv/r.git", RemoteFamily.GIT, True, "file:///srv/r.git", True),
        ("file:///srv/store", RemoteFamily.FILE, False, None, True),
        ("s3://b/db", RemoteFamily.S3, False, None, True),
        ("s3://b/db?endpoint=http://g:3900&path-style=true", RemoteFamily.S3, False, None, True),
        ("aws://[t:b]/db", RemoteFamily.AWS, False, None, True),
        ("https://doltremoteapi.dolthub.com/a/b", RemoteFamily.HOSTED, False, None, False),
    ],
)
def test_transport_contract_per_family(url, family, channel_git, clone_url, transfer):
    target = dolt_remote.classify(url)
    assert target.family == family
    channel = dolt_remote.channel_for(url)
    # Only a Git remote rides refs/dolt/data; nothing else may claim it.
    assert (channel == "refs/dolt/data") is channel_git
    assert dolt_remote.git_clone_url(url) == clone_url
    for op in (RemoteOp.PUSH, RemoteOp.PULL, RemoteOp.CLONE, RemoteOp.ADD, RemoteOp.REPLACE):
        assert target.allows(op) is transfer
    assert target.allows(RemoteOp.REMOVE)  # a registration can always be dropped


def test_channel_for_a_non_git_remote_names_the_store():
    channel = dolt_remote.channel_for("s3://b/db?endpoint=http://h:3900&path-style=true")
    assert channel == "s3:s3://b/db?endpoint=http://h:3900&path-style=true"


# ---- BdEngine with faked reads ----------------------------------------------------------------


class Fake:
    """A BdEngine whose bd/dolt processes are replaced by an in-memory registry."""

    def __init__(self, monkeypatch, remotes=(), backups=(), hidden=()):
        self.eng = engine.BdEngine()
        self.remotes = dict(remotes)
        self.backups = dict(backups)
        self.hidden = set(hidden)
        self.calls: list[list[str]] = []
        self.fail_on: set[str] = set()
        monkeypatch.setattr(self.eng, "_sql_rows", self._sql_rows)
        monkeypatch.setattr(self.eng, "invoke", self._invoke)
        monkeypatch.setattr(self.eng, "_state_call", self._state)
        monkeypatch.setattr(self.eng, "_dolt_write", self._write)

    def _sql_rows(self, cwd, query):
        table = self.remotes if "dolt_remotes" in query else self.backups
        return [{"name": n, "url": u} for n, u in table.items()], ""

    def _done(self, rc=0, err=""):
        return subprocess.CompletedProcess(args=["bd"], returncode=rc, stdout="", stderr=err)

    def _invoke(self, args, **kw):
        self.calls.append(list(args))
        if args[:3] == ["dolt", "remote", "list"]:
            shown = [{"name": n, "url": u} for n, u in self.remotes.items() if n not in self.hidden]
            return subprocess.CompletedProcess(
                args=["bd"], returncode=0, stdout=json.dumps(shown), stderr=""
            )
        if args[:3] == ["dolt", "remote", "add"]:
            if "add:" + args[3] in self.fail_on:
                return self._done(1, "boom")
            self.remotes[args[3]] = args[4]
            return self._done()
        if args[:3] == ["dolt", "remote", "remove"]:
            if args[3] in self.hidden:
                return self._done(1, "no such remote")
            self.remotes.pop(args[3], None)
            return self._done()
        return self._done()

    def _state(self, args, cwd, **kw):
        self.calls.append(list(args))
        if args[:2] == ["backup", "add"]:
            self.backups["default"] = args[2]
        if args[:2] == ["backup", "remove"]:
            self.backups.pop("default", None)
            # bd 1.3.0: `backup remove` also drops its other registration as collateral
            self.backups.pop(getattr(self, "collateral", ""), None)
        return self._done()

    def _write(self, cwd, kind, op, name, url=None, **kw):
        self.calls.append(["_dolt_write", kind, op, name])
        table = self.remotes if kind == "remote" else self.backups
        if op == "add":
            table[name] = url
        else:
            table.pop(name, None)
        return self._done()


def test_read_registry_reports_what_bd_hides(monkeypatch):
    fake = Fake(
        monkeypatch,
        remotes={"origin": "file:///a", "backup_export": "file:///b"},
        backups={"default": "file:///k"},
        hidden={"backup_export"},
    )
    read = fake.eng.read_registry("/hive")
    assert read.ok
    assert [e.name for e in read.snapshot.hidden_from_bd] == ["backup_export"]
    assert read.snapshot.backup("default").url == "file:///k"


def test_read_registry_failure_is_unknown_never_empty(monkeypatch):
    eng = engine.BdEngine()
    monkeypatch.setattr(eng, "_sql_rows", lambda cwd, q: (None, "timeout"))
    read = eng.read_registry("/hive")
    assert not read.ok and read.snapshot is None and "timeout" in read.error


def test_add_remote_refuses_an_address_a_hidden_backup_claims(monkeypatch):
    fake = Fake(monkeypatch, backups={"default": "file:///srv/bk"})
    refused = fake.eng.add_remote("/hive", "origin", "file:///srv/bk/")
    assert refused.returncode == 1 and "already claims" in refused.stderr
    assert ["dolt", "remote", "add", "origin", "file:///srv/bk/"] not in fake.calls


def test_add_backup_is_probe_then_add(monkeypatch):
    fake = Fake(monkeypatch, remotes={"backup_export": "file:///srv/bk"})
    blocked = fake.eng.add_backup("/hive", "file:///srv/bk")
    assert blocked.returncode == 1 and "backup_export" in blocked.stderr
    assert fake.eng.add_backup("/hive", "file:///srv/new").returncode == 0
    assert fake.backups["default"] == "file:///srv/new"
    assert fake.eng.add_backup("/hive", "file:///srv/new").returncode == 0  # idempotent
    other = fake.eng.add_backup("/hive", "file:///srv/other")
    assert other.returncode == 1 and fake.backups["default"] == "file:///srv/new"
    assert fake.eng.add_backup("/hive", "file:///srv/other", replace=True).returncode == 0
    assert fake.backups["default"] == "file:///srv/other"


def test_object_store_claims_conflict_with_remotes_too(monkeypatch):
    claimed = "s3://b/db?endpoint=http://h:3900&path-style=true"
    fake = Fake(monkeypatch, backups={"default": claimed})
    refused = fake.eng.add_remote("/hive", "origin", claimed)
    assert refused.returncode == 1 and "already claims" in refused.stderr


def test_cutover_targets_the_descriptor_name_never_origin(monkeypatch):
    fake = Fake(monkeypatch, remotes={"hq-private": "git+ssh://git@h/o/r.git"})
    out = fake.eng.cutover_remote("/hive", "hq-private", "file:///srv/new", retain_as="retained-t1")
    assert out.ok, out.error
    assert fake.remotes == {
        "hq-private": "file:///srv/new",
        "retained-t1": "git+ssh://git@h/o/r.git",
    }
    assert "origin" not in fake.remotes
    assert out.before.remote("hq-private").url == "git+ssh://git@h/o/r.git"
    assert not any("origin" in call for call in fake.calls)


def test_cutover_failure_restores_the_registry_exactly(monkeypatch):
    start = {"hq-private": "git+ssh://git@h/o/r.git", "peer": "file:///srv/p"}
    fake = Fake(monkeypatch, remotes=start, backups={"default": "file:///srv/bk"})
    fake.fail_on = {"add:hq-private"}  # the re-point fails after the retained copy is made
    out = fake.eng.cutover_remote("/hive", "hq-private", "file:///srv/new", retain_as="kept")
    assert not out.ok and out.restored
    assert fake.remotes == start and fake.backups == {"default": "file:///srv/bk"}


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"name": "missing", "retain_as": "kept"}, "not registered"),
        ({"name": "hq-private", "retain_as": "peer"}, "already registered"),
        ({"name": "hq-private", "retain_as": "hq-private"}, "distinct"),
        ({"name": "hq-private", "retain_as": "kept", "url": "file:///srv/bk"}, "claims the target"),
        ({"name": "hq-private", "retain_as": "kept", "url": "gs://b/db"}, "unsupported"),
    ],
)
def test_cutover_refuses_before_mutating(monkeypatch, kwargs, why):
    start = {"hq-private": "file:///srv/old", "peer": "file:///srv/p"}
    fake = Fake(monkeypatch, remotes=start, backups={"default": "file:///srv/bk"})
    url = kwargs.pop("url", "file:///srv/new")
    out = fake.eng.cutover_remote("/hive", url=url, **kwargs)
    assert not out.ok and why in out.error
    assert fake.remotes == start
    assert not any(c[:3] == ["dolt", "remote", "add"] for c in fake.calls)


def test_restore_registry_reaches_hidden_registrations_below_bd(monkeypatch):
    fake = Fake(
        monkeypatch,
        remotes={"origin": "file:///a", "backup_export": "file:///b"},
        hidden={"backup_export"},
    )
    out = fake.eng.restore_registry("/hive", _snap(remotes=[("origin", "file:///a")]))
    assert out.ok and fake.remotes == {"origin": "file:///a"}
    assert ["_dolt_write", "remote", "remove", "backup_export"] in fake.calls


def test_restore_registry_converges_when_bd_backup_remove_drops_a_neighbour(monkeypatch):
    fake = Fake(monkeypatch, backups={"default": "file:///k", "backup_export": "file:///e"})
    fake.collateral = "backup_export"
    out = fake.eng.restore_registry("/hive", _snap(backups=[("backup_export", "file:///e")]))
    assert out.ok, out.error
    assert fake.backups == {"backup_export": "file:///e"}


def test_restore_registry_reports_a_mismatch_it_cannot_fix(monkeypatch):
    fake = Fake(monkeypatch, remotes={"origin": "file:///a"})
    fake.fail_on = {"add:gone"}
    out = fake.eng.restore_registry(
        "/hive", _snap(remotes=[("origin", "file:///a"), ("gone", "file:///g")])
    )
    assert not out.ok and "missing remote 'gone'" in out.error


# ---- the Git epoch fence follows the remote's family, not the word "origin" --------------------


class _Calls:
    def __init__(self):
        self.reserved: list[str] = []


def _push_harness(monkeypatch, remotes):
    from beadhive import bd, guard, host_fence

    eng = engine.BdEngine()
    seen = _Calls()
    monkeypatch.setattr(bd, "_run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(guard, "primary_state", lambda **kw: ("tt", "host", object()))
    monkeypatch.setattr(
        eng,
        "list_remotes",
        lambda cwd: engine.RemoteListing(
            ok=True, remotes=tuple(engine.DoltRemoteEntry(n, u) for n, u in remotes.items())
        ),
    )
    reservation = host_fence.PushReservation(
        prefix="tt", held="a", fence=host_fence.EpochFence(epoch=1, host_id="h", seq=1)
    )

    def reserve(remote, **kw):
        seen.reserved.append(remote)
        return reservation

    monkeypatch.setattr(host_fence, "reserve_managed_push", reserve)
    monkeypatch.setattr(host_fence, "verify_managed_push", lambda *a, **kw: None)
    return eng, seen


def test_push_uses_the_descriptor_remote_name_for_the_git_fence(monkeypatch):
    eng, seen = _push_harness(monkeypatch, {"hq-private": "git+ssh://git@h/o/r.git"})
    assert eng.push_state("/hive", message="m").returncode == 0  # no name: the sole remote
    assert eng.push_state("/hive", message="m", remote="hq-private").returncode == 0
    assert seen.reserved == ["hq-private", "hq-private"]


def test_push_to_an_object_store_remote_skips_the_git_ref_fence(monkeypatch):
    eng, seen = _push_harness(monkeypatch, {"hq-private": "s3://b/db?endpoint=http://g:3900"})
    assert eng.push_state("/hive", message="m", remote="hq-private").returncode == 0
    assert seen.reserved == []


# ---- transport cache: rebuilt, never copied ---------------------------------------------------


def test_rebuild_is_not_applicable_to_a_non_git_remote(monkeypatch):
    fake = Fake(monkeypatch, remotes={"hq-private": "file:///srv/store"})
    result = fake.eng.rebuild_transport_cache("/hive", "hq-private")
    assert result.state == "not-applicable" and result.ok
    assert fake.calls == [["dolt", "remote", "list", "--json"]]  # no pull attempted


def test_rebuild_clones_the_bare_cache_dolt_names_then_retries_the_pull(monkeypatch, tmp_path):
    cache = tmp_path / "db" / ".dolt" / "git-remote-cache" / "abc123" / "repo.git"
    fake = Fake(monkeypatch, remotes={"hq": "git+ssh://git@h/o/r.git"})
    pulls = iter(
        [
            subprocess.CompletedProcess([], 1, "", f"fatal: not a git repository: '{cache}'"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(fake.eng, "pull_state", lambda cwd, **kw: next(pulls))
    cloned: list[list[str]] = []

    def fake_run(cmd, **kw):
        cloned.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("beadhive.run.run", fake_run)
    result = fake.eng.rebuild_transport_cache("/hive", "hq")
    assert result.state == "rebuilt"
    assert cloned == [["git", "clone", "--bare", "--quiet", "ssh://git@h/o/r.git", str(cache)]]


@pytest.mark.parametrize(
    ("path", "state"),
    [("/etc/passwd.d/repo.git", "failed"), ("{tmp}/git-remote-cache/x/other", "failed")],
)
def test_rebuild_refuses_a_path_that_is_not_a_dolt_cache(monkeypatch, tmp_path, path, state):
    fake = Fake(monkeypatch, remotes={"hq": "git+ssh://git@h/o/r.git"})
    bad = path.format(tmp=tmp_path)
    err = subprocess.CompletedProcess([], 1, "", f"not a git repository: '{bad}'")
    monkeypatch.setattr(fake.eng, "pull_state", lambda cwd, **kw: err)
    monkeypatch.setattr("beadhive.run.run", lambda *a, **kw: pytest.fail("must not clone"))
    assert fake.eng.rebuild_transport_cache("/hive", "hq").state == state


def test_rebuild_reports_a_pull_failure_that_is_not_a_missing_cache(monkeypatch):
    fake = Fake(monkeypatch, remotes={"hq": "git+ssh://git@h/o/r.git"})
    err = subprocess.CompletedProcess([], 1, "", "permission denied")
    monkeypatch.setattr(fake.eng, "pull_state", lambda cwd, **kw: err)
    result = fake.eng.rebuild_transport_cache("/hive", "hq")
    assert result.state == "failed" and "permission denied" in result.detail


def test_remote_channel_is_per_remote_family(monkeypatch):
    fake = Fake(monkeypatch, remotes={"g": "git+ssh://git@h/o/r.git", "f": "file:///srv/x"})
    assert fake.eng.remote_channel("/hive", "g") == "refs/dolt/data"
    assert fake.eng.remote_channel("/hive", "f").startswith("file:")
    assert fake.eng.remote_channel("/hive", "nope") == "unknown"


# ---- real bd + dolt, file:// only ---------------------------------------------------------------


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True, capture=True)
    return path


@pytest.fixture
def embedded(monkeypatch):
    for key in set(__import__("os").environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")


@pytest.mark.integration
@skip_if_no_bd
def test_real_registry_read_conflicts_cutover_and_exact_restore(tmp_path, embedded):
    eng = engine.BdEngine()
    seed = _git_repo(tmp_path / "seed")
    run(["bd", "init", "--prefix", "rg", "--quiet"], cwd=str(seed), check=True, capture=True)
    old, new, bk = (f"file://{tmp_path / n}" for n in ("old", "new", "bk"))

    assert eng.add_remote(seed, "hq-private", old).returncode == 0
    assert eng.add_backup(seed, bk).returncode == 0
    read = eng.read_registry(seed)
    assert read.ok
    assert {e.name: e.url for e in read.snapshot.remotes} == {"hq-private": old}
    # bd may also auto-register its own `backup_export` slot; the slot we asked for is exact.
    assert read.snapshot.backup("default").url == bk

    # Conflicts, both directions, against the real Dolt registry.
    assert eng.add_remote(seed, "export", bk).returncode == 1
    assert eng.add_backup(seed, old).returncode == 1
    assert eng.remote_channel(seed, "hq-private").startswith("file:")

    before = eng.read_registry(seed).snapshot
    out = eng.cutover_remote(seed, "hq-private", new, retain_as="retained-t1")
    assert out.ok, out.error
    assert {e.name: e.url for e in out.after.remotes} == {"hq-private": new, "retained-t1": old}
    assert out.after.backup("default").url == bk
    assert "origin" not in {e.name for e in out.after.remotes}

    # Rollback is a registry operation: put the captured snapshot back, exactly.
    run(
        ["dolt", "backup", "add", "extra", f"file://{tmp_path / 'xb'}"],
        cwd=str(tmp_path / "seed/.beads/embeddeddolt/rg"),
        check=True,
        capture=True,
    )
    restored = eng.restore_registry(seed, before)
    assert restored.ok, restored.error
    final = eng.read_registry(seed).snapshot
    assert before.diff(final).empty
    assert final.backup("extra") is None
