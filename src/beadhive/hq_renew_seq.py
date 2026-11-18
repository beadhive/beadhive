"""Client-owned renew sequence for the HQ service route (bh-1bsvh).

hq-service refuses a renew beat whose ``seq`` is not above its floor (``seq_not_advanced``), and
an accepted service renew advances only the service floor, never the SQL frame-authority row the
beat generator reads. No service route reads the floor, so the client keeps its own monotonic
``seq`` here, per ``(frame, holder, epoch)``.

Protocol (documented once, used by ``HqServiceAdapter.renew_session``):

* A fresh beat takes ``max(generator seq, last + 1)``. The generator seq is the SQL row + 1 when
  a row exists; ``last`` is the highest seq this client ever sent.
* The signed bytes are saved as ``pending`` BEFORE they are sent. While a pending beat is
  unanswered and still fresh (younger than half its lease), the next renew resends exactly those
  bytes, so a retry after a transport failure is a ``replayed`` beat, never the same seq with a
  different digest. An accepted, replayed or definitely refused beat clears ``pending``.
* Lost, unreadable or invalid state, and the state after ``seq_not_advanced``, set ``resync``:
  the next fresh seq is rebased above the wall clock in whole seconds,
  ``max(generator, last + 1, int(now) + 1)``. A counter that rises at most once per second never
  reaches the Unix time, so this clears any floor this client could have reached, in one step,
  deterministically. If the floor is still higher (another writer) the service refuses again and
  the refusal reaches the caller; nothing loops or retries here.

The file holds counters and the signed beat (public bytes); never key material. Atomic write,
mode 0600, one file per frame/holder/epoch.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

STATE_VERSION = 1
#: A pending beat is resent only while younger than this fraction of its lease (the service
#: refuses ``beat_not_fresh`` at the full lease).
RESEND_FRACTION = 0.5


@dataclass(frozen=True)
class Pending:
    seq: int
    sent_at: float
    duration: int
    body: str


@dataclass(frozen=True)
class SeqState:
    last_seq: int = 0
    resync: bool = True
    pending: Pending | None = None


class RenewSeqStore:
    """The local seq state of one host, under *directory* (the bh home's heartbeat dir)."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    def path(self, frame: str, holder: str, epoch: int) -> Path:
        key = hashlib.sha256(f"{frame}\0{holder}\0{epoch}".encode()).hexdigest()[:24]
        return self._directory / f"hq-renew-seq-{key}.json"

    def load(self, frame: str, holder: str, epoch: int) -> SeqState:
        """The saved state; anything missing or invalid is lost state (rebase on next beat)."""
        try:
            raw = json.loads(self.path(frame, holder, epoch).read_text(encoding="utf-8"))
            last = raw["last_seq"]
            pend = raw.get("pending")
            if raw["v"] != STATE_VERSION or type(last) is not int or last < 0:
                return SeqState()
            pending = None
            if pend is not None:
                pending = Pending(
                    pend["seq"], float(pend["sent_at"]), pend["duration"], pend["body"]
                )
                if (
                    type(pending.seq) is not int
                    or type(pending.duration) is not int
                    or not isinstance(pending.body, str)
                ):
                    return SeqState()
            return SeqState(last, bool(raw.get("resync", False)), pending)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return SeqState()

    def save(self, frame: str, holder: str, epoch: int, state: SeqState) -> None:
        pending = state.pending
        document = {
            "v": STATE_VERSION,
            "last_seq": state.last_seq,
            "resync": state.resync,
            "pending": None
            if pending is None
            else {
                "seq": pending.seq,
                "sent_at": pending.sent_at,
                "duration": pending.duration,
                "body": pending.body,
            },
        }
        path = self.path(frame, holder, epoch)
        self._directory.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(document, sort_keys=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


def resendable(pending: Pending | None, now: float) -> bool:
    """Whether *pending* may still be resent byte for byte (fresh, not future-dated)."""
    return pending is not None and 0 <= now - pending.sent_at < pending.duration * RESEND_FRACTION


def next_seq(state: SeqState, generator_seq: int, now: float) -> int:
    seq = max(generator_seq, state.last_seq + 1)
    return max(seq, int(now) + 1) if state.resync else seq
