"""``FileWriteLedger``: the host-level outbox write ledger, one file per GitHub API host
(``bh-eqtxv``; intake-outbox-paced-send-design.md, "The pacer and the write ledger").

Two files in ``$XDG_STATE_HOME/beadhive/github-writes/`` (falling back to
``~/.local/state/beadhive/github-writes/``), shared by every command and hive on the host that
sends through the outbox:

* ``<api host>.ledger``: one wall-clock entry (epoch seconds) per write request started, one per
  line;
* ``<api host>.ledger.lock``: the ``flock`` every step takes. A ``flock`` belongs to the open
  file's inode, so it is never taken on a file that is replaced: the lock file is created once
  and never renamed, truncated or deleted by bh (an operator deleting it while no send runs is
  harmless).

:meth:`FileWriteLedger.take` is ONE atomic step under the lock file's exclusive ``flock``: open the
data file by path (so it always sees the current inode), read it, apply
:func:`~beadhive.modules.intake.contracts.ledger_step` (prune older than the hour, count the minute
and the hour, append one entry or say why not; a peek appends nothing), then either append the one
line (one ``write`` plus ``fsync``) or, when anything was pruned or the last line was torn, rewrite
the file through a temporary file in the same directory, ``fsync``, then ``rename`` over it. A crash
leaves the old data file or the new one, never half of each, and at most a torn last line (skipped
by the next step, which then rewrites) or a leftover temporary file (removed by the next rewrite).

An entry dated in the future (the wall clock stepped back) is kept and counted. A data file that
cannot be read as a whole (an I/O error, bytes that are not entries) is reported ONCE on stderr
and replaced by one holding a full minute at ``now``: the pacer waits one minute, never treats it
as empty. A ledger that cannot be written at all is an ``IntakeStoreError`` (the operation is
refused before its write-ahead; a request mid-operation still goes).
"""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import re
import secrets
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from beadhive.modules.intake.contracts import IntakeStoreError, SlotGranted, ledger_step

__all__ = ["LEDGER_DIR", "FileWriteLedger", "ledger_directory", "ledger_name"]

#: The ledger's directory under the host's state directory.
LEDGER_DIR = Path("beadhive") / "github-writes"

_HOST = re.compile(r"[^A-Za-z0-9._-]")


def ledger_directory(environ: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/beadhive/github-writes`` (an absolute ``XDG_STATE_HOME`` only, as the
    XDG spec says), else ``~/.local/state/beadhive/github-writes``."""
    env = os.environ if environ is None else environ
    state = env.get("XDG_STATE_HOME", "")
    base = Path(state) if state and os.path.isabs(state) else Path.home() / ".local" / "state"
    return base / LEDGER_DIR


def ledger_name(api_host: str) -> str:
    """The data file's name for a GitHub API host (``api.github.com.ledger``): the host,
    lower-cased, with anything but letters, digits, ``.``, ``_`` and ``-`` replaced by ``_``."""
    host = _HOST.sub("_", (api_host or "").strip().lower()) or "api.github.com"
    return f"{host.lstrip('.') or 'api.github.com'}.ledger"


class FileWriteLedger:
    """A :class:`~beadhive.modules.intake.contracts.WriteLedger` over one data file and its
    permanent lock file (see the module docstring). ``directory`` may be a callable, so building
    the ledger resolves nothing; ``report`` receives the one stderr line for an unreadable file."""

    def __init__(
        self,
        directory: Path | Callable[[], Path],
        api_host: str,
        *,
        report: Callable[[str], None] | None = None,
    ) -> None:
        self._directory = directory
        self._name = ledger_name(api_host)
        self._report = report if report is not None else _stderr
        self._reported = False

    @property
    def directory(self) -> Path:
        return self._directory() if callable(self._directory) else self._directory

    @property
    def data_path(self) -> Path:
        return self.directory / self._name

    @property
    def lock_path(self) -> Path:
        return self.directory / f"{self._name}.lock"

    def take(
        self,
        now: float,
        *,
        per_minute: int,
        per_hour: int,
        need: int = 1,
        overdraw: bool = False,
        record: bool = True,
    ):
        try:
            with self._locked():
                return self._step(now, per_minute, per_hour, need, overdraw, record)
        except OSError as exc:
            raise IntakeStoreError(
                f"the outbox write ledger in {self.directory} cannot be used "
                f"({type(exc).__name__}: {exc.strerror or exc}); no write is sent unpaced",
                operation="write_ledger",
            ) from None

    # -- the one step, under the lock ----------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        directory = self.directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield  # closing the descriptor releases the flock (a crash: the kernel does)
        finally:
            os.close(fd)

    def _step(
        self, now: float, per_minute: int, per_hour: int, need: int, overdraw: bool, record: bool
    ):
        entries, clean = self._read(now, per_minute)
        answer, kept = ledger_step(
            entries,
            now,
            per_minute=per_minute,
            per_hour=per_hour,
            need=need,
            overdraw=overdraw,
            record=record,
        )
        granted = record and isinstance(answer, SlotGranted)
        pruned = len(kept) - (1 if granted else 0) < len(entries)
        if not clean or pruned:
            self._rewrite(kept)
        elif granted:
            self._append(now)
        return answer

    def _read(self, now: float, per_minute: int) -> tuple[list[float], bool]:
        """``(entries, clean)``: ``clean`` is false when the file must be rewritten (a torn last
        line skipped, or the whole file unreadable and replaced by a full minute)."""
        try:
            raw = self.data_path.read_bytes()
        except FileNotFoundError:
            return [], True
        except OSError:
            return self._unreadable(now, per_minute)
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return self._unreadable(now, per_minute)
        lines = text.split("\n")
        torn = lines.pop()  # "" when the file ends with a newline (complete)
        entries: list[float] = []
        for line in lines:
            value = _entry(line)
            if value is None:
                return self._unreadable(now, per_minute)
            entries.append(value)
        return entries, torn == ""

    def _unreadable(self, now: float, per_minute: int) -> tuple[list[float], bool]:
        if not self._reported:
            self._reported = True
            self._report(
                f"bh: the outbox write ledger {self.data_path} cannot be read; it is treated as "
                "holding a full minute of writes (one minute's wait) and rewritten"
            )
        return [now] * per_minute, False

    def _append(self, now: float) -> None:
        fd = os.open(self.data_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, _line(now))
            os.fsync(fd)
        finally:
            os.close(fd)

    def _rewrite(self, entries: tuple[float, ...]) -> None:
        directory = self.directory
        for leftover in directory.glob(f".{self._name}.*.tmp"):  # a crashed prune's
            with contextlib.suppress(OSError):
                leftover.unlink()
        temporary = directory / f".{self._name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                os.write(fd, b"".join(_line(t) for t in entries))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.data_path)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        with contextlib.suppress(OSError):  # make the rename itself durable
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)


def _entry(line: str) -> float | None:
    try:
        value = float(line)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _line(seconds: float) -> bytes:
    return f"{seconds:.6f}\n".encode("ascii")


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)
