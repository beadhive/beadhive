"""Durable invariants for the stream-v1 ordering contract (bh-jksq.1)."""

from __future__ import annotations

from pathlib import Path

_CONTRACT = (
    Path(__file__).resolve().parents[1] / "docs" / "design" / "beadhive-stream-v1-contract.md"
)


def _flat_contract() -> str:
    return " ".join(_CONTRACT.read_text(encoding="utf-8").split())


def test_every_stream_session_is_snapshot_first_even_with_since() -> None:
    text = _flat_contract()
    assert "Always the first frame of a stream session" in text
    assert "including a session opened with `--since`" in text
    assert "`--since <revision>` never changes snapshot-first startup ordering" in text
    assert "There is no leading `resync`" in text


def test_contract_does_not_restore_delta_first_reconnects() -> None:
    text = _flat_contract()
    assert "without repeating a `snapshot`" not in text
    assert "A `resync` is **mid-session recovery only**" in text
