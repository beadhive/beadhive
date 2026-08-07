"""`bh host rm <host_id>` (bh-salu; spelled `remove` until bh-2v6d) — the deregister verb.

`host_id` is minted once by `bh config init` and never regenerated or synced
(:mod:`beadhive.host`'s module docstring), so a wiped-and-rebuilt host comes back under a
DIFFERENT identity and its old manifest never goes away on its own — `bh host list`
accumulates orphans with no way to clear them.

Covers the acceptance bar directly:
  * `bh host rm <host_id> --confirm` drops `hosts/<host_id>.yaml` from HQ and commits it.
  * refuses to evict a host holding a live (unexpired) lease for ANY registered hive unless
    `--force`, naming the held hive(s).
  * refuses to remove a host whose manifest was touched recently (plausibly still alive)
    unless `--force`.
  * refuses ANY removal without `--confirm` (bh-gbcw) — the same FLEET-WIDE intent gate
    `hive rm` carries. `--confirm` also covers self-removal (it replaced the separate `--yes`),
    but does NOT bypass the lease/recency gates: those stay `--force`'s job.
  * `--dry-run` prints the removal plan and mutates nothing.
  * a repeated wipe-and-readopt cycle (mint under a NEW host_id, remove the OLD one) leaves
    `bh host list` with no orphan.
  * `bh host list` marks a stale manifest distinctly (STALE column).

Every HQ remote is a scratch BARE repo under the test's own `tmp_path`, wired as a REAL
`origin` on a real local clone (the same shape `bh hq init`/`clone` leaves behind) — never a
network remote, and (via the autouse `_sandbox_bh_home` fixture) never the operator's real
`~/.beadhive/hq`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest
from typer.testing import CliRunner

from beadhive import config, host, host_lease, hosts, registry
from beadhive.cli import app

runner = CliRunner()

PREFIX_A = "aa"
HOST_A = "11111111-1111-4111-8111-111111111111"
HOST_B = "22222222-2222-4222-8222-222222222222"
HOST_C = "33333333-3333-4333-8333-333333333333"
T0 = 1_800_000_000.0

# `_stale_after`'s default threshold (host.lease.ttl=1800 baseline * ROLE_TTL_SCALE's biggest
# entry, 4.0 for executor) — 7200s. Backdating a manifest well past this makes it read
# as stale without needing to mock `time.time()`.
_WELL_PAST_STALE = 3 * 3600.0


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _bare(tmp_path, name):
    path = tmp_path / name
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return path


def _clone_with_origin(path, origin):
    path.mkdir(parents=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    _git(["remote", "add", "origin", str(origin)], path)
    return path


def _write_config(hives):
    """`hives`: list of (provider, org, repo, prefix). Overwrites the sandboxed config.yaml
    (`_sandbox_bh_home`'s default seed) with the same shape, plus these managed_repos."""
    entries = "\n".join(
        f"  - provider: {p}\n    org: {o}\n    repo: {r}\n    prefix: {prefix}"
        for p, o, r, prefix in hives
    )
    config.config_path().write_text(
        "schema_version: 1\n"
        "providers: [github]\n"
        f"managed_repos:\n{entries}\n"
        "exclude:\n  orgs: []\n  repos: []\n"
        "otel:\n  enabled: false\n  protocol: grpc\n"
    )


@pytest.fixture
def hq(tmp_path):
    """This host's local HQ clone, with a REAL `origin` wired to a real bare remote."""
    hq_remote = _bare(tmp_path, "hq.git")
    hq_dir = config.hq_dir()
    _clone_with_origin(hq_dir, hq_remote)
    return hq_dir


@pytest.fixture
def one_hive(tmp_path, monkeypatch, hq):
    """One registered hive (PREFIX_A), with a real, `origin`-wired remote of its own — for the
    live-lease refusal tests."""
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(registry, "workspace_root", lambda: str(workspace))
    hive_remote = _bare(tmp_path, "r.git")
    hive_dir = workspace / "github" / "o" / "r"
    _clone_with_origin(hive_dir, hive_remote)
    _write_config([("github", "o", "r", PREFIX_A)])
    return {"hq_dir": hq, "hive_dir": hive_dir, "hive_remote": hive_remote}


def _mint_host(monkeypatch, host_id=HOST_A, label="fixture-host"):
    monkeypatch.setattr(host, "load", lambda: {"host_id": host_id, "label": label})
    return host_id, label


def _write_manifest(hq_dir, host_id, *, label="other-host", role="viewer", age=0.0):
    """Write `host_id`'s manifest directly (not necessarily THIS host's — `hosts.save` doesn't
    care whose it is) and commit it — `bh host remove` diffs against a real git history, same
    as a manifest that reached this clone via a prior `bh host init` + push/pull, never a
    file dropped in with no commit behind it. Backdates the file's mtime by `age` seconds
    (after the commit, which would otherwise reset it) so staleness tests don't need to mock
    the clock."""
    manifest = hosts.HostManifest(
        host_id=host_id,
        label=label,
        os="linux",
        arch="x86_64",
        role=role,
        identity=hosts.IdentityMechanism(kind="none", value=""),
    )
    path = hosts.save(hq_dir, manifest)
    _git(["add", "-A"], hq_dir)
    _git(["commit", "-m", f"chore(host): seed {host_id}"], hq_dir)
    if age:
        old = time.time() - age
        os.utime(path, (old, old))
    return path


def _last_commit_subject(hq_dir) -> str:
    result = _git(["log", "-1", "--format=%s"], hq_dir)
    return result.stdout.strip()


# ---- basic removal + commit -------------------------------------------------------------


def test_remove_drops_a_stale_foreign_manifest_and_commits(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=_WELL_PAST_STALE)

    result = runner.invoke(app, ["host", "rm", HOST_B, "--confirm"])

    assert result.exit_code == 0, result.output
    with pytest.raises(FileNotFoundError):
        hosts.load(hq, HOST_B)
    assert "remove" in _last_commit_subject(hq)
    assert HOST_B in _last_commit_subject(hq)


def test_remove_unknown_host_id_errors_cleanly(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)

    result = runner.invoke(app, ["host", "rm", "no-such-host"])

    assert result.exit_code == 1
    assert "no-such-host" in result.output


def test_remove_leaves_hq_clean_when_nothing_was_dirty_besides_the_deletion(hq, monkeypatch):
    """`_commit_if_dirty` only commits when there IS something dirty — a second `remove` on an
    already-removed host_id fails at the manifest-missing check, never reaching commit."""
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=_WELL_PAST_STALE)
    runner.invoke(app, ["host", "rm", HOST_B, "--confirm"])
    before = _last_commit_subject(hq)

    result = runner.invoke(app, ["host", "rm", HOST_B, "--confirm"])

    assert result.exit_code == 1
    assert _last_commit_subject(hq) == before  # no new commit


# ---- intent gate: --confirm / --dry-run (bh-gbcw) ----------------------------------------


def test_rm_refuses_without_confirm_and_changes_nothing(hq, monkeypatch):
    """The gate `hive rm` already carried, now on `host rm` too: a stale, lease-free, foreign
    manifest — every OTHER gate satisfied — still refuses bare, because this is fleet truth."""
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=_WELL_PAST_STALE)
    before = _last_commit_subject(hq)

    result = runner.invoke(app, ["host", "rm", HOST_B])

    assert result.exit_code == 1
    assert "--confirm" in result.output
    assert hosts.load(hq, HOST_B) is not None  # untouched
    assert _last_commit_subject(hq) == before  # no commit


def test_rm_dry_run_previews_the_plan_and_mutates_nothing(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=_WELL_PAST_STALE)
    before = _last_commit_subject(hq)

    result = runner.invoke(app, ["host", "rm", HOST_B, "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert HOST_B in result.output
    assert hosts.load(hq, HOST_B) is not None  # untouched
    assert _last_commit_subject(hq) == before  # no commit


def test_rm_refuses_dry_run_and_confirm_together(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=_WELL_PAST_STALE)

    result = runner.invoke(app, ["host", "rm", HOST_B, "--dry-run", "--confirm"])

    assert result.exit_code == 1
    assert hosts.load(hq, HOST_B) is not None  # untouched


# ---- recency gate -------------------------------------------------------------------------


def test_remove_refuses_a_recently_touched_manifest_without_force(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=0.0)  # freshly written: plausibly still alive

    result = runner.invoke(app, ["host", "rm", HOST_B])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert hosts.load(hq, HOST_B) is not None  # untouched


def test_remove_force_overrides_the_recency_gate(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=0.0)

    result = runner.invoke(app, ["host", "rm", HOST_B, "--confirm", "--force"])

    assert result.exit_code == 0, result.output
    with pytest.raises(FileNotFoundError):
        hosts.load(hq, HOST_B)


# ---- self-removal gate -------------------------------------------------------------------


def test_rm_refuses_this_hosts_own_manifest_without_confirm(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_A, age=_WELL_PAST_STALE)

    result = runner.invoke(app, ["host", "rm", HOST_A])

    assert result.exit_code == 1
    assert "--confirm" in result.output
    assert hosts.load(hq, HOST_A) is not None  # untouched


def test_rm_confirm_alone_does_not_bypass_the_recency_gate(hq, monkeypatch):
    """`--confirm` only answers "do you mean it" — a fresh self-manifest still needs `--force`
    too, exactly like a foreign one would (bh-gbcw kept the two axes separate)."""
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_A, age=0.0)  # fresh: plausibly still alive

    result = runner.invoke(app, ["host", "rm", HOST_A, "--confirm"])

    assert result.exit_code == 1
    assert "--force" in result.output
    assert hosts.load(hq, HOST_A) is not None  # untouched


def test_rm_self_succeeds_with_both_confirm_and_force(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_A, age=0.0)

    result = runner.invoke(app, ["host", "rm", HOST_A, "--confirm", "--force"])

    assert result.exit_code == 0, result.output
    with pytest.raises(FileNotFoundError):
        hosts.load(hq, HOST_A)


def test_remove_a_stale_non_self_host_needs_no_flags_at_all(hq, monkeypatch):
    """The common case this bead exists for: an orphaned manifest left behind by a host that
    was wiped and rebuilt under a different host_id — no lease, long stale, not self."""
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(hq, HOST_B, age=_WELL_PAST_STALE)

    result = runner.invoke(app, ["host", "rm", HOST_B, "--confirm"])

    assert result.exit_code == 0, result.output


# ---- live-lease gate ----------------------------------------------------------------------


def test_remove_refuses_a_host_holding_a_live_lease_without_force(one_hive, monkeypatch):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0)
    host_lease.adopt(
        "origin", PREFIX_A, host_id=HOST_B, label="desk", cwd=one_hive["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(one_hive["hq_dir"], HOST_B, age=_WELL_PAST_STALE)
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)

    result = runner.invoke(app, ["host", "rm", HOST_B])

    assert result.exit_code == 1
    assert PREFIX_A in result.output
    assert "--force" in result.output
    assert hosts.load(one_hive["hq_dir"], HOST_B) is not None  # untouched
    live = host_lease.read("origin", PREFIX_A, cwd=one_hive["hq_dir"])
    assert live.host_id == HOST_B and not live.is_tombstone  # untouched


def test_remove_force_releases_the_lease_then_removes(one_hive, monkeypatch):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0)
    host_lease.adopt(
        "origin", PREFIX_A, host_id=HOST_B, label="desk", cwd=one_hive["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(one_hive["hq_dir"], HOST_B, age=0.0)  # fresh: --force also needed for this
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)

    result = runner.invoke(app, ["host", "rm", HOST_B, "--confirm", "--force"])

    assert result.exit_code == 0, result.output
    assert "released" in result.output
    with pytest.raises(FileNotFoundError):
        hosts.load(one_hive["hq_dir"], HOST_B)
    live = host_lease.read("origin", PREFIX_A, cwd=one_hive["hq_dir"])
    assert live.is_tombstone


def test_remove_ignores_an_expired_lease(one_hive, monkeypatch):
    """An expired lease is `free`, not held — `remove` proceeds without `--force` for that
    reason (the manifest still needs to clear the independent recency gate on its own mtime)."""
    monkeypatch.setattr(host_lease.time, "time", lambda: T0)
    host_lease.adopt(
        "origin", PREFIX_A, host_id=HOST_B, label="desk", cwd=one_hive["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    _write_manifest(one_hive["hq_dir"], HOST_B, age=_WELL_PAST_STALE)
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 99999)  # well past the 600s TTL

    result = runner.invoke(app, ["host", "rm", HOST_B, "--confirm"])

    assert result.exit_code == 0, result.output
    live = host_lease.read("origin", PREFIX_A, cwd=one_hive["hq_dir"])
    assert live.host_id == HOST_B and not live.is_tombstone  # left as-is, merely expired


# ---- end-to-end: the motivating wipe-and-readopt cycle -------------------------------------


def test_repeated_wipe_and_readopt_leaves_no_orphan_manifests(hq, monkeypatch):
    """host A goes through its manifest lifecycle, then the machine is wiped and rebuilt —
    which (per host.py's module docstring) comes back as a DIFFERENT host_id, B. From B, `bh
    host rm A --confirm` clears the orphan A left behind, so the roster ends up with exactly
    one live entry."""
    _mint_host(monkeypatch, HOST_A)
    result = runner.invoke(app, ["host", "init", "--role", "viewer"])
    assert result.exit_code == 0, result.output
    manifest_a = hosts.manifest_path(hq, HOST_A)
    old = time.time() - _WELL_PAST_STALE
    os.utime(manifest_a, (old, old))  # simulate time elapsed since the wipe

    _mint_host(monkeypatch, HOST_B)
    result = runner.invoke(app, ["host", "init", "--role", "viewer"])
    assert result.exit_code == 0, result.output

    result = runner.invoke(app, ["host", "rm", HOST_A, "--confirm"])
    assert result.exit_code == 0, result.output

    rows = json.loads(runner.invoke(app, ["host", "list", "--json"]).stdout)
    assert [row["host_id"] for row in rows] == [HOST_B]


# ---- `bh host list` STALE column -----------------------------------------------------------


def test_list_marks_a_stale_manifest_distinctly(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    runner.invoke(app, ["host", "init", "--role", "viewer"])
    _write_manifest(hq, HOST_C, label="old-box", age=_WELL_PAST_STALE)

    result = runner.invoke(app, ["host", "list"])

    assert result.exit_code == 0, result.output
    assert "STALE" in result.output
    lines = {line.split()[0]: line for line in result.output.splitlines()[1:]}
    assert "stale" in lines[HOST_C]
    assert "stale" not in lines[HOST_A]


def test_list_json_carries_the_stale_marker_per_row(hq, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    runner.invoke(app, ["host", "init", "--role", "viewer"])
    _write_manifest(hq, HOST_C, label="old-box", age=_WELL_PAST_STALE)

    rows = json.loads(runner.invoke(app, ["host", "list", "--json"]).stdout)

    by_id = {row["host_id"]: row for row in rows}
    assert by_id[HOST_C]["stale"] == "stale"
    assert by_id[HOST_A]["stale"] == ""
