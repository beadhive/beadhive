"""Factory HQ — the durable central store.

HQ is the one durable central store: the aggregation primary (superseding the disposable
``~/.ws/hub``) that ALSO holds canonical hq-prefixed control-plane beads. A SINGLETON (kind=hq)
registered ONLY in the ws registry under the reserved synthetic identity ``local/factory/hq``.

Contract pinned here:
  * ``config.hq_dir()`` → ``~/.ws/hq`` (``$WS_HQ`` override), mirroring ``hub_dir()``;
  * registry gains kind=hq — ``classify``/``derive_prefix`` recognize it, ``rig_of_kind`` resolves
    the singleton, and ``rig_dir`` special-cases it to ``hq_dir()`` (NOT the $GIT_WORKSPACE path);
  * ``ws hq init`` stands up the store, registers the synthetic identity, moves aggregation onto
    HQ (``hub.sync``), and ENFORCES the singleton (refuses a second HQ);
  * the synthetic identity keeps ``ws rig ls`` / ``ws labels validate`` green.

The unit tests stub the bd-touching seams (``hub.ensure_store`` / ``hub.sync``); a real-bd test
(self-skips without the binary) proves the store is a genuine git+bd repo with prefix ``hq``.
"""

from __future__ import annotations

import pytest
import typer

from harness.beads import skip_if_no_bd
from ws import config, hq, hub, registry, validate

# ---- config.hq_dir() --------------------------------------------------------


def test_hq_dir_defaults_under_ws_home(world):
    # WS_HOME is the world's isolated ws home; hq lives beside hub/cache under it.
    assert config.hq_dir() == config.home() / "hq"


def test_hq_dir_env_override_wins(world, monkeypatch):
    monkeypatch.setenv("WS_HQ", "/tmp/elsewhere/hq")
    assert str(config.hq_dir()) == "/tmp/elsewhere/hq"


# ---- registry: kind=hq ------------------------------------------------------


def _hq_entry():
    return {
        "provider": registry.HQ_PROVIDER, "org": registry.HQ_ORG,
        "repo": registry.HQ_REPO, "prefix": registry.HQ_PREFIX, "kind": registry.HQ_KIND,
    }


def test_classify_reserved_triplet_is_hq():
    assert registry.classify(*registry.HQ_TRIPLET, cfg={}) == registry.HQ_KIND


def test_derive_prefix_hq_is_reserved_singleton():
    pref, warns = registry.derive_prefix(
        *registry.HQ_TRIPLET, kind=registry.HQ_KIND, cfg={"managed_repos": []}
    )
    assert pref == registry.HQ_PREFIX
    assert warns == []


def test_rig_of_kind_resolves_singleton():
    cfg = {"managed_repos": [
        {"provider": "github", "org": "a", "repo": "b", "prefix": "ab", "kind": "personal"},
        _hq_entry(),
    ]}
    entry = registry.rig_of_kind(cfg, registry.HQ_KIND)
    assert entry is not None and str(entry["prefix"]) == registry.HQ_PREFIX
    assert registry.rig_of_kind({"managed_repos": []}, registry.HQ_KIND) is None


def test_rig_dir_special_cases_hq_to_hq_dir(world):
    # kind=hq resolves to hq_dir(), NOT $GIT_WORKSPACE/local/factory/hq.
    assert registry.rig_dir(_hq_entry()) == config.hq_dir()
    # a normal rig still path-derives under $GIT_WORKSPACE.
    normal = {"provider": "github", "org": "a", "repo": "b", "prefix": "ab", "kind": "personal"}
    assert registry.rig_dir(normal).name == "b"
    assert config.hq_dir() not in registry.rig_dir(normal).parents


# ---- ws hq init -------------------------------------------------------------


def _stub_store_and_sync(monkeypatch, sync_result=None):
    """Stub the two bd-touching seams so hq.init runs without a real bd/store.

    ``ensure_store`` returns the requested dir (records the (dir, prefix) call);
    ``sync`` records that it ran and returns ``sync_result`` (default: no failures)."""
    calls = {"ensure": [], "sync": 0}

    def fake_ensure_store(store, prefix):
        calls["ensure"].append((store, prefix))
        return store

    def fake_sync():
        calls["sync"] += 1
        return list(sync_result or [])

    monkeypatch.setattr(hub, "ensure_store", fake_ensure_store)
    monkeypatch.setattr(hub, "sync", fake_sync)
    return calls


def test_hq_init_registers_synthetic_identity_and_aggregates(world, monkeypatch):
    calls = _stub_store_and_sync(monkeypatch)

    hq.init()

    # the store was stood up at hq_dir() with the reserved prefix …
    assert calls["ensure"] == [(config.hq_dir(), registry.HQ_PREFIX)]
    # … and aggregation moved onto HQ (hub.sync ran once).
    assert calls["sync"] == 1

    entry = registry.rig_of_kind(config.load(), registry.HQ_KIND)
    assert entry is not None
    assert (str(entry["provider"]), str(entry["org"]), str(entry["repo"])) == registry.HQ_TRIPLET
    assert str(entry["prefix"]) == registry.HQ_PREFIX
    assert str(entry["kind"]) == registry.HQ_KIND


def test_hq_init_refuses_second_hq_singleton(world, monkeypatch):
    calls = _stub_store_and_sync(monkeypatch)
    hq.init()  # first HQ

    with pytest.raises(typer.Exit) as exc:
        hq.init()  # second HQ — must be refused
    assert exc.value.exit_code == 1

    # the guard tripped before any store/sync work of the second call.
    assert calls["ensure"] == [(config.hq_dir(), registry.HQ_PREFIX)]
    assert calls["sync"] == 1
    # still exactly one HQ registered.
    hqs = [e for e in config.load().get("managed_repos", []) if str(e.get("kind")) == "hq"]
    assert len(hqs) == 1


def test_hq_init_creates_store_before_registering(world, monkeypatch):
    """A store-init failure must NOT leave a dangling HQ registration (create-then-register)."""
    def boom(store, prefix):
        raise typer.Exit(1)

    monkeypatch.setattr(hub, "ensure_store", boom)
    monkeypatch.setattr(hub, "sync", lambda: pytest.fail("sync must not run after ensure fails"))

    with pytest.raises(typer.Exit):
        hq.init()
    assert registry.rig_of_kind(config.load(), registry.HQ_KIND) is None


def test_hq_init_propagates_sync_failure(world, monkeypatch):
    _stub_store_and_sync(monkeypatch, sync_result=["a-rig"])
    with pytest.raises(typer.Exit) as exc:
        hq.init()
    assert exc.value.exit_code == 1


# ---- the synthetic identity stays green -------------------------------------


def test_hq_registration_adds_no_required_violation(world, monkeypatch):
    """The synthetic local/factory/hq identity trips no registry-level (required-org) check."""
    from ws.registry import required_violations

    _stub_store_and_sync(monkeypatch)
    hq.init()
    assert required_violations(config.load()) == []


def test_hq_bead_validates_against_synthetic_identity(world, monkeypatch):
    """A native hq-* bead labelled with the synthetic identity passes the per-issue checks —
    ``ws labels validate`` stays green for HQ's own control-plane beads."""
    import json as _json
    from collections import namedtuple

    _stub_store_and_sync(monkeypatch)
    hq.init()
    cfg = config.load()

    Completed = namedtuple("Completed", "returncode stdout stderr")
    bead = {
        "id": "hq-1",
        "labels": ["provider:local", "org:factory", "repo:hq"],
    }
    monkeypatch.setattr(
        validate, "run", lambda *a, **k: Completed(0, _json.dumps([bead]), "")
    )
    assert validate.has_violations(cfg) is False


def test_rig_ls_shows_hq(world, monkeypatch, capsys):
    from ws import rig

    _stub_store_and_sync(monkeypatch)
    hq.init()
    capsys.readouterr()  # drop init output

    rig.ls()
    out = capsys.readouterr().out
    assert "local/factory/hq" in out


# ---- real bd: the store is a genuine git+bd repo (prefix hq) ----------------


@skip_if_no_bd
def test_ensure_store_stands_up_git_bd_repo_prefix_hq(world):
    """hub.ensure_store (the seam hq.init reuses) bd-inits a real git+bd store at hq_dir()."""
    hqdir = config.hq_dir()
    returned = hub.ensure_store(hqdir, registry.HQ_PREFIX)
    assert returned == hqdir
    assert (hqdir / ".beads").is_dir()  # bd store present
    assert (hqdir / ".git").is_dir()    # git-backed (durable, local infra)
    # idempotent: a second call is a no-op that still returns the dir.
    assert hub.ensure_store(hqdir, registry.HQ_PREFIX) == hqdir


# ---- ws hq intake + ws hub deprecated alias (CLI surface) -------------------


def _stub_hub_for_cli(tmp_path, monkeypatch):
    """Stub the hub seams so CLI commands can run without a real bd store.

    Sets up a minimal on-disk .beads dir and monkeypatches config + hub.run so
    the guard (READ-ONLY) and the store-present check both pass."""
    (tmp_path / ".beads").mkdir(parents=True, exist_ok=True)
    calls = []

    class _Ok:
        returncode = 0

    monkeypatch.setenv("WS_HOME", str(tmp_path))
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    monkeypatch.setattr(hub.config, "hq_dir", lambda: tmp_path)
    monkeypatch.setattr(hub, "run", lambda cmd, **k: calls.append(cmd) or _Ok())
    return calls


def test_hq_intake_calls_hub_intake(tmp_path, monkeypatch):
    """``ws hq intake`` routes to hub.intake() — same aggregate read, no deprecation noise."""
    from typer.testing import CliRunner

    from ws import state
    from ws.cli import app

    calls = _stub_hub_for_cli(tmp_path, monkeypatch)

    # Monkeypatch hub._aggregation_target so it returns our tmp_path store.
    monkeypatch.setattr(hub, "_aggregation_target", lambda: (tmp_path, "hq"))

    res = CliRunner().invoke(app, ["hq", "intake"])

    assert res.exit_code == 0, res.output
    # must not print any deprecation warning
    assert "deprecated" not in res.output
    # hub.run was called with the intake filter args
    assert calls, "hub.run should have been called"
    combined_args = [arg for cmd in calls for arg in cmd]
    assert state.INTAKE_UNTRIAGED in combined_args


def test_hq_intake_forwards_extra_flags(tmp_path, monkeypatch):
    """Extra flags (e.g. --json) forwarded through ``ws hq intake`` reach hub.intake."""
    from typer.testing import CliRunner

    from ws import state
    from ws.cli import app

    calls = _stub_hub_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(hub, "_aggregation_target", lambda: (tmp_path, "hq"))

    res = CliRunner().invoke(app, ["hq", "intake", "--json"])

    assert res.exit_code == 0, res.output
    # --json must appear in the bd command forwarded to hub.run
    combined_args = [arg for cmd in calls for arg in cmd]
    assert "--json" in combined_args
    assert state.INTAKE_UNTRIAGED in combined_args


def test_hub_deprecated_alias_prints_deprecation_note(tmp_path, monkeypatch):
    """``ws hub intake`` prints a deprecation warning (CliRunner mixes stdout+stderr)."""
    from typer.testing import CliRunner

    from ws.cli import app

    _stub_hub_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(hub, "_aggregation_target", lambda: (tmp_path, "hq"))

    res = CliRunner().invoke(app, ["hub", "intake"])

    # CliRunner mixes stdout + stderr in res.output; the deprecation note must appear.
    assert "deprecated" in res.output.lower()
    assert "ws hq" in res.output


def test_hub_deprecated_alias_resolves_same_aggregate_read(tmp_path, monkeypatch):
    """``ws hub intake`` and ``ws hq intake`` route to the same hub.intake() implementation."""
    from typer.testing import CliRunner

    from ws.cli import app

    # Run ws hq intake — capture its bd call args.
    calls_hq = _stub_hub_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(hub, "_aggregation_target", lambda: (tmp_path, "hq"))
    CliRunner().invoke(app, ["hq", "intake"])
    hq_args = [tuple(cmd) for cmd in calls_hq]

    # Reset and run ws hub intake — capture its bd call args.
    calls_hub = _stub_hub_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(hub, "_aggregation_target", lambda: (tmp_path, "hq"))
    CliRunner().invoke(app, ["hub", "intake"])
    hub_args = [tuple(cmd) for cmd in calls_hub]

    # Both must invoke the same bd command sequence (deprecation wrapper excluded).
    assert hq_args == hub_args, (
        f"ws hq intake and ws hub intake produced different bd calls:\n"
        f"  hq: {hq_args}\n  hub: {hub_args}"
    )
