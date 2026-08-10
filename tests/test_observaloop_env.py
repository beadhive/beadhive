"""— per-worktree endpoint overlay (writer + loader + self-heal).

Covers:
- WRITER write_worktree_env: KEY=VALUE lines (endpoint + profile, optional resource attrs),
  and `.bh/` gitignored in a real worktree via the git exclude file.
- LOADER load_worktree_env: a present `.bh/otel.env` is overlaid into os.environ before
  otel.init, so config.otel_endpoint + the observaloop.profile Resource attr reflect it.
- no-overwrite: an already-set env var always wins the overlay.
- import-free common path: the cache-present (and observaloop-off) paths never import
  beadhive.observaloop.
- self-heal: a missing cache is regenerated when observaloop is enabled + available (faked),
  and only when the collector is actually RUNNING — a profile that cannot start writes no
  overlay at all rather than a durable dead endpoint (bh-nm1tu).
- stale repair: an overlay holding a scheme-less endpoint is regenerated rather than honoured,
  since the bare host:port form silently disables the OTLP exporter (bh-jdopc).
- skip-verify: ephemeral verify- worktrees are never overlaid.
- off/absent: outside a worktree, or cache-absent with observaloop off, is a quiet no-op.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from beadhive import config, observaloop_env, otel

_OVERLAY_KEYS = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "BH_OBSERVALOOP_PROFILE",
    "OTEL_RESOURCE_ATTRIBUTES",
)

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


@pytest.fixture(autouse=True)
def _clean_overlay_env():
    """The loader mutates os.environ directly (not via monkeypatch), so snapshot + restore the
    overlay keys around every test to keep the suite hermetic."""
    saved = {k: os.environ.get(k) for k in _OVERLAY_KEYS}
    for k in _OVERLAY_KEYS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _worktree(tmp_path, monkeypatch, leaf="ag-epic-3", *, chdir=True):
    """Create + return a managed worktree dir <root>/github/myorg/myrepo/<leaf> under a temp
    shadow root (via $WS_WORKTREES), optionally chdir'ing into it."""
    root = (tmp_path / "wts").resolve()
    monkeypatch.setenv("WS_WORKTREES", str(root))
    wt = root / "github" / "myorg" / "myrepo" / leaf
    wt.mkdir(parents=True)
    if chdir:
        monkeypatch.chdir(wt)
    return wt


# ---- writer -----------------------------------------------------------------


def test_write_worktree_env_writes_endpoint_and_profile(tmp_path):
    env_file = observaloop_env.write_worktree_env(tmp_path, "mr", "http://localhost:4318")

    assert env_file == tmp_path / ".bh" / "otel.env"
    lines = env_file.read_text().splitlines()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318" in lines
    assert "BH_OBSERVALOOP_PROFILE=mr" in lines
    # resource attrs omitted when not supplied
    assert not any(line.startswith("OTEL_RESOURCE_ATTRIBUTES=") for line in lines)


def test_write_worktree_env_includes_resource_attrs_when_given(tmp_path):
    env_file = observaloop_env.write_worktree_env(
        tmp_path, "mr", "http://localhost:4318", resource_attrs="ws.profile=mr"
    )
    assert "OTEL_RESOURCE_ATTRIBUTES=ws.profile=mr" in env_file.read_text().splitlines()


def test_write_worktree_env_gitignores_ws_dir_in_real_worktree(tmp_path):
    """`.bh/` lands in the worktree's git exclude file, so the cache never shows in git status."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_CLEAN_ENV)

    observaloop_env.write_worktree_env(repo, "mr", "http://localhost:4318")

    exclude = repo / ".git" / "info" / "exclude"
    assert ".bh/" in exclude.read_text().splitlines()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
        env=_CLEAN_ENV,
    )
    assert ".bh" not in status.stdout


def test_write_worktree_env_exclude_is_idempotent(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_CLEAN_ENV)

    observaloop_env.write_worktree_env(repo, "mr", "http://localhost:4318")
    observaloop_env.write_worktree_env(repo, "mr", "http://localhost:4318")

    lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count(".bh/") == 1  # appended once, not duplicated


def test_write_worktree_env_without_git_is_best_effort(tmp_path):
    """A plain (non-git) dir: the env file is still written, the exclude step simply skips."""
    env_file = observaloop_env.write_worktree_env(tmp_path, "mr", "http://localhost:4318")
    assert env_file.is_file()  # no raise despite no .git


# ---- loader: present cache --------------------------------------------------


def test_load_overlays_present_cache_into_environ(tmp_path, monkeypatch):
    wt = _worktree(tmp_path, monkeypatch)
    observaloop_env.write_worktree_env(wt, "mr", "http://localhost:4318")

    observaloop_env.load_worktree_env({})

    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4318"
    assert os.environ["BH_OBSERVALOOP_PROFILE"] == "mr"


def test_overlay_routes_otel_endpoint_and_profile_attr(tmp_path, monkeypatch):
    """End-to-end acceptance: after the overlay loads, config.otel_endpoint + the
    observaloop.profile Resource attr reflect the worktree's hive profile endpoint."""
    wt = _worktree(tmp_path, monkeypatch)
    observaloop_env.write_worktree_env(wt, "mr", "http://localhost:4318")
    cfg = {
        "managed_repos": [{"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}]
    }

    observaloop_env.load_worktree_env(cfg)

    assert config.otel_endpoint(cfg) == "http://localhost:4318"
    attrs: dict[str, str] = {}
    otel._enrich_resource(attrs, cfg)
    assert attrs["observaloop.profile"] == "mr"


def test_load_does_not_overwrite_already_set_env(tmp_path, monkeypatch):
    wt = _worktree(tmp_path, monkeypatch)
    observaloop_env.write_worktree_env(wt, "mr", "http://localhost:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://preset:9999")

    observaloop_env.load_worktree_env({})

    # the preset endpoint wins; the absent profile key is still filled from the overlay
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://preset:9999"
    assert os.environ["BH_OBSERVALOOP_PROFILE"] == "mr"


def test_apply_env_skips_comments_blanks_and_malformed(tmp_path):
    env_file = tmp_path / "otel.env"
    env_file.write_text("# a comment\n\nNO_EQUALS_HERE\nOTEL_EXPORTER_OTLP_ENDPOINT=http://x:1\n")

    observaloop_env._apply_env(env_file)

    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://x:1"
    assert "NO_EQUALS_HERE" not in os.environ


# ---- import-free common path ------------------------------------------------


def test_present_cache_does_not_import_observaloop(tmp_path, monkeypatch):
    wt = _worktree(tmp_path, monkeypatch)
    observaloop_env.write_worktree_env(wt, "mr", "http://localhost:4318")
    sys.modules.pop("beadhive.observaloop", None)

    observaloop_env.load_worktree_env({})

    assert "beadhive.observaloop" not in sys.modules  # hot path stays free of the observaloop seam


def test_missing_cache_observaloop_off_does_not_import_observaloop(tmp_path, monkeypatch):
    """Cache absent but observaloop disabled → quick check, no observaloop import, no overlay."""
    _worktree(tmp_path, monkeypatch)  # no write_worktree_env → cache missing
    sys.modules.pop("beadhive.observaloop", None)

    observaloop_env.load_worktree_env({"otel": {"enabled": False}})

    assert "beadhive.observaloop" not in sys.modules
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ


def _stub_collector(monkeypatch, observaloop, *, running=True, up_makes_it_run=False):
    """Stub the collector-liveness pair `_self_heal` consults, and record `up` calls.

    `running` is the state BEFORE any `up`; `up_makes_it_run` models a collector that starts
    successfully when asked. The returned list captures every `up` invocation so a test can assert
    the heal tried exactly once and no more."""
    state = {"running": running}
    up_calls: list[str] = []

    def _up(name, cfg=None):
        up_calls.append(name)
        if up_makes_it_run:
            state["running"] = True
        return {}

    monkeypatch.setattr(observaloop, "profile_status", lambda name, cfg=None: dict(state))
    monkeypatch.setattr(observaloop, "up", _up)
    return up_calls


# ---- stale-overlay repair (bh-jdopc) ----------------------------------------


def test_scheme_less_overlay_is_regenerated_not_honoured(tmp_path, monkeypatch):
    """An overlay written in the pre-bh-jdopc scheme-less form is HEALED, not loaded.

    This is the case the old cache-miss-only heal could never reach: the file exists, so the
    loader took the fast path and re-applied a broken endpoint on every invocation, forever. The
    assertion that matters is the SECOND one — not merely that healing ran, but that the
    scheme-less value never reached os.environ, because that value is what silently disables the
    exporter."""
    wt = _worktree(tmp_path, monkeypatch)
    (wt / ".bh").mkdir(parents=True, exist_ok=True)
    (wt / ".bh" / "otel.env").write_text(
        "OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4321\nBH_OBSERVALOOP_PROFILE=mr\n"
    )
    cfg = {
        "otel": {"enabled": True},
        "observaloop": {"enabled": True},
        "managed_repos": [{"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}],
    }
    from beadhive import observaloop

    monkeypatch.setattr(observaloop, "is_available", lambda cfg=None: True)
    _stub_collector(monkeypatch, observaloop, running=True)
    monkeypatch.setattr(
        observaloop, "endpoint_for", lambda name, proto, cfg=None: "http://healed:4318"
    )

    observaloop_env.load_worktree_env(cfg)

    assert (
        (wt / ".bh" / "otel.env")
        .read_text()
        .startswith("OTEL_EXPORTER_OTLP_ENDPOINT=http://healed:4318")
    )
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://healed:4318"


def test_scheme_qualified_overlay_takes_the_fast_path(tmp_path, monkeypatch):
    """A well-formed overlay is still honoured WITHOUT importing observaloop.

    The staleness check must not cost the common path its import-free fast route — if it did, the
    repair would be paid for on every CLI invocation in every worktree forever. Proven by deleting
    beadhive.observaloop from sys.modules and asserting it stays absent."""
    wt = _worktree(tmp_path, monkeypatch)
    (wt / ".bh").mkdir(parents=True, exist_ok=True)
    (wt / ".bh" / "otel.env").write_text(
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4321\nBH_OBSERVALOOP_PROFILE=mr\n"
    )
    sys.modules.pop("beadhive.observaloop", None)

    observaloop_env.load_worktree_env({"otel": {"enabled": True}, "observaloop": {"enabled": True}})

    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4321"
    assert "beadhive.observaloop" not in sys.modules


# ---- dead-endpoint prevention (bh-nm1tu) ------------------------------------


def test_self_heal_writes_no_overlay_when_collector_cannot_start(tmp_path, monkeypatch):
    """A collector that will not come up produces NO overlay — bh-nm1tu's regression guard.

    The old path wrote the manifest endpoint regardless, so one failed `profile_up` left a durable
    file pointing at a port with nothing behind it — and because the file then EXISTED, self-heal
    could never fire again to correct it. Absence is the correct degradation: it keeps the next
    invocation eligible to heal. Asserted on the FILE, because a written file is the trap."""
    wt = _worktree(tmp_path, monkeypatch)
    cfg = {
        "otel": {"enabled": True},
        "observaloop": {"enabled": True},
        "managed_repos": [{"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}],
    }
    from beadhive import observaloop

    monkeypatch.setattr(observaloop, "is_available", lambda cfg=None: True)
    up_calls = _stub_collector(monkeypatch, observaloop, running=False, up_makes_it_run=False)
    monkeypatch.setattr(
        observaloop, "endpoint_for", lambda name, proto, cfg=None: "http://dead:4321"
    )

    observaloop_env.load_worktree_env(cfg)

    assert not (wt / ".bh" / "otel.env").exists()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ
    assert up_calls == ["mr"]  # tried exactly once, never a retry loop


def test_self_heal_starts_a_stopped_collector_then_writes(tmp_path, monkeypatch):
    """The genuine first-run case: profile exists but was never started → bring it up, then write.

    This is the half of bh-nm1tu that must NOT regress into "never write anything". A collector
    that starts successfully should still produce a working overlay in the same invocation."""
    wt = _worktree(tmp_path, monkeypatch)
    cfg = {
        "otel": {"enabled": True},
        "observaloop": {"enabled": True},
        "managed_repos": [{"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}],
    }
    from beadhive import observaloop

    monkeypatch.setattr(observaloop, "is_available", lambda cfg=None: True)
    up_calls = _stub_collector(monkeypatch, observaloop, running=False, up_makes_it_run=True)
    monkeypatch.setattr(
        observaloop, "endpoint_for", lambda name, proto, cfg=None: "http://live:4318"
    )

    observaloop_env.load_worktree_env(cfg)

    assert up_calls == ["mr"]
    assert (wt / ".bh" / "otel.env").is_file()
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://live:4318"


def test_self_heal_does_not_call_up_when_already_running(tmp_path, monkeypatch):
    """The hot path pays a status read, never an `up`, when the collector is already live."""
    _worktree(tmp_path, monkeypatch)
    cfg = {
        "otel": {"enabled": True},
        "observaloop": {"enabled": True},
        "managed_repos": [{"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}],
    }
    from beadhive import observaloop

    monkeypatch.setattr(observaloop, "is_available", lambda cfg=None: True)
    up_calls = _stub_collector(monkeypatch, observaloop, running=True)
    monkeypatch.setattr(
        observaloop, "endpoint_for", lambda name, proto, cfg=None: "http://live:4318"
    )

    observaloop_env.load_worktree_env(cfg)

    assert up_calls == []  # no container start on the common path


# ---- self-heal --------------------------------------------------------------


def test_self_heal_regenerates_missing_cache_when_enabled_and_available(tmp_path, monkeypatch):
    wt = _worktree(tmp_path, monkeypatch)  # cache missing
    cfg = {
        "otel": {"enabled": True},
        "observaloop": {"enabled": True},
        "managed_repos": [{"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}],
    }
    from beadhive import observaloop

    monkeypatch.setattr(observaloop, "is_available", lambda cfg=None: True)
    _stub_collector(monkeypatch, observaloop, running=True)
    monkeypatch.setattr(
        observaloop, "endpoint_for", lambda name, proto, cfg=None: "http://healed:4318"
    )

    observaloop_env.load_worktree_env(cfg)

    # cache (re)written AND loaded into the environment
    assert (wt / ".bh" / "otel.env").is_file()
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://healed:4318"
    assert os.environ["BH_OBSERVALOOP_PROFILE"] == "mr"


def test_self_heal_skipped_when_observaloop_unavailable(tmp_path, monkeypatch):
    wt = _worktree(tmp_path, monkeypatch)
    cfg = {
        "otel": {"enabled": True},
        "observaloop": {"enabled": True},
        "managed_repos": [{"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}],
    }
    from beadhive import observaloop

    monkeypatch.setattr(observaloop, "is_available", lambda cfg=None: False)

    observaloop_env.load_worktree_env(cfg)

    assert not (wt / ".bh" / "otel.env").exists()  # nothing written when unavailable
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ


# ---- skip-verify + off/absent ------------------------------------------------


def test_verify_worktree_is_never_overlaid(tmp_path, monkeypatch):
    """Ephemeral verify- clean-checkout worktrees are skipped even with a cache present."""
    wt = _worktree(tmp_path, monkeypatch, leaf="verify-ag-epic-3")
    observaloop_env.write_worktree_env(wt, "mr", "http://localhost:4318")

    observaloop_env.load_worktree_env({})

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ


def test_outside_any_worktree_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_WORKTREES", str((tmp_path / "wts").resolve()))
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    observaloop_env.load_worktree_env({})  # no raise

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ


def test_loader_never_raises_on_unreadable_cache(tmp_path, monkeypatch):
    """A `.bh` that is a FILE (not a dir) makes the env-file read fail — the loader swallows it."""
    root = (tmp_path / "wts").resolve()
    monkeypatch.setenv("WS_WORKTREES", str(root))
    wt = root / "github" / "myorg" / "myrepo" / "ag-epic-3"
    wt.mkdir(parents=True)
    (wt / ".bh").write_text("not a dir")  # otel.env can't resolve under a file
    monkeypatch.chdir(wt)

    observaloop_env.load_worktree_env({})  # best-effort: no raise

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in os.environ
