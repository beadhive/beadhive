"""HQ remote policy applies before onboarding's Git-origin auto-detection (bh-b1scz.6).

Hermetic: the Engine and every bd/git process are faked. The real-bd clone semantics this relies
on (full history, failure on an empty or missing remote, no leftover hint) are proven against the
pinned binary in tests/test_engine_remotes.py.
"""

from __future__ import annotations

import subprocess

import pytest
import typer

from beadhive import engine, hive, onboard, store_locator

URL = "s3://acme-beads/beads/github/acme/widget"
AWS = {"AWS_ACCESS_KEY_ID": "AKIAFIXTURE", "AWS_SECRET_ACCESS_KEY": "fixture-secret"}


def _cfg(descriptor=None):
    entry = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "widget"}
    if descriptor is not None:
        entry["dolt_remote"] = descriptor
    return {"managed_repos": [entry]}


def _ctx(tmp_path, cfg, *, furnish=False):
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(tmp_path),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(tmp_path),
        cfg=cfg,
        prefix="widget",
        furnish=furnish,
    )
    ctx._derived = True
    return ctx


def _done(rc=0, stderr=""):
    return subprocess.CompletedProcess(["bd"], rc, stdout="", stderr=stderr)


class FakeEngine:
    def __init__(self, *, clone_rc=0, push_rc=0, remotes=None):
        self.clone_rc, self.push_rc = clone_rc, push_rc
        self.remotes = dict(remotes or {})
        self.calls: list[tuple] = []

    def clone_remote(self, cwd, url, *, env=None, extra_env=None):
        self.calls.append(("clone", url, dict(env or {}), dict(extra_env or {})))
        if not self.clone_rc:
            self.remotes["origin"] = url
            return _done()
        return _done(self.clone_rc, f"Error: clone {url}: remote at that url contains no Dolt data")

    def get_remote(self, cwd, name):
        url = self.remotes.get(name)
        return engine.DoltRemoteEntry(name, url) if url else None

    def add_remote(self, cwd, name, url, *, extra_env=None):
        self.calls.append(("add", name, url))
        self.remotes[name] = url
        return _done()

    def replace_remote(self, cwd, name, url, *, extra_env=None):
        self.calls.append(("replace", name, url))
        self.remotes[name] = url
        return _done()

    def remove_remote(self, cwd, name):
        self.calls.append(("remove", name))
        self.remotes.pop(name, None)
        return _done()

    def push_state(self, cwd, actor="", message="", *, remote="", force=False, extra_env=None):
        self.calls.append(("push", remote, force, dict(extra_env or {})))
        return _done(self.push_rc, "rejected: non-fast-forward" if self.push_rc else "")


@pytest.fixture
def wired(monkeypatch):
    """Fake every neighbour; record bd argv and cleanups."""
    bd_calls: list[list[str]] = []
    cleanups: list[object] = []
    fake = FakeEngine()
    for key, value in AWS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(onboard.SEED_REMOTE_ENV, raising=False)
    monkeypatch.setattr(engine, "get_engine", lambda cfg=None: fake)
    monkeypatch.setattr(onboard, "_configure_auto_export", lambda ctx: None)
    monkeypatch.setattr(onboard, "_bypass_gh2455_dirty_config", lambda ctx: None)
    monkeypatch.setattr(onboard, "_ensure_server_mode_persisted", lambda ctx: None)
    monkeypatch.setattr(hive, "_ensure_bd_backup_gitignored", lambda base: False)
    monkeypatch.setattr(hive, "cleanup_failed_bd_init", lambda base: cleanups.append(base))
    monkeypatch.setattr(store_locator, "ensure_server_mode_persisted", lambda base: False)

    def guard(ctx):
        raise AssertionError("descriptor path must not consult Git push access / sync.remote")

    def probe(ctx):
        raise AssertionError("HQ policy must win before any Git-origin probe")

    monkeypatch.setattr(onboard, "_guard_beads_remote", guard)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", probe)

    def fake_run(args, cwd, **kwargs):
        bd_calls.append(list(args))
        return _done()

    monkeypatch.setattr(onboard.bd_mod, "run", fake_run)
    return fake, bd_calls, cleanups


def test_absent_descriptor_keeps_the_git_origin_path(tmp_path, monkeypatch, wired):
    fake, bd_calls, _ = wired
    probed = []
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: probed.append(1) or False)
    monkeypatch.setattr(onboard, "_guard_beads_remote", lambda ctx: None)
    monkeypatch.setattr(hive, "_relocate_bd_gitignore", lambda base: False)

    onboard._act_bd_init(_ctx(tmp_path, _cfg()))

    assert probed == [1] and fake.calls == []
    assert bd_calls[0][:2] == ["init", "--prefix"]


def test_descriptor_clones_full_history_before_any_origin_probe(tmp_path, wired):
    fake, bd_calls, cleanups = wired

    onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": URL})))

    kind, url, env, extra = fake.calls[0]
    assert (kind, url) == ("clone", URL)
    assert env["BEADS_DOLT_SHARED_SERVER"] == "1"
    assert extra == {}  # ambient credentials stay in the inherited environment
    assert bd_calls == [] and cleanups == []


def test_clone_failure_is_never_empty_names_the_phase_and_leaves_nothing(tmp_path, wired, capsys):
    fake, bd_calls, cleanups = wired
    fake.clone_rc = 1

    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": URL})))

    err = capsys.readouterr().err
    assert "phase: clone" in err and "never treated as an empty remote" in err
    assert f"{onboard.SEED_REMOTE_ENV}=widget" in err
    assert cleanups == [tmp_path]
    assert bd_calls == [] and [c[0] for c in fake.calls] == ["clone"]  # no fresh mint


@pytest.mark.parametrize("url", ["gs://acme/widget", "s3://b/w?endpoint=https://minio:9000"])
def test_unsupported_family_fails_in_preflight_without_touching_anything(
    tmp_path, wired, url, capsys
):
    fake, bd_calls, cleanups = wired
    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": url})))
    assert "phase: preflight" in capsys.readouterr().err
    assert fake.calls == [] and bd_calls == [] and cleanups == []


def test_missing_credentials_fail_before_mutation(tmp_path, wired, monkeypatch, capsys):
    fake, _, cleanups = wired
    for key in AWS:
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": "aws://[t:b]/widget"})))
    assert "phase: credentials" in capsys.readouterr().err
    assert fake.calls == [] and cleanups == []


def test_a_non_origin_name_is_registered_and_the_bootstrap_alias_dropped(tmp_path, wired):
    fake, _, _ = wired
    onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": URL, "name": "beads"})))
    assert ("add", "beads", URL) in fake.calls and ("remove", "origin") in fake.calls
    assert fake.remotes == {"beads": URL}


def test_seed_needs_this_hive_named_explicitly(tmp_path, wired, monkeypatch):
    fake, bd_calls, _ = wired
    monkeypatch.setenv(onboard.SEED_REMOTE_ENV, "some-other-hive")
    onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": URL})))
    assert fake.calls[0][0] == "clone" and bd_calls == []


def test_explicit_seed_mints_registers_and_pushes_without_force(tmp_path, wired, monkeypatch):
    fake, bd_calls, cleanups = wired
    monkeypatch.setenv(onboard.SEED_REMOTE_ENV, "widget")
    monkeypatch.setattr(onboard, "_run_bd_mint", lambda cmd, ctx, env: bd_calls.append(cmd[1:]))
    fake.remotes["origin"] = "git+ssh://git@github.com/acme/widget.git"  # wired by bd init

    onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": URL})))

    assert bd_calls[0][:2] == ["init", "--prefix"] and "--setup-exclude" in bd_calls[0]
    assert ["config", "unset", "sync.remote"] in bd_calls
    assert ("replace", "origin", URL) in fake.calls
    assert ("push", "origin", False, {}) in fake.calls
    assert not any(call[0] == "clone" for call in fake.calls) and cleanups == []


def test_seed_into_a_populated_remote_is_rejected_and_cleaned_up(
    tmp_path, wired, monkeypatch, capsys
):
    fake, bd_calls, cleanups = wired
    monkeypatch.setenv(onboard.SEED_REMOTE_ENV, "github/acme/widget")
    monkeypatch.setattr(onboard, "_run_bd_mint", lambda cmd, ctx, env: bd_calls.append(cmd[1:]))
    fake.push_rc = 1

    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": URL})))

    assert "phase: seed-push" in capsys.readouterr().err
    assert cleanups == [tmp_path]
