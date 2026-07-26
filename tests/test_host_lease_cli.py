"""`bh host adopt|release|packup`, and `bh host list --lease-hive` (bh-ytbb.13).

The CLI's first wiring of the already-landed host-lease primitives
(:mod:`beadhive.host_lease`, :mod:`beadhive.host_adopt`, :mod:`beadhive.host_fence`).

Covers the acceptance bar directly:
  * `bh host adopt <hive>` / `release <hive>` / `packup` implemented.
  * `bh host list` gains a per-hive lease-state column (held/expiring/free, with holder) via
    `--lease-hive` (deliberately NOT the reserved `--hive` — see `list_cmd`'s docstring),
    using `host_cli.render_table`'s existing seam rather than restructuring `list`.
  * `packup` releases every held lease and reports what it released.
  * adopting an unexpired foreign lease refuses without `--force`; with `--force` it logs
    loudly (the `host_lease_forced_takeover` warning `host_lease.adopt` already emits).

Every remote is a scratch BARE repo under the test's own `tmp_path`, wired as a REAL `origin`
on real local clones (the same shape `bh hq init`/a real hive clone leaves behind) — never a
network remote, and (via the autouse `_sandbox_bh_home` fixture) never the operator's real
`~/.beadhive/hq`.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from beadhive import config, host, host_fence, host_lease, registry
from beadhive.cli import app

runner = CliRunner()

PREFIX_A = "aa"
PREFIX_B = "bb"
HOST_A = "11111111-1111-4111-8111-111111111111"
HOST_B = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


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


def _register_hive(tmp_path, monkeypatch, workspace, org, repo, prefix):
    hive_remote = _bare(tmp_path, f"{repo}.git")
    hive_dir = workspace / "github" / org / repo
    _clone_with_origin(hive_dir, hive_remote)
    return hive_dir, hive_remote, ("github", org, repo, prefix)


@pytest.fixture
def one_hive(tmp_path, monkeypatch, hq):
    """One registered hive (PREFIX_A), with a real, `origin`-wired remote of its own."""
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(registry, "workspace_root", lambda: str(workspace))
    hive_dir, hive_remote, entry = _register_hive(
        tmp_path, monkeypatch, workspace, "o", "r", PREFIX_A
    )
    _write_config([entry])
    return {"hq_dir": hq, "hive_dir": hive_dir, "hive_remote": hive_remote}


@pytest.fixture
def two_hives(tmp_path, monkeypatch, hq):
    """Two registered hives (PREFIX_A, PREFIX_B), each with its own real remote — packup's
    "every held lease" only means something with more than one hive in play."""
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(registry, "workspace_root", lambda: str(workspace))
    dir_a, remote_a, entry_a = _register_hive(
        tmp_path, monkeypatch, workspace, "oa", "ra", PREFIX_A
    )
    dir_b, remote_b, entry_b = _register_hive(
        tmp_path, monkeypatch, workspace, "ob", "rb", PREFIX_B
    )
    _write_config([entry_a, entry_b])
    return {
        "hq_dir": hq,
        "a": {"hive_dir": dir_a, "hive_remote": remote_a},
        "b": {"hive_dir": dir_b, "hive_remote": remote_b},
    }


def _mint_host(monkeypatch, host_id=HOST_A, label="fixture-host"):
    monkeypatch.setattr(host, "load", lambda: {"host_id": host_id, "label": label})
    return host_id, label


def _init_role(role="primary-default"):
    result = runner.invoke(app, ["host", "init", "--role", role])
    assert result.exit_code == 0, result.output


# ---- bh host adopt ---------------------------------------------------------------------


def test_adopt_becomes_primary_and_fences_the_hive_first(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _init_role("primary-default")

    result = runner.invoke(app, ["host", "adopt", PREFIX_A])

    assert result.exit_code == 0, result.output
    assert "adopted" in result.output and PREFIX_A in result.output

    lease = host_lease.read("origin", PREFIX_A, cwd=one_hive["hq_dir"])
    assert lease is not None and lease.host_id == HOST_A and lease.epoch == 1

    _sha, fence = host_fence.read_fence("origin", cwd=one_hive["hive_dir"])
    assert fence is not None and fence.epoch == 1 and fence.host_id == HOST_A


def test_adopt_refuses_an_unexpired_foreign_lease_without_force(one_hive, monkeypatch):
    host_lease.adopt(
        "origin", PREFIX_A, host_id=HOST_B, label="desk", cwd=one_hive["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    _init_role()
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)

    result = runner.invoke(app, ["host", "adopt", PREFIX_A])

    assert result.exit_code == 1
    assert HOST_B in result.output


def test_adopt_with_force_seizes_the_lease_and_logs_loudly(one_hive, monkeypatch):
    host_lease.adopt(
        "origin", PREFIX_A, host_id=HOST_B, label="desk", cwd=one_hive["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    _init_role()
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 1)

    seen: list[tuple] = []

    class _Recorder:
        def warning(self, event, **kw):
            seen.append((event, kw))

    monkeypatch.setattr(host_lease.log, "get_logger", lambda *_a, **_k: _Recorder())

    result = runner.invoke(app, ["host", "adopt", PREFIX_A, "--force"])

    assert result.exit_code == 0, result.output
    lease = host_lease.read("origin", PREFIX_A, cwd=one_hive["hq_dir"])
    assert lease.host_id == HOST_A
    # `seen` also catches unrelated warnings from other modules sharing `log.get_logger`
    # (e.g. config's own "fleet_config_missing" nudge) — filter for OUR event specifically.
    takeovers = [kw for evt, kw in seen if evt == "host_lease_forced_takeover"]
    assert takeovers and takeovers[0]["from_host"] == HOST_B


def test_adopt_refuses_when_this_hosts_role_is_worker(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _init_role("worker")

    result = runner.invoke(app, ["host", "adopt", PREFIX_A])

    assert result.exit_code == 1
    assert "worker" in result.output
    assert host_lease.read("origin", PREFIX_A, cwd=one_hive["hq_dir"]) is None


def test_adopt_requires_this_hosts_manifest_first(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A)  # host.yaml minted, but `bh host init` never run

    result = runner.invoke(app, ["host", "adopt", PREFIX_A])

    assert result.exit_code == 1
    assert "host init --role" in result.output


def test_adopt_unknown_hive_errors_cleanly(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _init_role()

    result = runner.invoke(app, ["host", "adopt", "no-such-hive"])

    assert result.exit_code == 1
    assert "no hive matching" in result.output


# ---- bh host release --------------------------------------------------------------------


def test_release_yields_the_lease_and_updates_the_local_cache(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _init_role()
    runner.invoke(app, ["host", "adopt", PREFIX_A])

    result = runner.invoke(app, ["host", "release", PREFIX_A])

    assert result.exit_code == 0, result.output
    live = host_lease.read("origin", PREFIX_A, cwd=one_hive["hq_dir"])
    assert live.is_tombstone
    cached = host_lease.read_cached(PREFIX_A, cwd=one_hive["hq_dir"])
    assert cached.is_tombstone  # the LOCAL mirror reflects the release immediately


def test_release_does_not_touch_the_epoch_fence(one_hive, monkeypatch):
    """Deliberate: release is HQ-only bookkeeping, never two-phase like adopt (see
    release_cmd's docstring for the reasoning) — the fence this host installed on adopt is
    left exactly as adopt left it."""
    _mint_host(monkeypatch, HOST_A)
    _init_role()
    runner.invoke(app, ["host", "adopt", PREFIX_A])
    _sha_before, fence_before = host_fence.read_fence("origin", cwd=one_hive["hive_dir"])

    runner.invoke(app, ["host", "release", PREFIX_A])

    _sha_after, fence_after = host_fence.read_fence("origin", cwd=one_hive["hive_dir"])
    assert fence_after == fence_before


def test_release_refuses_when_this_host_does_not_hold_it(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A)

    result = runner.invoke(app, ["host", "release", PREFIX_A])

    assert result.exit_code == 1
    assert "nothing to release" in result.output


# ---- bh host packup ----------------------------------------------------------------------


def test_packup_releases_every_held_hive_and_reports_it(two_hives, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _init_role()
    runner.invoke(app, ["host", "adopt", PREFIX_A])
    runner.invoke(app, ["host", "adopt", PREFIX_B])

    result = runner.invoke(app, ["host", "packup"])

    assert result.exit_code == 0, result.output
    assert PREFIX_A in result.output and PREFIX_B in result.output
    assert host_lease.read("origin", PREFIX_A, cwd=two_hives["hq_dir"]).is_tombstone
    assert host_lease.read("origin", PREFIX_B, cwd=two_hives["hq_dir"]).is_tombstone


def test_packup_skips_a_hive_this_host_does_not_hold(two_hives, monkeypatch):
    monkeypatch.setattr(host_lease.time, "time", lambda: T0)  # one consistent clock throughout
    host_lease.adopt(
        "origin", PREFIX_B, host_id=HOST_B, label="desk", cwd=two_hives["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    _init_role()
    runner.invoke(app, ["host", "adopt", PREFIX_A])

    result = runner.invoke(app, ["host", "packup"])

    assert result.exit_code == 0, result.output
    summary_line = next(line for line in result.output.splitlines() if "released" in line)
    assert PREFIX_A in summary_line and PREFIX_B not in summary_line
    assert host_lease.read("origin", PREFIX_A, cwd=two_hives["hq_dir"]).is_tombstone
    still_b = host_lease.read("origin", PREFIX_B, cwd=two_hives["hq_dir"])
    assert still_b.host_id == HOST_B and not still_b.is_tombstone  # untouched


def test_packup_reports_nothing_held_when_this_host_holds_nothing(two_hives, monkeypatch):
    _mint_host(monkeypatch, HOST_A)

    result = runner.invoke(app, ["host", "packup"])

    assert result.exit_code == 0, result.output
    assert "nothing held" in result.output


def test_packup_skips_an_already_expired_lease(two_hives, monkeypatch):
    host_lease.adopt(
        "origin", PREFIX_A, host_id=HOST_A, label="lap", cwd=two_hives["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 9999)  # well past the TTL

    result = runner.invoke(app, ["host", "packup"])

    assert result.exit_code == 0, result.output
    assert "nothing held" in result.output
    live = host_lease.read("origin", PREFIX_A, cwd=two_hives["hq_dir"])
    assert live.host_id == HOST_A and not live.is_tombstone  # left as-is, merely expired


# ---- bh host list --hive: the render_table seam -------------------------------------------


def test_list_without_hive_is_unaffected(one_hive, monkeypatch):
    """The base case stays byte-for-byte what bh-ytbb.5 shipped — no LEASE column at all."""
    _mint_host(monkeypatch, HOST_A)
    _init_role()

    result = runner.invoke(app, ["host", "list"])

    assert result.exit_code == 0, result.output
    assert "LEASE" not in result.output


def test_list_hive_shows_held_with_the_holder(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A, label="deskmac")
    _init_role()
    runner.invoke(app, ["host", "adopt", PREFIX_A])

    result = runner.invoke(app, ["host", "list", "--lease-hive", PREFIX_A])

    assert result.exit_code == 0, result.output
    assert "LEASE" in result.output
    assert "held" in result.output
    assert HOST_A in result.output
    assert f"lease ({PREFIX_A}): held" in result.output


def test_list_hive_shows_free_when_nobody_holds_it(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A)
    _init_role()

    result = runner.invoke(app, ["host", "list", "--lease-hive", PREFIX_A])

    assert result.exit_code == 0, result.output
    assert f"lease ({PREFIX_A}): free" in result.output


def test_list_hive_shows_expiring_within_the_renew_interval(one_hive, monkeypatch):
    host_lease.adopt(
        "origin", PREFIX_A, host_id=HOST_A, label="lap", cwd=one_hive["hq_dir"], ttl=600.0, at=T0
    )
    _mint_host(monkeypatch, HOST_A)
    monkeypatch.setattr(host_lease.time, "time", lambda: T0 + 301)  # inside the 300s window

    result = runner.invoke(app, ["host", "list", "--lease-hive", PREFIX_A])

    assert result.exit_code == 0, result.output
    assert f"lease ({PREFIX_A}): expiring" in result.output


def test_list_hive_json_carries_the_lease_key_on_the_holder_row(one_hive, monkeypatch):
    _mint_host(monkeypatch, HOST_A, label="deskmac")
    _init_role()
    runner.invoke(app, ["host", "adopt", PREFIX_A])

    result = runner.invoke(app, ["host", "list", "--lease-hive", PREFIX_A, "--json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["host_id"] == HOST_A
    assert rows[0]["lease"] == "held"


def test_list_hive_unknown_hive_errors_cleanly(one_hive):
    result = runner.invoke(app, ["host", "list", "--lease-hive", "no-such-hive"])
    assert result.exit_code == 1
    assert "no hive matching" in result.output
