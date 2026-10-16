"""StoreTarget resolution and execution seam (bh-lniwf.3).

docs/design/two-store-scope-adr.md: repository cwd and bead-store location are separate values;
a detached store is selected through BEADS_DIR, never `--database`; a requested scope never
falls back to another.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import bd as bd_mod
from beadhive import config, registry, store_target
from beadhive.store_target import (
    PublicStoreNotOperational,
    StoreConfigInvalid,
    StoreEnvConflict,
    StoreMissing,
    StoreNotConfigured,
    StoreResolutionError,
)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    checkout = root / "github" / "acme" / "widget"
    (checkout / ".beads").mkdir(parents=True)
    public_root = tmp_path / "stores" / "widget-public"
    monkeypatch.setenv("GIT_WORKSPACE", str(root))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    return SimpleNamespace(root=root, checkout=checkout, public_root=public_root, tmp=tmp_path)


def _legacy():
    return {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"}


def _partitioned(ws, **public):
    entry = _legacy()
    entry["stores"] = {
        "internal": {"remote": "widget-private"},
        "public": {
            "prefix": "widpub",
            "path": str(ws.public_root),
            "remote": "git-origin",
            "source": "github-issues",
            **public,
        },
    }
    return entry


def test_legacy_entry_resolves_the_one_store_unchanged(ws):
    target = store_target.resolve_store(_legacy())
    assert target.scope == store_target.SCOPE_LEGACY
    assert target.repo_dir == ws.checkout
    assert target.beads_dir == ws.checkout / ".beads"
    assert target.prefix == "wid"
    assert target.transport == store_target.TRANSPORT_GIT_REF
    assert target.operational and not target.detached
    # same answer for "internal" (the legacy store IS the internal store) and for None
    assert store_target.resolve_store(_legacy(), "internal") == target
    assert store_target.operational_store(_legacy()) == target


def test_legacy_env_is_untouched(ws, monkeypatch):
    monkeypatch.setenv("SOME", "value")
    env = store_target.resolve_store(_legacy()).env()
    assert "BEADS_DIR" not in env and env["SOME"] == "value"


def test_public_request_on_a_legacy_hive_never_falls_back(ws):
    with pytest.raises(StoreNotConfigured, match="refusing to answer a public request"):
        store_target.resolve_store(_legacy(), "public")


def test_public_request_without_a_public_store_never_falls_back(ws):
    entry = _legacy()
    entry["stores"] = {"internal": {"remote": "widget-private"}}
    with pytest.raises(StoreNotConfigured, match="refusing to fall back to internal"):
        store_target.resolve_store(entry, "public")


def test_partitioned_internal_and_public_are_distinct_targets(ws):
    entry = _partitioned(ws)
    internal = store_target.resolve_store(entry)
    public = store_target.resolve_store(entry, "public")
    assert internal.scope == "internal" and internal.beads_dir == ws.checkout / ".beads"
    assert internal.remote == "widget-private"
    assert internal.transport == store_target.TRANSPORT_DESCRIPTOR
    assert public.scope == "public" and public.detached
    assert public.beads_dir == ws.public_root / ".beads"
    assert public.repo_dir == ws.checkout  # bd still runs in the code repository
    assert public.prefix == "widpub" and public.authority == "remote"
    assert public.sync == "pull-only" and public.transport == store_target.TRANSPORT_GIT_REF
    assert not public.operational
    assert [t.scope for t in store_target.all_stores(entry)] == ["internal", "public"]


def test_detached_store_is_selected_through_beads_dir(ws):
    public = store_target.resolve_store(_partitioned(ws), "public")
    env = public.env({"PATH": "/bin"})
    assert env == {"PATH": "/bin", "BEADS_DIR": str(ws.public_root / ".beads")}


def test_linked_worktree_runs_in_the_worktree_but_opens_the_main_store(ws):
    worktree = ws.tmp / "worktrees" / "github" / "acme" / "widget" / "bh-1"
    worktree.mkdir(parents=True)
    target = store_target.resolve_store(_legacy(), cwd=worktree)
    assert target.repo_dir == worktree
    assert target.beads_dir == ws.checkout / ".beads"


def test_missing_store_is_refused_before_bd_can_fall_back(ws):
    public = store_target.resolve_store(_partitioned(ws), "public")
    with pytest.raises(StoreMissing, match="silently fall back"):
        public.require_present()
    (ws.public_root / ".beads").mkdir(parents=True)
    assert public.require_present() is public


def test_conflicting_inherited_beads_dir_is_refused(ws):
    internal = store_target.resolve_store(_partitioned(ws))
    public = store_target.resolve_store(_partitioned(ws), "public")
    with pytest.raises(StoreEnvConflict):
        internal.env({"BEADS_DIR": str(ws.public_root / ".beads")})
    with pytest.raises(StoreEnvConflict):
        public.env({"BEADS_DIR": str(ws.checkout / ".beads")})
    # an inherited value naming the SAME store is fine
    assert public.env({"BEADS_DIR": str(ws.public_root / ".beads")})["BEADS_DIR"]


def test_public_store_inside_the_checkout_is_invalid(ws):
    entry = _partitioned(ws, path=str(ws.checkout / "public"))
    with pytest.raises(StoreConfigInvalid, match="inside the code checkout"):
        store_target.resolve_store(entry, "public")


def test_invalid_stores_block_fails_loudly_at_resolution(ws):
    entry = _legacy()
    entry["stores"] = {"public": {"prefix": "x", "path": "/srv/x"}}
    with pytest.raises(StoreConfigInvalid, match="stores.internal is required"):
        store_target.resolve_store(entry)


def test_unknown_scope_is_refused(ws):
    with pytest.raises(StoreResolutionError, match="unknown store scope"):
        store_target.resolve_store(_legacy(), "private")


def test_resolve_store_is_not_resolve_hive(ws):
    """A store is resolved from an ENTRY, never from a name lookup that could pick a different
    hive's store."""
    assert store_target.resolve_store is not registry.resolve_hive
    with pytest.raises((TypeError, AttributeError)):
        store_target.resolve_store("widget")  # a name is not an entry


def test_operational_verbs_refuse_public_targets(ws):
    public = store_target.resolve_store(_partitioned(ws), "public")
    for verb in ("claim", "assign", "submit", "review", "merge", "dispatch"):
        with pytest.raises(PublicStoreNotOperational):
            store_target.require_operational(public, verb)
    assert store_target.require_explicit_public(public, "list") is public
    with pytest.raises(PublicStoreNotOperational):
        store_target.require_explicit_public(public, "create")


class _FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, args, cwd, **kw):
        self.calls.append(SimpleNamespace(args=args, cwd=cwd, **kw))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


def test_named_routes_run_in_repo_and_select_the_detached_store(ws, monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(bd_mod, "run", fake)
    public = store_target.resolve_store(_partitioned(ws), "public")
    (ws.public_root / ".beads").mkdir(parents=True)

    assert store_target.routes(public).issue_list(all_=True) == []

    (call,) = fake.calls
    assert call.args == ["list", "--all", "--json"]
    assert call.cwd == ws.checkout  # bd runs in the code repository
    assert call.env["BEADS_DIR"] == str(ws.public_root / ".beads")


def test_routes_refuse_an_uninitialized_store(ws, monkeypatch):
    monkeypatch.setattr(bd_mod, "run", _FakeRun())
    public = store_target.resolve_store(_partitioned(ws), "public")
    with pytest.raises(StoreMissing):
        store_target.routes(public).issue_list()


def test_shared_server_mode_never_uses_database_flag(ws, monkeypatch):
    """A shared-server hive names its server database in its own metadata.json; selecting the
    store by BEADS_DIR lets bd read THAT store's binding. `--database` is the proxied-server
    switch and is refused outright by the store transport."""
    fake = _FakeRun()
    monkeypatch.setattr(bd_mod, "run", fake)
    (ws.checkout / ".beads" / "metadata.json").write_text(
        '{"dolt_mode": "server", "dolt_database": "wid"}'
    )
    internal = store_target.resolve_store(_partitioned(ws))
    store_target.routes(internal).issue_list()
    assert "--database" not in fake.calls[0].args
    assert "BEADS_DIR" not in fake.calls[0].env
    transport = store_target.StoreTransport(internal, bd_mod)
    for bad in (["list", "--database", "x"], ["--db=x", "list"]):
        with pytest.raises(StoreResolutionError, match="never --database"):
            transport.run(bad, ws.checkout)


def test_public_bead_ids_are_recognised_by_prefix(ws, monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "managed_repos:\n"
        "  - {provider: github, org: acme, repo: widget, prefix: wid, stores: "
        "{internal: {remote: widget-private}, public: "
        f"{{prefix: widpub, path: {ws.public_root}}}}}}}\n"
    )
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    cfg = config.load()
    entry, scope = store_target.bead_scope(cfg, "widpub-abc")
    assert scope == "public" and str(entry["repo"]) == "widget"
    assert store_target.bead_scope(cfg, "wid-abc") is None
