"""Hub, safety and doctor are store-scope aware (bh-lniwf.7).

docs/design/two-store-scope-adr.md sections 7-9: the hub hydrates internal stores only, Git-ref
safety checks apply only to stores riding refs/dolt/data, descriptor-backed stores report
`unknown` rather than a proof nobody ran, and doctor lists every configured store.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import config, doctor, hub, safety, store_target


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    checkout = root / "github" / "acme" / "widget"
    checkout.mkdir(parents=True)
    public_root = tmp_path / "stores" / "widget-public"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "providers: [github]\n"
        "managed_repos:\n"
        "  - {provider: github, org: acme, repo: widget, prefix: wid,\n"
        "     stores: {internal: {remote: widget-private},\n"
        f"              public: {{prefix: widpub, path: {public_root}, remote: git-origin}}}}}}\n"
        "  - {provider: github, org: acme, repo: plain, prefix: pl}\n"
    )
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("GIT_WORKSPACE", str(root))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    cfg = config.load()
    entries = {str(e["repo"]): e for e in cfg["managed_repos"]}
    return SimpleNamespace(cfg=cfg, checkout=checkout, public_root=public_root, **entries)


def _no_cache(*_a, **_k):
    raise AssertionError("a partitioned hive must never be cache-cloned from the code origin")


def test_hub_hydrates_a_partitioned_hive_from_its_internal_checkout_only(ws, monkeypatch):
    monkeypatch.setattr(hub, "_fetch_cache", _no_cache)
    assert hub.hydration_source(ws.cfg, ws.widget) is None  # not cloned: skipped, not fetched
    (ws.checkout / ".beads").mkdir()
    (ws.public_root / ".beads").mkdir(parents=True)
    assert hub.hydration_source(ws.cfg, ws.widget) == ws.checkout  # never the public root


def test_hub_keeps_the_cache_fallback_for_one_store_hives(ws, monkeypatch):
    fetched = []
    monkeypatch.setattr(hub, "_fetch_cache", lambda cfg, e: fetched.append(e["repo"]) or "c")
    assert hub.hydration_source(ws.cfg, ws.plain) == "c"
    assert fetched == ["plain"]


def test_descriptor_store_health_is_unknown_never_clean(ws, monkeypatch):
    monkeypatch.setattr(safety, "_scan_dolt_ref", lambda *a, **k: pytest.fail("git-ref scan"))
    internal = store_target.resolve_store(ws.widget)
    state = safety.store_dolt_state(internal)
    assert state.status == "unknown"
    assert "widget-private" in state.reason and "advisory" in state.reason


def test_git_ref_stores_keep_the_git_ref_scan(ws, monkeypatch):
    seen = []
    monkeypatch.setattr(
        safety,
        "_scan_dolt_ref",
        lambda path, has_origin, fetch=False: seen.append(path) or safety.DoltRefInfo("clean"),
    )
    monkeypatch.setattr(safety, "_run", lambda args, repo: (1, ""))
    (ws.public_root / ".beads").mkdir(parents=True)
    public = store_target.resolve_store(ws.widget, "public")
    assert safety.store_dolt_state(public).status == "clean"
    legacy = store_target.resolve_store(ws.plain)
    assert safety.store_dolt_state(legacy).status == "clean"
    assert seen == [str(ws.public_root.resolve()), str(legacy.repo_dir.resolve())]


def test_doctor_lists_each_store_with_honest_health(ws, capsys):
    (ws.checkout / ".beads").mkdir()
    rows = doctor._data_stores(ws.cfg)
    assert [(r["scope"], r["health"]) for r in rows] == [
        ("internal", "unknown"),
        ("public", "missing"),
    ]
    internal = rows[0]
    assert internal["transport"] == "descriptor" and internal["authority"] == "local"
    assert internal["sync"] == "push-pull" and internal["prefix"] == "wid"
    assert rows[1]["transport"] == "git-ref" and rows[1]["sync"] == "pull-only"
    doctor._render_stores(rows)
    out = capsys.readouterr().out
    assert "# bead stores (2)" in out and "health=unknown" in out and "fence advisory" in out


def test_doctor_stores_section_is_silent_for_an_unpartitioned_fleet(ws, capsys):
    cfg = {"managed_repos": [ws.plain]}
    assert doctor._data_stores(cfg) == []
    doctor._render_stores([])
    assert capsys.readouterr().out == ""
