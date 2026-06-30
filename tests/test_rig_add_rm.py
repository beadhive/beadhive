"""`ws rig add` / `ws rig rm` — the registry-only rig-lifecycle verbs.

Contract:
  * `rig add <provider/org/repo>` registers a triplet with NO cwd requirement and NO `bd init`
    (the repo may be uncloned) — purely `derive_prefix` (config-only) + `register`;
  * `rig rm <rig-id>` resolves via `resolve_rig`, drops the managed_repos entry, and saves;
  * both leave other config (other rigs, orgs, dimensions) untouched.

These run without real `bd` and without any repo on disk — that is the point: these verbs are
registry-scoped, so no `.beads/` dir is created and no `gh`/`bd` is invoked.
"""

from __future__ import annotations

import pytest
import typer

from ws import config, registry, rig


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

    rig.add("github/acme/widget", kind="personal")

    e = _entry(org="acme", repo="widget")
    assert e is not None
    assert str(e["provider"]) == "github"
    assert str(e["prefix"]) == "ac-widget"  # derive_prefix(kind=personal) → <code>-<repo>
    assert str(e["kind"]) == "personal"


def test_add_honors_prefix_override(world):
    rig.add("github/acme/widget", prefix="wid", kind="prototype")

    assert str(_entry(org="acme", repo="widget")["prefix"]) == "wid"


def test_add_rejects_non_triplet(world):
    with pytest.raises(typer.Exit):
        rig.add("acme/widget")  # only two parts — not provider/org/repo


def test_rm_unregisters_via_resolve_drop_save(world):
    _register(world, org="acme", repo="widget", prefix="wid")
    assert _entry(org="acme", repo="widget") is not None

    rig.rm("wid")  # resolve by prefix (rig_match=flexible)

    assert _entry(org="acme", repo="widget") is None


def test_add_and_rm_leave_other_config_untouched(world):
    _register(world, org="other", repo="keep", prefix="keep")
    rig.add("github/acme/widget", kind="personal")
    rig.rm("ac-widget")

    cfg = config.load()
    # the unrelated rig survives both operations untouched
    assert _entry(org="other", repo="keep") is not None
    assert _entry(org="acme", repo="widget") is None
    # registry-only: unrelated top-level config preserved (save() didn't drop sections)
    assert list(cfg.get("providers", [])) == ["github"]
