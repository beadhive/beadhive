"""Convention done-gate (bh-2l1m.9).

Asserts — mechanically — that the CLI + MCP surface conforms to the decided naming/flag ADR
(``docs/design/cli-mcp-naming-conventions-adr.md``), so the surface cannot silently drift back.
Covers: singular group names, a rich_help_panel on every visible group/command, the 6-panel
scheme, every ``work``/``plan`` verb ``@otel.trace_verb``-wrapped, MCP tool == derived
``group_verb`` for the 1:1 cases, no ``ws``/``rig`` residue in tool names / resource URIs / the
health probe, and the ``beadhive://<group>/<view>`` resource-URI scheme.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from typer.models import DefaultPlaceholder

from beadhive import cli, plan, work

# ---- CLI introspection helpers ----------------------------------------------

SIX_PANELS = {
    "Planning plane",
    "Integration plane",
    "Hive",
    "Fleet / HQ",
    "Admin / infra",
    "Passthrough",
}

# Groups that may legitimately end in "s" despite the singular rule (none today).
SINGULAR_ALLOWLIST: set[str] = set()

# MCP tools that intentionally do NOT map to a native `bh <group> <verb>` (documented exceptions).
TOOL_MAP_EXCEPTIONS = {"bd_create"}  # maps to the `bd` passthrough, not a native bh verb

# Resource URI group segments that would signal plural / rename residue.
FORBIDDEN_URI_SEGMENTS = {"hives", "plans", "worktrees", "labels", "rigs"}


def _panel(info):
    """The command/group's rich_help_panel, or None when unset (a Typer DefaultPlaceholder)."""
    p = getattr(info, "rich_help_panel", None)
    return None if isinstance(p, DefaultPlaceholder) else p


def _visible_groups():
    return [g for g in cli.app.registered_groups if g.hidden is not True]


def _visible_commands():
    return [c for c in cli.app.registered_commands if c.hidden is not True]


def _cli_group_verbs():
    """{group_name: {verb, ...}} for every registered group's own subcommands."""
    out: dict[str, set[str]] = {}
    for g in cli.app.registered_groups:
        ti = g.typer_instance
        verbs = set()
        for c in ti.registered_commands:
            verbs.add(c.name or c.callback.__name__.rstrip("_"))
        out[g.name] = verbs
    return out


# ---- MCP introspection helpers ----------------------------------------------


def _mcp_surface():
    """(tool_names, concrete_resource_uris, resource_template_uris) via the in-memory client."""
    pytest.importorskip("fastmcp")
    from fastmcp import Client

    from beadhive import mcp as mcp_mod

    server = mcp_mod.build_server()

    async def go():
        async with Client(server) as client:
            await client.ping()
            tools = [t.name for t in await client.list_tools()]
            resources = [str(r.uri) for r in await client.list_resources()]
            templates = [str(t.uriTemplate) for t in await client.list_resource_templates()]
        return tools, resources, templates

    return asyncio.run(go())


def _read_probe():
    from fastmcp import Client

    from beadhive import mcp as mcp_mod

    server = mcp_mod.build_server()

    async def go():
        async with Client(server) as client:
            return await client.read_resource("beadhive://probe/health")

    return json.loads(asyncio.run(go())[0].text)


# ---- convention 1/2: singular group names -----------------------------------


def test_visible_groups_are_singular():
    for g in _visible_groups():
        assert not g.name.endswith("s") or g.name in SINGULAR_ALLOWLIST, (
            f"plural group name: {g.name!r}"
        )
    names = {g.name for g in _visible_groups()}
    assert "label" in names, "labels was not renamed to label"
    assert "labels" not in names, "the plural 'labels' group still exists"


# ---- convention 5b: shared verb vocabulary (bh-2v6d) -------------------------

# §5b fixes ONE spelling per concept ("the same verb means the same thing everywhere").
# These are the synonyms drift reaches for. `bh host remove` shipped in 0.7.0 against a
# ratified `rm`, and no rule here caught it — this is that rule.
BANNED_VERB_SYNONYMS = {
    "remove": "rm",
    "delete": "rm",
    "del": "rm",
    "destroy": "rm",
    "create": "add",
    "register": "add",
    "ls": "list",
    "info": "show",
    "describe": "show",
}

# Passthrough groups forward another tool's verbs verbatim — bd's/git's vocabulary is not ours.
PASSTHROUGH_GROUPS = {"bd", "git"}


def _visible_verb_paths(typer_app=None, prefix=()):
    """Yield ``(path_tuple, verb)`` for every VISIBLE command in the tree, recursing into
    sub-groups. Hidden commands are skipped on purpose: a hidden back-compat alias
    (`bh host adopt` -> `bh host lease adopt`) is how a rename stays non-breaking, and
    penalizing it would push renames toward hard cutovers instead."""
    typer_app = cli.app if typer_app is None else typer_app
    for c in typer_app.registered_commands:
        if c.hidden is True:
            continue
        yield prefix, c.name or c.callback.__name__.rstrip("_")
    for g in typer_app.registered_groups:
        if g.hidden is True or g.name in PASSTHROUGH_GROUPS:
            continue
        yield from _visible_verb_paths(g.typer_instance, (*prefix, g.name))


def test_visible_verbs_use_the_canonical_vocabulary():
    """§5b's verb table, enforced. A visible verb may not use a banned synonym of a canonical
    verb — that is exactly how `bh host remove` diverged from `bh hive rm`/`bh worktree rm`."""
    offenders = [
        f"`bh {' '.join((*path, verb))}` should use {BANNED_VERB_SYNONYMS[verb]!r}"
        for path, verb in _visible_verb_paths()
        if verb in BANNED_VERB_SYNONYMS
    ]
    assert not offenders, "§5b verb-vocabulary drift:\n  " + "\n  ".join(offenders)


def test_removal_verbs_are_spelled_rm_across_every_group():
    """The positive half: the three entities that can be unregistered all spell it `rm`, so a
    future revert of bh-2v6d fails here rather than silently re-splitting the vocabulary."""
    verbs = _cli_group_verbs()
    for group in ("hive", "worktree", "host"):
        assert "rm" in verbs[group], f"`bh {group} rm` is missing"


# ---- convention: panels ------------------------------------------------------


def test_every_visible_group_has_a_panel():
    for g in _visible_groups():
        assert _panel(g) is not None, f"group {g.name!r} has no rich_help_panel"


def test_every_visible_command_has_a_panel():
    for c in _visible_commands():
        assert _panel(c) is not None, f"command {c.name!r} has no rich_help_panel"


def test_panels_are_exactly_the_six_named_panels():
    used = {_panel(g) for g in _visible_groups()} | {_panel(c) for c in _visible_commands()}
    used.discard(None)
    assert used <= SIX_PANELS, f"unexpected panels: {sorted(used - SIX_PANELS)}"


def test_otel_and_dolt_are_hidden():
    hidden = {g.name for g in cli.app.registered_groups if g.hidden is True}
    assert {"otel", "dolt"} <= hidden, "otel and dolt must be hidden (deprecation-track)"


# ---- convention 5: every work/plan verb is traced ---------------------------


def test_every_work_and_plan_verb_is_trace_wrapped():
    for group_name, app in (("work", work.app), ("plan", plan.app)):
        for c in app.registered_commands:
            verb = c.name or c.callback.__name__
            assert getattr(c.callback, "__otel_verb__", None), (
                f"`{group_name} {verb}` is not @otel.trace_verb-wrapped"
            )


# ---- convention 6: MCP tool == derived group_verb (1:1 cases) ---------------


def test_mcp_tools_map_to_a_real_cli_verb():
    tools, _resources, _templates = _mcp_surface()
    cli_verbs = _cli_group_verbs()
    for tool in tools:
        if tool in TOOL_MAP_EXCEPTIONS:
            continue
        group, _sep, verb = tool.partition("_")
        assert _sep, f"tool {tool!r} is not a group_verb name"
        assert group in cli_verbs, f"tool {tool!r} references unknown CLI group {group!r}"
        assert verb in cli_verbs[group], (
            f"tool {tool!r} has no backing CLI verb `bh {group} {verb}`"
        )


# ---- convention 8: no ws / rig residue in the surface -----------------------


def test_no_rig_or_ws_residue_in_tool_and_resource_names():
    tools, resources, templates = _mcp_surface()
    ws = re.compile(r"\bws\b")
    for name in [*tools, *resources, *templates]:
        assert "rig" not in name, f"'rig' residue in {name!r}"
        assert "hives_" not in name, f"plural 'hives_' residue in {name!r}"
        assert not ws.search(name), f"'ws' residue in {name!r}"


def test_probe_health_reports_service_bh():
    pytest.importorskip("fastmcp")
    payload = _read_probe()
    assert payload.get("service") == "bh", f"probe service is {payload.get('service')!r}, not 'bh'"


# ---- convention 7: resource URI scheme beadhive://<group-singular>/<view> ----


def test_resource_uris_follow_the_singular_group_scheme():
    _tools, resources, templates = _mcp_surface()
    for uri in [*resources, *templates]:
        assert uri.startswith("beadhive://"), f"{uri!r} is not a beadhive:// URI"
        rest = uri[len("beadhive://") :]
        group = rest.split("/", 1)[0]
        assert group not in FORBIDDEN_URI_SEGMENTS, f"plural/residue group segment in {uri!r}"
