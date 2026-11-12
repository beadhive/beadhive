"""Shared bead-token recognition."""

from __future__ import annotations

import pytest

from beadlog_sources.tokens import (
    GENERIC_BEAD_TOKEN,
    bead_token_pattern,
    find_bead_tokens,
    token_mode,
)


def test_namespaces_find_a_digit_free_id_and_generic_mode_does_not() -> None:
    text = "see bh-art and bh-4r0uh"
    assert find_bead_tokens(text, bead_token_pattern(["bh"])) == ["bh-art", "bh-4r0uh"]
    assert find_bead_tokens(text, bead_token_pattern(None)) == ["bh-4r0uh"]  # documented limit


def test_order_preserving_deduplicated_lowercased() -> None:
    pattern = bead_token_pattern(["bh"])
    assert find_bead_tokens("BH-AB1 bh-zz2 bh-ab1", pattern) == ["bh-ab1", "bh-zz2"]


@pytest.mark.parametrize(
    ("text", "found"),
    [("see bh-4r0uh.", ["bh-4r0uh"]), ("well-known words", []), ("on 2026-01-02 we", [])],
)
def test_generic_shape(text: str, found: list[str]) -> None:
    assert find_bead_tokens(text, GENERIC_BEAD_TOKEN) == found


def test_token_mode() -> None:
    assert token_mode(["bh"]) == "namespaces"
    assert token_mode([]) == "generic" and token_mode(None) == "generic"
    assert bead_token_pattern([]) is GENERIC_BEAD_TOKEN
