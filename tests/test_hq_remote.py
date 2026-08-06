"""Tests for beadhive.hq's remote wiring — `bh hq init`'s scaffold + backup + push (bh-e0y8.2).

Real `git` (fast, no server) builds the HQ working tree and a bare local remote, mirroring
test_sync_remote.py's style. Every bd-touching call (`bd status`, `bd doctor`, `bd dolt remote
add`, `bd --version`, and `bd export`/`push_state` via the Engine seam) is FAKED — this repo's
convention keeps real-bd usage under `@pytest.mark.integration` only (see test_hub.py's
`fake_run` pattern, which this mirrors).

NEVER touches the operator's real ``~/.beadhive/hq`` or a real remote: `world` isolates
`BH_HOME`/`config.hq_dir()` under a pytest tmp_path, and ``_patch_remote_urls`` redirects the
GitHub-shaped remote `hq.init` would otherwise derive to a local bare repo under
`world.remotes`.
"""

from __future__ import annotations

import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer

from beadhive import config, hq
from harness.world import git


def _bd_stub(*, status_total=0, schema_version=1, remote_add_ok=True, version="HEAD-test123"):
    """A fake `bd` responder keyed on the sub-command, matching this repo's fake_run(cmd, **k)
    convention (test_hub.py)."""

    def _bd(cmd, **kw):
        if cmd == ["bd", "--version"]:
            return subprocess.CompletedProcess(cmd, 0, f"bd version {version} (test)", "")
        sub = cmd[3:]  # cmd = ["bd", "-C", <cwd>, *sub]
        if sub[:2] == ["dolt", "remote"]:
            rc = 0 if remote_add_ok else 1
            return subprocess.CompletedProcess(
                cmd, rc, "" if remote_add_ok else "", "" if remote_add_ok else "boom"
            )
        if sub[:1] == ["status"]:
            body = f'{{"summary": {{"total_issues": {status_total}}}}}'
            return subprocess.CompletedProcess(cmd, 0, body, "")
        if sub[:1] == ["doctor"]:
            body = f'{{"schema_version": {schema_version}}}'
            return subprocess.CompletedProcess(cmd, 0, body, "")
        raise AssertionError(f"unexpected bd call: {cmd}")

    return _bd


def _wire_run(monkeypatch, bd_fake):
    """Real `git`, faked `bd` — the two subprocess families `hq._git`/`hq._bd` both route
    through the single `hq.run` symbol this patches."""
    from beadhive.run import run as real_run

    def _run(cmd, **kw):
        if cmd and cmd[0] == "bd":
            return bd_fake(cmd, **kw)
        return real_run(cmd, **kw)

    monkeypatch.setattr(hq, "run", _run)


class _StubEngine:
    """Fakes the Engine methods hq.py calls (export_jsonl/push_state/backup) — matches
    test_sync_remote.py's `_StubEngine` pattern."""

    def __init__(self, *, export_lines=0, push_ok=True, backup_ok=True, backup_bytes=b"native"):
        self.export_lines = export_lines
        self.push_ok = push_ok
        self.backup_ok = backup_ok
        self.backup_bytes = backup_bytes
        self.export_calls: list[tuple[str, str]] = []
        self.push_calls: list[str] = []
        self.backup_calls: list[tuple[str, str]] = []

    def export_jsonl(self, cwd, out_path, *, env=None):
        self.export_calls.append((str(cwd), str(out_path)))
        lines = "".join(f'{{"id": "hq-{i}"}}\n' for i in range(self.export_lines))
        Path(out_path).write_text(lines)
        return subprocess.CompletedProcess(["bd", "export"], 0, "", "")

    def push_state(self, cwd, actor="", message=""):
        self.push_calls.append(str(cwd))
        rc = 0 if self.push_ok else 1
        err = "" if self.push_ok else "boom"
        return subprocess.CompletedProcess(["bd", "dolt", "push"], rc, "", err)

    def backup(self, cwd, dest, *, actor=""):
        self.backup_calls.append((str(cwd), str(dest)))
        if not self.backup_ok:
            return subprocess.CompletedProcess(["bd", "backup"], 1, "", "boom")
        if self.backup_bytes is not None:
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / "manifest").write_bytes(self.backup_bytes)
        return subprocess.CompletedProcess(["bd", "backup"], 0, "", "")


def _stub_engine(monkeypatch, engine_stub):
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)


def _patch_remote_urls(monkeypatch, remote_path: Path):
    """Redirect hq's github-shaped remote derivation at a local bare repo — the fixture never
    touches a real GitHub remote."""
    monkeypatch.setattr(
        hq, "_remote_urls", lambda remote: (str(remote_path), f"git+file://{remote_path}")
    )


def _make_hq(world) -> Path:
    """A real local git working tree standing in for an already-initialized HQ store — `.beads/`
    here is a plain fixture dir (every bd-touching call in the flow under test is faked)."""
    hq_dir = config.hq_dir()
    hq_dir.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=hq_dir)
    git("config", "user.email", "hq@fixture", cwd=hq_dir)
    git("config", "user.name", "HQ Fixture", cwd=hq_dir)
    dolt = hq_dir / ".beads" / "embeddeddolt"
    dolt.mkdir(parents=True)
    (dolt / "chunk.bin").write_text("dolt data\n")
    cache = dolt / "git-remote-cache"
    cache.mkdir()
    (cache / "junk").write_text("regenerable\n")
    (hq_dir / "README.md").write_text("hq\n")
    git("add", "-A", cwd=hq_dir)
    git("commit", "-qm", "init", cwd=hq_dir)
    return hq_dir


def _make_hq_server_mode(world) -> Path:
    """A real local git working tree standing in for an already-initialized SERVER-mode HQ
    store (bh-areg.1) — `.beads/` exists but carries NO `embeddeddolt/` directory, matching
    what owned/shared/external actually leave under the hive (bh-u562.1 finding 8)."""
    hq_dir = config.hq_dir()
    hq_dir.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=hq_dir)
    git("config", "user.email", "hq@fixture", cwd=hq_dir)
    git("config", "user.name", "HQ Fixture", cwd=hq_dir)
    (hq_dir / ".beads").mkdir(parents=True)
    (hq_dir / ".beads" / "dolt-server.port").write_text("3308\n")  # server-mode's own marker
    (hq_dir / "README.md").write_text("hq\n")
    git("add", "-A", cwd=hq_dir)
    git("commit", "-qm", "init", cwd=hq_dir)
    return hq_dir


def _make_remote(world, name="hq.git") -> Path:
    remote = world.remotes / name
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=world.remotes)
    return remote


def _push_to_remote(world, remote: Path, *, refspec: str) -> None:
    """An independent writer pushes `refspec` to `remote` — simulates pre-existing content."""
    other = world.tmp / f"other-{refspec.replace('/', '-').replace(':', '-')}"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    git("config", "user.email", "o@o", cwd=other)
    git("config", "user.name", "O", cwd=other)
    (other / "f.txt").write_text("other\n")
    git("add", "-A", cwd=other)
    git("commit", "-qm", "other", cwd=other)
    git("push", "-q", str(remote), refspec, cwd=other)


def _cfg(remote: str) -> dict:
    return {"hq": {"remote": remote}}


# ---- _wire_remote — happy path -----------------------------------------------


def test_wire_remote_first_push_writes_layout_backs_up_and_pushes(world, monkeypatch, capsys):
    hq_dir = _make_hq(world)
    remote = _make_remote(world)
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub(status_total=0))
    engine_stub = _StubEngine(export_lines=0)
    _stub_engine(monkeypatch, engine_stub)

    hq._wire_remote(_cfg("acme/beadhive-hq"))

    assert (hq_dir / "fleet.yaml").exists()
    assert (hq_dir / "workspace.toml").exists()
    assert (hq_dir / "hosts" / "README.md").exists()
    assert git("status", "--porcelain", cwd=hq_dir).stdout.strip() == ""  # scaffold committed

    assert git("remote", "get-url", "origin", cwd=hq_dir).stdout.strip() == str(remote)
    assert "refs/heads/main" in git("ls-remote", "--heads", str(remote), cwd=hq_dir).stdout
    assert engine_stub.push_calls == [str(hq_dir)]

    assert "HQ remote wired" in capsys.readouterr().out


def test_wire_remote_first_push_succeeds_against_a_server_mode_hq(world, monkeypatch, capsys):
    """bh-areg.1's headline acceptance: `bh hq push` (via `_wire_remote`) must succeed against
    a server-mode HQ, with a VERIFIED backup — not the pre-fix behavior of refusing forever
    because the tar level can never be satisfied off embedded mode."""
    hq_dir = _make_hq_server_mode(world)
    remote = _make_remote(world)
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub(status_total=0))
    engine_stub = _StubEngine(export_lines=0, backup_bytes=b"real dolt-native backup content")
    _stub_engine(monkeypatch, engine_stub)

    hq._wire_remote(_cfg("acme/beadhive-hq"))

    assert (hq_dir / "fleet.yaml").exists()  # reached the scaffold/push — the plan was ok
    assert git("remote", "get-url", "origin", cwd=hq_dir).stdout.strip() == str(remote)
    assert "refs/heads/main" in git("ls-remote", "--heads", str(remote), cwd=hq_dir).stdout
    assert engine_stub.push_calls == [str(hq_dir)]
    assert len(engine_stub.backup_calls) == 1  # the connection-oriented level actually ran
    assert engine_stub.backup_calls[0][0] == str(hq_dir)

    out = capsys.readouterr().out
    assert "HQ remote wired" in out
    assert "✓ dolt-native-backup" in out  # a real green checkmark, not a refused push


def test_wire_remote_prunes_old_hq_backups_after_a_verified_new_one(world, monkeypatch, capsys):
    """bh-cmqp.2: `_wire_remote` auto-prunes `hq-backups/`'s dated directories to `backup.
    hq_keep` (newest first) right after taking + verifying the new one — never before, and
    never below 1 (the fresh backup just taken always survives)."""
    _make_hq(world)
    remote = _make_remote(world)
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub(status_total=0))
    _stub_engine(monkeypatch, _StubEngine(export_lines=0))

    backups_root = config.home() / "hq-backups"
    for stale in ("2020-01-01", "2020-01-02", "2020-01-03"):
        d = backups_root / stale
        d.mkdir(parents=True)
        (d / "hq-issues.jsonl").write_text("stale\n")

    cfg = _cfg("acme/beadhive-hq")
    cfg["backup"] = {"hq_keep": 2}

    hq._wire_remote(cfg, auto=True, create=False)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    remaining = sorted(p.name for p in backups_root.iterdir())
    assert remaining == ["2020-01-03", today]  # newest stale + the just-taken one — never 0
    assert "pruned" in capsys.readouterr().out


def test_wire_remote_first_push_sets_upstream_tracking(world, monkeypatch):
    """The first push uses `-u` (bh-z9hl) — without it `main` has no upstream tracking, so a
    bare `git push`/`git pull` in ~/.beadhive/hq fails, and every ahead/behind primitive that
    reads `%(upstream:short)` (`safety.scan`, `bh hq status`, `bh doctor`'s fleet-health
    section) silently reports `has_upstream=False` forever."""
    hq_dir = _make_hq(world)
    remote = _make_remote(world)
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub())
    _stub_engine(monkeypatch, _StubEngine())

    hq._wire_remote(_cfg("acme/beadhive-hq"))

    tracking = git("rev-parse", "--abbrev-ref", "main@{upstream}", cwd=hq_dir)
    assert tracking.returncode == 0
    assert tracking.stdout.strip() == "origin/main"


def test_wire_remote_second_call_is_a_no_op_and_skips_backup(world, monkeypatch):
    hq_dir = _make_hq(world)
    remote = _make_remote(world)
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub())
    engine_stub = _StubEngine()
    _stub_engine(monkeypatch, engine_stub)

    hq._wire_remote(_cfg("acme/beadhive-hq"))
    assert engine_stub.push_calls == [str(hq_dir)]

    backup_calls: list[int] = []
    monkeypatch.setattr(
        hq,
        "_take_backup",
        lambda *a, **k: backup_calls.append(1) or hq.BackupPlan(dry_run=False),
    )

    hq._wire_remote(_cfg("acme/beadhive-hq"))  # second (idempotent) call

    assert backup_calls == []  # non-first "push" never re-runs the backup
    assert engine_stub.push_calls == [str(hq_dir)]  # and never pushes again either


# ---- refusals: unreachable / diverging — never force-push -------------------


def test_wire_remote_refuses_unreachable_remote(world, monkeypatch, tmp_path):
    hq_dir = _make_hq(world)
    nonexistent = tmp_path / "does-not-exist.git"
    _patch_remote_urls(monkeypatch, nonexistent)

    with pytest.raises(typer.Exit) as exc:
        hq._wire_remote(_cfg("acme/beadhive-hq"))

    assert exc.value.exit_code == 1
    assert not (hq_dir / "fleet.yaml").exists()  # refused before any scaffold/backup write


def test_wire_remote_refuses_diverging_main_branch(world, monkeypatch):
    hq_dir = _make_hq(world)
    remote = _make_remote(world)
    _push_to_remote(world, remote, refspec="main")  # pre-existing, unrelated `main`
    _patch_remote_urls(monkeypatch, remote)

    with pytest.raises(typer.Exit) as exc:
        hq._wire_remote(_cfg("acme/beadhive-hq"))

    assert exc.value.exit_code == 1
    assert not (hq_dir / "fleet.yaml").exists()


def test_wire_remote_pre_existing_dolt_data_is_backed_up_before_push(world, monkeypatch):
    """A remote carrying ONLY a pre-existing refs/dolt/data (no refs/heads/*) is not
    "diverging" in the branch sense — it's exactly what backup level 3 exists to protect."""
    hq_dir = _make_hq(world)
    remote = _make_remote(world)
    _push_to_remote(world, remote, refspec="HEAD:refs/dolt/data")
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub())
    _stub_engine(monkeypatch, _StubEngine())

    hq._wire_remote(_cfg("acme/beadhive-hq"))

    all_refs = git("ls-remote", str(remote), cwd=hq_dir).stdout
    assert "refs/backup/dolt-data-schema-" in all_refs
    assert "refs/heads/main" in all_refs  # our own push still went through


def test_wire_remote_refuses_push_when_backup_unverified(world, monkeypatch):
    hq_dir = _make_hq(world)
    remote = _make_remote(world)
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub(status_total=99))  # mismatched vs the 0-line stub export
    engine_stub = _StubEngine(export_lines=0)
    _stub_engine(monkeypatch, engine_stub)

    with pytest.raises(typer.Exit) as exc:
        hq._wire_remote(_cfg("acme/beadhive-hq"))

    assert exc.value.exit_code == 1
    assert engine_stub.push_calls == []  # never reached the push
    assert not (hq_dir / "fleet.yaml").exists()  # nor the scaffold write
    assert git("remote", cwd=hq_dir).stdout.strip() == ""  # origin never added


# ---- --dry-run: zero mutation -------------------------------------------------


def test_wire_remote_dry_run_previews_plan_with_zero_mutation(world, monkeypatch, capsys):
    hq_dir = _make_hq(world)
    remote = _make_remote(world)
    _patch_remote_urls(monkeypatch, remote)
    _wire_run(monkeypatch, _bd_stub(status_total=3))
    engine_stub = _StubEngine()
    _stub_engine(monkeypatch, engine_stub)
    before = git("rev-parse", "HEAD", cwd=hq_dir).stdout

    hq._wire_remote(_cfg("acme/beadhive-hq"), dry_run=True)

    assert not (hq_dir / "fleet.yaml").exists()
    assert git("rev-parse", "HEAD", cwd=hq_dir).stdout == before
    assert git("remote", cwd=hq_dir).stdout.strip() == ""
    assert engine_stub.export_calls == []
    assert engine_stub.push_calls == []
    assert git("ls-remote", "--heads", str(remote), cwd=hq_dir).stdout.strip() == ""

    assert "DRY-RUN" in capsys.readouterr().out


# ---- unresolvable hq.remote: skip, don't error -------------------------------


def test_wire_remote_unset_skips_without_error(world, monkeypatch, capsys):
    hq_dir = _make_hq(world)
    monkeypatch.setattr(hq.config, "hq_remote", lambda cfg=None, cwd=None: "")

    hq._wire_remote({})  # must not raise

    assert not (hq_dir / "fleet.yaml").exists()
    assert "hq.remote is unset" in capsys.readouterr().out


# ---- scaffold_layout — idempotent -------------------------------------------


def test_scaffold_layout_writes_fleet_workspace_hosts_and_is_idempotent(tmp_path):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    cfg = {"schema_version": 3, "managed_repos": [{"provider": "github", "org": "a", "repo": "b"}]}

    written = hq.scaffold_layout(hq_dir, cfg)

    assert {p.name for p in written} == {
        "fleet.yaml",
        "workspace.toml",
        "allowed_signers",  # the fleet's trusted PUBLIC SSH keys (bh-ijd4)
        "README.md",
    }
    assert "schema_version" in (hq_dir / "fleet.yaml").read_text()
    assert (hq_dir / "hosts").is_dir()

    again = hq.scaffold_layout(hq_dir, cfg)

    assert again == []  # idempotent — nothing missing, nothing written


# ---- backup levels — write + verify, in isolation ----------------------------


def test_backup_tar_excludes_git_remote_cache(tmp_path):
    hq_dir = tmp_path / "hq"
    dolt = hq_dir / ".beads" / "embeddeddolt"
    dolt.mkdir(parents=True)
    (dolt / "keep.bin").write_text("keep\n")
    cache = dolt / "git-remote-cache"
    cache.mkdir()
    (cache / "drop.bin").write_text("drop\n")

    target = hq._backup_tar(hq_dir, tmp_path / "backup", dry_run=False)

    assert target.verified
    with tarfile.open(target.path, "r:gz") as tf:
        names = tf.getnames()
    assert any(n.endswith("keep.bin") for n in names)
    assert not any("git-remote-cache" in n for n in names)


def test_backup_jsonl_verified_when_line_count_matches(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    engine_stub = _StubEngine(export_lines=2)
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)
    monkeypatch.setattr(
        hq,
        "_bd",
        lambda args, cwd: subprocess.CompletedProcess(
            args, 0, '{"summary": {"total_issues": 2}}', ""
        ),
    )

    target = hq._backup_jsonl(hq_dir, tmp_path / "backup", {}, dry_run=False)

    assert target.verified
    assert "2 lines" in target.detail


def test_backup_jsonl_unverified_on_count_mismatch(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    engine_stub = _StubEngine(export_lines=2)
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)
    monkeypatch.setattr(
        hq,
        "_bd",
        lambda args, cwd: subprocess.CompletedProcess(
            args, 0, '{"summary": {"total_issues": 5}}', ""
        ),
    )

    target = hq._backup_jsonl(hq_dir, tmp_path / "backup", {}, dry_run=False)

    assert not target.verified


def test_backup_dry_run_writes_nothing(tmp_path):
    hq_dir = tmp_path / "hq"
    dolt = hq_dir / ".beads" / "embeddeddolt"
    dolt.mkdir(parents=True)
    (dolt / "chunk.bin").write_text("x\n")
    backup_dir = tmp_path / "backup"

    jsonl_target = hq._backup_jsonl(hq_dir, backup_dir, {}, dry_run=True)
    tar_target = hq._backup_tar(hq_dir, backup_dir, dry_run=True)

    assert jsonl_target.path and tar_target.path  # plan names a target
    assert not backup_dir.exists()  # but writes nothing


# ---- absent store: never a green checkmark (bh-kobw) --------------------------
#
# Every fixture above builds `.beads/embeddeddolt`, which is why the miss path shipped
# returning verified=True: no test ever took it. These do.


def test_backup_tar_is_unverified_when_the_store_directory_is_absent(tmp_path):
    """A non-embedded HQ (owned/shared/external, bh-areg.1) has no
    `.beads/embeddeddolt` — a plain FILESYSTEM FACT, never a `bd dolt status` mode probe. That
    is the level being UNAVAILABLE, not an empty store — reporting it verified let `plan.ok`
    wave the first push through with no full-fidelity backup at all (bh-kobw)."""
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()

    target = hq._backup_tar(hq_dir, tmp_path / "backup", dry_run=False)

    assert not target.verified
    assert "embeddeddolt" in target.detail
    assert not hq.BackupPlan(dry_run=False, targets=[target]).ok  # so the push refuses


def test_backup_tar_absent_store_reason_is_a_filesystem_fact_not_a_mode_probe(tmp_path):
    """`_absent_store_reason` must never shell out to `bd dolt status` (bh-areg.1's binding
    reconciliation: that probe's own JSON shape is ambiguous by mode, bh-u562.1 finding 9) —
    it names the directory that's missing, nothing more."""
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()

    reason = hq._absent_store_reason(hq_dir)

    assert "embeddeddolt" in reason
    assert str(hq_dir) in reason or "embeddeddolt" in reason


def test_backup_tar_dry_run_does_not_promise_a_tarball_it_cannot_take(tmp_path):
    """`--dry-run` is the operator's preview of the real run. Previewing "would tar …" and then
    refusing on the real run is the same lie one step earlier."""
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()

    target = hq._backup_tar(hq_dir, tmp_path / "backup", dry_run=True)

    assert not target.verified
    assert "would tar" not in target.detail


# ---- dolt-native level: connection-oriented, non-embedded HQ (bh-areg.1) ------


class _BackupEngine:
    """Fakes `Engine.backup` — the connection-oriented seam `_backup_dolt_native` calls."""

    def __init__(self, *, returncode=0, stderr="", write_bytes=None):
        self.returncode = returncode
        self.stderr = stderr
        self.write_bytes = write_bytes  # None -> write nothing (the bh-kobw-shaped defense)
        self.calls: list[tuple[str, str]] = []

    def backup(self, cwd, dest, *, actor=""):
        self.calls.append((str(cwd), str(dest)))
        if self.returncode == 0 and self.write_bytes is not None:
            Path(dest).mkdir(parents=True, exist_ok=True)
            (Path(dest) / "manifest").write_bytes(self.write_bytes)
        return subprocess.CompletedProcess(["bd", "backup"], self.returncode, "", self.stderr)


def test_backup_dolt_native_verified_on_real_content(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    engine_stub = _BackupEngine(write_bytes=b"real dolt-native backup bytes")
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)

    target = hq._backup_dolt_native(hq_dir, tmp_path / "backup", {}, dry_run=False)

    assert target.verified, target.detail
    assert engine_stub.calls == [(str(hq_dir), str(tmp_path / "backup" / "hq-dolt-native"))]
    assert hq.BackupPlan(dry_run=False, targets=[target]).ok


def test_backup_dolt_native_unverified_when_bd_backup_fails(tmp_path, monkeypatch):
    """`bd backup add`/`sync` fail cleanly (non-zero exit) against an empty or missing store
    (measured) — the server-mode twin of bh-kobw must not be reintroduced here either."""
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    engine_stub = _BackupEngine(returncode=1, stderr="no beads database found")
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)

    target = hq._backup_dolt_native(hq_dir, tmp_path / "backup", {}, dry_run=False)

    assert not target.verified
    assert "no beads database found" in target.detail
    assert not hq.BackupPlan(dry_run=False, targets=[target]).ok


def test_backup_dolt_native_unverified_when_bd_reports_success_but_writes_nothing(
    tmp_path, monkeypatch
):
    """The exact bh-kobw shape, reproduced against the NEW level: a 0 exit code alone must
    never be trusted as "something restorable landed" — this is the test that fails if that
    trust-the-exit-code shortcut is (re)introduced, and passes with the real content check."""
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    engine_stub = _BackupEngine(returncode=0, write_bytes=None)  # "succeeds", writes nothing
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)

    target = hq._backup_dolt_native(hq_dir, tmp_path / "backup", {}, dry_run=False)

    assert not target.verified
    assert "wrote nothing" in target.detail
    assert not hq.BackupPlan(dry_run=False, targets=[target]).ok


def test_backup_dolt_native_dry_run_writes_nothing(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    engine_stub = _BackupEngine(write_bytes=b"x")
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)
    backup_dir = tmp_path / "backup"

    target = hq._backup_dolt_native(hq_dir, backup_dir, {}, dry_run=True)

    assert target.path and not target.verified
    assert engine_stub.calls == []
    assert not backup_dir.exists()


# ---- _take_backup: picks tar vs dolt-native from a FILESYSTEM FACT, not mode ---


def _stub_jsonl_and_remote_ref_levels(monkeypatch):
    """Isolate `_take_backup`'s level-CHOICE logic under test from the jsonl/remote-ref
    levels' own machinery, which is covered separately."""
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: None)  # overridden per-test below
    monkeypatch.setattr(
        hq, "_backup_jsonl", lambda *a, **k: hq.BackupTarget(name="jsonl", verified=True)
    )
    monkeypatch.setattr(
        hq, "_backup_remote_ref", lambda *a, **k: hq.BackupTarget(name="ref", verified=True)
    )


def test_take_backup_uses_tar_when_the_embedded_store_is_present(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    dolt = hq_dir / ".beads" / "embeddeddolt"
    dolt.mkdir(parents=True)
    (dolt / "chunk.bin").write_text("x\n")
    _stub_jsonl_and_remote_ref_levels(monkeypatch)
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: tmp_path / "backups")

    plan = hq._take_backup(hq_dir, "git@example/x.git", {}, dry_run=False)

    names = [t.name for t in plan.targets]
    assert "embeddeddolt-tar" in names
    assert "dolt-native-backup" not in names


def test_take_backup_uses_dolt_native_when_the_embedded_store_is_absent(tmp_path, monkeypatch):
    hq_dir = tmp_path / "hq"
    hq_dir.mkdir()
    _stub_jsonl_and_remote_ref_levels(monkeypatch)
    monkeypatch.setattr(hq, "_backup_root", lambda cfg: tmp_path / "backups")
    engine_stub = _BackupEngine(write_bytes=b"real bytes")
    monkeypatch.setattr(hq.engine, "get_engine", lambda cfg=None: engine_stub)

    plan = hq._take_backup(hq_dir, "git@example/x.git", {}, dry_run=False)

    names = [t.name for t in plan.targets]
    assert "dolt-native-backup" in names
    assert "embeddeddolt-tar" not in names
    assert plan.ok  # a server-mode HQ can now clear the pre-push gate (the bug this bead fixes)
