"""``FileWriteLedger``: the host-level outbox write ledger and its lock file (``bh-eqtxv``).

intake-outbox-paced-send-design.md test 14 (the file: prune, append under the lock, a torn last
line, an unreadable file, a missing file, a leftover temporary file, the lock file's inode across
prunes) and the two-process half of test 12 (two processes released together by a barrier each
take N slots; one of them prunes; no append is lost and the lock file is never held by both).
Every ``now`` is passed in: nothing reads a clock, and the process test asserts counts and the
order of lock holders, never a duration. Every file is under ``tmp_path``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from beadhive.adapters.intake.write_ledger import FileWriteLedger, ledger_directory, ledger_name
from beadhive.modules.intake.contracts import HourFull, MinuteFull, SlotGranted
from harness.processes import process_context

NOW = 1_800_000_000.0
FAST = {"per_minute": 1000, "per_hour": 100_000}  # the counting rule, not the limits, under test


def _ledger(tmp_path: Path, **kwargs) -> FileWriteLedger:
    return FileWriteLedger(tmp_path / "github-writes", "api.github.com", **kwargs)


def _lines(ledger: FileWriteLedger) -> list[float]:
    return [float(line) for line in ledger.data_path.read_text().splitlines()]


def test_the_files_live_per_api_host_under_the_state_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = tmp_path / "state"
    assert ledger_directory({"XDG_STATE_HOME": str(state)}) == state / "beadhive/github-writes"
    fallback = tmp_path / "home/.local/state/beadhive/github-writes"
    assert ledger_directory({"XDG_STATE_HOME": "relative/state"}) == fallback
    assert ledger_directory({}) == fallback
    assert ledger_name("api.github.com") == "api.github.com.ledger"
    assert ledger_name("GHE.Example.com:8443") == "ghe.example.com_8443.ledger"
    ledger = _ledger(tmp_path)
    assert ledger.lock_path.name == "api.github.com.ledger.lock"


def test_a_missing_file_starts_empty_and_each_slot_appends_one_line(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    assert not ledger.data_path.exists()

    for i in range(3):
        assert ledger.take(NOW + i, **FAST) == SlotGranted()

    assert _lines(ledger) == [NOW, NOW + 1, NOW + 2]
    assert ledger.lock_path.exists()
    assert oct(ledger.directory.stat().st_mode & 0o777) == "0o700"


def test_the_minute_and_the_hour_are_counted_from_the_file(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    for i in range(3):
        ledger.take(NOW + i, per_minute=3, per_hour=4)

    assert ledger.take(NOW + 3, per_minute=3, per_hour=4) == MinuteFull(NOW + 60)
    assert ledger.take(NOW + 3, per_minute=9, per_hour=4, need=2) == HourFull(NOW + 3600)
    assert len(_lines(ledger)) == 3  # a refusal appends nothing


def test_entries_older_than_the_hour_are_pruned_by_a_rename_and_the_lock_stays(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.directory.mkdir(parents=True)
    ledger.data_path.write_text("".join(f"{NOW - 4000 + i}\n" for i in range(5)))
    ledger.take(NOW - 10, **FAST)  # creates the lock file
    lock_inode = ledger.lock_path.stat().st_ino
    data_inode = ledger.data_path.stat().st_ino

    ledger.take(NOW, **FAST)

    assert _lines(ledger) == [NOW - 10, NOW]
    assert ledger.lock_path.stat().st_ino == lock_inode  # never replaced
    ledger.data_path.write_text("".join(f"{NOW - 7200}\n" for _ in range(2)) + f"{NOW}\n")
    ledger.take(NOW + 1, **FAST)
    assert ledger.data_path.stat().st_ino != data_inode  # the prune renamed a new file in
    assert ledger.lock_path.stat().st_ino == lock_inode
    assert _lines(ledger) == [NOW, NOW + 1]


def test_a_torn_last_line_is_skipped_and_the_rest_counted(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.directory.mkdir(parents=True)
    ledger.data_path.write_text(f"{NOW - 5}\n{NOW - 4}\n{NOW - 3}.12")  # a crash mid-append

    assert ledger.take(NOW, per_minute=3, per_hour=10) == SlotGranted()  # two counted, not three

    assert _lines(ledger) == [NOW - 5, NOW - 4, NOW]  # rewritten without the torn line


def test_an_unreadable_file_counts_as_a_full_minute_and_is_reported_once(tmp_path) -> None:
    said: list[str] = []
    ledger = _ledger(tmp_path, report=said.append)
    ledger.directory.mkdir(parents=True)
    ledger.data_path.write_bytes(b"\x00\xffnot a ledger\nat all\n")

    assert ledger.take(NOW, per_minute=80, per_hour=500) == MinuteFull(NOW + 60)
    assert ledger.take(NOW + 30, per_minute=80, per_hour=500) == MinuteFull(NOW + 60)
    assert ledger.take(NOW + 60, per_minute=80, per_hour=500) == SlotGranted()

    assert len(said) == 1 and "cannot be read" in said[0]
    assert len(_lines(ledger)) == 81  # the full minute it stood for, then the one slot


def test_a_leftover_temporary_file_is_ignored_and_removed_by_the_next_prune(tmp_path) -> None:
    ledger = _ledger(tmp_path)
    ledger.directory.mkdir(parents=True)
    leftover = ledger.directory / ".api.github.com.ledger.4242.deadbeef.tmp"
    leftover.write_text(f"{NOW}\n" * 999)  # a crashed prune's half-written file
    ledger.data_path.write_text(f"{NOW - 7200}\n{NOW - 1}\n")

    assert ledger.take(NOW, per_minute=2, per_hour=10) == SlotGranted()  # the leftover unread

    assert not leftover.exists() and _lines(ledger) == [NOW - 1, NOW]


def test_a_ledger_that_cannot_be_written_is_a_store_error(tmp_path) -> None:
    from beadhive.modules.intake.contracts import IntakeStoreError

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("")
    ledger = FileWriteLedger(blocker / "github-writes", "api.github.com")
    with pytest.raises(IntakeStoreError, match="write ledger"):
        ledger.take(NOW, **FAST)


# -- two processes, one prunes ---------------------------------------------------------------


def _take_slots(directory: str, probe: str, who: str, count: int, barrier) -> None:
    """Child process: take ``count`` slots on the shared ledger, recording around every step
    which process held the lock file (inside the lock, by an append to ``probe``)."""
    ledger = FileWriteLedger(Path(directory), "api.github.com")
    real = ledger._step

    def probed(*args):
        mark(f"enter {who}")
        try:
            return real(*args)
        finally:
            mark(f"exit {who}")

    def mark(line: str) -> None:
        fd = os.open(probe, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, f"{line}\n".encode())
        finally:
            os.close(fd)

    ledger._step = probed
    barrier.wait(30)
    offset = 0.0 if who == "a" else 0.5
    for i in range(count):
        answer = ledger.take(NOW + i + offset, **FAST)
        assert answer == SlotGranted(), answer


def test_two_processes_one_ledger_lose_no_append_to_a_prune(tmp_path) -> None:
    directory = tmp_path / "github-writes"
    directory.mkdir()
    stale = FileWriteLedger(directory, "api.github.com")
    stale.data_path.write_text("".join(f"{NOW - 7200 + i}\n" for i in range(50)))
    probe = tmp_path / "probe"
    count = 40
    ctx = process_context()
    barrier = ctx.Barrier(2)
    children = [
        ctx.Process(target=_take_slots, args=(str(directory), str(probe), who, count, barrier))
        for who in ("a", "b")
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(60)
        assert child.exitcode == 0

    entries = _lines(stale)
    assert len(entries) == 2 * count and all(t > NOW - 3600 for t in entries)
    events = probe.read_text().splitlines()
    assert len(events) == 2 * 2 * count
    for enter, leave in zip(events[::2], events[1::2], strict=True):  # never two holders at once
        assert enter.startswith("enter ") and leave == enter.replace("enter", "exit")
