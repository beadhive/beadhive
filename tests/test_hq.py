"""Factory HQ — the durable central store.

HQ is the one durable central store: the aggregation primary (superseding the disposable
``~/.beadhive/hub``) that ALSO holds canonical hq-prefixed control-plane beads. A SINGLETON
(kind=hq) registered ONLY in the bh registry under the reserved synthetic identity
``local/factory/hq``.

Contract pinned here:
  * ``config.hq_dir()`` → ``~/.beadhive/hq`` (``$BH_HQ`` override), mirroring ``hub_dir()``;
  * registry gains kind=hq — ``classify``/``derive_prefix`` recognize it, ``hive_of_kind`` resolves
    the singleton, and ``hive_dir`` special-cases it to ``hq_dir()`` (NOT the $GIT_WORKSPACE path);
  * ``bh hq init`` stands up the store, registers the synthetic identity, moves aggregation onto
    HQ (``hub.sync``), and ENFORCES the singleton (refuses a second HQ);
  * the synthetic identity keeps ``bh hive list`` / ``bh label validate`` green.

The unit tests stub the bd-touching seams (``hub.ensure_store`` / ``hub.sync``); a real-bd test
(self-skips without the binary) proves the store is a genuine git+bd repo with prefix ``hq``.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import config, hq, hub, registry, validate
from harness.beads import skip_if_no_bd

# ---- config.hq_dir() --------------------------------------------------------


def test_hq_dir_defaults_under_bh_home(world):
    # BH_HOME is the world's isolated bh home; hq lives beside hub/cache under it.
    assert config.hq_dir() == config.home() / "hq"


def test_hq_dir_env_override_wins(world, monkeypatch):
    monkeypatch.setenv("BH_HQ", "/tmp/elsewhere/hq")
    assert str(config.hq_dir()) == "/tmp/elsewhere/hq"


# ---- registry: kind=hq ------------------------------------------------------


def _hq_entry():
    return {
        "provider": registry.HQ_PROVIDER,
        "org": registry.HQ_ORG,
        "repo": registry.HQ_REPO,
        "prefix": registry.HQ_PREFIX,
        "kind": registry.HQ_KIND,
    }


def test_classify_reserved_triplet_is_hq():
    assert registry.classify(*registry.HQ_TRIPLET, cfg={}) == registry.HQ_KIND


def test_derive_prefix_hq_is_reserved_singleton():
    pref, warns = registry.derive_prefix(
        *registry.HQ_TRIPLET, kind=registry.HQ_KIND, cfg={"managed_repos": []}
    )
    assert pref == registry.HQ_PREFIX
    assert warns == []


def test_hive_of_kind_resolves_singleton():
    cfg = {
        "managed_repos": [
            {"provider": "github", "org": "a", "repo": "b", "prefix": "ab", "kind": "personal"},
            _hq_entry(),
        ]
    }
    entry = registry.hive_of_kind(cfg, registry.HQ_KIND)
    assert entry is not None and str(entry["prefix"]) == registry.HQ_PREFIX
    assert registry.hive_of_kind({"managed_repos": []}, registry.HQ_KIND) is None


def test_hive_dir_special_cases_hq_to_hq_dir(world):
    # kind=hq resolves to hq_dir(), NOT $GIT_WORKSPACE/local/factory/hq.
    assert registry.hive_dir(_hq_entry()) == config.hq_dir()
    # a normal hive still path-derives under $GIT_WORKSPACE.
    normal = {"provider": "github", "org": "a", "repo": "b", "prefix": "ab", "kind": "personal"}
    assert registry.hive_dir(normal).name == "b"
    assert config.hq_dir() not in registry.hive_dir(normal).parents


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


def test_hq_init_registers_synthetic_identity_and_does_not_aggregate(world, monkeypatch):
    """bh-89wxf.2: standing HQ up has nothing to do with hydrating the fleet. `hub.sync()` used
    to run here to "move the aggregation role onto HQ" — which is exactly what put a derived
    per-host aggregate on HQ's Dolt remote path."""
    calls = _stub_store_and_sync(monkeypatch)

    hq.init()

    # the store was stood up at hq_dir() with the reserved prefix …
    assert calls["ensure"] == [(config.hq_dir(), registry.HQ_PREFIX)]
    # … and NOTHING was aggregated into it.
    assert calls["sync"] == 0

    entry = registry.hive_of_kind(config.load(), registry.HQ_KIND)
    assert entry is not None
    assert (str(entry["provider"]), str(entry["org"]), str(entry["repo"])) == registry.HQ_TRIPLET
    assert str(entry["prefix"]) == registry.HQ_PREFIX
    assert str(entry["kind"]) == registry.HQ_KIND


def test_hq_init_self_heals_a_stale_un_migrated_host_config_before_validating(world, monkeypatch):
    """bh-17eb: `hq.init()` calls the validating `config.load()` itself, before ANYTHING else —
    even the "already initialized" no-op check. A re-run on a host that already joined a fleet
    (a real fleet.yaml exists locally) but whose OWN config.yaml still carries un-migrated
    legacy content (every existing user's pre-0.7.0 flat config) used to hard-fail right there,
    before `_wire_remote`/`scaffold_layout` ever got a chance to run."""
    calls = _stub_store_and_sync(monkeypatch)
    # world's own baseline config.yaml IS the un-migrated legacy shape: a flat `providers:
    # [github]` sitting host-side with no split awareness at all.
    assert "providers" in config.load_host()
    path = config.fleet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("orgs: {}\n")  # a real fleet.yaml already exists on this host

    hq.init()  # must not raise ConfigError

    assert calls["ensure"] == [(config.hq_dir(), registry.HQ_PREFIX)]
    assert "providers" not in config.load_host()  # the stale leaf was pruned, not left behind
    config.load()  # the next read must not raise either


def test_hq_init_second_call_is_a_clean_no_op(world, monkeypatch, capsys):
    """Re-running `bh hq init` once HQ is already registered is a clean no-op, not an error
    (bh-e0y8.2) — supersedes the old create-time-only singleton refusal: a second call no
    longer raises, it just skips straight to (idempotent) remote wiring."""
    calls = _stub_store_and_sync(monkeypatch)
    hq.init()  # first HQ

    hq.init()  # second call — must NOT raise
    capsys.readouterr()

    # the guard tripped before any store/sync work of the second call.
    assert calls["ensure"] == [(config.hq_dir(), registry.HQ_PREFIX)]
    assert calls["sync"] == 0  # bh-89wxf.2: init never aggregates
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
    assert registry.hive_of_kind(config.load(), registry.HQ_KIND) is None


def test_hq_init_never_runs_a_fleet_wide_sync(world, monkeypatch):
    """`hq init` used to propagate `hub.sync()`'s failed-hive list as its own exit code — it
    has no aggregation step to fail any more (bh-89wxf.2), so a fleet with broken hives cannot
    stop a host from standing HQ up."""
    calls = _stub_store_and_sync(monkeypatch, sync_result=["a-hive"])
    hq.init()  # must NOT raise
    assert calls["sync"] == 0


# ---- the synthetic identity stays green -------------------------------------


def test_hq_registration_adds_no_required_violation(world, monkeypatch):
    """The synthetic local/factory/hq identity trips no registry-level (required-org) check."""
    from beadhive.registry import required_violations

    _stub_store_and_sync(monkeypatch)
    hq.init()
    assert required_violations(config.load()) == []


def test_hq_bead_validates_against_synthetic_identity(world, monkeypatch):
    """A native hq-* bead labelled with the synthetic identity passes the per-issue checks —
    ``bh label validate`` stays green for HQ's own control-plane beads."""
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
    monkeypatch.setattr(validate, "run", lambda *a, **k: Completed(0, _json.dumps([bead]), ""))
    assert validate.has_violations(cfg) is False


def test_hive_ls_shows_hq(world, monkeypatch, capsys):
    from beadhive import hive

    _stub_store_and_sync(monkeypatch)
    hq.init()
    capsys.readouterr()  # drop init output

    hive.ls()
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
    assert (hqdir / ".git").is_dir()  # git-backed (durable, local infra)
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

    monkeypatch.setenv("BH_HOME", str(tmp_path))
    monkeypatch.setattr(hub.config, "hub_dir", lambda: tmp_path)
    monkeypatch.setattr(hub.config, "hq_dir", lambda: tmp_path)
    # `run_bounded`, not `run`: the aggregate read is bounded since bh-toitp.
    monkeypatch.setattr(hub, "run_bounded", lambda cmd, **k: calls.append(cmd) or _Ok())
    return calls


def test_hq_intake_reads_hq_and_points_at_the_fleet_wide_view(tmp_path, monkeypatch):
    """bh-89wxf.2: `bh hq intake` is HQ's OWN inbox now — a read of the HQ store, not the
    cross-hive aggregate — and it names where the fleet-wide view went."""
    from typer.testing import CliRunner

    from beadhive import state
    from beadhive.cli import app

    calls = _stub_hub_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(hub.config, "hq_dir", lambda: tmp_path)

    res = CliRunner().invoke(app, ["hq", "intake"])

    assert res.exit_code == 0, res.output
    assert "hub intake" in res.output  # the pointer, so the rename strands nobody
    assert calls, "the bounded bd read should have run"
    assert all(cmd[:3] == ["bd", "-C", str(tmp_path)] for cmd in calls), calls
    assert state.INTAKE_UNTRIAGED in [arg for cmd in calls for arg in cmd]


def test_hq_intake_forwards_extra_flags(tmp_path, monkeypatch):
    """Extra flags (e.g. --json) forwarded through ``bh hq intake`` reach the bd read."""
    from typer.testing import CliRunner

    from beadhive import state
    from beadhive.cli import app

    calls = _stub_hub_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(hub.config, "hq_dir", lambda: tmp_path)

    res = CliRunner().invoke(app, ["hq", "intake", "--json"])

    assert res.exit_code == 0, res.output
    combined_args = [arg for cmd in calls for arg in cmd]
    assert "--json" in combined_args
    assert state.INTAKE_UNTRIAGED in combined_args


def test_hub_intake_is_the_fleet_wide_inbox_and_no_longer_deprecated(tmp_path, monkeypatch):
    """`bh hub` was an alias for `bh hq` while both resolved to one store. They are two stores
    with two jobs now, so the alias is un-deprecated and means the cross-hive one."""
    from typer.testing import CliRunner

    from beadhive import state
    from beadhive.cli import app

    calls = _stub_hub_for_cli(tmp_path, monkeypatch)
    hub_dir = tmp_path / "hub"
    (hub_dir / ".beads").mkdir(parents=True)
    monkeypatch.setattr(hub.config, "hub_dir", lambda: hub_dir)

    res = CliRunner().invoke(app, ["hub", "intake"])

    assert res.exit_code == 0, res.output
    assert "deprecated" not in res.output.lower()
    assert all(cmd[:3] == ["bd", "-C", str(hub_dir)] for cmd in calls), calls
    assert state.INTAKE_UNTRIAGED in [arg for cmd in calls for arg in cmd]


def test_hq_bd_and_hub_bd_resolve_to_different_stores(tmp_path, monkeypatch):
    """THE SPLIT, asserted at the CLI: one remote path, one database (bh-89wxf.2)."""
    from typer.testing import CliRunner

    from beadhive.cli import app

    hub_dir = tmp_path / "hub"
    (hub_dir / ".beads").mkdir(parents=True)
    calls = _stub_hub_for_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(hub.config, "hub_dir", lambda: hub_dir)
    monkeypatch.setattr(hub.config, "hq_dir", lambda: tmp_path)

    CliRunner().invoke(app, ["hq", "bd", "ready"])
    CliRunner().invoke(app, ["hub", "bd", "ready"])

    assert [cmd[2] for cmd in calls] == [str(tmp_path), str(hub_dir)], calls
