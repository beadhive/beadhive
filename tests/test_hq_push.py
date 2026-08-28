"""Tests for beadhive.hq's `push`/`status` verbs — `bh hq push`/`bh hq status` (bh-z9hl): the
discoverable, repeatable counterpart to `hq init`'s one-shot first push.

Real `git` builds the HQ working tree + a bare local remote (mirrors test_hq_remote.py's
style, itself mirroring test_sync_remote.py). `bd`'s Dolt state is FAKED via
`beadhive.engine.get_engine` (the `_StubEngine` pattern both those files already use) — the
Dolt half's ahead/behind and its push both go through the Engine seam, a real network-touching
bd verb this repo's convention keeps out of the unit-test tier.

NEVER touches the operator's real ``~/.beadhive/hq``: the `world` fixture isolates
`BH_HOME`/`config.hq_dir()` under a pytest tmp_path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest
import typer

from beadhive import config, hq
from beadhive.engine import FederationPeer, FederationStatus
from harness.world import git


def _init_repo_with_remote(world) -> tuple[Path, Path]:
    """A real HQ working tree, already wired to a real (local, bare) remote with `-u` tracking
    — the post-`hq init` steady state `push`/`status` operate against.

    Deliberately tracks a plain `note.txt` rather than a real `fleet.yaml`: once a `fleet.yaml`
    exists, `config.load()` enforces the fleet/host config partition
    (`config._reject_fleet_overrides`) against `world`'s own host `config.yaml` (which sets
    `providers`/`managed_repos` for unrelated tests) — an orthogonal concern this module isn't
    exercising."""
    hq_dir = config.hq_dir()
    hq_dir.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=hq_dir)
    git("config", "user.email", "hq@fixture", cwd=hq_dir)
    git("config", "user.name", "HQ Fixture", cwd=hq_dir)
    (hq_dir / ".beads").mkdir()
    (hq_dir / "note.txt").write_text("hq\n")
    git("add", "-A", cwd=hq_dir)
    git("commit", "-qm", "init", cwd=hq_dir)

    remote = world.remotes / "hq.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=world.remotes)
    git("remote", "add", "origin", str(remote), cwd=hq_dir)
    git("push", "-q", "-u", "origin", "main", cwd=hq_dir)
    return hq_dir, remote


def _drift(hq_dir: Path) -> None:
    """One extra local commit on `main`, ahead of the already-pushed `origin/main`."""
    (hq_dir / "note.txt").write_text("drift\n")
    git("commit", "-aqm", "drift", cwd=hq_dir)


def _bare_hq_no_remote() -> Path:
    hq_dir = config.hq_dir()
    hq_dir.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=hq_dir)
    (hq_dir / ".beads").mkdir()
    return hq_dir


class _StubEngine:
    def __init__(self, *, fed_status: FederationStatus, push_ok: bool = True):
        self.fed_status = fed_status
        self.push_ok = push_ok
        self.push_calls: list[str] = []

    def federation_status(self, cwd, *, timeout=None):
        return self.fed_status

    def push_state(self, cwd, actor="", message="", *, remote="", force=False):
        self.push_calls.append(str(cwd))
        rc = 0 if self.push_ok else 1
        err = "" if self.push_ok else "boom"
        return subprocess.CompletedProcess(["bd", "dolt", "push"], rc, "", err)


def _stub_engine(monkeypatch, engine_stub):
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)


def _no_hive_sync(monkeypatch, failed=None):
    monkeypatch.setattr(hq.hub, "sync", lambda: list(failed or []))


def _fed(ahead: int = 0, behind: int = 0) -> FederationStatus:
    return FederationStatus(
        ok=True,
        peers=(FederationPeer(peer="origin", url="x", reachable=True, ahead=ahead, behind=behind),),
    )


# ---- status() -------------------------------------------------------------------


def _hq_status_schema() -> dict:
    path = Path(__file__).resolve().parents[1] / "docs/schemas/hq-status-v1.schema.json"
    return json.loads(path.read_text())


def test_status_reports_clean_both_halves(world, monkeypatch, capsys):
    _init_repo_with_remote(world)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))

    hq.status()

    out = capsys.readouterr().out
    assert "up to date with origin/main" in out
    assert "refs/dolt/data is up to date with origin" in out
    assert "✓ HQ is up to date with its remote" in out


def test_status_reports_git_ahead(world, monkeypatch, capsys):
    hq_dir, _remote = _init_repo_with_remote(world)
    _drift(hq_dir)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))

    hq.status()

    out = capsys.readouterr().out
    assert "1 commit(s) ahead of origin/main" in out
    assert f"run `{config.BINARY_ALIAS} hq push`" in out


def test_status_reports_dolt_ahead(world, monkeypatch, capsys):
    _init_repo_with_remote(world)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed(ahead=5)))

    hq.status()

    assert "5 commit(s) ahead of origin" in capsys.readouterr().out


def test_status_no_remote_reports_and_returns(world, capsys):
    _bare_hq_no_remote()

    hq.status()  # must not raise

    assert "has no remote configured" in capsys.readouterr().out


def test_status_not_initialized_exits(world, capsys):
    with pytest.raises(typer.Exit) as exc:
        hq.status()

    assert exc.value.exit_code == 1
    assert "not initialized" in capsys.readouterr().err


def test_status_json_present_is_versioned_stable_and_schema_valid(world, monkeypatch, capsys):
    hq_dir, _remote = _init_repo_with_remote(world)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))

    first = hq.status_payload(generated_at=1000)
    second = hq.status_payload(generated_at=2000)

    jsonschema.Draft202012Validator.check_schema(_hq_status_schema())
    jsonschema.Draft202012Validator(_hq_status_schema()).validate(first)
    assert first["schema_version"] == 1
    assert first["command"] == "hq status"
    assert first["identity"] == {
        "canonical_id": "local/factory/hq",
        "provider": "local",
        "organization": "factory",
        "repository": "hq",
        "prefix": "hq",
        "kind": "hq",
    }
    assert first["cwd"] == str(hq_dir.resolve())
    assert first["availability"] == {
        "state": "available",
        "reason_code": "hq_initialized",
    }
    assert first["coverage"]["state"] == "complete"
    assert first["freshness"] == {"state": "fresh", "as_of": 1000}
    assert first["retirement"] == {
        "intent": "retain",
        "reason_code": "hq_available",
        "advisory": True,
    }
    assert first["remote"]["configured"] is True
    assert first["remote"]["git"]["state"] == "clean"
    assert first["remote"]["dolt"]["state"] == "clean"
    assert first["source_revision"] == second["source_revision"]

    hq.status(as_json=True)
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["source_revision"] == first["source_revision"]


def test_status_json_revision_changes_with_observed_state(world, monkeypatch):
    hq_dir, _remote = _init_repo_with_remote(world)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))
    clean = hq.status_payload(generated_at=1000)

    _drift(hq_dir)
    ahead = hq.status_payload(generated_at=1000)

    assert clean["source_revision"] != ahead["source_revision"]
    assert ahead["remote"]["git"]["state"] == "ahead"


def test_status_json_absence_is_authoritative_and_non_mutating(world):
    hq_dir = config.hq_dir()

    payload = hq.status_payload(generated_at=1000)

    jsonschema.Draft202012Validator(_hq_status_schema()).validate(payload)
    assert payload["cwd"] == str(hq_dir.resolve())
    assert payload["availability"] == {
        "state": "absent",
        "reason_code": "hq_not_initialized",
    }
    assert payload["coverage"]["state"] == "complete"
    assert payload["retirement"] == {
        "intent": "retire",
        "reason_code": "hq_authoritatively_absent",
        "advisory": True,
    }
    assert payload["remote"] is None
    assert not hq_dir.exists()


def test_status_json_unavailable_is_retain_only(world, monkeypatch):
    _init_repo_with_remote(world)
    monkeypatch.setattr(
        hq.safety,
        "scan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("unreadable")),
    )

    payload = hq.status_payload(generated_at=1000)

    jsonschema.Draft202012Validator(_hq_status_schema()).validate(payload)
    assert payload["availability"]["state"] == "unavailable"
    assert payload["coverage"]["state"] == "partial"
    assert payload["freshness"] == {"state": "unknown", "as_of": None}
    assert payload["retirement"]["intent"] == "retain"
    assert payload["retirement"]["advisory"] is True


def test_status_json_cwd_honors_bh_hq_before_bh_home(world, monkeypatch):
    bh_home = world.tmp / "alternate-home"
    explicit_hq = world.tmp / "explicit" / "factory-hq"
    monkeypatch.setenv("BH_HOME", str(bh_home))
    monkeypatch.delenv("BH_HQ", raising=False)

    inherited = hq.status_payload(generated_at=1000)

    assert inherited["cwd"] == str((bh_home / "hq").resolve())

    monkeypatch.setenv("BH_HQ", str(explicit_hq))

    payload = hq.status_payload(generated_at=1000)

    assert payload["cwd"] == str(explicit_hq.resolve())
    assert payload["cwd"] != str((bh_home / "hq").resolve())


def test_status_human_output_is_unchanged_when_json_mode_is_added(world, monkeypatch, capsys):
    hq_dir, _remote = _init_repo_with_remote(world)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))

    hq.status()

    assert capsys.readouterr().out == (
        f"HQ ({hq_dir})\n"
        "  git:  `main` is up to date with origin/main\n"
        "  dolt: refs/dolt/data is up to date with origin\n"
        "✓ HQ is up to date with its remote\n"
    )


def test_hq_status_cli_json_is_the_only_stdout_document(world, monkeypatch):
    from typer.testing import CliRunner

    from beadhive.cli import app

    _init_repo_with_remote(world)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))

    result = CliRunner().invoke(app, ["hq", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "hq status"


# ---- push() ----------------------------------------------------------------------


def test_push_nothing_to_push_is_idempotent(world, monkeypatch, capsys):
    _init_repo_with_remote(world)
    _no_hive_sync(monkeypatch)
    engine_stub = _StubEngine(fed_status=_fed())
    _stub_engine(monkeypatch, engine_stub)

    hq.push()

    out = capsys.readouterr().out
    assert "git: nothing to push (up to date)" in out
    assert "dolt: nothing to push (up to date)" in out
    assert "✓ HQ already up to date — nothing to push" in out
    assert engine_stub.push_calls == []  # dolt state was clean — never attempted


def test_push_pushes_ahead_git_and_dolt(world, monkeypatch, capsys):
    hq_dir, remote = _init_repo_with_remote(world)
    _drift(hq_dir)
    _no_hive_sync(monkeypatch)
    engine_stub = _StubEngine(fed_status=_fed(ahead=3))
    _stub_engine(monkeypatch, engine_stub)

    hq.push()

    out = capsys.readouterr().out
    assert "✓ git: pushed main (1 commit(s))" in out
    assert "✓ dolt: pushed refs/dolt/data" in out
    assert "✓ HQ published" in out
    assert engine_stub.push_calls == [str(hq_dir)]
    assert "refs/heads/main" in git("ls-remote", "--heads", str(remote), cwd=hq_dir).stdout


def test_push_commits_dirty_tree_before_publishing(world, monkeypatch, capsys):
    hq_dir, _remote = _init_repo_with_remote(world)
    (hq_dir / "note.txt").write_text("uncommitted drift\n")  # unstaged, not yet committed
    _no_hive_sync(monkeypatch)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))

    hq.push()

    assert git("status", "--porcelain", cwd=hq_dir).stdout.strip() == ""


def test_push_never_refreshes_an_aggregate(world, monkeypatch, capsys):
    """bh-89wxf.2: publishing HQ has nothing to hydrate. `hub.sync()` used to run here, which
    is what made every host's `hq push` rewrite shared replicated state — the whole defect.
    The cross-hive aggregate is the hub's, is per-host and derived, and `bh sync` owns it."""
    _init_repo_with_remote(world)
    monkeypatch.setattr(hq.hub, "sync", lambda: pytest.fail("hq push must never aggregate"))
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))

    hq.push()

    assert "refreshing aggregate" not in capsys.readouterr().out


def test_push_dry_run_makes_no_writes(world, monkeypatch, capsys):
    hq_dir, _remote = _init_repo_with_remote(world)
    _drift(hq_dir)
    before = git("rev-parse", "HEAD", cwd=hq_dir).stdout
    engine_stub = _StubEngine(fed_status=_fed(ahead=2))
    _stub_engine(monkeypatch, engine_stub)

    hq.push(dry_run=True)

    assert git("rev-parse", "HEAD", cwd=hq_dir).stdout == before
    assert engine_stub.push_calls == []
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "would push git main (1 commit(s))" in out
    assert "would run `bd dolt push`" in out
    assert "no writes made" in out


def test_push_no_remote_refuses(world, capsys):
    _bare_hq_no_remote()

    with pytest.raises(typer.Exit) as exc:
        hq.push()

    assert exc.value.exit_code == 1
    assert "no remote configured" in capsys.readouterr().err


def test_push_not_initialized_exits(world, capsys):
    with pytest.raises(typer.Exit) as exc:
        hq.push()

    assert exc.value.exit_code == 1
    assert "not initialized" in capsys.readouterr().err


def test_push_git_failure_exits_nonzero(world, monkeypatch, capsys):
    hq_dir, _remote = _init_repo_with_remote(world)
    _drift(hq_dir)
    _no_hive_sync(monkeypatch)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed()))
    # Repoint the wired remote at a nonexistent path — `set-url` (unlike remove+re-add) leaves
    # `main`'s upstream tracking config intact, so ahead-detection still sees the drift commit;
    # get-url still resolves, so the earlier "no remote configured" guard doesn't fire either.
    # The push itself must fail.
    git("remote", "set-url", "origin", str(world.remotes / "does-not-exist.git"), cwd=hq_dir)

    with pytest.raises(typer.Exit) as exc:
        hq.push()

    assert exc.value.exit_code == 1
    assert "git push origin main failed" in capsys.readouterr().err


def test_push_dolt_failure_exits_nonzero(world, monkeypatch, capsys):
    _init_repo_with_remote(world)
    _no_hive_sync(monkeypatch)
    _stub_engine(monkeypatch, _StubEngine(fed_status=_fed(ahead=1), push_ok=False))

    with pytest.raises(typer.Exit) as exc:
        hq.push()

    assert exc.value.exit_code == 1
    assert "bd dolt push failed" in capsys.readouterr().err


# ---- the removed flags (bh-89wxf.2) -----------------------------------------
# `--no-sync` / `--git-only` (bh-d5jhc.1) existed ONLY to dodge the fleet-wide aggregate walk
# `push` used to run. There is no walk to dodge now, so the flags went with the reason for
# them — and their absence is pinned, so nobody re-adds a knob for a cost that no longer exists.


def test_push_takes_no_sync_or_git_only_flags(world):
    import inspect

    params = set(inspect.signature(hq.push).parameters)
    assert params == {"dry_run"}, params


def test_hq_push_cli_rejects_the_removed_flags(world):
    from typer.testing import CliRunner

    from beadhive.cli import app

    for flag in ("--no-sync", "--git-only"):
        res = CliRunner().invoke(app, ["hq", "push", flag])
        assert res.exit_code != 0, f"{flag} should no longer be accepted"
