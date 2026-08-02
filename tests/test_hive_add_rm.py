"""`ws hive add` / `ws hive rm` — the registry-only hive-lifecycle verbs.

Contract:
  * `hive add <provider/org/repo>` registers a triplet with NO cwd requirement and NO `bd init`
    (the repo may be uncloned) — purely `derive_prefix` (config-only) + `register`;
  * `hive rm <hive-id>` resolves via `resolve_hive`, drops the managed_repos entry, and saves;
  * both leave other config (other hives, orgs, dimensions) untouched.

These run without real `bd` and without any repo on disk — that is the point: these verbs are
registry-scoped, so no `.beads/` dir is created and no `gh`/`bd` is invoked.
"""

from __future__ import annotations

import pytest
import typer

from beadhive import config, hive, registry


def _register(world, *, org="myorg", repo="myrepo", prefix="mr", kind="personal"):
    provider = "github"
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": provider, "org": org, "repo": repo, "prefix": prefix, "kind": kind}
    )
    config.save(cfg)


def _entry(provider="github", org="myorg", repo="myrepo"):
    return registry.find_entry(config.load(), provider, org, repo)


def test_add_registers_triplet_without_cwd_or_bd_init(world):
    # No repo on disk, cwd is the (empty) ws root — add must still register from the triplet.
    assert _entry(org="acme", repo="widget") is None

    hive.add("github/acme/widget", kind="personal")

    e = _entry(org="acme", repo="widget")
    assert e is not None
    assert str(e["provider"]) == "github"
    assert str(e["prefix"]) == "ac-widget"  # derive_prefix(kind=personal) → <code>-<repo>
    assert str(e["kind"]) == "personal"


def test_add_honors_prefix_override(world):
    hive.add("github/acme/widget", prefix="wid", kind="prototype")

    assert str(_entry(org="acme", repo="widget")["prefix"]) == "wid"


def test_add_rejects_non_triplet(world):
    with pytest.raises(typer.Exit):
        hive.add("acme/widget")  # only two parts — not provider/org/repo


def test_rm_unregisters_via_resolve_drop_save(world):
    _register(world, org="acme", repo="widget", prefix="wid")
    assert _entry(org="acme", repo="widget") is not None

    hive.rm("wid")  # resolve by prefix (hive_match=flexible)

    assert _entry(org="acme", repo="widget") is None


def test_add_and_rm_leave_other_config_untouched(world):
    _register(world, org="other", repo="keep", prefix="keep")
    hive.add("github/acme/widget", kind="personal")
    hive.rm("ac-widget")

    cfg = config.load()
    # the unrelated hive survives both operations untouched
    assert _entry(org="other", repo="keep") is not None
    assert _entry(org="acme", repo="widget") is None
    # registry-only: unrelated top-level config preserved (save() didn't drop sections)
    assert list(cfg.get("providers", [])) == ["github"]


# ---- fleet routing (bh-e0y8.11) -----------------------------------------------
# managed_repos is FLEET-scoped truth (config_partition.py, bh-e0y8.3); once a host has a real
# fleet.yaml (has run `hq init`/`hq clone`), register()/unregister() must persist it through
# `config.save_fleet()`, never leave it in the host's own config.yaml — the exact bug
# tests/test_hq_clone.py's `test_clone_registers_hq_so_bd_ready_targets_the_clone` regresses:
# writing it to host raised `config.ConfigError` on the very next `config.load()` once
# fleet.yaml existed (a host-side FLEET-classified key is rejected the moment there is a real
# fleet base to diverge from — config.py's `_reject_fleet_overrides`).


def _write_fleet_yaml(world, text="orgs: {}\n"):
    """A minimal real fleet.yaml in the HQ working copy — the "this host has joined the fleet"
    signal `registry._managed_repos_base`/`_save_managed_repos` gate on.

    `world`'s shared baseline also seeds a legacy host-side `providers: [github]` (pre-fleet-
    split test content — harmless while there is no fleet.yaml to diverge from, exactly as
    `test_add_and_rm_leave_other_config_untouched` above still exercises). A real fleet.yaml is
    precisely what this helper creates, so strip that unrelated legacy key first (mirrors
    tests/test_hq_clone.py's `_no_legacy_fleet_keys_in_host` fixture) — otherwise every test
    below would trip on it instead of on what it actually sets out to test. The default empty
    `managed_repos: []` is dropped too (an empty leaf still counts as an override) — but a
    REAL pre-existing entry (from `_register`, called before this helper) is left in place, so
    the migration test below can prove it gets carried into fleet.yaml rather than dropped."""
    host_cfg = config.load_host()
    host_cfg.pop("providers", None)
    if not host_cfg.get("managed_repos"):
        host_cfg.pop("managed_repos", None)
    config.save(host_cfg)

    path = config.fleet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_add_does_not_create_a_fleet_yaml_on_a_host_with_no_hq_yet(world):
    """register()/unregister() must never themselves conjure a fleet.yaml into existence — only
    `hq init`/`hq clone` stand up the HQ working copy. A host with no HQ keeps registering hives
    into its own config.yaml exactly as before this bead."""
    assert not config.fleet_path().is_file()

    hive.add("github/acme/widget", kind="personal")

    assert not config.fleet_path().is_file()
    assert any(str(e["repo"]) == "widget" for e in config.load_host().get("managed_repos", []))


def test_add_routes_managed_repos_through_fleet_yaml_once_host_is_fleet_managed(world):
    _write_fleet_yaml(world)

    hive.add("github/acme/widget", kind="personal")

    fleet_repos = {str(e["repo"]) for e in config.load_fleet().get("managed_repos", [])}
    assert fleet_repos == {"widget"}
    assert "managed_repos" not in config.load_host()  # never written into host config
    # the actual regression: the next config.load() must succeed, not raise ConfigError
    assert _entry(org="acme", repo="widget") is not None


def test_register_migrates_pre_fleet_host_side_managed_repos_into_fleet(world):
    """A host that registered a hive BEFORE it ever had a fleet.yaml (a legacy entry still
    sitting in its own config.yaml) must have that entry carried into fleet.yaml the next time
    register()/unregister() runs, not left behind to trip the next config.load().

    Drives `registry.register()` directly (as `hq.clone()` does), not `hive.add()`: the latter
    calls the validating `config.load()` itself BEFORE ever reaching register() — so on a host
    whose OWN un-migrated content is the thing tripping the violation, `hive.add()`/`hive.init()`
    fail at their own entry, before register() gets a chance to self-heal. Fixing that ordering
    is a wider `hq.py`-genesis-scaffold concern (`hq.init`'s `_wire_remote`/`scaffold_layout`
    never clears the host side either) — out of scope for this bead, which is narrowly about
    register()/unregister()'s own persistence routing."""
    _register(world, org="legacy", repo="keep", prefix="keep")  # pre-existing HOST-side entry
    _write_fleet_yaml(world)

    registry.register("github", "acme", "widget", "ac-widget", "personal")

    fleet_repos = {str(e["repo"]) for e in config.load_fleet().get("managed_repos", [])}
    assert fleet_repos == {"keep", "widget"}
    assert "managed_repos" not in config.load_host()
    config.load()  # must not raise


def test_rm_also_routes_through_fleet_yaml(world):
    _write_fleet_yaml(world)
    hive.add("github/acme/widget", kind="personal")

    hive.rm("ac-widget")

    assert config.load_fleet().get("managed_repos", []) == []
    assert "managed_repos" not in config.load_host()
    config.load()  # must not raise


# ---- self-healing the ordering gap `test_register_migrates_pre_fleet_host_side_managed_repos_
# into_fleet` above declined to fix (bh-17eb) --------------------------------------------------
#
# `hive.add()` calls the validating `config.load()` itself BEFORE `register()` ever runs — so a
# host whose OWN un-migrated legacy content (a flat, pre-split config.yaml — every existing
# user's shape before 0.7.0) collides with an already-existing `fleet.yaml` used to hard-fail
# right there, before the self-healing routing got a chance. `load_reconciling()` closes that
# gap generically for this entry point.


def test_add_self_heals_a_stale_un_migrated_host_config_before_validating(world):
    # world's own baseline config.yaml IS the un-migrated legacy shape: a flat `providers:
    # [github]` sitting host-side with no split awareness at all.
    assert "providers" in config.load_host()
    path = config.fleet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("orgs: {}\n")  # a real fleet.yaml now exists — this WOULD collide pre-fix

    hive.add("github/acme/widget", kind="personal")  # must not raise ConfigError

    assert _entry(org="acme", repo="widget") is not None
    assert "providers" not in config.load_host()  # the stale leaf was pruned, not left behind
    config.load()  # the next read must not raise either


def test_furnish_of_inference_and_persistence(world):
    from beadhive import registry

    # Missing key: forks were never furnished, everything else was (zero migration).
    assert registry.furnish_of({"kind": "fork"}) == "none"
    assert registry.furnish_of({"kind": "personal"}) == "full"
    assert registry.furnish_of({}) == "full"
    # Explicit key wins over inference.
    assert registry.furnish_of({"kind": "fork", "furnish": "full"}) == "full"
    assert registry.furnish_of({"kind": "personal", "furnish": "none"}) == "none"
    # register() persists the declaration on the entry.
    registry.register("github", "acme", "zf", "zf", "prototype", furnish="none")
    entry = registry.find_entry(config.load(), "github", "acme", "zf")
    assert str(entry["furnish"]) == "none"
