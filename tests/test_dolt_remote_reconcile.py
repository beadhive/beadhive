"""Reconcile one hive's Dolt remote from HQ config (bh-b1scz.5).

Unit tests use a stateful fake Engine; the integration test drives ``bh hive remote`` against a
real embedded bd hive with ``file://`` remotes.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from beadhive import cli, config, dolt_remote_reconcile, engine, safety
from beadhive.dolt_remote_reconcile import Outcome
from beadhive.run import run
from harness.beads import embedded_env, skip_if_no_bd

TOKEN = "ghp_fixtureTOKENvalue"
AWS = {"AWS_ACCESS_KEY_ID": "AKIAFIXTURE", "AWS_SECRET_ACCESS_KEY": "fixture-secret"}


class FakeEngine:
    """Stateful registration store; records every mutation attempt."""

    def __init__(self, remotes=None, list_ok=True):
        self.remotes = dict(remotes or {})
        self.list_ok = list_ok
        self.added: list[tuple[str, str, dict]] = []
        self.forbidden_calls: list[str] = []

    def list_remotes(self, cwd):
        if not self.list_ok:
            return engine.RemoteListing(ok=False, error="timeout")
        rows = tuple(engine.DoltRemoteEntry(n, u) for n, u in self.remotes.items())
        return engine.RemoteListing(ok=True, remotes=rows)

    def add_remote(self, cwd, name, url, *, extra_env=None):
        self.added.append((name, url, dict(extra_env or {})))
        self.remotes[name] = url
        return subprocess.CompletedProcess(["bd"], 0, stdout="", stderr="")

    def __getattr__(self, item):  # replace/remove/push/pull must never be reached
        if item in {"replace_remote", "remove_remote", "push_state", "pull_state"}:
            self.forbidden_calls.append(item)
        raise AttributeError(item)


def _entry(url=None, name="origin", credentials="", repo="api"):
    entry = {"provider": "github", "org": "acme", "repo": repo, "prefix": f"ac-{repo}"}
    if url:
        entry["dolt_remote"] = {"url": url, "name": name, "credentials": credentials}
    return entry


@pytest.fixture
def hive_dir(tmp_path, monkeypatch):
    path = tmp_path / "hive"
    (path / ".beads").mkdir(parents=True)
    (path / ".beads" / "metadata.json").write_text('{"dolt_mode": "server"}')
    monkeypatch.setattr(dolt_remote_reconcile, "_leased", lambda cfg, entry: False)
    monkeypatch.setattr(safety, "scan", _no_git_inference)
    return path


def _no_git_inference(*_a, **_k):
    raise AssertionError("reconcile must not infer remote state from Git refs")


def _run(entry, eng, hive_dir, *, dry_run=False, environ=None, home=None):
    return dolt_remote_reconcile.reconcile_hive(
        entry,
        {},
        dry_run=dry_run,
        eng=eng,
        environ=AWS if environ is None else environ,
        home=home or Path("/nonexistent"),
        hive_dir=hive_dir,
    )


def test_no_descriptor_is_todays_behaviour(hive_dir):
    eng = FakeEngine()
    report = _run(_entry(), eng, hive_dir)
    assert report.outcome == Outcome.NO_DESCRIPTOR and not report.failing and eng.added == []


def test_dry_run_reports_the_exact_mutation_and_writes_nothing(hive_dir):
    eng = FakeEngine()
    before = sorted(p.name for p in (hive_dir / ".beads").iterdir())
    report = _run(_entry("s3://acme-beads/beads/api"), eng, hive_dir, dry_run=True)
    assert report.outcome == Outcome.WOULD_ADD
    assert report.mutation == "bd dolt remote add origin s3://acme-beads/beads/api"
    assert eng.added == [] and eng.remotes == {}
    assert sorted(p.name for p in (hive_dir / ".beads").iterdir()) == before


def test_missing_remote_is_added_then_a_second_run_is_a_no_op(hive_dir):
    eng = FakeEngine()
    first = _run(_entry("s3://acme-beads/beads/api"), eng, hive_dir)
    assert first.outcome == Outcome.ADDED and first.credential_source == "ambient:AWS_ACCESS_KEY_ID"
    second = _run(_entry("s3://acme-beads/beads/api"), eng, hive_dir)
    assert second.outcome == Outcome.IN_SYNC and len(eng.added) == 1
    assert eng.forbidden_calls == []


def test_a_different_canonical_remote_is_migration_required_and_not_replaced(hive_dir):
    eng = FakeEngine({"origin": "git+ssh://git@github.com/acme/api.git"})
    report = _run(_entry("s3://acme-beads/beads/api"), eng, hive_dir)
    assert report.outcome == Outcome.MIGRATION_REQUIRED and report.failing
    assert eng.added == [] and eng.remotes["origin"].startswith("git+ssh")
    assert eng.forbidden_calls == []


def test_the_url_under_another_name_is_reported_not_renamed(hive_dir):
    eng = FakeEngine({"backup": "s3://acme-beads/beads/api"})
    report = _run(_entry("s3://acme-beads/beads/api"), eng, hive_dir)
    assert report.outcome == Outcome.NAME_MISMATCH and eng.added == []


@pytest.mark.parametrize(
    "url", ["gs://acme/api", "s3://b/api?endpoint=https://minio:9000", "future://h/api"]
)
def test_unsupported_families_are_refused_before_mutation(hive_dir, url):
    eng = FakeEngine()
    report = _run(_entry(url), eng, hive_dir)
    assert report.outcome == Outcome.UNSUPPORTED and eng.added == []


def test_s3_on_an_embedded_store_is_unsupported_with_the_reason(hive_dir):
    (hive_dir / ".beads" / "metadata.json").write_text('{"dolt_mode": "embedded"}')
    report = _run(_entry("s3://acme-beads/beads/api"), FakeEngine(), hive_dir)
    assert report.outcome == Outcome.UNSUPPORTED and "embedded" in report.detail


def test_missing_credentials_fail_before_mutation(hive_dir, tmp_path):
    eng = FakeEngine()
    report = _run(_entry("aws://[t:b]/api"), eng, hive_dir, environ={}, home=tmp_path)
    assert report.outcome == Outcome.CREDENTIALS_MISSING and eng.added == []
    assert "AWS_ACCESS_KEY_ID" in report.detail


def test_unreadable_registration_is_an_error_never_an_empty_list(hive_dir):
    eng = FakeEngine(list_ok=False)
    report = _run(_entry("file:///srv/r"), eng, hive_dir)
    assert report.outcome == Outcome.ERROR and eng.added == []


def test_no_local_store_is_reported_not_failed(tmp_path):
    report = _run(_entry("file:///srv/r"), FakeEngine(), tmp_path / "absent")
    assert report.outcome == Outcome.NO_LOCAL_STORE and not report.failing


def test_object_store_reports_unknown_health_and_never_refs_dolt_data(hive_dir):
    eng = FakeEngine({"origin": "s3://acme-beads/beads/api"})
    report = _run(_entry("s3://acme-beads/beads/api"), eng, hive_dir)
    rendered = "\n".join(dolt_remote_reconcile.render([report], dry_run=False))
    assert dolt_remote_reconcile.HEALTH_CAVEAT in rendered
    assert "refs/dolt/data" not in rendered


def test_output_json_and_logs_are_redacted(hive_dir, monkeypatch):
    events = []

    class Log:
        def info(self, event, **fields):
            events.append((event, fields))

    monkeypatch.setattr(dolt_remote_reconcile, "get_logger", lambda name: Log())
    eng = FakeEngine({"origin": f"https://{TOKEN}@doltremoteapi.dolthub.com/acme/api"})
    reports = dolt_remote_reconcile.reconcile(
        None,
        cfg={"managed_repos": [_entry("file:///srv/remote/api")]},
        reconcile_one=lambda entry, cfg, dry_run: _run(entry, eng, hive_dir, dry_run=dry_run),
    )
    blob = json.dumps(dolt_remote_reconcile.to_json(reports, dry_run=False))
    blob += "\n".join(dolt_remote_reconcile.render(reports, dry_run=False))
    blob += json.dumps(events)
    assert reports[0].outcome == Outcome.MIGRATION_REQUIRED
    assert TOKEN not in blob and "***@doltremoteapi.dolthub.com" in blob


def test_fleet_mode_isolates_failures_and_returns_a_complete_summary(hive_dir):
    entries = [
        _entry("file:///srv/a", repo="a"),
        _entry("file:///srv/b", repo="b"),
        _entry(repo="c"),
    ]

    def one(entry, cfg, dry_run):
        if entry["repo"] == "a":
            raise RuntimeError(f"boom {TOKEN}")
        return _run(entry, FakeEngine(), hive_dir, dry_run=dry_run)

    reports = dolt_remote_reconcile.reconcile(
        None, cfg={"managed_repos": entries}, reconcile_one=one
    )
    assert [r.outcome for r in reports] == [Outcome.ERROR, Outcome.ADDED, Outcome.NO_DESCRIPTOR]
    assert TOKEN not in reports[0].detail
    assert dolt_remote_reconcile.to_json(reports, dry_run=False)["ok"] is False


# ---- real bd: `bh hive remote` over file:// remotes ----------------------------------------


@pytest.fixture
def real_fleet(tmp_path, monkeypatch):
    for key in set(os.environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    ws = tmp_path / "ws"
    hive = ws / "github" / "acme" / "api"
    hive.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], cwd=str(hive), check=True, capture=True)
    run(["bd", "init", "--prefix", "ac-api", "--quiet"], cwd=str(hive), check=True, capture=True)
    remote = tmp_path / "remotes" / "acme-api"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "schema_version: 1\nmanaged_repos:\n"
        '  - {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api", '
        f'"kind": "personal", "dolt_remote": {{"url": "file://{remote}"}}}}\n'
    )
    monkeypatch.setenv("GIT_WORKSPACE", str(ws))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.setattr(dolt_remote_reconcile, "_leased", lambda cfg, entry: False)
    return hive, remote


@pytest.mark.integration
@skip_if_no_bd
def test_cli_reconcile_against_real_bd(real_fleet):
    hive, remote = real_fleet
    runner = CliRunner()

    dry = runner.invoke(cli.app, ["hive", "remote", "--all", "--reconcile", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert f"would run: bd dolt remote add origin file://{remote}" in dry.output
    assert engine.BdEngine().list_remotes(hive).remotes == ()

    live = runner.invoke(cli.app, ["hive", "remote", "ac-api", "--reconcile", "--json"])
    assert live.exit_code == 0, live.output
    assert json.loads(live.stdout)["hives"][0]["outcome"] == "added"
    assert engine.BdEngine().get_remote(hive, "origin").url == f"file://{remote}"
    assert not (remote / "manifest").exists()  # registration only: nothing was pushed

    again = runner.invoke(cli.app, ["hive", "remote", "--all"])
    assert again.exit_code == 0 and "in-sync" in again.output

    engine.BdEngine().replace_remote(hive, "origin", f"file://{remote}-elsewhere")
    drift = runner.invoke(cli.app, ["hive", "remote", "--all", "--reconcile"])
    assert drift.exit_code == 1 and "migration-required" in drift.output
    assert engine.BdEngine().get_remote(hive, "origin").url.endswith("-elsewhere")
    assert config.load()["managed_repos"][0]["dolt_remote"]["url"] == f"file://{remote}"
