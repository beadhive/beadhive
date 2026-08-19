"""Tests for beadhive.hq.clone — `bh hq clone`, the mirror image of `bh hq init`'s remote-wiring
path: bootstrap a fresh host that has NO local HQ from the remote alone (bh-e0y8.4).

Real `git` builds a "remote" HQ (a bare repo carrying `main`'s scaffolded fleet.yaml/
workspace.toml/hosts/, standing in for an already `hq init`'d + pushed HQ on another host) and
clones from it — mirroring test_hq_remote.py's style. The `bd bootstrap` hydration leg goes
through the SAME Engine seam `hub._fetch_cache` already relies on to hydrate an uncloned hive's
beads from a fresh git clone; it is FAKED here (this repo's convention keeps real-bd usage under
`@pytest.mark.integration` only — see test_hq_remote.py's docstring), with the fake reproducing
`bd bootstrap`'s real, observable effect (materializing `.beads/`) so the post-clone routing
assertions below exercise the real code paths `bh hq bd ready` runs. `bd bootstrap`'s own
git-origin-refs/dolt/data auto-detection is proven directly (real `git` + a real `bd` binary,
no fakes) by test_dolt_embedded_engine_int.py's `_bootstrap_second_clone` and by
test_hq.py's `@skip_if_no_bd` real-store test.

NEVER touches the operator's real ``~/.beadhive/hq`` or a real remote: `world` isolates
`BH_HOME`/`config.hq_dir()` under a pytest tmp_path, and `_patch_remote_urls` redirects the
GitHub-shaped remote `hq.clone` would otherwise derive to a local bare repo under
`world.remotes`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from beadhive import config, hq, hub, registry
from harness.world import git


@pytest.fixture(autouse=True)
def _no_legacy_fleet_keys_in_host(world):
    """`world`'s shared baseline seeds a legacy host-side ``providers: [github]`` — pre-fleet-
    split (bh-e0y8.3/.5) test content that is harmless with no ``fleet.yaml`` around (a host
    that has never joined a fleet keeps a FLEET-classified key in its own config.yaml exactly
    as before — ``config.load()`` only rejects that once a real fleet base exists to diverge
    from). ``hq.clone()`` is precisely the "this host joins the fleet for the first time"
    trigger this bead's regression covers — a real ``fleet.yaml`` lands mid-test — so start
    these tests from a host that genuinely has neither key of its own, matching a fresh host
    that has never registered anything, rather than incidentally re-exercising the unrelated
    pre-existing-host-content case. An explicit ``managed_repos: []`` leaf (even empty) still
    counts as a host override once fleet.yaml exists, so this clears the key entirely rather
    than emptying its value."""
    world.cfg_path.write_text("{}\n")


@pytest.fixture(autouse=True)
def _stub_hub_run(monkeypatch):
    """`hq.clone`'s post-bootstrap `hub.persist_shared_server_mode` step (bh-hpeye) issues a
    real `bd config set` — fake hub.py's own `run` so this file keeps its real-bd-free
    convention (see module docstring), the same way `test_hub.py`'s `ensure_store` tests fake
    it for the identical belt-and-suspenders step. Individual tests are still free to
    re-patch `hub.run` afterward (e.g. to assert on later calls)."""
    monkeypatch.setattr(hub, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, "", ""))


def _patch_remote_urls(monkeypatch, remote_path: Path):
    """Redirect hq's github-shaped remote derivation at a local bare repo — the fixture never
    touches a real GitHub remote."""
    monkeypatch.setattr(
        hq, "_remote_urls", lambda remote: (str(remote_path), f"git+file://{remote_path}")
    )


def _make_remote_hq(world) -> Path:
    """A real bare git remote carrying `main`'s already-scaffolded layout — stands in for an
    HQ another host already `hq init`'d + pushed."""
    remote = world.remotes / "hq.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=world.remotes)
    src = world.tmp / "hq-source"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    git("config", "user.email", "hq@fixture", cwd=src)
    git("config", "user.name", "HQ Fixture", cwd=src)
    (src / "fleet.yaml").write_text("orgs: {}\n")
    (src / "workspace.toml").write_text("# providers\n")
    (src / "hosts").mkdir()
    (src / "hosts" / "README.md").write_text("hosts\n")
    git("add", "-A", cwd=src)
    git("commit", "-qm", "init", cwd=src)
    git("push", "-q", str(remote), "main", cwd=src)
    return remote


class _StubEngine:
    """Fakes `bootstrap`, reproducing `bd bootstrap`'s real observable effect (materializing
    `.beads/`) without a real bd/Dolt round trip — matches test_hq_remote.py's `_StubEngine`
    convention."""

    def __init__(self, *, ok=True):
        self.ok = ok
        self.calls: list[str] = []
        self.envs: list[dict] = []

    def bootstrap(self, cwd, *, env=None):
        self.calls.append(str(cwd))
        self.envs.append(env or {})
        if self.ok:
            (Path(cwd) / ".beads").mkdir(parents=True, exist_ok=True)
        rc = 0 if self.ok else 1
        return subprocess.CompletedProcess(["bd", "bootstrap"], rc, "", "" if self.ok else "boom")


def _stub_engine(monkeypatch, engine_stub):
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)


def _stub_hq_remote(monkeypatch, remote: str):
    monkeypatch.setattr(hq.config, "hq_remote", lambda cfg=None, cwd=None: remote)


# ---- clean-clone path: produces a working HQ ---------------------------------


def test_clone_produces_a_working_hq(world, monkeypatch):
    remote = _make_remote_hq(world)
    _patch_remote_urls(monkeypatch, remote)
    _stub_hq_remote(monkeypatch, "acme/beadhive-hq")
    engine_stub = _StubEngine()
    _stub_engine(monkeypatch, engine_stub)
    hq_dir = config.hq_dir()
    assert not hq_dir.exists()

    hq.clone()

    # main's already-scaffolded layout landed on disk via a real git clone …
    assert (hq_dir / "fleet.yaml").exists()
    assert (hq_dir / "workspace.toml").exists()
    assert (hq_dir / "hosts" / "README.md").exists()
    # … and bead state was hydrated via the SAME seam hub._fetch_cache uses for an uncloned hive.
    assert engine_stub.calls == [str(hq_dir)]
    assert (hq_dir / ".beads").is_dir()


def test_clone_activates_shared_server_on_bootstrap(world, monkeypatch):
    """bh-hpeye: a second host cloning HQ is the same 'second-host case' onboard.py's own
    zero-footprint bootstrap branch activates BEADS_DOLT_SHARED_SERVER=1 for — without it this
    bootstrap landed embedded, the same drift `hub._fetch_cache` had."""
    remote = _make_remote_hq(world)
    _patch_remote_urls(monkeypatch, remote)
    _stub_hq_remote(monkeypatch, "acme/beadhive-hq")
    engine_stub = _StubEngine()
    _stub_engine(monkeypatch, engine_stub)

    hq.clone()

    assert len(engine_stub.envs) == 1
    assert engine_stub.envs[0]["BEADS_DOLT_SHARED_SERVER"] == "1"


def test_clone_reconciles_a_host_config_that_would_collide_with_the_cloned_fleet(
    world, monkeypatch
):
    """bh-w2u9: `bh config init` scaffolds FLEET-classified keys LIVE (the template is written
    for the host that FOUNDS a fleet). A host that CLONES one inherits someone else's
    fleet.yaml, so those copies collide and every later `config.load()` raises — breaking
    effectively every bh command. Clone must reconcile at the moment it creates the conflict."""
    remote = _make_remote_hq(world)
    _patch_remote_urls(monkeypatch, remote)
    _stub_hq_remote(monkeypatch, "acme/beadhive-hq")
    _stub_engine(monkeypatch, _StubEngine())

    # A freshly-templated host config carrying a fleet-classified key, as `config init` leaves it.
    host_cfg = config.load_host()
    host_cfg["delimiter"] = "-"
    config.save(host_cfg)
    assert "delimiter" in config.fleet_override_violations(config.load_host())

    hq.clone()

    # The collision is gone and the config loads again — the whole point.
    assert config.fleet_override_violations(config.load_host()) == []
    config.load()  # must not raise


def test_clone_leaves_a_clean_host_config_untouched(world, monkeypatch):
    """Only reconcile a config that ACTUALLY collides — never a speculative rewrite."""
    remote = _make_remote_hq(world)
    _patch_remote_urls(monkeypatch, remote)
    _stub_hq_remote(monkeypatch, "acme/beadhive-hq")
    _stub_engine(monkeypatch, _StubEngine())
    before = config.config_path().read_bytes()

    hq.clone()

    assert config.config_path().read_bytes() == before


def test_clone_registers_hq_so_bd_ready_targets_the_clone(world, monkeypatch):
    """`bh hq bd ready` reads HQ's OWN store (`hq.query`, since bh-89wxf.2 — it is no longer
    the cross-hive aggregate). Clone must register the synthetic HQ identity and leave a real
    `.beads` at `config.hq_dir()` so that read resolves and runs."""
    remote = _make_remote_hq(world)
    _patch_remote_urls(monkeypatch, remote)
    _stub_hq_remote(monkeypatch, "acme/beadhive-hq")
    _stub_engine(monkeypatch, _StubEngine())
    hq_dir = config.hq_dir()

    hq.clone()

    entry = registry.hive_of_kind(config.load(), registry.HQ_KIND)
    assert entry is not None
    assert (str(entry["provider"]), str(entry["org"]), str(entry["repo"])) == registry.HQ_TRIPLET
    # Aggregation no longer follows HQ around — it is unconditionally the hub (bh-89wxf.2).
    assert hub.hub_target() == (config.hub_dir(), hub.HUB_PREFIX)

    # `bh hq bd ready` == hq.query(["ready"]); its own "store not initialized" guard must now
    # pass (a real `.beads` sits at hq_dir) and the bd call must target hq_dir.
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    # `run_bounded`, not `run`: the aggregate read is bounded since bh-toitp.
    monkeypatch.setattr(hub, "run_bounded", fake_run)
    hq.query(["ready"])
    assert calls == [["bd", "-C", str(hq_dir), "ready"]]


def test_clone_refuses_when_bootstrap_fails(world, monkeypatch):
    """A failed hydration must not leave a half-registered HQ."""
    remote = _make_remote_hq(world)
    _patch_remote_urls(monkeypatch, remote)
    _stub_hq_remote(monkeypatch, "acme/beadhive-hq")
    _stub_engine(monkeypatch, _StubEngine(ok=False))

    with pytest.raises(typer.Exit) as exc:
        hq.clone()

    assert exc.value.exit_code == 1
    assert registry.hive_of_kind(config.load(), registry.HQ_KIND) is None


# ---- already-exists refusal ---------------------------------------------------


def test_clone_refuses_when_hq_dir_already_exists(world, monkeypatch):
    hq_dir = config.hq_dir()
    hq_dir.mkdir(parents=True)
    (hq_dir / "marker").write_text("pre-existing\n")

    with pytest.raises(typer.Exit) as exc:
        hq.clone()

    assert exc.value.exit_code == 1
    assert (hq_dir / "marker").exists()  # untouched — never clobbered
    assert registry.hive_of_kind(config.load(), registry.HQ_KIND) is None


def test_clone_refuses_when_remote_unresolvable(world, monkeypatch):
    _stub_hq_remote(monkeypatch, "")

    with pytest.raises(typer.Exit) as exc:
        hq.clone()

    assert exc.value.exit_code == 1
    assert not config.hq_dir().exists()
