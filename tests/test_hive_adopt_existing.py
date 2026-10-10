"""`hive.adopt_existing` — registration adopts the store's own identity (bh-m2irb), hermetic.

With the key on, `bh hive add` and `bh hive onboard` ask the hive's own bead store for its
prefix and project id (the local checkout first, then the remote's `refs/dolt/data`) and only
derive one for a hive that has no store anywhere. Off (the default) keeps every derivation
byte-for-byte what it was. The real-bd/real-dolt round trip is `test_hive_adopt_existing_int`.
"""

from __future__ import annotations

import json
import types

import pytest
import typer

from beadhive import config, hive, hub, onboard, registry, store_identity, store_locator
from harness.world import git

_ADOPT = {"hive": {"adopt_existing": True}}


def _identity(prefix="nvhack", project_id="pid-1", source="the remote"):
    return store_identity.StoreIdentity(prefix, project_id, source)


def _enable(world, extra: str = "") -> None:
    world.cfg_path.write_text(
        "providers: [github]\nmanaged_repos: []\nhive:\n  adopt_existing: true\n" + extra
    )


def _entry(org="acme", repo="widget"):
    return registry.find_entry(config.load(), "github", org, repo)


# ---- config -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ({}, False),
        (None, False),
        ({"hive": {}}, False),
        ({"hive": {"adopt_existing": False}}, False),
        ({"hive": {"adopt_existing": "true"}}, False),  # a string is not an opt-in
        (_ADOPT, True),
    ],
)
def test_adopt_existing_is_opt_in(cfg, expected):
    assert store_identity.adopt_existing_enabled(cfg) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 120.0), (30, 30.0), ("45", 45.0), (0, 120.0), (-5, 120.0), ("soon", 120.0)],
)
def test_probe_timeout_is_configurable_with_a_safe_fallback(value, expected):
    cfg = {"hive": {} if value is None else {"adopt_probe_timeout": value}}
    assert store_identity.probe_timeout(cfg) == expected


def test_keys_are_host_scoped_and_in_the_schema():
    from beadhive import config_partition

    assert config_partition.partition_of("hive.adopt_existing") == config_partition.HOST
    assert config_partition.partition_of("hive.adopt_probe_timeout") == config_partition.HOST
    assert "hive.adopt_existing" in config_partition.HOST_KEYS
    from beadhive.modules.config.contracts import BeadhiveConfig

    parsed = BeadhiveConfig(hive={"adopt_existing": True})
    assert parsed.hive.adopt_existing is True
    assert BeadhiveConfig().hive.adopt_existing is False


# ---- the remote probe's pure pieces ----------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/widget.git", "git+ssh://git@github.com/acme/widget.git"),
        ("ssh://git@example.com/acme/widget.git", "git+ssh://git@example.com/acme/widget.git"),
        ("https://github.com/acme/widget.git", "git+https://github.com/acme/widget.git"),
        ("file:///srv/widget.git", "git+file:///srv/widget.git"),
        ("/srv/widget.git", "git+file:///srv/widget.git"),
        ("git+ssh://git@github.com/acme/widget.git", "git+ssh://git@github.com/acme/widget.git"),
        ("", ""),
        ("widget", ""),
    ],
)
def test_dolt_remote_url_maps_every_git_form(url, expected):
    assert store_identity.dolt_remote_url(url) == expected


def test_parse_rows_reads_one_json_document_per_statement():
    out = (
        '{"rows": [{"key":"issue_prefix","value":"nvhack"}]}\n'
        '{"rows": [{"key":"_project_id","value":"pid-1"}]}\n'
    )
    assert store_identity._parse_rows(out) == {"issue_prefix": "nvhack", "_project_id": "pid-1"}
    assert store_identity._parse_rows("") == {}
    assert store_identity._parse_rows("not json") == {}


def test_database_is_the_sanitized_prefix():
    assert _identity("bc-workspace").database == "bc_workspace"


def test_a_remote_without_a_data_ref_is_never_cloned(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[:2])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(store_identity, "run", fake_run)
    assert store_identity.read_remote("git@github.com:acme/widget.git", scratch=tmp_path) is None
    assert calls == [["git", "ls-remote"]]


def test_a_failed_or_timed_out_probe_answers_none(monkeypatch, tmp_path):
    import subprocess

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "ls-remote"]:
            return types.SimpleNamespace(returncode=0, stdout="abc\trefs/dolt/data\n", stderr="")
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 1)

    monkeypatch.setattr(store_identity, "run", fake_run)
    assert store_identity.read_remote("/srv/widget.git", scratch=tmp_path, timeout=1) is None
    assert list(tmp_path.iterdir()) == [], "the throwaway clone is removed however it ends"


def test_local_read_never_asks_bd_about_a_store_that_is_not_here(tmp_path, monkeypatch):
    """A server-mode checkout whose database is not on this host makes bd retry for over a
    minute; the probe must decide from filesystem facts before spawning it."""
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(tmp_path / "ss"))
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "metadata.json").write_text(
        json.dumps({"dolt_mode": "server", "dolt_database": "nvhack"})
    )
    from beadhive import bd

    monkeypatch.setattr(bd, "store_prefix", lambda _cwd: pytest.fail("bd must not be asked"))
    assert store_identity.read_local(tmp_path) is None

    (tmp_path / "ss" / "dolt" / "nvhack").mkdir(parents=True)
    monkeypatch.setattr(bd, "store_prefix", lambda _cwd: "nvhack")
    found = store_identity.read_local(tmp_path)
    assert found is not None and found.prefix == "nvhack"


def test_resolve_prefers_the_local_store_and_falls_back_to_the_remote(monkeypatch, tmp_path):
    asked = []
    monkeypatch.setattr(store_identity, "read_local", lambda p: asked.append("local") or None)
    monkeypatch.setattr(
        store_identity,
        "read_remote",
        lambda url, scratch, timeout: asked.append(("remote", url)) or _identity(),
    )
    assert store_identity.resolve(tmp_path, "git@x:a/b.git", scratch=tmp_path).prefix == "nvhack"
    assert asked == ["local", ("remote", "git@x:a/b.git")]

    monkeypatch.setattr(store_identity, "read_local", lambda p: _identity("local1"))
    assert store_identity.resolve(tmp_path, "git@x:a/b.git", scratch=tmp_path).prefix == "local1"
    assert store_identity.resolve(None, "", scratch=tmp_path) is None


# ---- registry records the identity ------------------------------------------------


def test_register_records_project_id_and_keeps_it_for_the_same_prefix(world):
    registry.register("github", "acme", "widget", "nvhack", "personal", project_id="pid-1")
    assert str(_entry()["project_id"]) == "pid-1"

    registry.register("github", "acme", "widget", "nvhack", "personal")  # e.g. --force re-run
    assert str(_entry()["project_id"]) == "pid-1", "a re-register never forgets the identity"

    registry.register("github", "acme", "widget", "other", "personal")
    assert "project_id" not in _entry(), "a different prefix names a different store"


# ---- bh hive add ----------------------------------------------------------------


def test_hive_add_adopts_the_remote_store_identity(world, monkeypatch, capsys):
    _enable(world)
    seen = {}

    def fake_resolve(hive_dir, url, *, scratch, timeout):
        seen.update(hive_dir=hive_dir, url=url, timeout=timeout)
        return _identity()

    monkeypatch.setattr(store_identity, "resolve", fake_resolve)

    hive.add("github/acme/widget", kind="personal")

    entry = _entry()
    assert str(entry["prefix"]) == "nvhack"
    assert str(entry["project_id"]) == "pid-1"
    assert seen["hive_dir"] is None, "no checkout on this host yet"
    assert seen["url"] == "git@github.com:acme/widget.git"
    assert seen["timeout"] == 120.0
    assert "adopting prefix 'nvhack'" in capsys.readouterr().err


def test_hive_add_reads_a_local_checkout_first(world, monkeypatch):
    _enable(world)
    target = world.ws_root / "github" / "acme" / "widget"
    target.mkdir(parents=True)
    seen = {}

    def fake_resolve(hive_dir, url, *, scratch, timeout):
        seen["hive_dir"] = hive_dir
        return _identity("localpfx", "pid-2")

    monkeypatch.setattr(store_identity, "resolve", fake_resolve)
    hive.add("github/acme/widget", kind="personal")

    assert seen["hive_dir"] == target
    assert str(_entry()["prefix"]) == "localpfx"


def test_hive_add_derives_when_the_hive_has_no_store_anywhere(world, monkeypatch, capsys):
    _enable(world)
    monkeypatch.setattr(store_identity, "resolve", lambda *a, **k: None)

    hive.add("github/acme/widget", kind="personal")

    entry = _entry()
    assert str(entry["prefix"]) == "ac-widget"
    assert "project_id" not in entry
    assert "no existing bead store" in capsys.readouterr().err


def test_hive_add_explicit_prefix_wins_without_probing(world, monkeypatch):
    _enable(world)
    monkeypatch.setattr(store_identity, "resolve", lambda *a, **k: pytest.fail("no probe"))

    hive.add("github/acme/widget", prefix="wid", kind="personal")

    assert str(_entry()["prefix"]) == "wid"


def test_hive_add_without_the_key_never_probes(world, monkeypatch):
    monkeypatch.setattr(store_identity, "resolve", lambda *a, **k: pytest.fail("no probe"))

    hive.add("github/acme/widget", kind="personal")

    assert str(_entry()["prefix"]) == "ac-widget"


def test_hive_add_warns_when_the_adopted_prefix_is_taken(world, monkeypatch, capsys):
    _enable(world)
    registry.register("github", "acme", "other", "nvhack", "personal")
    monkeypatch.setattr(store_identity, "resolve", lambda *a, **k: _identity())

    hive.add("github/acme/widget", kind="personal")

    assert "already registered to another hive" in capsys.readouterr().err


# ---- bh hive onboard: identity derivation ---------------------------------------


def _ctx(tmp_path, *, cfg=None, exists=True, **kw) -> onboard.Ctx:
    target = tmp_path / "github" / "acme" / "widget"
    if exists:
        target.mkdir(parents=True, exist_ok=True)
    return onboard.Ctx(
        hive="github/acme/widget",
        target=str(target),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(target),
        cfg=_ADOPT if cfg is None else cfg,
        **kw,
    )


def test_onboard_adopts_before_deriving(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "org-native")
    monkeypatch.setattr(registry, "derive_prefix", lambda *a, **k: pytest.fail("not derived"))
    monkeypatch.setattr(onboard, "_origin_url", lambda base: "git@github.com:acme/widget.git")
    seen = {}

    def fake_resolve(hive_dir, url, *, scratch, timeout):
        seen.update(hive_dir=hive_dir, url=url)
        return _identity()

    monkeypatch.setattr(store_identity, "resolve", fake_resolve)
    ctx = _ctx(tmp_path)

    onboard._resolve_kind_prefix_upstream(ctx, ctx.cfg, "github", "acme", "widget", None)

    assert (ctx.prefix, ctx.project_id) == ("nvhack", "pid-1")
    assert seen == {"hive_dir": ctx.target, "url": "git@github.com:acme/widget.git"}


def test_onboard_with_no_store_derives_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "org-native")
    monkeypatch.setattr(registry, "derive_prefix", lambda *a, **k: ("ac-widget", []))
    monkeypatch.setattr(onboard, "_origin_url", lambda base: "")
    monkeypatch.setattr(store_identity, "resolve", lambda *a, **k: None)
    monkeypatch.setattr(onboard.bd_mod, "store_prefix", lambda cwd: "")
    ctx = _ctx(tmp_path)

    onboard._resolve_kind_prefix_upstream(ctx, ctx.cfg, "github", "acme", "widget", None)

    assert (ctx.prefix, ctx.project_id) == ("ac-widget", "")


def test_onboard_without_the_key_never_probes_the_remote(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "org-native")
    monkeypatch.setattr(registry, "derive_prefix", lambda *a, **k: ("ac-widget", []))
    monkeypatch.setattr(store_identity, "resolve", lambda *a, **k: pytest.fail("no probe"))
    monkeypatch.setattr(onboard.bd_mod, "store_prefix", lambda cwd: "")
    ctx = _ctx(tmp_path, cfg={})

    onboard._resolve_kind_prefix_upstream(ctx, {}, "github", "acme", "widget", None)

    assert ctx.prefix == "ac-widget"


def test_onboard_carries_the_registered_project_id(tmp_path):
    ctx = _ctx(tmp_path)
    existing = {"prefix": "nvhack", "kind": "personal", "project_id": "pid-1"}

    onboard._resolve_kind_prefix_upstream(ctx, ctx.cfg, "github", "acme", "widget", existing)

    assert (ctx.prefix, ctx.project_id) == ("nvhack", "pid-1")


def test_an_adopted_registration_is_not_reported_as_drift(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(registry, "derive_prefix", lambda *a, **k: ("ac-widget", []))
    ctx = _ctx(tmp_path, prefix="nvhack")
    existing = {"prefix": "nvhack", "kind": "personal", "project_id": "pid-1"}

    onboard._note_prefix_drift(ctx, ctx.cfg, "github", "acme", "widget", existing)
    assert capsys.readouterr().err == ""

    onboard._note_prefix_drift(
        ctx, ctx.cfg, "github", "acme", "widget", {"prefix": "nvhack", "kind": "personal"}
    )
    assert "differs from the registered prefix" in capsys.readouterr().err


# ---- bh hive onboard: no --clone-url for a registered hive ---------------------


def test_a_registered_hive_needs_no_clone_url_when_adopting(tmp_path, monkeypatch):
    entry = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "nvhack"}
    monkeypatch.setattr(registry, "find_entry", lambda *a, **k: entry)
    monkeypatch.setattr(hub, "_hive_url", lambda cfg, e: "git@github.com:acme/widget.git")
    ctx = _ctx(tmp_path, exists=False)

    ok, detail = onboard._chk_clone_url_present(ctx)

    assert ok
    assert ctx.clone_url == "git@github.com:acme/widget.git"
    assert "fleet registry" in detail


def test_an_unregistered_hive_still_needs_a_clone_url(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "find_entry", lambda *a, **k: None)
    ctx = _ctx(tmp_path, exists=False)

    ok, detail = onboard._chk_clone_url_present(ctx)

    assert not ok and "--clone-url" in detail


def test_without_the_key_a_registered_hive_still_needs_a_clone_url(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "find_entry", lambda *a, **k: pytest.fail("not consulted"))
    ctx = _ctx(tmp_path, cfg={}, exists=False)

    ok, _ = onboard._chk_clone_url_present(ctx)

    assert not ok


# ---- bh hive onboard: bootstrap into the hive's own database --------------------


@pytest.fixture
def server_dir(tmp_path, monkeypatch):
    root = tmp_path / "shared-server"
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(root))
    return root / "dolt"


def _adopted_ctx(tmp_path) -> onboard.Ctx:
    ctx = _ctx(tmp_path, prefix="bc-widget")
    ctx.project_id = "pid-1"
    ctx._derived = True
    return ctx


def _fake_bootstrap(monkeypatch, server_dir, *, database_after=None, project_id="pid-1"):
    """Stand in for `bd bootstrap`: record the metadata it was handed, then write back what
    bd would (the database it landed in + the project id it backfilled)."""
    seen = {}

    def mint(cmd, ctx, *, env):
        meta = ctx.base / ".beads" / "metadata.json"
        seeded = json.loads(meta.read_text())
        seen.update(cmd=cmd, seeded=dict(seeded), env=env)
        landed = database_after or seeded["dolt_database"]
        (server_dir / landed).mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({**seeded, "dolt_database": landed, "project_id": project_id}))

    monkeypatch.setattr(onboard, "_run_bd_mint", mint)
    monkeypatch.setattr(hub, "_ensure_shared_server_running", lambda base: None)
    monkeypatch.setattr(onboard.bd_mod, "store_prefix", lambda cwd: "bc-widget")
    return seen


def test_bootstrap_seeds_the_hives_own_database_before_bd_runs(tmp_path, monkeypatch, server_dir):
    seen = _fake_bootstrap(monkeypatch, server_dir)
    ctx = _adopted_ctx(tmp_path)
    git("init", "-q", "-b", "main", cwd=ctx.base)

    onboard._bootstrap_adopted(ctx, {})

    assert seen["cmd"] == ["bd", "bootstrap", "--non-interactive"]
    assert seen["env"]["BEADS_DOLT_SHARED_SERVER"] == "1"
    assert seen["seeded"]["dolt_mode"] == "server"
    assert seen["seeded"]["dolt_database"] == "bc_widget", "the sanitized prefix, never 'beads'"
    assert "project_id" not in seen["seeded"], "a stale id would let bd re-map the name"
    assert store_locator.recorded_server_database(ctx.base) == "bc_widget"
    assert (ctx.base / ".beads" / "config.yaml").read_text() == "dolt:\n    shared-server: true\n"
    exclude = (ctx.base / ".git" / "info" / "exclude").read_text().splitlines()
    assert "/.beads.gate.lock" in exclude, "bd's root gate lock must not dirty the checkout"


def test_bootstrap_that_lands_elsewhere_fails_loudly(tmp_path, monkeypatch, server_dir, capsys):
    _fake_bootstrap(monkeypatch, server_dir, database_after="beads")
    ctx = _adopted_ctx(tmp_path)

    with pytest.raises(typer.Exit):
        onboard._bootstrap_adopted(ctx, {})

    assert "resolved database 'beads'" in capsys.readouterr().err
    assert not (ctx.base / ".beads").exists(), "nothing left behind to look initialized"


def test_bootstrap_onto_another_stores_database_fails_loudly(
    tmp_path, monkeypatch, server_dir, capsys
):
    _fake_bootstrap(monkeypatch, server_dir, project_id="someone-else")
    ctx = _adopted_ctx(tmp_path)

    with pytest.raises(typer.Exit):
        onboard._bootstrap_adopted(ctx, {})

    assert "someone-else" in capsys.readouterr().err


def test_a_generic_database_name_is_refused_before_bootstrap(tmp_path, monkeypatch, server_dir):
    monkeypatch.setattr(onboard, "_run_bd_mint", lambda *a, **k: pytest.fail("no bootstrap"))
    ctx = _adopted_ctx(tmp_path)
    ctx.prefix = "beads"

    with pytest.raises(typer.Exit):
        onboard._bootstrap_adopted(ctx, {})


def test_bd_init_bootstraps_instead_of_minting_when_origin_has_data(tmp_path, monkeypatch):
    """Even a furnished registration must not mint a fresh store over an existing one."""
    calls = []
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: True)
    monkeypatch.setattr(onboard, "_bootstrap_adopted", lambda ctx, env: calls.append("adopt"))
    monkeypatch.setattr(onboard, "_run_bd_mint", lambda *a, **k: calls.append("mint"))
    for name in ("_ensure_server_mode_persisted", "_configure_auto_export", "_guard_beads_remote"):
        monkeypatch.setattr(onboard, name, lambda ctx: None)
    monkeypatch.setattr(hive, "_ensure_bd_backup_gitignored", lambda base: None)
    ctx = _adopted_ctx(tmp_path)
    ctx.furnish = True

    onboard._act_bd_init(ctx)

    assert calls == ["adopt"]
