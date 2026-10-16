"""Pull-only contribution policy in bulk hive sync (bh-0azjq).

Reference fixture: the ``contrib/briancripe/beads`` dual-remote shape — Git ``origin`` is the
writable contribution fork, Git ``upstream`` is the canonical repo with a disabled pushurl, the
hive's Dolt store has BOTH an ``origin`` and an ``upstream`` Dolt remote (Git-backed,
``refs/dolt/data``), and its Beads config says ``no-push: true`` with ``sync.remote`` still
pointing at the fork. Every remote is a disposable bare repo under the test workspace; the bd
engine is stubbed at the ``Engine`` seam and records every pull/push it is asked for.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import config, hive_sync, sync_policy, sync_remote
from beadhive.cli import app
from beadhive.engine import SyncOutcome
from beadhive.identity import workspace_root
from beadhive.sync_policy import SyncPolicy, normalize_url, pull_route

_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
_BEADS_ID = "contrib/briancripe/beads"


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=_ENV
    )


def _bare(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "--bare", "-b", "main", cwd=path)
    return path


def _refs(bare: Path) -> str:
    return _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=bare).stdout


def _init_clone(clone: Path) -> None:
    clone.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=clone)
    _git("config", "user.email", "test@ws.dev", cwd=clone)
    _git("config", "user.name", "WS Test", cwd=clone)
    (clone / ".gitignore").write_text(".beads/\n")
    (clone / "file.txt").write_text("hello")
    _git("add", ".", cwd=clone)
    _git("commit", "-qm", "init", cwd=clone)


class _Fixture:
    def __init__(self, clone: Path, fork: Path, upstream: Path):
        self.clone, self.fork, self.upstream = clone, fork, upstream

    def snapshot(self) -> tuple[str, str]:
        return _refs(self.fork), _refs(self.upstream)


def _make_contrib_beads(
    *, entry_overrides: dict | None = None, beads_cfg: str | None = None
) -> _Fixture:
    """Build + register the contrib/briancripe/beads dual-remote shape with an UNPUSHED
    contribution branch on the fork and (stubbed) unverifiable Dolt state."""
    root = Path(workspace_root())
    fork = _bare(root / "_remotes" / "briancripe" / "beads.git")
    upstream = _bare(root / "_remotes" / "gastownhall" / "beads.git")
    clone = root / "contrib" / "briancripe" / "beads"
    _init_clone(clone)
    _git("remote", "add", "origin", str(fork), cwd=clone)
    _git("remote", "add", "upstream", str(upstream), cwd=clone)
    _git("push", "-q", "upstream", "main", cwd=clone)
    _git("remote", "set-url", "--push", "upstream", "disabled://read-only", cwd=clone)
    _git("push", "-q", "-u", "origin", "main", cwd=clone)
    _git("checkout", "-q", "-b", "fix/contribution", cwd=clone)
    _git("push", "-q", "-u", "origin", "fix/contribution", cwd=clone)
    (clone / "fix.txt").write_text("unpublished contribution work")
    _git("add", ".", cwd=clone)
    _git("commit", "-qm", "fix: contribution", cwd=clone)

    (clone / ".beads").mkdir()
    (clone / ".beads" / "config.yaml").write_text(
        beads_cfg
        if beads_cfg is not None
        else (f'no-push: true\ndolt:\n    auto-push: false\nsync.remote: "git+file://{fork}"\n')
    )

    entry = {
        "provider": "contrib",
        "org": "briancripe",
        "repo": "beads",
        "prefix": "bd",
        "kind": "external",
        "contribution": "pull",
        "upstream": "gastownhall/beads",
        **(entry_overrides or {}),
    }
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(entry)
    config.save(cfg)
    return _Fixture(clone, fork, upstream)


def _make_internal_hive() -> tuple[Path, Path]:
    """An ordinary internal hive with an unpushed commit — the control that must still publish."""
    root = Path(workspace_root())
    remote = _bare(root / "_remotes" / "myrepo.git")
    clone = root / "github" / "myorg" / "myrepo"
    _init_clone(clone)
    _git("remote", "add", "origin", str(remote), cwd=clone)
    _git("push", "-q", "-u", "origin", "main", cwd=clone)
    (clone / "extra.txt").write_text("internal work")
    _git("add", ".", cwd=clone)
    _git("commit", "-qm", "feat: internal", cwd=clone)
    (clone / ".beads").mkdir()
    cfg = config.load()
    cfg.setdefault("managed_repos", []).append(
        {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr", "kind": "personal"}
    )
    config.save(cfg)
    return clone, remote


class _Recorder:
    """Engine stub: records pulls/pushes by (hive dir name, remote[, force])."""

    def __init__(self, pull_rc: int = 0):
        self.pulls: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, str, bool]] = []
        self._pull_rc = pull_rc

    def federation_status(self, cwd, *, timeout=None):  # pragma: no cover - tripwire
        raise AssertionError("sync remotes must not probe federation peers")

    def pull_state(self, cwd, *, remote=""):
        self.pulls.append((Path(cwd).name, remote))
        stderr = "" if self._pull_rc == 0 else "fatal: remote 'upstream' unreachable"
        return subprocess.CompletedProcess(args=[], returncode=self._pull_rc, stderr=stderr)

    def push_state(self, cwd, actor="", message="", *, remote="", force=False):
        self.pushes.append((Path(cwd).name, remote, force))
        return subprocess.CompletedProcess(args=[], returncode=0)


@pytest.fixture
def stub(monkeypatch, world):
    recorder = _Recorder()
    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: recorder)
    # Any .beads store has an (unverifiable) Dolt remote → "unknown", pull- and push-eligible.
    monkeypatch.setattr(
        "beadhive.safety._bd_dolt_status_payload",
        lambda path: {"mode": "embedded", "schema_version": 1},
    )
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)
    return recorder


def _dolt_remotes(monkeypatch, fx: _Fixture, *, with_upstream: bool = True) -> None:
    remotes = {
        "beads": [{"name": "origin", "url": f"git+file://{fx.fork}"}]
        + ([{"name": "upstream", "url": f"git+file://{fx.upstream}"}] if with_upstream else [])
    }
    monkeypatch.setattr(
        sync_remote, "_dolt_remotes", lambda clone_path: remotes.get(Path(clone_path).name, [])
    )


# ---------------------------------------------------------------------------
# policy resolution — pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "pull_only"),
    [
        ({"kind": "external", "contribution": "pull"}, True),
        ({"kind": "external"}, True),
        ({"kind": "fork", "contribution": "pull"}, True),
        ({"kind": "fork"}, False),
        ({"kind": "personal"}, False),
        ({"kind": "org-native"}, False),
    ],
)
def test_resolve_reads_registry_contribution_policy(tmp_path, entry, pull_only):
    assert sync_policy.resolve(entry, tmp_path).pull_only is pull_only


def test_resolve_honors_beads_no_push(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "config.yaml").write_text("no-push: true\n")

    policy = sync_policy.resolve({"kind": "personal"}, tmp_path)

    assert policy.pull_only
    assert policy.reasons == (".beads/config.yaml no-push=true",)


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("git+https://github.com/gastownhall/beads.git", "github.com/gastownhall/beads"),
        ("https://GitHub.com/gastownhall/beads", "github.com/gastownhall/beads"),
        ("git@github.com:gastownhall/beads.git", "github.com/gastownhall/beads"),
        ("git+ssh://git@github.com/gastownhall/beads.git", "github.com/gastownhall/beads"),
        ("git+file:///srv/remotes/beads.git", "/srv/remotes/beads"),
        ("/srv/remotes/beads.git/", "/srv/remotes/beads"),
        ("", ""),
    ],
)
def test_normalize_url(url, want):
    assert normalize_url(url) == want


_POLICY = SyncPolicy(pull_only=True, reasons=("registry contribution=pull",), upstream="gh/beads")
_REMOTES = [
    {"name": "origin", "url": "git+https://github.com/me/beads.git"},
    {"name": "mirror", "url": "git+https://github.com/gh/beads.git"},
]


def test_pull_route_matches_declared_upstream_by_url_not_name():
    route = pull_route(
        _POLICY,
        dolt_remotes=_REMOTES,
        git_origin_url="https://github.com/me/beads.git",
        sync_remote_url="git+https://github.com/me/beads.git",
    )

    assert (route.remote, route.refused) == ("mirror", "")
    assert any("writable Git origin fork" in note for note in route.notes)
    assert any(
        "bd config set sync.remote git+https://github.com/gh/beads.git" in n for n in route.notes
    )


def test_pull_route_refuses_when_no_dolt_remote_reaches_upstream():
    route = pull_route(_POLICY, dolt_remotes=_REMOTES[:1])

    assert route.remote == ""
    assert "bd dolt remote add upstream git+https://github.com/gh/beads.git" in route.refused


def test_pull_route_without_declared_upstream_uses_bd_default():
    policy = SyncPolicy(pull_only=True, reasons=("no-push",))

    assert pull_route(policy, dolt_remotes=_REMOTES[:1]) == sync_policy.PullRoute()


def test_pull_route_explicit_remote_wins():
    assert pull_route(_POLICY, dolt_remotes=_REMOTES, explicit_remote="origin").remote == "origin"


# ---------------------------------------------------------------------------
# bulk sync against the contrib/briancripe/beads dual-remote shape
# ---------------------------------------------------------------------------


def test_bulk_sync_pulls_upstream_and_publishes_nothing(world, stub, monkeypatch, capsys):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    before = fx.snapshot()

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == []
    assert stub.pulls == [("beads", "upstream")]
    assert stub.pushes == []
    assert fx.snapshot() == before  # fork code refs + upstream untouched
    assert plan.pulled == [_BEADS_ID]
    assert plan.pull_sources == {_BEADS_ID: "upstream"}
    assert plan.policy_skipped == {_BEADS_ID: ["git: fix/contribution", "dolt: refs/dolt/data"]}
    assert plan.pushed_branches == {} and plan.dolt_pushed == []
    assert plan.records[0].policy == "pull-only"
    out = capsys.readouterr().out
    assert "push skipped — pull-only" in out
    assert "    pushed git" not in out and "    pushed dolt" not in out
    assert "would attempt idempotent bd dolt push" not in out
    # sync.remote still names the fork: diagnosed, with a non-publishing repair command.
    assert "is the writable Git origin fork" in out
    assert f"bd config set sync.remote git+file://{fx.upstream}" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["hive", "sync", "--all"],
        ["hive", "sync", "remotes", "--all"],
        ["hive", "sync", "remotes", "--all", "--push"],
        ["hive", "sync", "remotes", "--all", "--push", "--force"],
        ["hive", "sync", "remotes", "--all", "--push", "--remote", "origin", "--force"],
        ["hive", "sync", "remotes", "--all", "--push", "--remote", "upstream", "--force"],
        ["hive", "sync", "remotes", _BEADS_ID, "--push", "--force"],
        ["hive", "sync-remote", "--all"],
    ],
)
def test_no_cli_path_bypasses_pull_only_policy(world, stub, monkeypatch, argv):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    before = fx.snapshot()

    res = CliRunner().invoke(app, argv)

    assert res.exit_code == 0, res.output
    assert stub.pushes == []
    assert fx.snapshot() == before
    assert "push skipped — pull-only" in res.output


def test_internal_hive_still_publishes_beside_a_pull_only_hive(world, stub, monkeypatch):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    _clone, remote = _make_internal_hive()
    before = fx.snapshot()

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == []
    assert plan.pushed_branches == {"github/myorg/myrepo": ["main"]}
    assert plan.dolt_pushed == ["github/myorg/myrepo"]
    assert stub.pushes == [("myrepo", "", False)]
    assert ("myrepo", "") in stub.pulls
    assert "feat: internal" in _git("log", "--format=%s", "main", cwd=remote).stdout
    assert fx.snapshot() == before
    assert list(plan.policy_skipped) == [_BEADS_ID]


def test_dry_run_names_the_policy_skip_and_upstream_source(world, stub, monkeypatch, capsys):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)

    plan = sync_remote.sync_remote(dry_run=True, pull=True, push=True)

    out = capsys.readouterr().out
    assert stub.pulls == [] and stub.pushes == []
    assert "would pull from upstream: refs/dolt/data (push withheld: pull-only)" in out
    assert "would skip push — pull-only" in out
    assert "would push" not in out
    assert plan.offending == []


def test_missing_upstream_dolt_remote_refuses_pull_and_still_publishes_nothing(
    world, stub, monkeypatch, capsys
):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx, with_upstream=False)
    before = fx.snapshot()

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == [_BEADS_ID]
    assert stub.pulls == [] and stub.pushes == []
    assert fx.snapshot() == before
    assert "bd dolt remote add upstream" in capsys.readouterr().err


def test_genuine_pull_failure_is_distinct_from_a_policy_skip(world, monkeypatch, capsys):
    recorder = _Recorder(pull_rc=1)
    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: recorder)
    monkeypatch.setattr(
        "beadhive.safety._bd_dolt_status_payload",
        lambda path: {"mode": "embedded", "schema_version": 1},
    )
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == [_BEADS_ID]
    assert plan.pulled == []
    assert recorder.pushes == []
    assert "failed to pull dolt" in capsys.readouterr().err


def test_dirty_pull_only_checkout_is_not_a_fleet_failure(world, stub, monkeypatch):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    (fx.clone / "file.txt").write_text("contributor work in progress")

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == []
    assert stub.pushes == []
    assert plan.records[0].status == sync_remote.SyncStatus.DIRTY


def test_beads_no_push_alone_makes_an_internal_hive_pull_only(world, stub, monkeypatch):
    clone, remote = _make_internal_hive()
    (clone / ".beads" / "config.yaml").write_text("no-push: true\n")
    monkeypatch.setattr(sync_remote, "_dolt_remotes", lambda clone_path: [])
    before = _refs(remote)

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert stub.pushes == []
    assert stub.pulls == [("myrepo", "")]  # no declared upstream → bd's default remote
    assert _refs(remote) == before
    assert plan.policy_skipped == {"github/myorg/myrepo": ["git: main", "dolt: refs/dolt/data"]}


def test_federation_sync_skips_a_pull_only_hive(world, monkeypatch, capsys):
    _make_contrib_beads()
    calls: list[Path] = []

    class _Fed:
        def sync_state(self, cwd, *, peer=None, strategy=None, timeout=None):
            calls.append(Path(cwd))
            return SyncOutcome(ok=True)

    monkeypatch.setattr(hive_sync.engine, "get_engine", lambda cfg=None: _Fed())

    offending = hive_sync.hive_sync(hive_ids=None, strategy="ours")

    assert offending == [] and calls == []
    assert "federation sync skipped — pull-only" in capsys.readouterr().out


def test_plan_payload_distinguishes_policy_skip_push_and_pull(world, stub, monkeypatch):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    _make_internal_hive()

    doc = sync_remote.plan_payload(sync_remote.sync_remote(dry_run=False, pull=True, push=True))

    by_hive = {h["hive"]: h for h in doc["hives"]}
    beads = by_hive[_BEADS_ID]
    assert beads["outcome"] == "policy-skipped"
    assert beads["policy"] == "pull-only"
    assert beads["pulled"] is True and beads["pull_source"] == "upstream"
    assert beads["withheld"] == ["git: fix/contribution", "dolt: refs/dolt/data"]
    assert beads["dolt_pushed"] is False and beads["pushed_branches"] == []
    internal = by_hive["github/myorg/myrepo"]
    assert internal["outcome"] == "pushed" and internal["policy"] == "read-write"
    assert doc["ok"] is True and doc["offending"] == []


def test_plan_payload_marks_a_genuine_pull_failure_offending(world, monkeypatch):
    recorder = _Recorder(pull_rc=1)
    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: recorder)
    monkeypatch.setattr(
        "beadhive.safety._bd_dolt_status_payload",
        lambda path: {"mode": "embedded", "schema_version": 1},
    )
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)

    doc = sync_remote.plan_payload(sync_remote.sync_remote(dry_run=False, pull=True, push=True))

    assert doc["ok"] is False
    assert doc["hives"][0]["outcome"] == "offending"
    assert recorder.pushes == []


# ---------------------------------------------------------------------------
# pull-only hive that ALSO has an HQ-declared object-store remote (bh-b1scz.7)
# ---------------------------------------------------------------------------

_HQ_LABEL = "beads-store (s3 bucket beads-store/beads)"
_HQ_ENV = {"AWS_ACCESS_KEY_ID": "stub-key-id", "AWS_SECRET_ACCESS_KEY": "stub-secret"}


class _EnvRecorder(_Recorder):
    """Engine stub that also records the per-call credential env a pull/push was handed."""

    def __init__(self):
        super().__init__()
        self.pull_envs: list[dict | None] = []
        self.push_envs: list[dict | None] = []

    def pull_state(self, cwd, *, remote="", extra_env=None):
        self.pull_envs.append(extra_env)
        return super().pull_state(cwd, remote=remote)

    def push_state(self, cwd, actor="", message="", *, remote="", force=False, extra_env=None):
        self.push_envs.append(extra_env)
        return super().push_state(cwd, actor, message, remote=remote, force=force)


@pytest.fixture
def hq_stub(monkeypatch, world):
    recorder = _EnvRecorder()
    monkeypatch.setattr(sync_remote.engine, "get_engine", lambda cfg=None: recorder)
    monkeypatch.setattr(
        "beadhive.safety._bd_dolt_status_payload",
        lambda path: {"mode": "embedded", "schema_version": 1},
    )
    monkeypatch.setattr("beadhive.safety._bd_has_dolt_remote", lambda path: True)
    return recorder


def _hq_object_store(monkeypatch, hive_id: str, *, missing: str = "") -> None:
    """Declare an HQ object-store remote for ``hive_id`` without touching S3 or a secret store."""

    def fake(cfg, hid, *, resolve_credentials=True):
        if hid != hive_id:
            return None
        if not resolve_credentials:
            return sync_remote.HqRemote(label=_HQ_LABEL, name="beads-store")
        return sync_remote.HqRemote(
            label=_HQ_LABEL,
            name="beads-store",
            extra_env={} if missing else dict(_HQ_ENV),
            missing=missing,
        )

    monkeypatch.setattr(sync_remote, "_hq_remote", fake)


def test_pull_only_hive_with_hq_object_store_pulls_upstream_with_hq_env(
    world, hq_stub, monkeypatch, capsys
):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    _hq_object_store(monkeypatch, _BEADS_ID)
    before = fx.snapshot()

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == []
    # The declared-upstream route outranks the HQ remote name; the HQ credentials still ride.
    assert hq_stub.pulls == [("beads", "upstream")]
    assert hq_stub.pull_envs == [_HQ_ENV]
    assert plan.pull_sources == {_BEADS_ID: "upstream"}
    # An object-store remote never unlocks a push for a pull-only hive.
    assert hq_stub.pushes == []
    assert fx.snapshot() == before
    assert plan.dolt_pushed == []
    assert plan.policy_skipped == {_BEADS_ID: ["git: fix/contribution", f"dolt: {_HQ_LABEL}"]}
    assert "push skipped — pull-only" in capsys.readouterr().out


def test_pull_only_hive_without_upstream_route_pulls_the_hq_remote(world, hq_stub, monkeypatch):
    clone, remote = _make_internal_hive()
    (clone / ".beads" / "config.yaml").write_text("no-push: true\n")
    monkeypatch.setattr(sync_remote, "_dolt_remotes", lambda clone_path: [])
    _hq_object_store(monkeypatch, "github/myorg/myrepo")
    before = _refs(remote)

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == []
    assert hq_stub.pulls == [("myrepo", "beads-store")]  # no route remote → HQ remote name
    assert hq_stub.pull_envs == [_HQ_ENV]
    assert hq_stub.pushes == []
    assert _refs(remote) == before
    assert plan.policy_skipped == {"github/myorg/myrepo": ["git: main", f"dolt: {_HQ_LABEL}"]}


def test_pull_only_hive_with_hq_object_store_dry_run_names_label(
    world, hq_stub, monkeypatch, capsys
):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    _hq_object_store(monkeypatch, _BEADS_ID)

    plan = sync_remote.sync_remote(dry_run=True, pull=True, push=True)

    out = capsys.readouterr().out
    assert hq_stub.pulls == [] and hq_stub.pushes == []
    assert f"would pull from upstream: {_HQ_LABEL} (push withheld: pull-only)" in out
    assert "would skip push — pull-only" in out
    assert "would push" not in out
    assert plan.offending == []


def test_pull_only_hive_with_missing_hq_credentials_refuses_the_pull(
    world, hq_stub, monkeypatch, capsys
):
    fx = _make_contrib_beads()
    _dolt_remotes(monkeypatch, fx)
    _hq_object_store(monkeypatch, _BEADS_ID, missing="no credential profile 'prod'")
    before = fx.snapshot()

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True)

    assert plan.offending == [_BEADS_ID]
    assert hq_stub.pulls == [] and hq_stub.pushes == []
    assert fx.snapshot() == before
    assert "credentials: no credential profile 'prod'" in capsys.readouterr().err
