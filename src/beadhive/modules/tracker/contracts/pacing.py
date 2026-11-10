"""The two optional seams a host can hand an adapter to pace its writes (``bh-eqtxv``).

intake-outbox-paced-send-design.md, "Two seams": the outbox paces every write REQUEST, not every
operation, so the adapter that sends them calls the host back at the one place each request
starts.

* :data:`WriteGate` is called **before every attempt of a write** (``POST``, ``PATCH``,
  ``DELETE``; a rate-limit resend included) with the request's method. It may block (the host's
  pacer waits for its spacing there) and it never refuses: an operation already past its
  write-ahead must finish its requests.
* :data:`WaitObserver` is called **whenever the adapter's retry decision returns a wait** (``0``
  included), before it sleeps, with the error that decided it (``RateLimited`` for a rate
  limit, ``TransientFailure`` for a read's server error or timeout) and the seconds it will
  wait. The host classifies by the error: a rate limit stops its batch, a transient failure only
  delays its next request.

An adapter built without either behaves exactly as before. Standard library only.
"""

from __future__ import annotations

from collections.abc import Callable

from .errors import TrackerError

__all__ = ["WaitObserver", "WriteGate"]

#: ``gate(method)``: called before every attempt of a write; may block, never refuses.
WriteGate = Callable[[str], None]

#: ``observe(error, seconds)``: called whenever the retry decision returns a wait.
WaitObserver = Callable[[TrackerError, float], None]
