"""The in-package transit subset: round trips of sets, nested timestamps and escaped strings.

The ``REAL_*`` constants are verbatim transit text captured from XTDB 2.2.0 over pgwire with
``SET fallback_output_format='transit'`` (the non-verbose dialect with cache references), so the
reader is pinned to what the server really sends without needing a server here.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from uuid import UUID

import pytest

from beadlog_xtdb import (
    Instant,
    Keyword,
    Tagged,
    TransitError,
    decode,
    encode,
    escape_string,
    unescape_string,
)

REAL_NESTED_STRUCT = (
    '["^ ","git.commits","[\\"x\\"]","deps",[["^ ","created_at",'
    '["~#time/zoned-date-time","2026-10-22T00:06Z[UTC]"],"type","blocks"]],"~~weird",1]'
)
REAL_CACHED_LIST = (
    '[["^ ","created_at",["~#time/zoned-date-time","2026-10-22T00:06Z[UTC]"],'
    '"type","blocks","who","a"],'
    '["^ ","^0",["^1","2026-10-22T00:06:01.500Z[UTC]"],"^2","blocks","who","a"],'
    '["^ ","^0",["^1","2026-10-22T00:06:01.123456Z[UTC]"],"^2","blocks","who","a"]]'
)
REAL_NESTED_CACHE = '["^ ","alpha_key",1,"beta_key",["^ ","^0",2,"gamma_key",[1,2,3]]]'
REAL_SET = '["~#set",["b:c","a","~~x"]]'
REAL_EMPTY_SET = '["~#set",[]]'


def roundtrip(value: object) -> object:
    return decode(encode(value))


def test_strings_that_look_like_transit_are_escaped_and_survive() -> None:
    for text in ("~tilde", "^caret", "`tick", "~#set", "~:kw", "^ ", "^0", "~", "^", "`"):
        assert escape_string(text) == "~" + text
        assert unescape_string("~" + text) == text
        assert json.loads(encode(text)) == "~" + text
        assert roundtrip(text) == text
    assert escape_string("plain") == "plain"
    assert escape_string("") == ""
    assert encode("plain") == '"plain"'


def test_escaped_strings_survive_inside_every_container() -> None:
    value = {"~key": ["~a", {"^b": "`c"}], "ok": {"~#time/instant": "not a tag"}}
    wire = json.loads(encode(value))
    assert "~~key" in wire and "~#time/instant" not in wire["ok"]
    assert roundtrip(value) == value
    assert roundtrip({"~#set": [1]}) == {"~#set": [1]}  # a plain map is never mistaken for a set


def test_sets_round_trip_as_frozensets_in_a_deterministic_order() -> None:
    value = frozenset({"b:c", "a", "~x"})
    assert roundtrip(value) == value
    assert encode(value) == encode(frozenset(["~x", "a", "b:c"]))
    assert roundtrip(frozenset()) == frozenset()
    assert roundtrip({"labels": {"a", "b"}}) == {"labels": frozenset({"a", "b"})}
    assert json.loads(encode({1, 2})) == {"~#set": [1, 2]}


def test_instants_are_tagged_and_keep_microseconds() -> None:
    moment = Instant.parse("2026-10-30T19:25:37.123456Z")
    assert json.loads(encode(moment)) == {"~#time/instant": "2026-10-30T19:25:37.123456Z"}
    assert roundtrip(moment) == moment.at
    assert Instant.parse("2026-10-30T19:25:00Z").iso() == "2026-10-30T19:25:00Z"
    assert Instant.parse("2026-10-30T19:25:00.50Z").iso() == "2026-10-30T19:25:00.5Z"
    assert encode(datetime(2026, 1, 1, tzinfo=UTC)) == encode(Instant.parse("2026-01-01T00:00:00Z"))


def test_nested_timestamps_round_trip_in_lists_of_structs() -> None:
    value = {
        "deps": [
            {"created_at": Instant.parse("2026-10-22T00:06:00Z"), "type": "blocks"},
            {"created_at": Instant.parse("2026-10-22T00:06:01.5Z"), "type": "blocks"},
        ]
    }
    back = roundtrip(value)
    assert back == {
        "deps": [
            {"created_at": datetime(2026, 10, 22, 0, 6, tzinfo=UTC), "type": "blocks"},
            {"created_at": datetime(2026, 10, 22, 0, 6, 1, 500000, tzinfo=UTC), "type": "blocks"},
        ]
    }


def test_scalars_and_containers_round_trip() -> None:
    value = {
        "none": None,
        "t": True,
        "f": False,
        "int": 2**53 + 1,
        "neg": -(2**63),
        "float": 1.5,
        "empty_list": [],
        "empty_map": {},
        "mixed": [1, "a", True, None, [2], {"k": "v"}],
        "unicode": "em — dash, 中文, \U0001f600, שלום",
        "nul": "a\x00b",
    }
    assert roundtrip(value) == value
    assert roundtrip((1, 2)) == [1, 2]


def test_unsupported_values_are_refused_not_guessed() -> None:
    for bad in (2**63, -(2**63) - 1, math.nan, math.inf, b"bytes", object(), {1: "x"}):
        with pytest.raises(TransitError):
            encode(bad)
    with pytest.raises(TransitError):
        encode(datetime(2026, 1, 1))  # naive
    with pytest.raises(TransitError):
        Instant.parse("2026-10-30 19:25:37")
    with pytest.raises(TransitError):
        Instant.parse("2026-02-30T00:00:00Z")
    with pytest.raises(TransitError, match="microsecond"):
        Instant.parse("2026-10-30T19:25:37.1234567Z")
    assert Instant.parse("2026-10-30T19:25:37.1234560Z").at.microsecond == 123456


def test_microsecond_split_helper_moves_by_whole_microseconds() -> None:
    base = Instant.parse("2026-10-30T19:25:37Z")
    assert base.plus_microseconds(3).iso() == "2026-10-30T19:25:37.000003Z"
    assert base.plus_microseconds(-1).iso() == "2026-10-30T19:25:36.999999Z"


def test_reader_decodes_a_real_nested_struct_with_zoned_timestamps() -> None:
    back = decode(REAL_NESTED_STRUCT)
    assert back == {
        "git.commits": '["x"]',
        "deps": [{"created_at": datetime(2026, 10, 22, 0, 6, tzinfo=UTC), "type": "blocks"}],
        "~weird": 1,
    }


def test_reader_resolves_cache_references_in_real_output() -> None:
    stamps = [d["created_at"] for d in decode(REAL_CACHED_LIST)]
    assert stamps == [
        datetime(2026, 10, 22, 0, 6, tzinfo=UTC),
        datetime(2026, 10, 22, 0, 6, 1, 500000, tzinfo=UTC),
        datetime(2026, 10, 22, 0, 6, 1, 123456, tzinfo=UTC),
    ]
    assert all(set(d) == {"created_at", "type", "who"} for d in decode(REAL_CACHED_LIST))
    assert decode(REAL_NESTED_CACHE) == {
        "alpha_key": 1,
        "beta_key": {"alpha_key": 2, "gamma_key": [1, 2, 3]},
    }


def test_reader_decodes_real_sets_with_escaped_members() -> None:
    assert decode(REAL_SET) == frozenset({"b:c", "a", "~x"})
    assert decode(REAL_EMPTY_SET) == frozenset()
    assert decode("[]") == []


def test_cache_codes_past_the_first_44_entries_resolve() -> None:
    keys = [f"key_{index:03d}" for index in range(60)]
    flat: list[object] = ["^ "]
    for index, key in enumerate(keys):
        flat += [key, index]

    # A second map reuses all sixty keys by cache code: one char up to 43, two chars after.
    def code(index: int) -> str:
        return (
            "^" + chr(48 + index)
            if index < 44
            else "^" + chr(48 + index // 44) + chr(48 + index % 44)
        )

    second: list[object] = ["^ "]
    for index in range(60):
        second += [code(index), -index]
    back = decode(json.dumps([flat, second]))
    assert back[0] == {key: index for index, key in enumerate(keys)}
    assert back[1] == {key: -index for index, key in enumerate(keys)}


def test_cache_reference_before_definition_is_an_error() -> None:
    with pytest.raises(TransitError, match="cache"):
        decode('["^ ","^5",1]')


def test_reader_handles_verbose_json_and_the_other_standard_tags() -> None:
    assert decode('{"~#set":[1,2]}') == frozenset({1, 2})
    assert decode('{"a":{"~#time/instant":"2026-01-01T00:00:00Z"}}') == {
        "a": datetime(2026, 1, 1, tzinfo=UTC)
    }
    assert decode('["~:blocks","~$sym","~_","~?t","~i5","~d1.5","~zINF"]') == [
        Keyword("blocks"),
        decode('"~$sym"'),
        None,
        True,
        5,
        1.5,
        math.inf,
    ]
    assert decode('"~u0a0a0a0a-0a0a-0a0a-0a0a-0a0a0a0a0a0a"') == UUID(
        int=0x0A0A0A0A0A0A0A0A0A0A0A0A0A0A0A0A
    )
    assert decode('["~#xtdb/odd",[1]]') == Tagged("xtdb/odd", [1])
    assert decode('"~t2026-10-30T19:25:37.123Z"') == datetime(
        2026, 10, 30, 19, 25, 37, 123000, tzinfo=UTC
    )
    assert decode('"~~escaped"') == "~escaped"


def test_zoned_timestamps_with_offsets_normalise_to_utc() -> None:
    assert decode('["~#time/zoned-date-time","2026-10-22T02:06+02:00[Europe/Paris]"]') == datetime(
        2026, 10, 22, 0, 6, tzinfo=UTC
    )


def test_malformed_text_is_a_transit_error() -> None:
    for bad in ("not json", '["^ ","k"]', '["~#time/instant","never"]'):
        with pytest.raises(TransitError):
            decode(bad)
