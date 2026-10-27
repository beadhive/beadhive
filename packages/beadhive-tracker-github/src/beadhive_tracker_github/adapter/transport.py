"""The githubkit transport behind the adapter: one credential per operation, reads by GET and the
gated writes through one write seam, retried by the adapter's own decision on an injected sleep
and clock.

githubkit (MIT, about 250 ms to import) is imported **only inside functions here**, so importing
this package never loads it (rule P1, tested). It is used as the HTTP layer: client construction,
authentication, the ``base_url`` (GHES) join, error detection and the optional ``http_cache``.
Three of its defaults are overridden on purpose (port ADR section 1):

* ``auto_retry=False``: githubkit never retries and so never reaches its ``time.sleep`` (the
  clamp); :mod:`.retry` decides and :attr:`Transport.sleep` (the injected seam) waits.
* ``http_cache`` is passed explicitly and is **off** unless the operator opts in (githubkit's own
  default is on). When on, the cache store is this adapter's own, never githubkit's process-wide
  default.
* ``timeout`` is finite (githubkit's default is none at all): a hung connection is a transient
  failure, not a hang.

Writes are the exception, not the rule: only an operation opened with ``write=True`` (by a
writer, after its grant was checked) has :meth:`Session.send`, the ONE write seam every writer
(``comment.create``, ``labels.change``) sends through. Its client neither caches nor follows a
redirect (httpx re-sends a redirected ``POST`` as a ``GET``), and it is retried only on a
rate-limit refusal; a server error, a timeout, a dropped connection after the send, a redirect or
an unreadable success body is ``WriteOutcomeUnknown``, never retried.

The mapping reads the decoded JSON rather than githubkit's generated models: the models are not
verified against every live payload (port ADR, "Unverified"), so a model mismatch would turn one
odd field into a failed read. Requests also pin ``X-GitHub-Api-Version`` to the version the spikes
recorded, and every query value is a pre-formatted string (githubkit renders a ``datetime``
``since`` with ``str()``, which is not GitHub's ISO 8601 form).

The credential: :class:`Transport` holds the injected zero-argument ``TokenSource``, calls it
**once per operation** (:meth:`Transport.operation`) and keeps the value only for that
operation; nothing is cached, so a rotated token is picked up. Only provenance is ever shown, and
every upstream text is scrubbed of the resolved value and of any token shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    Operation,
    RetryBudget,
    TrackerError,
    WriteOutcomeUnknown,
)
from beadhive_plugins.credentials import ResolvedToken, TokenSource

from .retry import Refusal, classify, classify_transport_failure, next_wait

if TYPE_CHECKING:
    import httpx

__all__ = ["API_VERSION", "DEFAULT_TIMEOUT_SECONDS", "USER_AGENT", "Reply", "Session", "Transport"]

#: The REST API version every request pins (the version the spikes' evidence was recorded on).
API_VERSION = "2022-11-28"

#: Seconds before a connect or read is abandoned (githubkit's default is unbounded).
DEFAULT_TIMEOUT_SECONDS = 30.0

USER_AGENT = "beadhive-tracker-github"


@dataclass(frozen=True)
class Reply:
    """One successful response: its status, headers and decoded JSON body."""

    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: Any = field(repr=False)
    url: str = ""


def _message(response: Any) -> str:
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON error body has no message
        return ""
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return body["message"]
    return ""


class Session:
    """One port operation: one resolved credential, one githubkit client, any number of requests."""

    def __init__(
        self,
        transport: Transport,
        token: ResolvedToken | None,
        client: Any,
        operation: Operation | None,
        *,
        write: bool = False,
    ) -> None:
        self._transport = transport
        self._token = token
        self._client = client
        self._operation = operation
        self._write = write

    @property
    def source(self) -> str | None:
        return self._token.source if self._token is not None else None

    @property
    def anonymous(self) -> bool:
        return self._token is None

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self._token.value,) if self._token is not None else ()

    def _attempt(
        self,
        target: str,
        params: Mapping[str, str] | None,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
    ) -> Reply | Refusal:
        from githubkit.exception import RequestError, RequestFailed, RequestTimeout

        transport = self._transport
        write = method != "GET"
        try:
            response = self._client.request(
                method,
                target,
                params=dict(params) if params else None,
                json=dict(payload) if payload is not None else None,
                headers={"X-GitHub-Api-Version": API_VERSION},
            )
        except RequestFailed as exc:
            raw = exc.response
            return classify(
                raw.status_code,
                raw.headers,
                _message(raw),
                write=write,
                now=transport.clock(),
                source=self.source,
                operation=self._operation,
                secrets=self.secrets,
            )
        except RequestTimeout:
            return classify_transport_failure(write=write, timeout=True)
        except RequestError:
            return classify_transport_failure(write=write, timeout=False)
        except Exception:  # noqa: BLE001 - after a send, anything else leaves a write ambiguous
            if write:
                return classify_transport_failure(write=True, timeout=False)
            raise
        if write and not 200 <= response.status_code < 300:
            # Never followed (a write client does not follow redirects): GitHub did not confirm.
            return Refusal(
                WriteOutcomeUnknown(
                    f"GitHub answered {response.status_code} to a write without confirming it"
                ),
                retryable=False,
            )
        try:
            decoded = response.json()
        except (ValueError, json.JSONDecodeError):
            if write:  # accepted, but the receipt is unreadable: the consumer reconciles
                raise WriteOutcomeUnknown(
                    "GitHub accepted the write but its answer is not JSON; it may have applied"
                ) from None
            raise TrackerError("GitHub answered with a body that is not JSON") from None
        return Reply(response.status_code, response.headers, decoded, str(response.url))

    def _run(
        self,
        target: str,
        params: Mapping[str, str] | None,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
    ) -> Reply:
        """Send until a reply, or until the retry decision says stop (then raise its error)."""
        transport = self._transport
        waited, attempt = 0.0, 0
        while True:
            outcome = self._attempt(target, params, method=method, payload=payload)
            if isinstance(outcome, Reply):
                return outcome
            if isinstance(outcome.error, WriteOutcomeUnknown):
                raise outcome.error from None  # never retried, whatever the budget says
            wait = next_wait(outcome, attempt=attempt, waited=waited, budget=transport.budget)
            if wait is None:
                raise outcome.error from None
            if wait > 0:
                transport.sleep(wait)
            waited, attempt = waited + wait, attempt + 1

    def get(self, target: str, params: Mapping[str, str] | None = None) -> Reply:
        """GET ``target`` (an API path, or a checked absolute ``next`` URL), retried within the
        budget; raise the port error of the last refusal otherwise."""
        return self._run(target, params)

    def send(self, method: str, target: str, payload: Mapping[str, Any] | None = None) -> Reply:
        """One WRITE (``POST``, ``DELETE``) of ``payload`` to ``target``: the one write seam.

        Retried only when GitHub refused it with a rate limit (proof that nothing was written),
        within the budget, on the injected sleep. A server error, a timeout, a dropped
        connection, an unconfirmed (non-2xx, e.g. a redirect, never followed) or unreadable
        answer raises ``WriteOutcomeUnknown`` and is NEVER retried: the write may have applied
        exactly once, and the consumer reconciles (by the own-message marker, or by reading the
        issue's labels). Every other refusal raises its port error with the exact diagnostic."""
        if method == "GET":
            raise ValueError("send() is for writes; use get() to read")
        if not self._write:
            raise ValueError("a write needs an operation opened with write=True")
        return self._run(target, None, method=method, payload=payload)


@dataclass
class Transport:
    """The adapter's HTTP seam. ``transport`` is an ``httpx`` transport to send through (tests pass
    an ``httpx.MockTransport``; ``None`` is the real network)."""

    token_source: TokenSource = field(repr=False)
    api_base: str
    budget: RetryBudget
    sleep: Callable[[float], None] = field(repr=False)
    clock: Callable[[], datetime] = field(repr=False)
    http_cache: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    _cache_strategy: Any = field(default=None, init=False, repr=False)

    def _resolve(self) -> ResolvedToken | None:
        try:
            token = self.token_source()
        except Exception:  # noqa: BLE001 - its text may hold a credential; never repeated
            raise AuthenticationFailed("the credential source failed") from None
        if token is not None and not isinstance(token, ResolvedToken):
            raise AuthenticationFailed("the credential source returned no ResolvedToken")
        return token

    def _client(self, token: ResolvedToken | None, *, write: bool = False) -> Any:
        from githubkit import GitHub

        options: dict[str, Any] = {}
        if self.http_cache and not write:
            if self._cache_strategy is None:
                from githubkit.cache import MemCacheStrategy

                self._cache_strategy = MemCacheStrategy()
            options["cache_strategy"] = self._cache_strategy
        return GitHub(
            token.value if token is not None else None,
            base_url=self.api_base,
            user_agent=USER_AGENT,
            timeout=self.timeout_seconds,
            transport=self.transport,
            # A write never caches, and never follows a redirect (httpx would re-send a
            # redirected POST as a GET, silently turning the write into a read).
            http_cache=self.http_cache and not write,
            follow_redirects=not write,
            auto_retry=False,
            **options,
        )

    @contextmanager
    def operation(self, operation: Operation | None, *, write: bool = False) -> Iterator[Session]:
        """Resolve the credential once, open one client, and yield the operation's seam: GETs,
        plus :meth:`Session.send` when ``write`` (a client that neither caches nor redirects)."""
        token = self._resolve()
        client = self._client(token, write=write)
        with client:
            yield Session(self, token, client, operation, write=write)
