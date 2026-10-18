"""Scheme-neutral Dolt remote lifecycle on the Engine seam (bh-b1scz.3).

Unit tests drive ``BdEngine`` through a recording ``invoke`` fake; the integration tests at the
bottom run the real pinned ``bd`` against ``file://`` remotes (no network, no cloud).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from beadhive import dolt_remote, engine
from beadhive.dolt_remote import RemoteFamily, RemoteOp, SupportLevel
from beadhive.run import run
from harness.beads import embedded_env, skip_if_no_bd

# ---- URL family contract ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "family", "support"),
    [
        ("git+ssh://git@github.com/acme/api.git", RemoteFamily.GIT, SupportLevel.IMPLEMENTED),
        ("git+https://github.com/acme/api.git", RemoteFamily.GIT, SupportLevel.IMPLEMENTED),
        ("file:///srv/remotes/api", RemoteFamily.FILE, SupportLevel.IMPLEMENTED),
        ("s3://acme-beads/beads/api", RemoteFamily.S3, SupportLevel.IMPLEMENTED),
        ("aws://[tbl:acme-beads]/api", RemoteFamily.AWS, SupportLevel.IMPLEMENTED),
        # S3-compatible: promoted by bh-82gpi's end-to-end proof (moto server mode).
        ("s3://b/api?endpoint=https://s3.example:9000", RemoteFamily.S3, SupportLevel.IMPLEMENTED),
        ("gs://acme-beads/api", RemoteFamily.GS, SupportLevel.EXTENSION_POINT),
        (
            "https://doltremoteapi.dolthub.com/a/b",
            RemoteFamily.HOSTED,
            SupportLevel.EXTENSION_POINT,
        ),
        ("http://doltlab.local:50051/a/b", RemoteFamily.HOSTED, SupportLevel.EXTENSION_POINT),
        ("az://acct/container/db", RemoteFamily.UNKNOWN, SupportLevel.UNSUPPORTED),
        ("future://host/db", RemoteFamily.UNKNOWN, SupportLevel.UNSUPPORTED),
    ],
)
def test_families_classify_by_url_not_enum(url, family, support):
    target = dolt_remote.classify(url)
    assert (target.family, target.capabilities.support) == (family, support)
    # Only Git carries refs/dolt/data and the Git-ref epoch fence; nothing else pretends to.
    assert target.capabilities.git_ref_fence is (family == RemoteFamily.GIT)
    assert ("refs/dolt/data" in target.describe()) is (family == RemoteFamily.GIT)


def test_unknown_scheme_is_never_git():
    target = dolt_remote.classify("ssh-ish://host/repo.git")
    assert target.family == RemoteFamily.UNKNOWN
    assert not target.allows(RemoteOp.ADD) and not target.allows(RemoteOp.PUSH)
    assert target.allows(RemoteOp.REMOVE)  # a stale registration can always be dropped


def test_malformed_url_is_a_typed_unsupported_target():
    target = dolt_remote.classify("s3://AKIA:secret@bucket/db")
    assert target.error == "dolt_remote_embedded_credential"
    assert not target.supported
    assert "secret" not in target.describe()


def test_same_remote_normalises_location_and_case():
    assert dolt_remote.same_remote("s3://Bucket/db/", "s3://bucket/db")
    assert not dolt_remote.same_remote("s3://bucket/db", "s3://bucket/db2")
    assert dolt_remote.same_remote(
        "git+ssh://git@GitHub.com/a/b.git", "git+ssh://git@github.com/a/b.git"
    )
    assert not dolt_remote.same_remote("file:///a", "s3://a/x")


# ---- Engine lifecycle (fake bd) ---------------------------------------------------------


class Recorder:
    def __init__(self, *results):
        self.calls: list[tuple[list[str], dict]] = []
        self.results = list(results)

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        if self.results:
            return self.results.pop(0)
        return subprocess.CompletedProcess(args=["bd", *args], returncode=0, stdout="", stderr="")


def _ok(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(args=["bd"], returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def bd_engine(monkeypatch):
    eng = engine.BdEngine()
    rec = Recorder()
    monkeypatch.setattr(eng, "invoke", rec)
    return eng, rec


def test_list_remotes_parses_verified_shape(bd_engine):
    eng, rec = bd_engine
    rec.results = [_ok(json.dumps([{"name": "origin", "url": "s3://b/db", "status": "ok"}]))]
    listing = eng.list_remotes("/hive")
    assert listing.ok and listing.get("origin") == engine.DoltRemoteEntry("origin", "s3://b/db")
    assert rec.calls[0][0] == ["dolt", "remote", "list", "--json"]


@pytest.mark.parametrize(
    ("result", "error"),
    [(_ok(rc=124), "timeout"), (_ok("nope"), "parse-error"), (_ok(rc=1, stderr="boom"), "boom")],
)
def test_list_remotes_failure_is_unknown_never_empty(bd_engine, result, error):
    eng, rec = bd_engine
    rec.results = [result]
    listing = eng.list_remotes("/hive")
    assert not listing.ok and listing.error == error and listing.remotes == ()
    rec.results = [result]
    assert eng.get_remote("/hive", "origin") is None


@pytest.mark.parametrize(
    "url",
    ["git+ssh://git@github.com/a/b.git", "file:///srv/r", "s3://b/db", "aws://[t:b]/db"],
)
def test_add_remote_runs_bd_for_implemented_families(bd_engine, url):
    eng, rec = bd_engine
    rec.results = [_ok("[]")]
    result = eng.add_remote("/hive", "origin", url, extra_env={"AWS_PROFILE": "p"})
    assert result.returncode == 0
    args, kwargs = rec.calls[1]
    assert args == ["dolt", "remote", "add", "origin", url]
    assert kwargs["env"]["AWS_PROFILE"] == "p"


def test_add_remote_is_idempotent_for_the_same_url(bd_engine):
    eng, rec = bd_engine
    rec.results = [_ok(json.dumps([{"name": "origin", "url": "s3://B/db/"}]))]
    assert eng.add_remote("/hive", "origin", "s3://b/db").returncode == 0
    assert len(rec.calls) == 1  # listed, never re-added


def test_add_remote_never_silently_repoints_an_existing_name(bd_engine):
    """bd 1.3.0's `dolt remote add` re-points an existing name and exits 0 (measured)."""
    eng, rec = bd_engine
    rec.results = [_ok(json.dumps([{"name": "origin", "url": "git+ssh://git@github.com/a.git"}]))]
    result = eng.add_remote("/hive", "origin", "s3://b/db")
    assert result.returncode == 1 and "not replaced" in result.stderr
    assert len(rec.calls) == 1


def test_add_remote_refuses_when_the_registration_is_unreadable(bd_engine):
    eng, rec = bd_engine
    rec.results = [_ok(rc=124)]
    result = eng.add_remote("/hive", "origin", "s3://b/db")
    assert result.returncode == 1 and "timeout" in result.stderr
    assert len(rec.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://doltremoteapi.dolthub.com/a/b",
        "gs://b/db",
        "future://h/db",
    ],
)
@pytest.mark.parametrize("op", ["add", "replace", "clone"])
def test_unsupported_operations_fail_explicitly_without_running_bd(bd_engine, tmp_path, url, op):
    eng, rec = bd_engine
    if op == "add":
        result = eng.add_remote(tmp_path, "origin", url)
    elif op == "replace":
        result = eng.replace_remote(tmp_path, "origin", url)
    else:
        result = eng.clone_remote(tmp_path, url)
    assert result.returncode == 2
    assert isinstance(result.unsupported, dolt_remote.UnsupportedRemoteOperation)
    assert "unsupported remote operation" in result.stderr
    assert rec.calls == []
    assert not (tmp_path / ".beads").exists()


def test_remove_remote_only_edits_configuration(bd_engine):
    eng, rec = bd_engine
    eng.remove_remote("/hive", "origin")
    assert rec.calls[0][0] == ["dolt", "remote", "remove", "origin"]


def test_replace_remote_restores_the_old_url_when_the_add_fails(bd_engine):
    eng, rec = bd_engine
    rec.results = [
        _ok(json.dumps([{"name": "origin", "url": "file:///old"}])),  # list
        _ok(rc=1, stderr="add failed"),  # re-point
        _ok(),  # restore old
    ]
    result = eng.replace_remote("/hive", "origin", "file:///new")
    assert result.returncode == 1 and "restored the previous URL" in result.stderr
    assert [call[0] for call in rec.calls] == [
        ["dolt", "remote", "list", "--json"],
        ["dolt", "remote", "add", "origin", "file:///new"],
        ["dolt", "remote", "add", "origin", "file:///old"],
    ]


def test_clone_remote_refuses_an_initialised_store(bd_engine, tmp_path):
    eng, rec = bd_engine
    (tmp_path / ".beads" / "embeddeddolt").mkdir(parents=True)
    result = eng.clone_remote(tmp_path, "file:///srv/r")
    assert result.returncode == 1 and "already-initialised" in result.stderr
    assert rec.calls == []


@pytest.mark.parametrize(
    ("metadata", "refused"),
    [
        ({"dolt_mode": "server", "dolt_database": "widget"}, False),  # pre-clone identity stamp
        ({"dolt_mode": "server", "dolt_database": "widget", "project_id": "p"}, True),
        ({"database": "dolt", "backend": "dolt"}, True),
    ],
)
def test_clone_remote_admits_only_a_bare_identity_stamp(bd_engine, tmp_path, metadata, refused):
    eng, rec = bd_engine
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "metadata.json").write_text(json.dumps(metadata))
    result = eng.clone_remote(tmp_path, "file:///srv/r")
    assert ("already-initialised" in (result.stderr or "")) is refused
    assert bool(rec.calls) is not refused


def test_clone_remote_hands_bd_a_one_call_hint_and_removes_it(bd_engine, tmp_path):
    eng, rec = bd_engine
    beads = tmp_path / ".beads"
    beads.mkdir()
    (beads / "config.yaml").write_text("# keep me\nexport.auto: true\n")
    seen = {}

    def fake(args, **kwargs):
        seen["config"] = (beads / "config.yaml").read_text()
        seen["args"], seen["kwargs"] = args, kwargs
        return _ok(rc=1, stderr="clone failed; remote at that url contains no Dolt data")

    eng.invoke = fake
    result = eng.clone_remote(tmp_path, "s3://b/db", extra_env={"AWS_PROFILE": "p"})
    assert result.returncode == 1
    assert seen["args"] == ["bootstrap", "--non-interactive"]
    assert 'sync.remote: "s3://b/db"' in seen["config"]
    assert seen["kwargs"]["pin_process_cwd"] and not seen["kwargs"]["hive_aware"]
    assert seen["kwargs"]["env"]["AWS_PROFILE"] == "p"
    assert (beads / "config.yaml").read_text() == "# keep me\nexport.auto: true\n"


def test_push_and_pull_carry_extra_env_only_to_the_child(bd_engine, monkeypatch):
    eng, rec = bd_engine
    eng.pull_state("/hive", extra_env={"AWS_PROFILE": "p"})
    assert rec.calls[0][1]["env"]["AWS_PROFILE"] == "p"
    eng.pull_state("/hive")
    assert rec.calls[1][1]["env"] is None


# ---- real bd, file:// remotes --------------------------------------------------------------


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
def test_real_bd_remote_lifecycle_over_file_remotes(tmp_path, embedded):
    eng = engine.BdEngine()
    seed = _git_repo(tmp_path / "seed")
    run(["bd", "init", "--prefix", "rl", "--quiet"], cwd=str(seed), check=True, capture=True)
    store = tmp_path / "remote-a"
    url = f"file://{store}"

    assert eng.list_remotes(seed).remotes == ()
    assert eng.add_remote(seed, "origin", url).returncode == 0
    assert eng.get_remote(seed, "origin") == engine.DoltRemoteEntry("origin", url)
    assert eng.add_remote(seed, "origin", url).returncode == 0  # same URL: idempotent
    other = f"file://{tmp_path / 'remote-b'}"
    refused = eng.add_remote(seed, "origin", other)
    assert refused.returncode == 1 and eng.get_remote(seed, "origin").url == url

    assert eng.replace_remote(seed, "origin", other).returncode == 0
    assert eng.get_remote(seed, "origin").url == other
    assert eng.remove_remote(seed, "origin").returncode == 0
    assert eng.list_remotes(seed).remotes == ()

    eng.add_remote(seed, "origin", url)
    run(["bd", "create", "lifecycle proof", "-t", "task"], cwd=str(seed), check=True, capture=True)
    run(["bd", "dolt", "push"], cwd=str(seed), check=True, capture=True)

    fresh = _git_repo(tmp_path / "fresh")
    cloned = eng.clone_remote(fresh, url)
    assert cloned.returncode == 0, cloned.stderr
    assert eng.get_remote(fresh, "origin").url == url
    titles = json.loads(
        run(["bd", "list", "--json"], cwd=str(fresh), check=True, capture=True).stdout
    )
    assert [row["title"] for row in titles] == ["lifecycle proof"]
    config = (
        (fresh / ".beads" / "config.yaml").read_text()
        if (fresh / ".beads" / "config.yaml").is_file()
        else ""
    )
    assert "sync.remote" not in config  # the URL never lands in a (possibly tracked) file


@pytest.mark.integration
@skip_if_no_bd
def test_real_bd_clone_of_an_empty_or_missing_remote_fails_and_mints_nothing(tmp_path, embedded):
    eng = engine.BdEngine()
    for name, url in (
        ("empty", f"file://{(tmp_path / 'empty-store').mkdir() or tmp_path / 'empty-store'}"),
        ("missing", f"file://{tmp_path / 'no' / 'such'}"),
    ):
        fresh = _git_repo(tmp_path / name)
        result = eng.clone_remote(fresh, url)
        assert result.returncode != 0
        assert "no Dolt data" in (result.stderr + result.stdout)
        assert not (fresh / ".beads" / "config.yaml").exists()
