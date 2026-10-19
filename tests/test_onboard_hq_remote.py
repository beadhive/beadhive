"""HQ remote policy applies before onboarding's Git-origin auto-detection (bh-b1scz.6).

Hermetic: the Engine and every bd/git process are faked. The real-bd clone semantics this relies
on (full history, failure on an empty or missing remote, no leftover hint) are proven against the
pinned binary in tests/test_engine_remotes.py.
"""

from __future__ import annotations

import subprocess

import pytest
import typer

from beadhive import engine, hive, hub, onboard, store_locator

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
    monkeypatch.setattr(hub, "_ensure_shared_server_running", lambda base: None)

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


@pytest.mark.parametrize(
    "url", ["gs://acme/widget", "https://doltremoteapi.dolthub.com/acme/widget"]
)
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


# ---- one ordered flow with partitioned stores (bh-rz379) ----------------------------------


def _partitioned(tmp_path, monkeypatch, *, public_remote=True):
    """A GIT_WORKSPACE checkout of acme/widget whose HQ entry declares both stores."""
    from beadhive import store_bootstrap

    workspace = tmp_path / "ws"
    checkout = workspace / "github" / "acme" / "widget"
    checkout.mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(workspace))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    public = {"prefix": "widpub", "path": str(tmp_path / "stores" / "public")}
    if public_remote:
        public["dolt_remote"] = {"name": "widget-pub", "url": "file:///srv/remotes/widget-pub"}
    cfg = _cfg({"name": "widget-private", "url": URL})
    cfg["managed_repos"][0]["stores"] = {"internal": {"remote": "widget-private"}, "public": public}
    mints: list[tuple] = []

    def runner(argv, cwd, env):
        mints.append((list(argv), cwd))
        (cwd / ".beads").mkdir()
        return _done()

    monkeypatch.setattr(store_bootstrap, "_default_runner", runner)
    return checkout, cfg, mints


def test_partitioned_hive_clones_internal_once_through_hq_then_mints_public(
    tmp_path, monkeypatch, wired
):
    fake, bd_calls, _ = wired
    checkout, cfg, mints = _partitioned(tmp_path, monkeypatch)

    def clone(cwd, url, *, env=None, extra_env=None):
        fake.calls.append(("clone", url, dict(env or {}), dict(extra_env or {})))
        (checkout / ".beads").mkdir(exist_ok=True)
        fake.remotes["origin"] = url
        return _done()

    monkeypatch.setattr(fake, "clone_remote", clone)

    onboard._act_bd_init(_ctx(checkout, cfg))

    clones = [c for c in fake.calls if c[0] == "clone"]
    assert [c[1] for c in clones] == [URL]  # exactly one internal clone, from the HQ descriptor
    assert clones[0][2]["BEADS_DOLT_SHARED_SERVER"] == "1"
    assert ("add", "widget-private", URL) in fake.calls  # b1scz registration, then...
    (mint,) = mints  # ...the public store is minted at its detached root, never cloned
    assert mint[1] == tmp_path / "stores" / "public" and "--remote" not in mint[0]
    assert ("add", "widget-pub", "file:///srv/remotes/widget-pub") in fake.calls
    assert not any(c[:2] == ["init", "--remote"] for c in bd_calls)


def test_partitioned_hive_without_hq_descriptor_fails_closed_before_any_probe(
    tmp_path, monkeypatch, wired
):
    fake, _bd_calls, _ = wired
    checkout, cfg, mints = _partitioned(tmp_path, monkeypatch, public_remote=False)
    del cfg["managed_repos"][0]["dolt_remote"]

    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(checkout, cfg))
    assert fake.calls == [] and mints == []
    assert not (checkout / ".beads").exists()


def test_public_remote_registration_failure_names_the_public_phase(
    tmp_path, monkeypatch, wired, capsys
):
    fake, _bd_calls, _ = wired
    checkout, cfg, _mints = _partitioned(tmp_path, monkeypatch)
    (checkout / ".beads").mkdir()  # internal already present: a re-run adds only public

    def add(cwd, name, url, *, extra_env=None):
        fake.calls.append(("add", name, url))
        return _done(1, f"Error: cannot reach {url}")

    monkeypatch.setattr(fake, "add_remote", add)
    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(checkout, cfg))
    err = capsys.readouterr().err
    assert "[public-init]" in err and "widget-pub" in err
    assert not (tmp_path / "stores" / "public" / ".beads").exists()
    assert (checkout / ".beads").is_dir()
    assert [c for c in fake.calls if c[0] == "clone"] == []


def test_hq_clone_names_its_own_shared_server_database_before_bootstrap(
    tmp_path, monkeypatch, wired
):
    """`bd bootstrap` would otherwise land every HQ-cloned hive in bd's generic `beads`
    database on the shared server (bh-g5ujg); the stamp names it from the prefix first."""
    import json

    fake, _bd_calls, _ = wired
    seen = []
    persisted = []

    def clone(cwd, url, *, env=None, extra_env=None):
        seen.append(json.loads((cwd / ".beads" / "metadata.json").read_text()))
        return _done()

    monkeypatch.setattr(fake, "clone_remote", clone)
    monkeypatch.setattr(
        store_locator,
        "ensure_server_database_persisted",
        lambda base, db: persisted.append(db),
    )
    onboard._act_bd_init(_ctx(tmp_path, _cfg({"url": URL})))
    assert seen == [{"dolt_database": "widget", "dolt_mode": "server"}]
    assert persisted == ["widget"]


# ---- two-store hygiene on the HQ-remote paths (bh-lniwf.8) -------------------------------


def _partitioned_cfg(name="widget-private"):
    cfg = _cfg({"name": name, "url": URL})
    cfg["managed_repos"][0]["stores"] = {
        "internal": {"remote": name},
        "public": {
            "prefix": "widpub",
            "path": "/srv/stores/widget-public",
            "source": "github-issues",
        },
    }
    return cfg


def _descriptor(cfg):
    from beadhive.modules.config.contracts import DoltRemoteDescriptor

    return DoltRemoteDescriptor(**cfg["managed_repos"][0]["dolt_remote"])


def test_partitioned_hive_drops_bds_auto_wired_code_origin(tmp_path, capsys):
    cfg = _partitioned_cfg()
    fake = FakeEngine(remotes={"origin": "git+file:///srv/git/widget.git", "widget-private": URL})
    onboard._drop_code_origin_registration(_ctx(tmp_path, cfg), fake, _descriptor(cfg))
    assert fake.calls == [("remove", "origin")]
    assert fake.remotes == {"widget-private": URL}
    assert "dropped the code origin" in capsys.readouterr().out


def test_one_store_hive_also_drops_bds_auto_wired_code_origin(tmp_path):
    # bh-wy2ia: a differently named descriptor must not leave the code origin as `origin`.
    cfg = _cfg({"name": "widget-private", "url": URL})
    fake = FakeEngine(remotes={"origin": "git+file:///srv/w.git", "widget-private": URL})
    onboard._drop_code_origin_registration(_ctx(tmp_path, cfg), fake, _descriptor(cfg))
    assert fake.calls == [("remove", "origin")]
    assert fake.remotes == {"widget-private": URL}


@pytest.mark.parametrize(
    ("cfg", "remotes"),
    [
        # The HQ descriptor IS `origin`: nothing auto-wired to drop.
        (_partitioned_cfg("origin"), {"origin": URL}),
        # A non-Git `origin` is not the code origin's refs/dolt/data.
        (_partitioned_cfg(), {"origin": "file:///srv/elsewhere"}),
        # Nothing registered.
        (_partitioned_cfg(), {}),
    ],
)
def test_code_origin_registration_is_left_alone_otherwise(tmp_path, cfg, remotes):
    fake = FakeEngine(remotes=remotes)
    onboard._drop_code_origin_registration(_ctx(tmp_path, cfg), fake, _descriptor(cfg))
    assert fake.calls == [] and fake.remotes == remotes


def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _ignored(repo, rel):
    return subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", rel]).returncode == 0


def test_zero_footprint_clone_excludes_the_store_and_bds_gate_lock(tmp_path):
    repo = _git_repo(tmp_path / "hive")
    ctx = _ctx(repo, _cfg())
    onboard._exclude_clone_footprint(ctx)
    onboard._exclude_clone_footprint(ctx)  # idempotent
    assert _ignored(repo, ".beads/metadata.json") and _ignored(repo, ".beads.gate.lock")
    exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert exclude.count(hive._GATE_LOCK_PATTERN) == 1
    assert not (repo / ".gitignore").exists()


def test_furnished_clone_footprint_is_not_rewritten(tmp_path):
    repo = _git_repo(tmp_path / "hive")
    before = (repo / ".git" / "info" / "exclude").read_text()
    onboard._exclude_clone_footprint(_ctx(repo, _cfg(), furnish=True))
    assert (repo / ".git" / "info" / "exclude").read_text() == before
