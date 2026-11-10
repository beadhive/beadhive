"""The wiring-free ``TrackerAdapter`` implementation for GitHub: the read path and its writers.

Rules (tested package-locally): this is the only place ``githubkit`` may be imported, and only
inside functions, never at module top level; it reads no configuration and imports no plugin
kernel; from the plugin layer it imports only ``beadhive_plugins.credentials``.

=====================  =========================================================================
Module                 Holds
=====================  =========================================================================
``tracker``            :class:`GitHubTrackerAdapter` (reader, identity, ``IssueResolver``)
``compose``            the writers of the enabled write operations composed onto it
``comments``           the ``comment.create`` writer (``CommentWriter.post_comment``)
``labels``             the ``labels.change`` writer (``LabelWriter.change_labels``): owned
                       labels only
``label_definitions``  ``labels.read`` (``LabelReader``) and the ``labels.define`` writer
                       (``LabelDefiner.define_labels``): repository label definitions, owned only
``closing``            the ``issue.close`` writer (``IssueCloser.close_issue``): the state and
                       its reason only, never the labels
``reopening``          the ``issue.reopen`` writer (``IssueReopener.reopen_issue``): the state
                       only, never the labels
``issue_values``       the ``issue.type`` (``IssueTypeWriter.set_issue_type``) and
                       ``issue.fields`` (``IssueFieldWriter.set_issue_fields``) writers: the
                       type, or the named fields' values, only, never the labels
``grants``             the in-process single-use ``WriteGrant`` ledger every writer spends from
``transport``          the githubkit seam: one credential per operation, adapter retry, writes
``retry``              the pure retry classification and budget decision (injected clock)
``diagnostics``        the exact scrubbed one-line refusal wording and its port errors
``identity``           the read-only tri-state capability probe (pure)
``mapping``            GitHub JSON to port records (pure)
``marker``             the own-message marker codec (``bh:v1``)
``urls``               the repository location and the ``IssueResolver`` URL grammar
``settings``           the folded ``plugins.tracker-github`` fragment to constructor arguments
=====================  =========================================================================
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from beadhive.modules.tracker.contracts import ProjectRef
from beadhive_plugins.credentials import TokenSource

from .closing import IssueClosing
from .comments import CommentCreate
from .compose import adapter_class, new_adapter
from .diagnostics import diagnose, refusal_error
from .grants import GrantLedger
from .issue_values import IssueFieldWriting, IssueTyping
from .label_definitions import LabelDefining, LabelDefinitionReading
from .labels import LabelWriting
from .marker import decode_marker, encode_marker
from .reopening import IssueReopening
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
    "IssueClosing",
    "IssueFieldWriting",
    "IssueReopening",
    "IssueTyping",
    "LabelDefining",
    "LabelDefinitionReading",
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
    command_scoped: bool = False,
) -> GitHubTrackerAdapter:
    """The adapter for ``project`` from the folded plugin fragment.

    ``transport`` is the ``httpx`` transport to send through (``None``: the network); tests pass
    an ``httpx.MockTransport``. Building sends no request and imports no githubkit; a writer
    method exists on the result only for an enabled write operation (:mod:`.compose`).

    ``command_scoped`` says the adapter lives for ONE command: it then keeps one HTTP client
    (and connection pool) and the organization's issue-field definitions for that life
    (bh-vy7dx, bh-n0h15). A resident process (the host daemon) leaves it off."""
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
        command_scoped=command_scoped,
    )
