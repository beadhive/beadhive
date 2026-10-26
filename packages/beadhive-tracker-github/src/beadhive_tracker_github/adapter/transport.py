"""The githubkit transport behind the adapter: one credential per operation, GET only, retried
by the adapter's own decision on an injected sleep and clock.

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
    """One port operation: one resolved credential, one githubkit client, any number of GETs."""

    def __init__(
        self,
        transport: Transport,
        token: ResolvedToken | None,
        client: Any,
        operation: Operation | None,
    ) -> None:
        self._transport = transport
        self._token = token
        self._client = client
        self._operation = operation

    @property
    def source(self) -> str | None:
        return self._token.source if self._token is not None else None

    @property
    def anonymous(self) -> bool:
        return self._token is None

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self._token.value,) if self._token is not None else ()

    def _attempt(self, target: str, params: Mapping[str, str] | None) -> Reply | Refusal:
        from githubkit.exception import RequestError, RequestFailed, RequestTimeout

        transport = self._transport
        try:
            response = self._client.request(
                "GET",
                target,
                params=dict(params) if params else None,
                headers={"X-GitHub-Api-Version": API_VERSION},
            )
        except RequestFailed as exc:
            raw = exc.response
            return classify(
                raw.status_code,
                raw.headers,
                _message(raw),
                write=False,
                now=transport.clock(),
                source=self.source,
                operation=self._operation,
                secrets=self.secrets,
            )
        except RequestTimeout:
            return classify_transport_failure(write=False, timeout=True)
        except RequestError:
            return classify_transport_failure(write=False, timeout=False)
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            raise TrackerError("GitHub answered with a body that is not JSON") from None
        return Reply(response.status_code, response.headers, body, str(response.url))

    def get(self, target: str, params: Mapping[str, str] | None = None) -> Reply:
        """GET ``target`` (an API path, or a checked absolute ``next`` URL), retried within the
        budget; raise the port error of the last refusal otherwise."""
        transport = self._transport
        waited, attempt = 0.0, 0
        while True:
            outcome = self._attempt(target, params)
            if isinstance(outcome, Reply):
                return outcome
            wait = next_wait(outcome, attempt=attempt, waited=waited, budget=transport.budget)
            if wait is None:
                raise outcome.error from None
            if wait > 0:
                transport.sleep(wait)
            waited, attempt = waited + wait, attempt + 1


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

    def _client(self, token: ResolvedToken | None) -> Any:
        from githubkit import GitHub

        options: dict[str, Any] = {}
        if self.http_cache:
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
            http_cache=self.http_cache,
            auto_retry=False,
            **options,
        )

    @contextmanager
    def operation(self, operation: Operation | None) -> Iterator[Session]:
        """Resolve the credential once, open one client, and yield the operation's GET seam."""
        token = self._resolve()
        client = self._client(token)
        with client:
            yield Session(self, token, client, operation)
