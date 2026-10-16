"""Bootstrap precedence for partitioned hives (bh-lniwf.4).

docs/design/two-store-scope-adr.md section 6: a managed hive with an internal remote clones it
BEFORE anything probes the code origin's refs/dolt/data; an unmanaged hive keeps today's path;
the public store initialises separately; failures name their phase and are retry-safe.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import store_bootstrap
from beadhive.store_bootstrap import BootstrapError, RemoteUnresolved


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    checkout = root / "github" / "acme" / "widget"
    checkout.mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(root))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    return SimpleNamespace(checkout=checkout, public_root=tmp_path / "stores" / "pub")


def _entry(ws, *, internal_remote="widget-private", public=True, descriptor=True):
    entry = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"}
    stores = {"internal": {"remote": internal_remote}} if internal_remote else {"internal": {}}
    if public:
        stores["public"] = {"prefix": "widpub", "path": str(ws.public_root)}
    entry["stores"] = stores
    if descriptor:
        entry["dolt_remote"] = {"name": "widget-private", "url": "s3://bucket/internal/widget"}
    return entry


class _Runner:
    """Fake `bd init`: records calls and materialises the store dir like bd would."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __call__(self, argv, cwd, env):
        self.calls.append(SimpleNamespace(argv=argv, cwd=cwd, env=env))
        if any(flag in argv for flag in self.fail_on):
            (cwd / ".beads").mkdir(exist_ok=True)  # partial wreckage a failure leaves
            return SimpleNamespace(returncode=1)
        (cwd / ".beads").mkdir(exist_ok=True)
        return SimpleNamespace(returncode=0)


class _Clone:
    """Fake internal-clone delegate (the HQ remote bootstrap in production)."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, phase, url):
        self.calls.append((phase.name, phase.remote, url))
        if self.fail:
            raise BootstrapError("internal-clone", "clone failed")
        phase.target.beads_dir.mkdir(parents=True)


def _never(*_a, **_k):
    raise AssertionError("the code origin's refs/dolt/data must not be probed")


def _run(entry, **kw):
    kw.setdefault("runner", _Runner())
    kw.setdefault("clone_internal", _Clone())
    return store_bootstrap.execute_bootstrap(entry, **kw), kw["runner"], kw["clone_internal"]


def test_internal_clone_runs_first_and_origin_is_never_probed(ws):
    legacy_calls = []
    phases, runner, clone = _run(
        _entry(ws), origin_probe=_never, legacy=lambda: legacy_calls.append(1)
    )

    assert [p.name for p in phases] == ["internal-clone", "public-init"]
    assert legacy_calls == []  # the legacy (origin-probing) path never ran
    assert clone.calls == [("internal-clone", "widget-private", "s3://bucket/internal/widget")]
    (public,) = runner.calls  # this module never clones by itself: only the public mint runs
    assert public.cwd == ws.public_root
    assert public.argv[:3] == ["init", "--prefix", "widpub"]
    assert "--remote" not in public.argv  # the public store is minted, never cloned from origin
    assert (ws.checkout / ".beads").is_dir() and (ws.public_root / ".beads").is_dir()


def test_the_hive_descriptor_is_the_internal_remote_when_none_is_named(ws):
    phases, _runner, clone = _run(_entry(ws, internal_remote=None, public=False), legacy=_never)
    assert [(p.name, p.remote) for p in phases] == [("internal-clone", "widget-private")]
    assert clone.calls == [("internal-clone", "widget-private", "s3://bucket/internal/widget")]


def test_an_unresolvable_internal_remote_fails_closed_without_touching_anything(ws):
    runner, clone = _Runner(), _Clone()
    with pytest.raises(RemoteUnresolved) as excinfo:
        store_bootstrap.execute_bootstrap(
            _entry(ws, descriptor=False), runner=runner, clone_internal=clone, legacy=_never
        )
    assert excinfo.value.phase == "internal-clone"
    assert "refusing to fall back to the code origin" in str(excinfo.value)
    assert runner.calls == [] and clone.calls == []
    assert not (ws.checkout / ".beads").exists() and not ws.public_root.exists()


def test_hive_without_internal_remote_keeps_the_legacy_path(ws):
    legacy_calls = []
    phases, runner, clone = _run(
        _entry(ws, internal_remote=None, public=False, descriptor=False),
        legacy=lambda: legacy_calls.append(1),
    )
    assert [p.name for p in phases] == ["legacy"]
    assert legacy_calls == [1] and runner.calls == [] and clone.calls == []


def test_legacy_entry_plans_only_the_legacy_phase(ws):
    entry = {"provider": "github", "org": "acme", "repo": "widget", "prefix": "wid"}
    assert [(p.name, p.action) for p in store_bootstrap.plan_bootstrap(entry)] == [
        ("legacy", "delegate")
    ]


def test_retry_is_idempotent_once_both_stores_exist(ws):
    _run(_entry(ws))
    phases, runner, clone = _run(_entry(ws), origin_probe=_never)
    assert [p.action for p in phases] == ["skip-present", "skip-present"]
    assert runner.calls == [] and clone.calls == []


def test_public_failure_names_its_phase_and_leaves_internal_intact(ws):
    runner = _Runner(fail_on={"widpub"})
    with pytest.raises(BootstrapError) as excinfo:
        _run(_entry(ws), runner=runner)
    assert excinfo.value.phase == "public-init"
    assert (ws.checkout / ".beads").is_dir()  # internal store kept
    assert not ws.public_root.exists()  # only the root this run created is removed

    runner.fail_on.clear()  # retry resumes at the public phase only
    runner.calls.clear()
    phases, _runner, clone = _run(_entry(ws), runner=runner)
    assert [p.action for p in phases] == ["skip-present", "init"]
    assert len(runner.calls) == 1 and clone.calls == []


def test_internal_failure_names_its_phase_and_stops_before_public(ws):
    runner = _Runner()
    with pytest.raises(BootstrapError) as excinfo:
        _run(_entry(ws), runner=runner, clone_internal=_Clone(fail=True))
    assert excinfo.value.phase == "internal-clone"
    assert runner.calls == [] and not ws.public_root.exists()


def test_default_internal_clone_delegates_to_engine_clone_remote(ws, monkeypatch):
    from beadhive import engine

    calls = []

    class _Engine:
        def clone_remote(self, cwd, url, *, env=None, extra_env=None):
            calls.append((cwd, url, "BEADS_DIR" in (env or {})))
            (cwd / ".beads").mkdir()
            (cwd / ".beads" / "partial").write_text("x")
            return SimpleNamespace(returncode=1, stderr="no such remote")

    monkeypatch.setattr(engine, "get_engine", lambda cfg=None: _Engine())
    with pytest.raises(BootstrapError) as excinfo:
        store_bootstrap.execute_bootstrap(
            _entry(ws, public=False), runner=_Runner(), env={"BEADS_DIR": "/elsewhere/.beads"}
        )
    assert calls == [(ws.checkout, "s3://bucket/internal/widget", False)]
    assert excinfo.value.phase == "internal-clone"
    assert not (ws.checkout / ".beads").exists()  # partial clone discarded


def test_inherited_beads_dir_never_reaches_a_bootstrap_phase(ws):
    _phases, runner, _clone = _run(_entry(ws), env={"BEADS_DIR": "/elsewhere/.beads", "PATH": "/b"})
    assert runner.calls and all("BEADS_DIR" not in call.env for call in runner.calls)


# ---- a public store's own nested descriptor (bh-rz379) -----------------------------------


def _with_public_remote(entry, url="file:///srv/remotes/widget-public"):
    entry["stores"]["public"]["dolt_remote"] = {"name": "widget-pub", "url": url}
    entry["stores"]["public"]["remote"] = "widget-pub"
    return entry


def test_public_store_registers_its_own_descriptor_never_the_internal_one(ws):
    registered = []
    phases, runner, _clone = _run(
        _with_public_remote(_entry(ws)),
        register_public=lambda phase, url: registered.append((phase.name, phase.remote, url)),
    )
    assert [p.name for p in phases] == ["internal-clone", "public-init"]
    assert "--remote" not in runner.calls[0].argv  # still minted fresh, then registered
    assert registered == [("public-init", "widget-pub", "file:///srv/remotes/widget-public")]


def test_public_registration_failure_discards_the_mint_so_a_retry_redoes_it(ws):
    def _fail(phase, url):
        raise RuntimeError(f"cannot reach {url}")

    entry = _with_public_remote(_entry(ws), url="file:///srv/remotes/widget-public")
    with pytest.raises(BootstrapError) as excinfo:
        _run(entry, register_public=_fail)
    assert excinfo.value.phase == "public-init"
    assert (ws.checkout / ".beads").is_dir()
    assert not (ws.public_root / ".beads").exists()
    phases, _runner, _clone = _run(entry, register_public=lambda *_: None)
    assert [p.action for p in phases] == ["skip-present", "init"]


# ---- onboard wiring -----------------------------------------------------------------------


def _onboard_ctx(ws, entry):
    from beadhive import onboard

    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(ws.checkout),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(ws.checkout),
        prefix="wid",
        cfg={"managed_repos": [entry]},
    )
    ctx._derived = True
    return ctx


def test_onboard_rerun_on_an_initialised_hive_adds_only_the_public_store(ws, monkeypatch):
    from beadhive import hive, onboard

    (ws.checkout / ".beads").mkdir()
    runner = _Runner()
    monkeypatch.setattr(store_bootstrap, "_default_runner", runner)
    monkeypatch.setattr(onboard, "_configure_auto_export", lambda ctx: None)
    monkeypatch.setattr(hive, "_ensure_bd_backup_gitignored", lambda base: None)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", _never)

    onboard._act_bd_init(_onboard_ctx(ws, _entry(ws)))

    (call,) = runner.calls
    assert call.cwd == ws.public_root and "widpub" in call.argv


def test_onboard_refuses_an_invalid_stores_declaration(ws, monkeypatch):
    import typer

    from beadhive import onboard

    entry = _entry(ws)
    entry["stores"].pop("internal")
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", _never)
    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_onboard_ctx(ws, entry))
    assert not (ws.checkout / ".beads").exists()
