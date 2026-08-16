"""`bh bd create --json` — the shell-free CLI transport, and its round trip against MCP.

WHY (bh-fwhlu, 2026-08-16). Bead prose is markdown; markdown marks identifiers with backticks.
Passed as a DOUBLE-QUOTED shell argument, a backtick is command substitution — bash ran the spans
before `bh` was exec'd, and `just push` + `just bump` executed against this repo. `just release`
(publish to PyPI, recoverable only by yank) was a code span in the same argument and did not fire
only because bash pairs backticks POSITIONALLY, so which spans run drifts out of phase with what
the author wrote. bh cannot detect it: substitution completes before the process exists.

`--json` takes the whole bead as one document — the same schema the `bd_create` MCP tool takes —
so no prose field is ever a shell token. The round-trip test is the load-bearing one: the prose
below carries backticks, `$(...)`, both quote styles and newlines, and must arrive at `bd`
BYTE-IDENTICAL through both transports.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from beadhive import bd as bd_mod
from beadhive import mcp as mcp_mod
from beadhive import registry as registry_mod

# Every character class that was fatal in the incident, in one string.
PROSE = (
    "Run `just push`, then `just release` — the one-way door.\n"
    "Substitution: $(rm -rf /) and `bump` and ${BH_EXEC:-bh}.\n"
    'It\'s "quoted" both ways; trailing backtick pair: `a` `b` `c`\n'
)

TRIPLET = ["-l", "provider:github,org:beadhive,repo:beadhive"]


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Capture the argv `bd` would be exec'd with, and make create's gates deterministic."""
    calls: list[list[str]] = []

    class _Res:
        returncode = 0

    def _fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _Res()

    monkeypatch.setattr(bd_mod, "_run", _fake_run)
    monkeypatch.setattr(bd_mod, "new_bead_problems", lambda *a, **k: [])
    monkeypatch.setattr(bd_mod, "triplet_label_args", lambda cwd: list(TRIPLET))
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": str(tmp_path))
    return calls


def _item(title="incident repro"):
    return {
        "title": title,
        "type": "bug",
        "priority": 0,
        "description": PROSE,
        "acceptance": PROSE,
        "design": PROSE,
        "labels": ["component:runtime"],
    }


def _payload_file(tmp_path, payload):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(payload))
    return str(path)


def _field(argv, flag):
    return argv[argv.index(flag) + 1]


# ---- the CLI flag ------------------------------------------------------------------------


def test_json_from_a_file_creates_the_bead(captured, tmp_path):
    assert bd_mod._create(["--json", _payload_file(tmp_path, [_item()])], str(tmp_path)) == 0
    assert len(captured) == 1
    assert captured[0][:3] == ["bd", "create", "incident repro"]


def test_json_accepts_a_bare_object_as_well_as_a_list(captured, tmp_path):
    assert bd_mod._create(["--json", _payload_file(tmp_path, _item())], str(tmp_path)) == 0
    assert len(captured) == 1


def test_json_dash_reads_stdin(captured, tmp_path, monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([_item("a"), _item("b")])))
    assert bd_mod._create(["--json", "-"], str(tmp_path)) == 0
    assert [c[2] for c in captured] == ["a", "b"]


def test_json_refuses_to_be_combined_with_per_field_flags(captured, tmp_path, capsys):
    src = _payload_file(tmp_path, [_item()])
    assert bd_mod._create(["--json", src, "-d", "inline"], str(tmp_path)) == 1
    assert "cannot be combined with per-field flags" in capsys.readouterr().err
    assert captured == []  # nothing was created — no silent precedence


def test_json_without_a_source_is_refused(captured, tmp_path, capsys):
    assert bd_mod._create(["--json"], str(tmp_path)) == 1
    assert "--json needs a payload" in capsys.readouterr().err
    assert captured == []


def test_malformed_json_is_refused_by_bh_not_by_bd(captured, tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert bd_mod._create(["--json", str(bad)], str(tmp_path)) == 1
    assert "is not valid JSON" in capsys.readouterr().err
    assert captured == []


def test_an_item_without_a_title_is_named_in_the_failure(captured, tmp_path, capsys):
    src = _payload_file(tmp_path, [_item(), {"description": "no title"}])
    assert bd_mod._create(["--json", src], str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "#1: missing 'title'" in err
    assert "created 1 of 2" in err  # partial success is reported, not hidden


def test_json_goes_through_the_same_triplet_and_label_gate_as_the_flag_path(
    captured, tmp_path, monkeypatch, capsys
):
    """Identical behaviour, because it is literally the same `create` call."""
    monkeypatch.setattr(bd_mod, "new_bead_problems", lambda *a, **k: ["label 'nope' not in vocab"])
    assert bd_mod._create(["--json", _payload_file(tmp_path, [_item()])], str(tmp_path)) == 1
    assert "label violations" in capsys.readouterr().err
    assert captured == []

    monkeypatch.setattr(bd_mod, "new_bead_problems", lambda *a, **k: [])
    assert bd_mod._create(["--json", _payload_file(tmp_path, [_item()])], str(tmp_path)) == 0
    assert captured[0][-len(TRIPLET) :] == TRIPLET  # triplet appended, same as the flag path


# ---- the round trip ----------------------------------------------------------------------


def _via_mcp(item, tmp_path):
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    server = mcp_mod.build_server()

    async def run():
        async with Client(server) as client:
            return await client.call_tool("bd_create", {"issues": [item]})

    return asyncio.run(run())


def test_prose_survives_both_shell_free_transports_byte_identical(captured, tmp_path):
    """The proof: backticks, $(...), both quote styles and newlines arrive unchanged, and the
    two transports produce the SAME argv — one schema, one core, no drift."""
    item = _item()

    _via_mcp(item, tmp_path)
    assert bd_mod._create(["--json", _payload_file(tmp_path, [item])], str(tmp_path)) == 0
    assert len(captured) == 2

    via_mcp, via_json = captured
    assert via_mcp == via_json

    for flag, sent in (("-d", PROSE), ("--acceptance", PROSE), ("--design", PROSE)):
        assert _field(via_mcp, flag) == sent
        assert _field(via_json, flag) == sent
    # And nothing in the prose was ever a shell token: argv is a list, `run` uses no shell.
    assert "`" in _field(via_json, "-d") and "$(" in _field(via_json, "-d")
