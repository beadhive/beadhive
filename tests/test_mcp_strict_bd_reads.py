"""bh-fzh4h — an absent bd reaches an MCP resource's reader as an ERROR, never as an empty result.

bh-8x452 threaded ``strict=`` through the resources that call ``bd.json`` / ``bd.show`` directly.
The ones that reach bd INDIRECTLY — through ``triage.intake_payload`` / ``triage.find_dupes`` /
``work_show.show_payload`` / ``work.schedule_payload`` / ``worktree.status_rows``, none of which
take a strict flag — never saw it and kept returning ``{"rows": [], "dupes": []}``, ``[]``, a full
worktree classification derived from empty statuses, and a bead with NO GATES.

So the tests here assert the SURFACE property rather than a remembered list of URIs:

  * every registered resource reads bd strictly, with ``beadhive://doctor`` the one declared
    exemption — the check walks the built server, so a resource added LATER is covered too;
  * the previously-null producers raise when read inside that strictness, at whatever depth;
  * the envelope turns that into a client-visible error NAMING the binary;
  * doctor keeps answering with bd absent, because diagnosing a broken seat is its whole job.

MCP tests are gated behind importorskip so CI stays green without the [mcp] extra.
"""

from __future__ import annotations

import asyncio

import pytest

from beadhive import bd as bd_mod
from beadhive import config as config_mod
from beadhive import mcp as mcp_mod
from beadhive import registry as registry_mod
from beadhive import triage as triage_mod
from beadhive import work as work_mod
from beadhive import work_show as work_show_mod
from beadhive import worktree as worktree_mod

#: The one resource that must NOT read bd strictly — see doctor_resource's docstring.
DOCTOR_URI = "beadhive://doctor"


def _absent_bd(monkeypatch):
    """Make every bd invocation look like an uninstalled binary (the review's A/B)."""
    from beadhive import run as run_mod

    def _no_such_binary(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'bd'")

    monkeypatch.setattr(run_mod.subprocess, "run", _no_such_binary)
    monkeypatch.setattr(bd_mod, "_MISSING_BINARY_WARNED", set())


# ---- the surface property ---------------------------------------------------------------------


def _registered_readers(server):
    """(uri, handler) for every registered resource AND resource template."""
    resources = asyncio.run(server.list_resources())
    templates = asyncio.run(server.list_resource_templates())
    return [(str(r.uri), r.fn) for r in resources] + [
        (str(t.uri_template), t.fn) for t in templates
    ]


def test_every_registered_resource_reads_bd_strictly_except_doctor():
    """The acceptance criterion that survives a resource added next month: strictness belongs to
    the surface, so it is asserted over whatever the server actually registered rather than over
    the list of URIs this bead happened to know about. A new resource that reaches bd through a new
    helper is covered without anyone remembering to plumb a flag."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()

    lax = [uri for uri, fn in _registered_readers(server) if not getattr(fn, "bh_strict_bd", False)]

    assert lax == [DOCTOR_URI], (
        "every MCP resource must read bd strictly; the ONLY declared exemption is "
        f"{DOCTOR_URI} (its job is reporting on a broken seat). Non-strict: {lax}"
    )


def test_doctor_is_exempt_deliberately_and_still_answers_with_bd_absent(monkeypatch):
    """Blanket strictness applied without this opt-out makes doctor RAISE — measured while fixing
    bh-fzh4h. That would replace the finding with the failure to make it: the caller asked what is
    broken and would learn only that something is."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    doctor_fn = dict(_registered_readers(server))[DOCTOR_URI]

    assert getattr(doctor_fn, "bh_strict_bd", False) is False

    _absent_bd(monkeypatch)
    monkeypatch.setattr(
        mcp_mod.doctor, "doctor_payload", lambda: {"ok": bd_mod.json(["list"], "/tmp") is None}
    )

    assert doctor_fn() == {"ok": True}


def test_a_resource_opts_out_of_strictness_only_explicitly():
    """`strict_bd` defaults to True, so forgetting it cannot silently reintroduce the null shape —
    the failure mode has to be typed out."""
    wrapped = mcp_mod._strict_bd_reads(lambda: None)

    assert wrapped.bh_strict_bd is True


# ---- the indirect producers, at depth --------------------------------------------------------


def test_intake_producers_raise_instead_of_an_empty_inbox(monkeypatch, tmp_path):
    """`{"rows": [], "dupes": []}` told an agent the intake queue was empty when the truth was that
    the binary is gone."""
    _absent_bd(monkeypatch)

    with pytest.raises(bd_mod.BinaryMissing):
        with bd_mod.strict_reads():
            triage_mod.intake_payload(str(tmp_path))

    with pytest.raises(bd_mod.BinaryMissing):
        with bd_mod.strict_reads():
            triage_mod.find_dupes(str(tmp_path))


def test_show_payload_raises_instead_of_reporting_NO_GATES(monkeypatch, tmp_path):
    """The worst of the five: an agent reading `{"gates": []}` concludes there is nothing to
    review — a false statement on the exact integrity boundary the review gate defends."""
    _absent_bd(monkeypatch)
    monkeypatch.setattr(worktree_mod, "integration_base", lambda entry, bead, integration: "main")
    monkeypatch.setattr(worktree_mod, "base_of", lambda entry, branch, integration: "abc1234")
    monkeypatch.setattr(worktree_mod, "commit_rows", lambda entry, base, branch: [])
    monkeypatch.setattr(config_mod, "integration_branch", lambda cfg, entry: "main")
    monkeypatch.setattr(config_mod, "max_commits", lambda cfg, entry: 10)

    with pytest.raises(bd_mod.BinaryMissing):
        with bd_mod.strict_reads():
            work_show_mod.show_payload({}, {"prefix": "bh"}, "bh-1", "wt/bead/issue/bh-1", tmp_path)


def test_schedule_payload_stops_blaming_the_epic_for_the_missing_binary(monkeypatch, tmp_path):
    """The manufactured finding verbatim: it asked 'is it an epic in this hive?' when the truth was
    that bd is not installed. A failed lookup must not be reported as a fact about the data."""
    _absent_bd(monkeypatch)

    with pytest.raises(bd_mod.BinaryMissing):
        with bd_mod.strict_reads():
            work_mod.schedule_payload("bh-epic", {}, {"prefix": "bh"}, tmp_path)


def test_worktree_statuses_raise_instead_of_classifying_from_an_empty_status(monkeypatch, tmp_path):
    """The FIFTH resource, which this bead's filed list did not have (found by redoing the audit).
    `_bead_statuses_for_entry` falls back to "" per bead, so every SAFE / ACTIVE / REVIEW /
    MERGED_ORPHAN verdict in beadhive://worktree/list was derived from an absent answer."""
    _absent_bd(monkeypatch)
    monkeypatch.setattr(registry_mod, "hive_dir", lambda entry: tmp_path)
    rows = [("leaf", str(tmp_path), "wt/bead/issue/bh-1")]

    # Unfenced the per-bead status is still "" — but it no longer classifies from it silently:
    # the read now comes back WITH A REASON, and wt_status buckets it UNKNOWN rather than ACTIVE
    # (bh-167s0, the CLI-side half of the same defect this bead fixed for MCP).
    statuses, _reasons, _unknown, store_reason = worktree_mod._bead_statuses_for_entry(
        {"prefix": "bh"}, rows
    )
    assert statuses == {"bh-1": ""}
    assert "could not be READ" in store_reason

    with pytest.raises(bd_mod.BinaryMissing):
        with bd_mod.strict_reads():
            worktree_mod._bead_statuses_for_entry({"prefix": "bh"}, rows)


# ---- end to end through the envelope ----------------------------------------------------------


def test_the_client_sees_an_error_naming_the_binary(monkeypatch, tmp_path):
    """Through a live client: the measured envelope maps BinaryMissing to a ResourceError whose
    text names `bd`, so the agent reads the cause instead of an empty inbox."""
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    _absent_bd(monkeypatch)
    monkeypatch.setattr(config_mod, "load", lambda: {})
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": tmp_path)
    server = mcp_mod.build_server()

    async def _read():
        async with Client(server) as client:
            return await client.read_resource("beadhive://work/intake")

    with pytest.raises(Exception) as excinfo:
        asyncio.run(_read())
    assert "`bd` is not on PATH" in str(excinfo.value)


# ---- bh-8x452: the TOOL half of the same surface ----------------------------------------------


def test_every_registered_tool_reads_bd_strictly():
    """The resource half of this property landed with bh-fzh4h and the tool half did not, so
    `plan_file` still reached `plan.file_molecule`'s `bd.json` calls and read the None that means
    "no such bead". Asserted over the registered set, not a list of names, for the same reason the
    resource test is: a tool added later must inherit it. No tool is exempt — there is no tool
    whose job is diagnosing a broken seat, which is the one thing that earns `beadhive://doctor`
    its opt-out."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()

    tools = asyncio.run(server.list_tools())
    lax = [t.name for t in tools if not getattr(t.fn, "bh_strict_bd", False)]

    assert tools, "no tools registered — the assertion would pass vacuously"
    assert lax == [], f"every MCP tool must read bd strictly; non-strict: {lax}"


def test_bd_create_says_the_binary_is_gone_not_that_the_bead_is_bad(monkeypatch, tmp_path):
    """The write path's version of the null read. `bd.create` returned a bare 127, so
    `create_items` rendered it as ``#0 'my bead': bd exit 127`` — a claim about THAT ITEM, which
    sends the agent to inspect a bead that is perfectly fine, exactly as "bead not found" sent it
    looking for a bead that exists. Driven through a live client, because the server's stderr is
    the one stream an MCP client never reads."""
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    _absent_bd(monkeypatch)
    monkeypatch.setattr(config_mod, "load", lambda: {})
    monkeypatch.setattr(registry_mod, "hive_dir_for", lambda cfg, hive="": tmp_path)
    server = mcp_mod.build_server()

    async def _call():
        async with Client(server) as client:
            return await client.call_tool("bd_create", {"issues": [{"title": "a real bead"}]})

    with pytest.raises(Exception) as excinfo:
        asyncio.run(_call())
    message = str(excinfo.value)
    assert "`bd` is not on PATH" in message, message
    assert "bd exit 127" not in message, f"the bare exit code is back: {message}"


def test_the_stderr_narration_repeats_once_per_HIVE(monkeypatch, capsys, tmp_path):
    """The granularity decision, asserted rather than only written down: keyed on the binary alone,
    a fleet-wide sweep narrated for the first hive and went silent for the other thirty-nine."""
    _absent_bd(monkeypatch)
    hive_a, hive_b = tmp_path / "a", tmp_path / "b"

    for cwd in (hive_a, hive_b, hive_a):  # the repeat must NOT narrate again
        bd_mod.json(["list"], str(cwd))

    narrated = capsys.readouterr().err.count("is not on PATH")
    assert narrated == 2, f"expected one line per hive, got {narrated}"
