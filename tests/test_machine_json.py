"""`bh setup check --json` and `bh doctor --json` (bh-0olv9.2) — the machine renderings.

WHAT THESE TESTS ARE FOR. An agent guiding an install has to decide "what is missing, and what
do I do next". Before this bead it had to answer that by parsing Rich-rendered boxes, which is
brittle normally and silently wrong when a terminal width or a Rich version moves. The flag
alone does not fix that: a `--json` assembled by a SECOND code path drifts from the human one,
and the drift is invisible because nobody diffs them. So the bar these tests hold is not "the
flag emits JSON" but:

1. the JSON is the only thing on stdout, and it parses;
2. it carries everything the human rendering carries — presence, version, satisfied, AND the
   remedy (a payload without the remedy sends the agent back to the prose it replaced);
3. the two renderings AGREE, asserted field by field against one probe, so a future edit to
   either path that forgets the other turns this file red;
4. the contract is versioned from day one, because it becomes a contract the moment the bundled
   setup Guide's `010-preflight` / `040-verify` steps read it.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from beadhive import deps, jsonout
from beadhive import setup as setup_mod
from beadhive.cli import app
from test_work import fakebd, hive  # noqa: F401 — fixtures resolved by name

runner = CliRunner()


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture()
def ws_home(tmp_path, monkeypatch):
    """Redirect ~/.beadhive to tmp_path so no real home-dir state is read/written."""
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    return tmp_path


def _tools(**overrides):
    """Every PROBE_TABLE row found at 1.0, with named rows overridden."""
    found = {n: {"found": True, "version": f"{n} 1.0"} for n, _, _ in setup_mod.PROBE_TABLE}
    found.update(overrides)
    return found


@pytest.fixture()
def all_found(monkeypatch):
    monkeypatch.setattr(setup_mod, "probe_tools", _tools)
    monkeypatch.setattr(setup_mod, "dolt_server_advisory", lambda cwd=None: None)


@pytest.fixture()
def one_missing(monkeypatch):
    """`dolt` absent — an ALWAYS-required row, so the remedy is the toolchain one."""
    tools = _tools(dolt={"found": False, "version": None})
    monkeypatch.setattr(setup_mod, "probe_tools", lambda: tools)
    monkeypatch.setattr(setup_mod, "dolt_server_advisory", lambda cwd=None: None)


# ---- 1. valid JSON, alone on stdout ------------------------------------------


def test_setup_check_json_is_the_only_thing_on_stdout(ws_home, all_found):
    """No progress line, no table, no Rich box interleaved with the document."""
    result = runner.invoke(app, ["setup", "check", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)  # raises if anything else was echoed
    assert payload["satisfied"] is True
    assert "Checking post-ws dependencies" not in result.stdout


def test_setup_check_json_exit_code_still_gates(ws_home, one_missing):
    """--json changes the RENDERING, not the verdict: a missing dep still exits 1, and the
    cache is still written (the gate every other bh verb reads)."""
    result = runner.invoke(app, ["setup", "check", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["satisfied"] is False
    assert setup_mod.is_setup_complete() is False


def test_doctor_json_is_the_only_thing_on_stdout(hive, fakebd):  # noqa: F811
    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "doctor"


# ---- 2. everything the human rendering carries -------------------------------


def test_every_probed_tool_has_presence_version_satisfied_and_remedy(ws_home, one_missing):
    """ACCEPTANCE: per-item presence, version, satisfied/unsatisfied, and the remedy."""
    payload = setup_mod.check_payload()
    assert payload["tools"], "the probe table is never empty"
    for row in payload["tools"]:
        assert set(row) == {"name", "found", "version", "satisfied", "remedy"}
        assert isinstance(row["found"], bool)
        assert isinstance(row["satisfied"], bool)
    by_name = {r["name"]: r for r in payload["tools"]}
    assert by_name["dolt"]["satisfied"] is False
    assert by_name["dolt"]["remedy"], "an unsatisfied row without a remedy is the whole bug"
    # ...and a satisfied row carries none, mirroring the text render, which prints a remedy
    # only under `✗ missing:`. A consumer can act on every non-null remedy it sees.
    assert by_name["bd"]["remedy"] is None


def test_the_remedy_is_actionable_not_prose(ws_home, one_missing):
    """`dolt` is required-always, so the toolchain verb IS the remedy — the bead's bar is that
    an agent can run what it reads rather than going back to INSTALL.md."""
    payload = setup_mod.check_payload()
    remedy = next(r for r in payload["tools"] if r["name"] == "dolt")["remedy"]
    assert "bh setup toolchain" in remedy


@pytest.mark.parametrize("dep", [d for d in deps.DEPS if d.name != "git"])
def test_every_dep_in_the_table_can_be_given_a_remedy(dep, monkeypatch):
    """No row in `deps.DEPS` falls through to an empty remedy — including rows bh deliberately
    does not install (the store-runtime group), which must still say something true."""
    monkeypatch.delenv("BH_IN_CONTAINER", raising=False)
    assert setup_mod.tool_remedy(dep.name).strip()


def test_advisories_are_fields_not_a_dropped_stderr_line(ws_home, monkeypatch):
    """The dolt-server advisory reaches stderr in the text render; in JSON it must be IN the
    payload — an advisory that only exists in the human path is exactly the asymmetry this
    bead forbids."""
    monkeypatch.setattr(setup_mod, "probe_tools", _tools)
    monkeypatch.setattr(setup_mod, "dolt_server_advisory", lambda cwd=None: "⚠ server down")
    payload = setup_mod.check_payload()
    assert [a["id"] for a in payload["advisories"]] == ["dolt-shared-server"]
    assert payload["advisories"][0]["message"] == "⚠ server down"


# ---- 3. the two renderings agree ---------------------------------------------


def _human(args):
    result = runner.invoke(app, args)
    return result.stdout + (result.stderr or ""), result.exit_code


@pytest.mark.parametrize("fixture", ["all_found", "one_missing"])
def test_setup_check_json_and_text_agree(ws_home, fixture, request):
    """ACCEPTANCE: a test asserts the two outputs agree.

    Field by field over ONE probe, not a spot-check of a summary line: every tool name, every
    version string, the verdict, the missing list and the remedy must all be findable in the
    text render, and the exit codes must match. A `--json` that quietly reports a different
    verdict from the box beside it is worse than no `--json` at all.
    """
    request.getfixturevalue(fixture)
    payload = setup_mod.check_payload()
    text, code = _human(["setup", "check"])

    assert code == (0 if payload["satisfied"] else 1)
    for row in payload["tools"]:
        glyph = "✓" if row["found"] else "✗"
        assert f"{glyph} {row['name']}" in text
        if row["version"]:
            assert row["version"] in text
    if payload["missing"]:
        assert f"✗ missing: {', '.join(payload['missing'])}" in text
        assert payload["remedy"] in text
    else:
        assert "setup complete" in text
    for advisory in payload["advisories"]:
        assert advisory["message"] in text


def test_doctor_json_and_text_render_the_same_object(hive, fakebd, monkeypatch):  # noqa: F811
    """`doctor()` consumes `doctor_payload()` rather than re-collecting, so a stubbed payload
    reaches BOTH renderings — which is what makes "one source, two renderings" checkable
    instead of merely intended."""
    from beadhive import doctor as doctor_mod

    real = doctor_mod.doctor_payload()
    seen: list[dict] = []
    monkeypatch.setattr(doctor_mod, "doctor_payload", lambda **kwargs: (seen.append(real), real)[1])

    json_out = runner.invoke(app, ["doctor", "--json"])
    text_out = runner.invoke(app, ["doctor"])

    assert len(seen) == 2, "both renderings must read the one builder"
    assert json.loads(json_out.stdout) == real
    # The text render is the same object echoed: a section the payload reports must show up.
    assert str(real["inventory"]["hives_registered"]) in text_out.stdout


# ---- 4. the contract is versioned --------------------------------------------


def test_setup_check_payload_is_schema_versioned(ws_home, all_found):
    """ACCEPTANCE: a schema version field is present — from day one, because versioning a
    contract after a consumer exists is the breaking change."""
    payload = setup_mod.check_payload()
    assert payload["schema_version"] == jsonout.SETUP_CHECK_SCHEMA
    assert payload["command"] == "setup check"


def test_doctor_payload_is_schema_versioned(hive, fakebd):  # noqa: F811
    from beadhive import doctor as doctor_mod

    payload = doctor_mod.doctor_payload()
    assert payload["schema_version"] == jsonout.DOCTOR_SCHEMA
    assert payload["command"] == "doctor"


def test_the_envelope_leads_and_is_not_nested():
    """Flat, top-level, envelope-first — matching `bd`'s own `--json` shape (the tool bh wraps)
    rather than inventing a `{"data": …}` wrapper that would re-shape every existing payload."""
    enveloped = jsonout.envelope("x", 3, {"a": 1})
    assert list(enveloped) == ["schema_version", "command", "a"]
    assert enveloped == {"schema_version": 3, "command": "x", "a": 1}
