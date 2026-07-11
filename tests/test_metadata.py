"""ws.metadata — the read-through workspace-metadata cache.

Covers the acceptance triangle (cache HIT / MISS / STALENESS) plus the surrounding contract:
cold-start tolerance, atomic persistence, the workspace_root guard, TTL semantics, and the
``read_fleet`` read-through seam. The expensive ``measure`` / ``fingerprint`` plumbing is stubbed
so the suite is fast and hermetic — no real repos are walked and the cache dir is a tmp_path.
"""

from __future__ import annotations

import json

import pytest

from beadhive import config, metadata


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    """Isolate the cache dir ($BH_CACHE) and the workspace root (GIT_WORKSPACE) into tmp_path so
    nothing touches the operator's real ~/.beadhive/cache or workspace."""
    cache = tmp_path / "cache"
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("BH_CACHE", str(cache))
    monkeypatch.setenv("GIT_WORKSPACE", str(root))
    return tmp_path


def _fake_record(**over) -> metadata.RepoMetadata:
    """A minimal well-formed RepoMetadata, overridable per field."""
    base = dict(
        git_head="abc123",
        git_mtime=1000.0,
        measured_at=metadata._now(),
        category="READY",
        has_origin=True,
        stash_count=0,
        disk_bytes=42,
        commit_count=7,
        age_days=1.5,
        last_commit="2026-06-27",
        branches=[{"name": "main", "ahead": 0, "behind": 0, "has_upstream": True, "dirty": False}],
        worktrees=[],
    )
    base.update(over)
    return metadata.RepoMetadata(**base)


def _stub_measure(monkeypatch, counter, record_for=None):
    """Replace the expensive measure() with a counting stub. `counter` is a mutable list used as a
    call log (each element is the key measured); `record_for` maps a key → the record to return."""
    record_for = record_for or {}

    def fake_measure(path):
        key = "/".join(str(path).split("/")[-3:])
        counter.append(key)
        return record_for.get(key, _fake_record(git_head=f"head-{key}"))

    monkeypatch.setattr(metadata, "measure", fake_measure)


# ---------------------------------------------------------------------------
# load — cold start tolerance
# ---------------------------------------------------------------------------


def test_load_absent_file_is_empty_cache_not_error(cache_env):
    cache = metadata.load()
    assert cache.version == metadata.CACHE_VERSION
    assert cache.repos == {}
    assert cache.last_updated is None


def test_load_unparseable_file_is_empty_cache(cache_env):
    path = config.cache_dir()
    path.mkdir(parents=True, exist_ok=True)
    (path / "metadata.json").write_text("{ not json ][")
    assert metadata.load().repos == {}


def test_load_wrong_version_is_empty_cache(cache_env):
    path = config.cache_dir()
    path.mkdir(parents=True, exist_ok=True)
    (path / "metadata.json").write_text(json.dumps({"version": 999, "repos": {"a/b/c": {}}}))
    assert metadata.load().repos == {}


def test_load_root_moved_drops_cached_repos(cache_env, monkeypatch):
    cache = metadata.MetadataCache(
        version=metadata.CACHE_VERSION,
        last_updated=metadata._now(),
        workspace_root="/somewhere/else",
        repos={"github/o/r": _fake_record()},
    )
    metadata.store(None, cache)
    # The persisted workspace_root != current GIT_WORKSPACE ⇒ coarse invalidate.
    assert metadata.load().repos == {}


# ---------------------------------------------------------------------------
# store / round-trip / atomicity
# ---------------------------------------------------------------------------


def test_store_then_load_round_trips_the_record(cache_env):
    from beadhive import identity

    cache = metadata.MetadataCache(
        version=metadata.CACHE_VERSION,
        last_updated=metadata._now(),
        workspace_root=identity.workspace_root(),
        repos={"github/o/r": _fake_record(disk_bytes=999, commit_count=12)},
    )
    metadata.store(None, cache)

    loaded = metadata.load()
    rec = loaded.repos["github/o/r"]
    assert rec.disk_bytes == 999
    assert rec.commit_count == 12
    assert rec.branches[0]["name"] == "main"


def test_store_leaves_no_temp_file_behind(cache_env):
    metadata.store(None, metadata.load())
    leftovers = list(config.cache_dir().glob(".metadata.json.*"))
    assert leftovers == []


# ---------------------------------------------------------------------------
# is_stale — fingerprint + TTL semantics
# ---------------------------------------------------------------------------


def test_is_stale_true_when_entry_missing(cache_env):
    assert metadata.is_stale(None, "any/path", ttl=300) is True


def test_is_stale_fingerprint_match_and_ttl_fresh(cache_env, monkeypatch):
    entry = _fake_record(git_head="H", git_mtime=5.0)
    monkeypatch.setattr(metadata, "fingerprint", lambda p: ("H", 5.0))
    assert metadata.is_stale(entry, "p", ttl=300) is False


def test_is_stale_fingerprint_mismatch(cache_env, monkeypatch):
    entry = _fake_record(git_head="OLD", git_mtime=5.0)
    monkeypatch.setattr(metadata, "fingerprint", lambda p: ("NEW", 5.0))
    assert metadata.is_stale(entry, "p", ttl=300) is True


def test_is_stale_ttl_expiry_even_when_fingerprint_matches(cache_env, monkeypatch):
    # measured long ago, fingerprint unchanged, small positive TTL ⇒ TTL backstop fires.
    entry = _fake_record(git_head="H", git_mtime=5.0, measured_at="2000-01-01T00:00:00Z")
    monkeypatch.setattr(metadata, "fingerprint", lambda p: ("H", 5.0))
    assert metadata.is_stale(entry, "p", ttl=300) is True


def test_ttl_zero_bypasses_cache_always_stale(cache_env, monkeypatch):
    entry = _fake_record(git_head="H", git_mtime=5.0, measured_at=metadata._now())
    monkeypatch.setattr(metadata, "fingerprint", lambda p: ("H", 5.0))
    assert metadata.is_stale(entry, "p", ttl=0) is True


def test_ttl_negative_never_expires(cache_env, monkeypatch):
    entry = _fake_record(git_head="H", git_mtime=5.0, measured_at="2000-01-01T00:00:00Z")
    monkeypatch.setattr(metadata, "fingerprint", lambda p: ("H", 5.0))
    assert metadata.is_stale(entry, "p", ttl=-1) is False


def test_ttl_none_uses_fingerprint_only(cache_env, monkeypatch):
    entry = _fake_record(git_head="H", git_mtime=5.0, measured_at="2000-01-01T00:00:00Z")
    monkeypatch.setattr(metadata, "fingerprint", lambda p: ("H", 5.0))
    assert metadata.is_stale(entry, "p", ttl=None) is False


# ---------------------------------------------------------------------------
# refresh — computes + persists
# ---------------------------------------------------------------------------


def test_refresh_computes_named_keys_and_persists(cache_env, monkeypatch):
    calls: list[str] = []
    _stub_measure(monkeypatch, calls)

    cache = metadata.refresh(None, ["github/o/r1", "github/o/r2"])
    assert sorted(calls) == ["github/o/r1", "github/o/r2"]
    assert set(cache.repos) == {"github/o/r1", "github/o/r2"}
    assert cache.last_updated is not None

    # Persisted across invocations: a fresh load sees the same records.
    assert set(metadata.load().repos) == {"github/o/r1", "github/o/r2"}


def test_refresh_merges_into_existing_cache(cache_env, monkeypatch):
    _stub_measure(monkeypatch, [])
    metadata.refresh(None, ["github/o/r1"])
    metadata.refresh(None, ["github/o/r2"])
    assert set(metadata.load().repos) == {"github/o/r1", "github/o/r2"}


def test_refresh_none_walks_on_disk_fleet(cache_env, monkeypatch):
    # Build a fake on-disk fleet: github/org/{a,b} are git repos, github/org/c is not.
    root = config.cache_dir().parent / "workspace"
    for name, is_git in (("a", True), ("b", True), ("c", False)):
        repo = root / "github" / "org" / name
        repo.mkdir(parents=True)
        if is_git:
            (repo / ".git").mkdir()
    monkeypatch.setattr(metadata.registry, "effective_providers", lambda cfg: ["github"])

    calls: list[str] = []
    _stub_measure(monkeypatch, calls)
    cache = metadata.refresh(None, None)
    assert sorted(calls) == ["github/org/a", "github/org/b"]  # non-git 'c' excluded
    assert set(cache.repos) == {"github/org/a", "github/org/b"}


# ---------------------------------------------------------------------------
# read_fleet — the read-through acceptance triangle
# ---------------------------------------------------------------------------


def test_read_fleet_hit_serves_cache_without_recompute(cache_env, monkeypatch):
    # Seed a fresh entry, then assert the read serves it WITHOUT calling measure.
    key = "github/o/r"
    _stub_measure(monkeypatch, [])  # only used to seed
    metadata.refresh(None, [key])

    monkeypatch.setattr(metadata, "is_stale", lambda entry, path, *, ttl: False)
    calls: list[str] = []
    _stub_measure(monkeypatch, calls)  # re-stub with a fresh call log

    out = metadata.read_fleet(None, [key], ttl=300)
    assert key in out
    assert calls == []  # HIT: nothing recomputed


def test_read_fleet_miss_computes_and_persists(cache_env, monkeypatch):
    key = "github/o/new"
    calls: list[str] = []
    _stub_measure(monkeypatch, calls)

    out = metadata.read_fleet(None, [key], ttl=300)
    assert key in out
    assert calls == [key]  # MISS: computed inline
    assert key in metadata.load().repos  # ...and persisted


def test_read_fleet_staleness_recomputes(cache_env, monkeypatch):
    key = "github/o/r"
    _stub_measure(monkeypatch, [])
    metadata.refresh(None, [key])  # seed

    # Force the entry stale ⇒ read_fleet must recompute it.
    monkeypatch.setattr(metadata, "is_stale", lambda entry, path, *, ttl: True)
    calls: list[str] = []
    _stub_measure(monkeypatch, calls)

    out = metadata.read_fleet(None, [key], ttl=300)
    assert key in out
    assert calls == [key]  # STALE: recomputed


def test_read_fleet_on_miss_stale_serves_stale_without_recompute(cache_env, monkeypatch):
    key = "github/o/r"
    _stub_measure(monkeypatch, [])
    metadata.refresh(None, [key])  # seed a (soon-to-be-stale) entry

    monkeypatch.setattr(metadata, "is_stale", lambda entry, path, *, ttl: True)
    calls: list[str] = []
    _stub_measure(monkeypatch, calls)

    out = metadata.read_fleet(None, [key], ttl=300, on_miss="stale")
    assert key in out  # serve-stale-while-revalidate: the stale record is still returned
    assert calls == []  # background refresh is bead .4 — nothing recomputed inline


def test_read_fleet_on_miss_stale_absent_when_truly_missing(cache_env, monkeypatch):
    calls: list[str] = []
    _stub_measure(monkeypatch, calls)
    out = metadata.read_fleet(None, ["github/o/absent"], ttl=300, on_miss="stale")
    assert out == {}  # missing entry left absent (no inline compute)
    assert calls == []


# ---------------------------------------------------------------------------
# config surface
# ---------------------------------------------------------------------------


def test_ttl_default_and_override():
    assert config.metadata_ttl({}) == 300.0
    assert config.metadata_ttl({"metadata": {"ttl": 60}}) == 60.0
    assert metadata.ttl({"metadata": {"ttl": 0}}) == 0.0


def test_background_reload_default_and_override():
    assert config.metadata_background_reload({}) is True
    assert config.metadata_background_reload({"metadata": {"background_reload": False}}) is False


# ---------------------------------------------------------------------------
# invalidate / invalidate_all — the mutating-op hook (bead .4)
# ---------------------------------------------------------------------------


def _seed(monkeypatch, *keys):
    """Persist a warm cache with one record per key (via a stubbed measure)."""
    _stub_measure(monkeypatch, [])
    metadata.refresh(None, list(keys))


def test_invalidate_all_clears_repos_and_resets_last_updated(cache_env, monkeypatch):
    _seed(monkeypatch, "github/o/a", "github/o/b")
    assert set(metadata.load().repos) == {"github/o/a", "github/o/b"}

    metadata.invalidate_all(None)

    loaded = metadata.load()
    assert loaded.repos == {}
    assert loaded.last_updated is None


def test_invalidate_key_none_delegates_to_invalidate_all(cache_env, monkeypatch):
    _seed(monkeypatch, "github/o/a", "github/o/b")
    assert metadata.invalidate({}, None) is None  # coarse form returns no reload thread
    assert metadata.load().repos == {}


def test_invalidate_per_repo_drops_only_that_entry(cache_env, monkeypatch):
    _seed(monkeypatch, "github/o/a", "github/o/b")
    metadata.invalidate({}, "github/o/a", reload=False)
    # Only the named entry is dropped; the other 89-warm-entries story holds (docs §3).
    assert set(metadata.load().repos) == {"github/o/b"}


def test_invalidate_absent_key_is_a_safe_noop(cache_env, monkeypatch):
    _seed(monkeypatch, "github/o/a")
    metadata.invalidate({}, "github/o/missing", reload=False)
    assert set(metadata.load().repos) == {"github/o/a"}


def test_invalidate_background_reload_recomputes_only_that_key(cache_env, monkeypatch):
    # ACCEPTANCE: an invalidated repo is background-refreshed (one key, not the fleet) so a later
    # read serves it warm WITHOUT a full blocking recompute on the hot path.
    key = "github/o/r"
    _seed(monkeypatch, key, "github/o/other")

    calls: list[str] = []
    _stub_measure(monkeypatch, calls)
    thread = metadata.invalidate({}, key)  # reload on by default
    assert thread is not None
    thread.join(timeout=5)

    assert calls == [key]  # background reload recomputed exactly the invalidated key
    assert key in metadata.load().repos  # ...and warmed it straight back into the cache

    # The subsequent hot-path read is a HIT — no inline (blocking) recompute.
    monkeypatch.setattr(metadata, "is_stale", lambda entry, path, *, ttl: False)
    hit_calls: list[str] = []
    _stub_measure(monkeypatch, hit_calls)
    out = metadata.read_fleet({}, [key], ttl=300)
    assert key in out
    assert hit_calls == []


def test_invalidate_background_reload_disabled_by_config(cache_env, monkeypatch):
    key = "github/o/r"
    _seed(monkeypatch, key)
    cfg = {"metadata": {"background_reload": False}}
    assert metadata.invalidate(cfg, key) is None  # no thread spawned
    assert key not in metadata.load().repos  # entry still dropped (invalidation happened)


def test_invalidate_is_best_effort_and_never_raises(cache_env, monkeypatch):
    # A cache-write failure must never propagate into the mutating op that called invalidate.
    _seed(monkeypatch, "github/o/a")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(metadata, "store", boom)
    assert metadata.invalidate({}, "github/o/a", reload=False) is None  # swallowed, no raise


# ---------------------------------------------------------------------------
# route.invalidate_targets — the git/bd passthrough hook
# ---------------------------------------------------------------------------


def test_route_invalidate_targets_single_rig_is_per_repo(cache_env, monkeypatch):
    from pathlib import Path

    from beadhive import identity, route

    calls: list = []
    monkeypatch.setattr(metadata, "invalidate", lambda cfg, key=None, **kw: calls.append(key))
    root = identity.workspace_root()
    route.invalidate_targets({}, [("r", str(Path(root) / "github/o/r"))])
    assert calls == ["github/o/r"]


def test_route_invalidate_targets_cwd_is_noop(cache_env, monkeypatch):
    from beadhive import route

    calls: list = []
    monkeypatch.setattr(metadata, "invalidate", lambda cfg, key=None, **kw: calls.append(key))
    route.invalidate_targets({}, [(None, None)])  # current-dir passthrough
    assert calls == []  # fingerprint probe self-heals; nothing invalidated


def test_route_invalidate_targets_fanout_is_coarse(cache_env, monkeypatch):
    from pathlib import Path

    from beadhive import identity, route

    calls: list = []
    monkeypatch.setattr(metadata, "invalidate", lambda cfg, key=None, **kw: calls.append(key))
    root = Path(identity.workspace_root())
    tgts = [("a", str(root / "github/o/a")), ("b", str(root / "github/o/b"))]
    route.invalidate_targets({}, tgts)
    assert calls == [None]  # -a fan-out ⇒ one coarse invalidate


# ---------------------------------------------------------------------------
# mutating-op wiring — representative sites call the hook (bead .4)
# ---------------------------------------------------------------------------


def test_registry_register_invalidates_the_new_key(world, monkeypatch):
    from beadhive import registry

    calls: list = []
    monkeypatch.setattr(metadata, "invalidate", lambda cfg, key=None, **kw: calls.append((key, kw)))
    registry.register("github", "acme", "widget", "aw", "personal")
    assert calls and calls[0][0] == "github/acme/widget"


def test_registry_unregister_invalidates_removed_key_without_reload(world, monkeypatch):
    from beadhive import registry

    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "github", "org": "acme", "repo": "widget", "prefix": "aw", "kind": "personal"}
    )
    config.save(cfg)

    calls: list = []
    monkeypatch.setattr(metadata, "invalidate", lambda cfg, key=None, **kw: calls.append((key, kw)))
    registry.unregister("github", "acme", "widget")
    assert calls[0][0] == "github/acme/widget"
    assert calls[0][1].get("reload") is False  # removed repo — drop only, no background walk
