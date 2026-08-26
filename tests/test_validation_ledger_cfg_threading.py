"""bh-v520n: nothing asserted that a caller-supplied `cfg` is actually USED by the ledger's TTL
lookup — reverting `_ttl`'s `cfg` threading back to an internal `config.load()` left the full
gate green (5037 passed, 9 skipped) because every test that passes `cfg` also happens to pass
under a freshly-loaded config. These two tests close that gap: they make
`validation_ledger.config.load` a trap and assert it is never reached when a caller supplies
`cfg` — reaching it is itself the failure, per the bead's acceptance criteria (bh-ku9n9.19).

New file, deliberately: bh-ehmd8 and bh-8c2yo are in flight from `main` on `worktree.py`,
`prepush.py`, `release.py`, `config_schema.py` and the ledger's hit seam — a new file cannot
conflict with either at merge time.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import host, validation_ledger


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """`record`'s stamped `host` field needs `host.yaml` to exist (bh-ytbb.4) — the shared
    `_sandbox_bh_home` fixture (tests/conftest.py) only seeds `config.yaml`. Mirrors
    `tests/test_worktree.py`'s fixture of the same name; duplicated rather than imported so this
    file has no cross-file dependency on one that's being edited by other in-flight beads."""
    host.mint_if_needed()


def _hive(tmp_path, monkeypatch):
    """Minimal real hive entry; the test asserts only whether ``config.load`` is reached.

    Validation runs also allocate canonical repo-private artifacts, whose shared-root resolver
    intentionally requires real git metadata rather than a directory merely named ``.git``.
    """
    repo = tmp_path / "ws" / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    return {"provider": "github", "org": "myorg", "repo": "myrepo"}


def _forbid_config_load(monkeypatch):
    """Trap: fail loudly if the ledger ever calls `config.load()` despite a caller-supplied
    `cfg`. This IS the assertion — the bead is explicit that reaching `config.load()` is itself
    the failure, not something whose return value needs checking."""

    def _boom():
        raise AssertionError("config.load() reached — caller-supplied cfg was not honoured")

    monkeypatch.setattr(validation_ledger.config, "load", _boom)


def test_record_honours_caller_supplied_cfg_without_reading_config(tmp_path, monkeypatch):
    """`record`'s TTL lookup (via `_ttl`) uses a caller-supplied `cfg` instead of re-reading
    config from disk (bh-ku9n9.19, item 2). Revert `_ttl` back to
    `config.ledger_ttl(config.load(), entry)` internally and this goes red on the trap."""
    entry = _hive(tmp_path, monkeypatch)
    _forbid_config_load(monkeypatch)
    cfg = {"work": {"ledger_ttl": "P1D"}}

    validation_ledger.record(entry, "abc123", "just check", 0, cfg=cfg)  # must not raise


def test_green_verdict_honours_caller_supplied_cfg_without_reading_config(tmp_path, monkeypatch):
    """Same contract on the read side: `green_verdict` (via `verdict` -> `_ttl`) must use a
    caller-supplied `cfg`, not `config.load()`. The write above happens before the trap is set
    so only THIS call's threading is under test."""
    entry = _hive(tmp_path, monkeypatch)
    cfg = {"work": {"ledger_ttl": "P1D"}}
    validation_ledger.record(entry, "abc123", "just check", 0, cfg=cfg)

    _forbid_config_load(monkeypatch)
    hit = validation_ledger.green_verdict(entry, "abc123", "just check", cfg=cfg)
    assert hit is not None and hit["rc"] == 0  # must not raise, and the entry is still found
