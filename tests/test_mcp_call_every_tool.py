"""Every registered MCP tool is CALLED, not merely imported — the gap that hid bh-fwhlu.

THE INCIDENT (2026-08-15/16). `mcp__plugin_bh_bh__bd_create` failed for 18 hours with
``No module named 'fastmcp.server.tasks.routing'``. The module ships in every fastmcp in our
range and the server *built* perfectly: the failure was in fastmcp's `Tool.run`, which imports
that module lazily on each invocation, against an environment `uv tool install` had replaced
under the long-lived process. So the tool defined cleanly and exploded on call — precisely what
an import-only smoke test cannot see, and why nobody noticed until agents had spent a session
filing beads through the shell instead (where markdown backticks are command substitution, and
`just push` + `just bump` ran).

Two guards here, and they are different guards:

  * :func:`test_every_registered_tool_survives_being_called` drives the WHOLE tool surface
    through `Tool.run`. The arg table must cover the registered set exactly, so a tool added
    later cannot quietly skip the gate.
  * :func:`test_serve_path_imports_are_resident_and_survive_a_vanished_env` reproduces the
    incident directly — make `fastmcp.server.tasks.routing` unimportable *after* startup and
    assert tool calls still work — with the negative control that proves the warm-up is what
    saves them.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from beadhive import config as config_mod
from beadhive import mcp as mcp_mod
from beadhive import work as work_mod

# One inert argument set per tool. Every entry either fails input validation before touching
# anything or is read-only — the point is to reach `Tool.run`, not to mutate a hive. The two that
# cannot be made inert by argument alone (config_set writes, work_refine touches git) are stubbed
# in `_stub_mutators`.
TOOL_ARGS = {
    "plan_check": {"spec": {}},
    "plan_file": {"spec": {}, "dry_run": True},
    "work_refine": {"bead": "bh-none", "dry_run": True},
    "bd_create": {"issues": []},
    "hive_list": {},
    "config_set": {"key": "otel.protocol", "value": "grpc"},
    "hive_add": {"provider": "", "org": "", "repo": ""},
    "hive_onboard": {"provider": "", "org": "", "repo": ""},
    "hive_status": {},
    "toolchain_exec": {"argv": []},
}

# Substrings that mean the FAILURE WAS THE MACHINERY, not the request. A tool refusing an empty
# triplet or an invalid spec is the surface working; a missing module means the call path itself
# is broken and every tool is down with it.
BROKEN_MACHINERY = ("No module named", "ModuleNotFoundError", "ImportError", "AttributeError")


@pytest.fixture
def _stub_mutators(monkeypatch):
    """Neutralize the two tools whose inert-by-argument form still writes."""
    monkeypatch.setattr(
        config_mod,
        "set_value",
        lambda key, raw, as_json=False, cfg=None: {
            "ok": False,
            "problems": ["stubbed"],
            "old": None,
            "new": None,
        },
    )

    def _refuse(*a, **kw):
        raise work_mod.WorkError(["stubbed: no branch"])

    monkeypatch.setattr(work_mod, "refine_branch", _refuse)


def _call_all(server, names):
    """Call each tool in `names`; return {name: None on success, else the error text}."""
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    async def run():
        outcomes = {}
        async with Client(server) as client:
            for name in names:
                try:
                    await client.call_tool(name, TOOL_ARGS[name])
                    outcomes[name] = None
                except ToolError as exc:
                    outcomes[name] = str(exc)
        return outcomes

    return asyncio.run(run())


def _tool_names(server):
    from fastmcp import Client

    async def run():
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(run())


def test_arg_table_covers_every_registered_tool():
    """The table is the gate; a tool missing from it is a tool nobody calls."""
    pytest.importorskip("fastmcp")
    assert _tool_names(mcp_mod.build_server()) == set(TOOL_ARGS)


def test_every_registered_tool_survives_being_called(_stub_mutators):
    """Invoke the whole tool surface. A tool may REFUSE the request; it may not be undialable."""
    pytest.importorskip("fastmcp")
    server = mcp_mod.build_server()
    outcomes = _call_all(server, sorted(TOOL_ARGS))

    broken = {
        name: err
        for name, err in outcomes.items()
        if err and any(marker in err for marker in BROKEN_MACHINERY)
    }
    assert not broken, f"tools defined but not callable: {broken}"


# Run in a FRESH interpreter, and that is the whole point: fastmcp imports
# `fastmcp.server.tasks.routing` on the first component invocation, so by the time any other test
# in this session has called a tool the module is resident and the reproduction can no longer
# fail. In-process, this passes with the warm-up deleted — which is a test that proves nothing.
_REPRO = '''
import asyncio, sys
from fastmcp import Client
from fastmcp.exceptions import ToolError
from beadhive import mcp as mcp_mod

VICTIM = "fastmcp.server.tasks.routing"
assert VICTIM in mcp_mod._SERVE_PATH_DEFERRED
assert VICTIM not in sys.modules, "victim resident before the server was even built"

server = mcp_mod.build_server()
for name in mcp_mod._SERVE_PATH_DEFERRED:
    assert name in sys.modules, f"{name} is imported inside a request, not at startup"

class ExplodingFinder:
    """`uv tool install` has replaced the tree this interpreter was launched from."""
    def find_spec(self, name, path=None, target=None):
        if name == VICTIM:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, ExplodingFinder())

def call():
    async def run():
        async with Client(server) as client:
            try:
                await client.call_tool("bd_create", {"issues": []})
                return None
            except ToolError as exc:
                return str(exc)
    return asyncio.run(run())

# Warmed: resident, so the request path never consults the finder.
err = call()
assert err is None, f"warmed server still broke: {err}"

# Negative control — evict it and the 2026-08-15 failure comes straight back.
del sys.modules[VICTIM]
err = call()
assert err and "No module named" in err, f"expected the incident, got {err!r}"
print("REPRO-OK")
'''


def test_serve_path_imports_are_resident_and_survive_a_vanished_env(tmp_path):
    """The bh-fwhlu reproduction: the env goes away mid-session, the server keeps working."""
    pytest.importorskip("fastmcp")
    script = tmp_path / "repro.py"
    script.write_text(_REPRO)
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "REPRO-OK" in proc.stdout
