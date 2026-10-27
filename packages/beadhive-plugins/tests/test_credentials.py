"""``beadhive_plugins.credentials``: the shared token value type, port and scrubber (bh-99xst).

Every token below is synthetic and shaped like a real one only so the pattern pass is exercised.
No network, no environment, no subprocess, no clock.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import logging
from pathlib import Path

import pytest

from beadhive_plugins import credentials
from beadhive_plugins.credentials import (
    REDACTED,
    TOKEN_PATTERN,
    ResolvedToken,
    TokenSource,
    scrub,
)

CLASSIC = "ghp_" + "A1b2C3d4" * 5
FINE_GRAINED = "github_pat_" + "Z9y8X7w6" * 4
SOURCE_PATH = Path(credentials.__file__)


def test_module_defines_all_and_every_name_resolves() -> None:
    assert set(credentials.__all__) == {
        "REDACTED",
        "TOKEN_PATTERN",
        "ResolvedToken",
        "TokenSource",
        "scrub",
    }
    assert all(hasattr(credentials, name) for name in credentials.__all__)


def test_module_is_stdlib_only() -> None:
    allowed = {"__future__", "re", "collections", "dataclasses", "typing"}
    tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module
            roots.add(node.module.split(".")[0])
    assert roots <= allowed, sorted(roots - allowed)


def test_resolved_token_never_shows_its_value_but_shows_provenance(caplog, tmp_path) -> None:
    token = ResolvedToken(CLASSIC, "env:GH_TOKEN")

    shown = [repr(token), str(token), f"{token}", f"{token!r}"]
    assert all(CLASSIC not in text for text in shown)
    assert str(token) == "<token from env:GH_TOKEN>"
    assert "env:GH_TOKEN" in repr(token)

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("adapter").warning("using %s (%r)", token, token)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"source": token.source}))
    assert CLASSIC not in caplog.text + ledger.read_text()
    assert token.value == CLASSIC  # the holder can still use it


def test_resolved_token_is_immutable_and_rejects_blank_parts_without_echo() -> None:
    token = ResolvedToken(CLASSIC, "injected")
    with pytest.raises(dataclasses.FrozenInstanceError):
        token.value = "other"  # type: ignore[misc]
    for value, source in (("", "injected"), ("   ", "injected"), (CLASSIC, " ")):
        with pytest.raises(ValueError) as info:
            ResolvedToken(value, source)
        assert CLASSIC not in str(info.value)


def test_a_zero_argument_callable_is_a_token_source() -> None:
    fixed = ResolvedToken(CLASSIC, "injected")

    def source() -> ResolvedToken | None:
        return fixed

    assert isinstance(source, TokenSource)
    assert source() is fixed
    assert isinstance(lambda: None, TokenSource)


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(CLASSIC, id="classic-ghp"),
        pytest.param("gho_" + "x" * 36, id="oauth-gho"),
        pytest.param("ghu_" + "x" * 36, id="user-to-server-ghu"),
        pytest.param("ghs_" + "x" * 36, id="installation-ghs"),
        pytest.param("ghr_" + "x" * 36, id="refresh-ghr"),
        pytest.param(FINE_GRAINED, id="fine-grained-github_pat"),
    ],
)
def test_scrub_redacts_every_documented_token_shape_without_being_told_the_value(token) -> None:
    text = f"Bad token {token}; header=Bearer {token}, glued:x{token}y"

    out = scrub(text)

    assert token not in out
    assert not TOKEN_PATTERN.search(out)
    assert out.count(REDACTED) >= 2


def test_scrub_leaves_non_token_text_and_short_lookalikes_alone() -> None:
    text = "ghp_short github_pat_short a0b1c2d3e4f5a6b7c8d9a0b1c2d3e4f5a6b7c8d9 plain"
    assert scrub(text) == text
    assert scrub("") == ""


def test_scrub_replaces_exact_secrets_longest_first_and_ignores_empty() -> None:
    short, long_ = "tok12345", "tok12345-tail"
    assert scrub(f"x {long_} y", [short, long_]) == f"x {REDACTED} y"  # no '-tail' residue
    assert scrub(f"x {long_} y", [long_, short]) == f"x {REDACTED} y"
    assert scrub("nothing to hide", ["", ""]) == "nothing to hide"


def test_scrub_catches_both_a_known_secret_and_an_unknown_token_in_one_pass() -> None:
    known = "not-token-shaped-secret-value"
    out = scrub(f"a {known} b {FINE_GRAINED} c", [known])
    assert known not in out and FINE_GRAINED not in out
    assert out == f"a {REDACTED} b {REDACTED} c"


def test_planted_token_is_absent_from_every_surface(caplog, tmp_path) -> None:
    """The planted-token bar: resolve, fail, log and persist, then search the whole haystack."""
    token = ResolvedToken(CLASSIC, "env:GITHUB_TOKEN")
    upstream = f"401 Bad credentials for {CLASSIC}; also saw {FINE_GRAINED}"
    diagnostic = scrub(upstream, [token.value])
    try:
        raise RuntimeError(f"write refused: {diagnostic} (token {token})")
    except RuntimeError as exc:
        message = str(exc)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("adapter").warning("%s", message)
    written = tmp_path / "out.json"
    written.write_text(json.dumps({"source": token.source, "diagnostic": diagnostic}))

    haystack = caplog.text + written.read_text() + message + repr(token) + str(token)
    assert CLASSIC not in haystack and FINE_GRAINED not in haystack
    assert not TOKEN_PATTERN.search(haystack)


def test_the_planted_token_bar_is_not_vacuous() -> None:
    """Mutation check: with the pattern pass removed, an unknown token IS found."""
    upstream = f"echo {FINE_GRAINED}"
    exact_only = upstream.replace("a-secret-the-text-never-contained", REDACTED)
    assert TOKEN_PATTERN.search(exact_only)  # what a missing pattern pass would leave behind
    assert not TOKEN_PATTERN.search(scrub(upstream))
