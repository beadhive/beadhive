"""The own-message marker codec (``bh:v1``): last non-empty line only, round trip, refusals;
and the URL shape that marks a comment as a pull-request comment."""

from __future__ import annotations

import pytest

from beadhive.modules.tracker.contracts import OwnMarker
from beadhive_tracker_github.adapter import decode_marker, encode_marker
from beadhive_tracker_github.adapter.mapping import is_change_request_comment
from beadhive_tracker_github.adapter.marker import marker_line

MARKER = OwnMarker("acme/widgets", "6f1c2a7e-0b4d-4c5e-9a1b-2c3d4e5f6a7b", "ob-17")
LINE = "<!-- bh:v1 hive=acme/widgets install=6f1c2a7e-0b4d-4c5e-9a1b-2c3d4e5f6a7b outbox=ob-17 -->"


def test_the_marker_is_the_documented_footer_line():
    assert marker_line(MARKER) == LINE
    assert encode_marker("We reproduced this.\n", MARKER) == f"We reproduced this.\n\n{LINE}"


@pytest.mark.parametrize(
    "body", ["We reproduced this.", "multi\nline\n\nbody", "", "  indented\n", "> quote\nend"]
)
def test_round_trip(body):
    assert decode_marker(encode_marker(body, MARKER)) == (body.rstrip(), MARKER)


@pytest.mark.parametrize(
    "encoded",
    [
        f"We reproduced this.\r\n\r\n{LINE}",  # a web edit returns CRLF
        f"We reproduced this.\n\n{LINE}\n\n  \n",  # trailing blank lines
        f"We reproduced this.\n\n   {LINE}   ",  # surrounding spaces
    ],
)
def test_decoding_tolerates_line_endings_and_trailing_space(encoded):
    assert decode_marker(encoded) == ("We reproduced this.", MARKER)


@pytest.mark.parametrize(
    "body",
    [
        f"> {LINE}\n\nthanks",  # quoted
        f"> {LINE}",  # quoted, even as the last line
        f"{LINE}\n\nfollow-up text",  # not the last non-empty line
        LINE.replace("bh:v1", "bh:v2"),  # another grammar version
        LINE.replace("outbox=ob-17", "outbox=ob--17"),  # would break the HTML comment
        "no marker at all",
        "<!-- bh:v1 hive=acme/widgets install=x -->",  # the older bh-mqvdq form, no outbox
    ],
)
def test_anything_else_is_not_a_marker_and_the_body_is_unchanged(body):
    assert decode_marker(body) == (body, None)


def test_a_marker_naming_another_hive_still_decodes_classifying_is_the_consumers_job():
    other = OwnMarker("someone/else", "i-1", "o-1")
    assert decode_marker(encode_marker("hi", other)) == ("hi", other)


@pytest.mark.parametrize(
    "marker",
    [
        OwnMarker("acme widgets", "i", "o"),
        OwnMarker("acme/widgets", "", "o"),
        OwnMarker("acme/widgets", "i", "o -->evil"),
        OwnMarker("acme/widgets", "i--j", "o"),
    ],
)
def test_encoding_refuses_a_field_that_would_break_the_grammar_without_echoing_it(marker):
    with pytest.raises(ValueError) as caught:
        encode_marker("body", marker)
    assert "evil" not in str(caught.value) and "i--j" not in str(caught.value)


@pytest.mark.parametrize(
    ("url", "on_pull_request"),
    [
        ("https://github.com/acme/widgets/pull/8#issuecomment-502", True),
        ("https://github.com/acme/widgets/issues/8#issuecomment-502", False),
        ("https://github.com/acme/pull/issues/8#issuecomment-502", False),  # a repo named pull
        ("https://github.com/pull/123/issues/8#issuecomment-502", False),  # an owner named pull
    ],
)
def test_a_pull_request_comment_is_recognised_by_its_url_shape(url, on_pull_request):
    assert is_change_request_comment({"html_url": url}) is on_pull_request
