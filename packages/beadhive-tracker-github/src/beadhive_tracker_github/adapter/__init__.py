"""The wiring-free ``TrackerAdapter`` implementation for GitHub: the read path and its writers.

Rules (tested package-locally): this is the only place ``githubkit`` may be imported, and only
inside functions, never at module top level; it reads no configuration and imports no plugin
kernel; from the plugin layer it imports only ``beadhive_plugins.credentials``.

=================  ==========================================================================
Module             Holds
=================  ==========================================================================
``tracker``        :class:`GitHubTrackerAdapter` (reader, identity, ``IssueResolver``)
``compose``        the writers of the enabled write operations composed onto it
``comments``       the ``comment.create`` writer (``CommentWriter.post_comment``)
``labels``         the ``labels.change`` writer (``LabelWriter.change_labels``): owned labels only
``grants``         the in-process single-use ``WriteGrant`` ledger every writer spends from
``transport``      the githubkit seam: one credential per operation, adapter retry, writes
``retry``          the pure retry classification and budget decision (injected clock)
``diagnostics``    the exact scrubbed one-line refusal wording and its port errors
``identity``       the read-only tri-state capability probe (pure)
``mapping``        GitHub JSON to port records (pure)
``marker``         the own-message marker codec (``bh:v1``)
``urls``           the repository location and the ``IssueResolver`` URL grammar
``settings``       the folded ``plugins.tracker-github`` fragment to constructor arguments
=================  ==========================================================================
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from beadhive.modules.tracker.contracts import ProjectRef
from beadhive_plugins.credentials import TokenSource

from .comments import CommentCreate
from .compose import adapter_class, new_adapter
from .diagnostics import diagnose, refusal_error
from .grants import GrantLedger
from .labels import LabelWriting
from .marker import decode_marker, encode_marker
from .settings import AdapterSettings, parse_settings
from .tracker import CHECKPOINT_PREFIX, SUPPORTED_OPERATIONS, GitHubTrackerAdapter

if TYPE_CHECKING:
    import httpx

__all__ = [
    "CHECKPOINT_PREFIX",
    "SUPPORTED_OPERATIONS",
    "AdapterSettings",
    "CommentCreate",
    "GitHubTrackerAdapter",
    "GrantLedger",
    "LabelWriting",
    "adapter_class",
    "build_adapter",
    "decode_marker",
    "diagnose",
    "encode_marker",
    "new_adapter",
    "parse_settings",
    "refusal_error",
]


def build_adapter(
    settings: Mapping[str, Any],
    token_source: TokenSource,
    *,
    project: ProjectRef,
    sleep: Callable[[float], None],
    clock: Callable[[], datetime] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> GitHubTrackerAdapter:
    """The adapter for ``project`` from the folded plugin fragment.

    ``transport`` is the ``httpx`` transport to send through (``None``: the network); tests pass
    an ``httpx.MockTransport``. Building sends no request and imports no githubkit; a writer
    method exists on the result only for an enabled write operation (:mod:`.compose`)."""
    parsed = parse_settings(settings)
    return new_adapter(
        project=project,
        token_source=token_source,
        sleep=sleep,
        enabled=parsed.enabled,
        budget=parsed.budget,
        page_size=parsed.page_size,
        include_closed=parsed.include_closed,
        base_url=parsed.base_url,
        http_cache=parsed.http_cache,
        label_prefixes=parsed.label_prefixes,
        timeout_seconds=parsed.timeout_seconds,
        clock=clock,
        transport=transport,
    )
