"""bh-0tmvk: a non-regular file at a path bh reads with `Path.read_text()` must degrade to
"absent" rather than block forever. A FIFO is the case that matters, because it is the one that
neither `except OSError` nor `except ValueError` catches — the read never returns at all, so the
command has no error, no exit code and no log line, just a prompt that never comes back.

Two sites, both reached from `bh work submit` and the pre-push hook:
  * `validation_ledger._read_index` and the legacy importer — verdict lookup readers.
  * `worktree._read_verify_marker` — `sweep_verify_dirs` runs at the top of every
    `clean_checkout`, so the "orphan reaper" is on that same request path.

ASSERTED, NOT WEDGED. bh-0jgdz's equivalent test failed *by hanging* (its reviewer flagged that
the CI signal was a stuck run rather than a red line), and `pytest-timeout` is not wired in this
repo. `_deadline` below gets a real assertion out of the stdlib instead: `signal.setitimer` arms
a SIGALRM whose handler raises, which unblocks the pending `open()` (PEP 475 only retries a
syscall when the handler returns normally). Revert either guard and these go RED in ~2s.

New file rather than an edit to `tests/test_worktree.py`: the two tiers share one helper and one
rationale, and other beads are in flight from this base.
"""

from __future__ import annotations

import json
import os
import signal
import time
from contextlib import contextmanager

import pytest

from beadhive import host, validation_ledger, worktree

pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"),
    reason="FIFOs and SIGALRM are POSIX-only",
)


class _Blocked(BaseException):
    """Deliberately NOT an `Exception`, and not `TimeoutError`.

    Both readers under test catch `OSError`, and `TimeoutError` IS a subclass of `OSError` — so
    raising one from the alarm handler would be swallowed by the very `except` clause the FIFO
    bug bypasses, and a still-blocking reader would report a clean `[]`/`None` PASS. Verified:
    with `TimeoutError` the pre-fix code prints a green result; with this, it fails."""


@contextmanager
def _deadline(seconds: float = 2.0):
    """Fail (not hang) if the body has not finished within `seconds`."""

    def _alarm(*_):
        raise _Blocked

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    except _Blocked:
        pytest.fail(f"the read blocked for >{seconds}s instead of degrading to absent")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """`_write_verify_marker` / `_verify_dir_is_orphan` stamp and compare `host.host_id()`
    (bh-ytbb.4), which needs `host.yaml`; the shared `_sandbox_bh_home` fixture only seeds
    `config.yaml`. Mirrors the fixture of the same name in `tests/test_worktree.py`."""
    host.mint_if_needed()


def test_the_deadline_helper_actually_catches_a_blocking_read(tmp_path):
    """The guard on the guard: if `_deadline` could not interrupt a blocked FIFO open, every
    other test in this file would pass vacuously the moment either fix is reverted. Asserts the
    helper's own failure path, so "the alarm fires and is reported as a failure" is itself
    covered — not merely that a `_Blocked` escapes somewhere."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    with pytest.raises(pytest.fail.Exception, match="blocked for >1.0s"):
        with _deadline(1.0):
            fifo.read_text()


# ---- tier 1: the ledger, on every verdict lookup ----


def test_a_fifo_at_the_verdict_pointer_reads_as_a_miss(tmp_path):
    fifo = tmp_path / "verdict.json"
    os.mkfifo(fifo)

    with _deadline():
        assert validation_ledger._read_index(fifo) is None


@pytest.mark.parametrize("name", ["a directory", "a dangling symlink"])
def test_other_unreadable_verdict_paths_also_degrade(tmp_path, name):
    path = tmp_path / "verdict.json"
    if name == "a directory":
        path.mkdir()
    else:
        path.symlink_to(tmp_path / "gone.json")

    assert validation_ledger._read_index(path) is None, name


def test_a_symlink_to_a_real_verdict_pointer_is_still_read(tmp_path):
    """The guard is worthless if it breaks a legitimate case. `is_file()` follows symlinks —
    proven here by behaviour rather than by citing the docs, per bh-0jgdz's reviewer."""
    real = tmp_path / "real-verdict.json"
    payload = {"tree": "t1", "command_hash": "h1", "rc": 0}
    real.write_text(json.dumps(payload))
    link = tmp_path / "link.json"
    link.symlink_to(real)

    assert validation_ledger._read_index(link) == payload


def test_a_fifo_legacy_ledger_does_not_hang_a_real_verdict_lookup(tmp_path, monkeypatch):
    """End to end through the public seam, not just the private reader: `green_verdict` is what
    `bh work submit` and the pre-push hook call, and a FIFO ledger must make it MISS (revalidate)
    rather than wedge the command."""
    repo = tmp_path / "ws" / "github" / "myorg" / "myrepo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    entry = {"provider": "github", "org": "myorg", "repo": "myrepo"}
    os.mkfifo(repo / ".git" / validation_ledger.LEGACY_LEDGER_FILENAME)

    with _deadline():
        assert validation_ledger.green_verdict(entry, "abc123", "just check") is None


# ---- tier 2: the verify-dir marker, read at the top of every clean_checkout ----


def test_a_fifo_verify_marker_reads_as_no_marker(tmp_path):
    d = tmp_path / f"{worktree.VERIFY_LEAF_PREFIX}b1"
    d.mkdir()
    marker_root = tmp_path / "active"
    marker_root.mkdir()
    os.mkfifo(worktree._verify_marker_path(marker_root, d))

    with _deadline():
        assert worktree._read_verify_marker(d, marker_root=marker_root) is None
        # ...and both classifiers that share the reader stay unblocked and conservative:
        # a fresh dir with no usable marker is not yet an orphan, and contributes no live pid.
        assert (
            worktree._verify_dir_is_orphan(
                d, time.time(), grace=999999, ttl=999999, marker_root=marker_root
            )
            is False
        )
        assert worktree._live_marker_pids([d], marker_root=marker_root) == set()


def test_a_real_verify_marker_is_still_read(tmp_path):
    """Same "don't break the legitimate case" check on the second site, including through a
    symlinked marker file."""
    d = tmp_path / f"{worktree.VERIFY_LEAF_PREFIX}b2"
    d.mkdir()
    marker_root = tmp_path / "active"
    worktree._write_verify_marker(d, "b2", "just check", marker_root=marker_root)
    assert worktree._read_verify_marker(d, marker_root=marker_root)["pid"] == os.getpid()

    linked = tmp_path / f"{worktree.VERIFY_LEAF_PREFIX}b3"
    linked.mkdir()
    legacy = {"pid": os.getpid(), "host": host.host_id(), "branch": "b3"}
    (d / worktree.VERIFY_MARKER).write_text(json.dumps(legacy))
    (linked / worktree.VERIFY_MARKER).symlink_to(d / worktree.VERIFY_MARKER)
    assert worktree._read_verify_marker(linked) == legacy


def test_a_fifo_verify_marker_past_the_grace_window_is_still_reaped(tmp_path):
    """Unreadable-means-absent all the way through: past the grace window an unreadable marker
    must not pin the dir forever either."""
    d = tmp_path / f"{worktree.VERIFY_LEAF_PREFIX}b4"
    d.mkdir()
    marker_root = tmp_path / "active"
    marker_root.mkdir()
    os.mkfifo(worktree._verify_marker_path(marker_root, d))

    with _deadline():
        assert (
            worktree._verify_dir_is_orphan(
                d, time.time(), grace=0, ttl=999999, marker_root=marker_root
            )
            is True
        )
