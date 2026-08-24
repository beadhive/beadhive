"""CLI transport for the backend-neutral state stream.

This module owns only request selection and flushed NDJSON output.  Providers own backend reads;
``state_stream.stream_frames`` owns snapshot-first ordering and delta construction.  Keeping the
command at those two stable boundaries lets consumers run one ``bh`` process without learning
about, importing, or spawning the current backend.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from enum import StrEnum
from typing import TextIO

import typer

from . import config, registry
from .state_stream import StreamFrame, StreamRequest, StreamScope, frame_payload, stream_frames
from .state_stream_polling import get_polling_provider
from .state_stream_process import StreamProcessScope


class StreamFormat(StrEnum):
    NDJSON = "ndjson"


_SCOPE_OPTION = typer.Option(..., "--scope", help="factory | hub | hive")
_FORMAT_OPTION = typer.Option(
    StreamFormat.NDJSON, "--format", help="stream encoding (v1: ndjson only)"
)
_HIVE_OPTION = typer.Option(
    "", "--hive", help="hive slug/prefix/triplet for hive scope (default: current hive)"
)
_SINCE_OPTION = typer.Option("", "--since", help="opaque revision hint; startup is snapshot-first")


def emit_ndjson(frames: Iterable[StreamFrame], stream: TextIO | None = None) -> None:
    """Write one compact, LF-terminated frame at a time and flush each frame immediately."""

    output = stream if stream is not None else sys.stdout
    for frame in frames:
        output.write(
            json.dumps(frame_payload(frame), ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        output.flush()


def _hive_slug(cfg: dict, hive: str) -> str:
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    if entry is None:
        typer.echo(
            "✗ hive scope needs --hive HIVE when the current directory is not a managed hive",
            err=True,
        )
        raise typer.Exit(1)
    return str(entry["repo"])


def command(
    scope: StreamScope = _SCOPE_OPTION,
    format_: StreamFormat = _FORMAT_OPTION,
    hive: str = _HIVE_OPTION,
    since: str = _SINCE_OPTION,
) -> None:
    """Stream backend-neutral bead state as snapshot-first NDJSON frames."""

    # The enum makes unsupported formats a CLI validation error.  Retain the local name so the
    # transport choice is explicit at the handoff even while v1 has only one encoding.
    assert format_ is StreamFormat.NDJSON
    cfg = config.load()
    if scope is StreamScope.HIVE:
        selected_hive = _hive_slug(cfg, hive)
    else:
        if hive:
            typer.echo(f"✗ --hive only applies to --scope {StreamScope.HIVE.value}", err=True)
            raise typer.Exit(1)
        selected_hive = None

    request = StreamRequest(scope=scope, hive=selected_hive, since_revision=since or None)
    # The scope spans BOTH backend iteration and output.  A timeout/cancellation while polling,
    # or BrokenPipeError while emitting, therefore has one finalizer that reaps every descendant
    # backend process before the command returns or preserves the caller's signal exit.
    with StreamProcessScope() as processes:
        provider = get_polling_provider(cfg, process_scope=processes)
        emit_ndjson(stream_frames(provider, request))
