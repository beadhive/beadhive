"""End-to-end proof of HQ-configured Dolt remotes (bh-b1scz.7, bh-82gpi).

The lifecycle runs against the pinned real bd over ``file://`` remotes: distinct paths per hive,
reconcile, a causal bead pushed through ``bh hive sync remotes``, a fresh store cloned from the
declared remote with the same change and the same commit history, an interrupted push retried,
and sync/status/doctor describing the remote honestly.

The S3 leg (bh-82gpi) runs the same lifecycle against an S3-COMPATIBLE endpoint: moto in server
mode on loopback (``harness.s3``), dummy credentials, never real S3. bd's embedded engine does not
speak ``s3://``, so that leg runs bd in server mode on an isolated shared server whose environment
carries the credentials (in server mode the sql-server, not the bd client, performs the transfer).
It skips — never fails — when ``moto[server]`` is not installed.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import cli, config, doctor, dolt_remote, dolt_remote_reconcile, engine
from beadhive.run import run
from harness.beads import embedded_env, skip_if_no_bd
from harness.s3 import moto_s3  # noqa: F401 - pytest fixture
from harness.world import free_port, reap_dolt_server

pytestmark = [pytest.mark.integration, skip_if_no_bd]


def _git(*args, cwd):
    return run(["git", *args], cwd=str(cwd), check=True, capture=True)


def _bd(*args, cwd, check=True):
    return run(["bd", *args], cwd=str(cwd), check=check, capture=True, timeout=180)


def _hive(ws: Path, repo: str, prefix: str, tmp: Path) -> Path:
    path = ws / "github" / "acme" / repo
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "e2e@example.com", cwd=path)
    _git("config", "user.name", "e2e", cwd=path)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    # bd init BEFORE the Git origin exists, so bd does not auto-wire the code repository as
    # the Dolt remote: the state of a hive onboarded under an HQ descriptor.
    _bd("init", "--prefix", prefix, "--quiet", "--skip-hooks", "--skip-agents", cwd=path)
    bare = tmp / "git" / f"{repo}.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp)
    _git("remote", "add", "origin", str(bare), cwd=path)
    _git("push", "-q", "-u", "origin", "main", cwd=path)
    return path


def _write_config(path: Path, remotes: dict[str, Path | str]) -> None:
    def url(remote: Path | str) -> str:
        return remote if isinstance(remote, str) else f"file://{remote}"

    rows = [
        f'  - {{"provider": "github", "org": "acme", "repo": "{repo}", "prefix": "ac-{repo}", '
        f'"kind": "personal", "dolt_remote": {{"url": "{url(remote)}"}}}}'
        for repo, remote in remotes.items()
    ]
    path.write_text("schema_version: 1\nmanaged_repos:\n" + "\n".join(rows) + "\n")


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    for key in set(os.environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    ws = tmp_path / "ws"
    hives = {repo: _hive(ws, repo, f"ac-{repo}", tmp_path) for repo in ("api", "web")}
    remotes = {repo: tmp_path / "dolt-remotes" / "github" / "acme" / repo for repo in hives}
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, remotes)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.setattr(dolt_remote_reconcile, "_leased", lambda cfg, entry: False)
    return hives, remotes, cfg_path


def _invoke(*args):
    result = CliRunner().invoke(cli.app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def _history(path: Path, bead: str) -> list[str]:
    rows = json.loads(_bd("history", bead, "--json", cwd=path).stdout)
    return [row["CommitHash"] for row in rows]


def _clone_fresh(tmp: Path, url: str) -> Path:
    fresh = tmp / f"fresh-{uuid.uuid4().hex[:6]}"
    fresh.mkdir()
    _git("init", "-q", "-b", "main", cwd=fresh)
    cloned = engine.BdEngine().clone_remote(fresh, url)
    assert cloned.returncode == 0, cloned.stderr
    return fresh


def _chmod_tree(root: Path, mode_dirs: int, mode_files: int) -> None:
    for path in [root, *root.rglob("*")]:
        path.chmod(mode_dirs if path.is_dir() else mode_files)


def test_shared_location_is_refused_before_anything_runs(fleet):
    _, remotes, cfg_path = fleet
    _write_config(cfg_path, {"api": remotes["api"], "web": remotes["api"]})
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert "dolt_remote_path_shared" in str(caught.value)


def test_configure_reconcile_sync_bootstrap_and_recover(fleet, tmp_path):
    hives, remotes, _ = fleet
    api = hives["api"]

    # 1. HQ desire is installed (registration only) for each hive at its own path.
    code, out = _invoke("hive", "remote", "--all", "--reconcile")
    assert code == 0, out
    assert out.count(": added\n") == 2
    for repo, path in hives.items():
        assert engine.BdEngine().get_remote(path, "origin").url == f"file://{remotes[repo]}"

    # 2. A causal change, published through the ordinary sync verb.
    bead = _bd("create", "causal change", "-t", "task", "--silent", cwd=api).stdout.strip()
    _bd("update", bead, "--status", "in_progress", cwd=api)
    code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push", "--dry-run")
    assert code == 0, out
    assert f"file remote file://{remotes['api']}" in out and "refs/dolt/data" not in out
    assert "ahead/behind unknown" in out
    code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push")
    assert code == 0, out
    assert "pushed dolt: origin (file remote" in out

    # 3. A fresh store initialised from the declared remote has the change AND its history.
    fresh = _clone_fresh(tmp_path, f"file://{remotes['api']}")
    shown = json.loads(_bd("show", bead, "--json", cwd=fresh).stdout)
    shown = shown[0] if isinstance(shown, list) else shown
    assert shown["status"] == "in_progress"
    assert _history(fresh, bead) == _history(api, bead)

    # 4. An interrupted push (remote unwritable) fails loudly, changes nothing, and a retry
    #    after the fault clears lands the same change.
    second = _bd("create", "after the fault", "-t", "task", "--silent", cwd=api).stdout.strip()
    _chmod_tree(remotes["api"], stat.S_IRUSR | stat.S_IXUSR, stat.S_IRUSR)
    try:
        code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push")
    finally:
        _chmod_tree(remotes["api"], stat.S_IRWXU, stat.S_IRUSR | stat.S_IWUSR)
    assert code == 1 and "failed to push dolt: origin (file remote" in out
    code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push")
    assert code == 0, out
    retried = _clone_fresh(tmp_path, f"file://{remotes['api']}")
    assert _bd("show", second, "--json", cwd=retried, check=False).returncode == 0

    # 5. Status and doctor compare HQ desire with bd's registration, honestly.
    code, out = _invoke("hive", "remote", "--all")
    assert code == 0 and out.count(": in-sync\n") == 2
    assert dolt_remote_reconcile.HEALTH_CAVEAT in out and "refs/dolt/data" not in out
    rows = doctor._data_hq_remotes(config.load())
    assert {row["outcome"] for row in rows} == {"in-sync"}

    # 6. Drift is reported, never repaired by replacement; HQ config is untouched.
    engine.BdEngine().replace_remote(api, "origin", f"file://{tmp_path / 'elsewhere'}")
    code, out = _invoke("hive", "remote", "ac-api", "--reconcile")
    assert code == 1 and "migration-required" in out
    rows = doctor._data_hq_remotes(config.load())
    assert {row["outcome"] for row in rows} == {"in-sync", "migration-required"}


@pytest.fixture
def s3_fleet(tmp_path, monkeypatch, moto_s3):  # noqa: F811 - the imported fixture
    """One SERVER-mode hive whose HQ descriptor is an ``s3://…?endpoint=`` URL on moto.

    Its own shared server (data dir + free port), started with moto's dummy credentials in its
    environment and reaped however the test ends."""
    for key in set(os.environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    server_dir = tmp_path / "shared-server"
    monkeypatch.setenv("BEADS_DOLT_SHARED_SERVER", "1")
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server_dir))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(free_port()))
    for key, value in moto_s3.server_env().items():
        monkeypatch.setenv(key, value)
    ws = tmp_path / "ws"
    api = _hive(ws, "api", "ac-api", tmp_path)
    url = moto_s3.url("internal/github/acme/api")
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {"api": url})
    monkeypatch.setenv("GIT_WORKSPACE", str(ws))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.setattr(dolt_remote_reconcile, "_leased", lambda cfg, entry: False)
    try:
        yield api, url, moto_s3
    finally:
        reap_dolt_server(server_dir)


def _clone_fresh_server(tmp: Path, url: str) -> Path:
    """A fresh server-mode store cloned from ``url`` into its OWN database on the shared server
    (named before ``bd bootstrap`` reads it, as onboarding does — never bd's generic ``beads``)."""
    tag = uuid.uuid4().hex[:6]
    fresh = tmp / f"fresh-{tag}"
    (fresh / ".beads").mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=fresh)
    stamp = {"dolt_database": f"fresh_{tag}", "dolt_mode": "server"}
    (fresh / ".beads" / "metadata.json").write_text(json.dumps(stamp))
    cloned = engine.BdEngine().clone_remote(fresh, url)
    assert cloned.returncode == 0, cloned.stderr
    return fresh


@pytest.mark.dolt_server
def test_s3_leg_round_trip(s3_fleet, tmp_path):
    api, url, s3 = s3_fleet
    target = dolt_remote.classify(url)
    assert target.family == dolt_remote.RemoteFamily.S3 and target.supported

    # 1. HQ desire installs the S3-compatible registration on the server-mode hive.
    code, out = _invoke("hive", "remote", "ac-api", "--reconcile")
    assert code == 0, out
    assert ": added\n" in out
    assert engine.BdEngine().get_remote(api, "origin").url == url

    # 2. A causal change with history, published through the ordinary sync verb.
    bead = _bd("create", "s3 causal change", "-t", "task", "--silent", cwd=api).stdout.strip()
    _bd("update", bead, "--status", "in_progress", cwd=api)
    code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push", "--dry-run")
    assert code == 0, out
    assert "s3 remote s3://" in out and "refs/dolt/data" not in out
    assert "ahead/behind unknown" in out
    code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push")
    assert code == 0, out
    assert "pushed dolt: origin (s3 remote" in out
    assert s3.keys("internal/github/acme/api"), "nothing landed in the bucket"

    # 3. A fresh store cloned from the bucket has the change AND the same commit history.
    #    (bd 1.3.0's server-mode `bd bootstrap` first waits ~70 s for the not-yet-existing
    #    database — 10 + 20 + 40 s "possible transient restart" retries, bh-mi40l — so this
    #    leg clones once and proves the retry below with a pull into the same fresh store.)
    fresh = _clone_fresh_server(tmp_path, url)
    shown = json.loads(_bd("show", bead, "--json", cwd=fresh).stdout)
    shown = shown[0] if isinstance(shown, list) else shown
    assert shown["status"] == "in_progress"
    assert _history(fresh, bead) == _history(api, bead)

    # 4. The endpoint goes away mid-sync: the push fails loudly and lands nothing; once it is
    #    back the retry lands the same change.
    second = _bd("create", "after the outage", "-t", "task", "--silent", cwd=api).stdout.strip()
    before = s3.keys("internal/github/acme/api")
    s3.pause()
    try:
        code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push")
    finally:
        s3.resume()
    assert code == 1 and "failed to push dolt: origin (s3 remote" in out
    assert s3.keys("internal/github/acme/api") == before
    assert _bd("show", second, "--json", cwd=fresh, check=False).returncode != 0
    code, out = _invoke("hive", "sync", "remotes", "ac-api", "--push")
    assert code == 0, out
    _bd("dolt", "pull", cwd=fresh)
    assert _bd("show", second, "--json", cwd=fresh, check=False).returncode == 0
    assert _history(fresh, bead) == _history(api, bead)

    # 5. Status and doctor describe the S3-compatible remote honestly and redact nothing
    #    they should not (the endpoint is addressing, not a secret).
    code, out = _invoke("hive", "remote", "ac-api")
    assert code == 0 and ": in-sync\n" in out
    assert dolt_remote_reconcile.HEALTH_CAVEAT in out and "refs/dolt/data" not in out
    assert "testing" not in out
    rows = doctor._data_hq_remotes(config.load())
    assert {row["outcome"] for row in rows} == {"in-sync"}
