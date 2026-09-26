"""Completeness and policy gates for the canonical operation catalog."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import inspect
import io
import json
import re
import subprocess
from pathlib import Path

from click import Context
from fastmcp import Client
from jsonschema import Draft202012Validator
from typer.main import get_command

from beadhive import plan, work
from beadhive.cli import app
from beadhive.mcp import build_server
from beadhive.operation_catalog import document, operations

ROOT = Path(__file__).resolve().parents[1]
_WIRE_ROOT = ROOT / "docs" / "schemas" / "wire"
_WIRE_INDEX = json.loads((_WIRE_ROOT / "index.json").read_text())
# The live catalog tracks whichever release index.json names as latest (bh-bwnys.5).
_LATEST = next(row for row in _WIRE_INDEX["releases"] if row["version"] == _WIRE_INDEX["latest"])
WIRE = (_WIRE_ROOT / _LATEST["manifest"]).parent
RETIRED_SURFACE_TOKEN = re.compile(r"(?<![a-z0-9])(?P<token>ws|rig)(?![a-z0-9])", re.I)
MANIFEST_CLI_COMMANDS = frozenset(app._bh_manifest_commands)
MANIFEST_CLI_PARENTS = frozenset(app._bh_manifest_parents)

# There are deliberately no exceptions today.  Any future compatibility seam must name the exact
# scanned descriptor and explain why it cannot be removed; an unscoped regex suppression is never
# accepted here.
RETIRED_SURFACE_EXCLUSIONS: dict[str, str] = {}


def _cli_leaves(
    command,
    path: tuple[str, ...] = (),
    *,
    inherited_hidden: bool = False,
) -> dict[str, dict[str, object]]:
    leaves = {}
    for name, child in sorted(getattr(command, "commands", {}).items()):
        child_path = (*path, name)
        effective_hidden = inherited_hidden or bool(child.hidden)
        if getattr(child, "commands", None) is not None:
            leaves.update(_cli_leaves(child, child_path, inherited_hidden=effective_hidden))
        else:
            leaves[" ".join(child_path)] = {
                "command": child,
                "declared_hidden": bool(child.hidden),
                "effective_hidden": effective_hidden,
            }
    return leaves


def _cli_parents(
    command,
    path: tuple[str, ...] = (),
    *,
    inherited_hidden: bool = False,
    inherited_panel: str | None = None,
) -> dict[str, dict[str, object]]:
    parents = {}
    for name, child in sorted(getattr(command, "commands", {}).items()):
        if getattr(child, "commands", None) is None:
            continue
        child_path = (*path, name)
        panel = child.rich_help_panel or None
        effective_hidden = inherited_hidden or bool(child.hidden)
        effective_panel = panel or inherited_panel
        parents[" ".join(child_path)] = {
            "declared_hidden": bool(child.hidden),
            "effective_hidden": effective_hidden,
            "panel": panel,
            "effective_panel": effective_panel,
        }
        parents.update(
            _cli_parents(
                child,
                child_path,
                inherited_hidden=effective_hidden,
                inherited_panel=effective_panel,
            )
        )
    return parents


def _click_type(parameter) -> str:
    name = type(parameter.type).__name__.lower()
    if "bool" in name:
        return "boolean"
    if "int" in name:
        return "integer"
    if "float" in name:
        return "number"
    return "array" if parameter.multiple else "string"


def _projected_cli() -> dict[str, tuple[str, dict[str, object]]]:
    projected = {}
    for operation in operations():
        projection = operation.surfaces.get("cli")
        if not projection:
            continue
        projected[projection["path"]] = (operation.name, projection)
        for alias in projection["aliases"]:
            projected[alias["path"]] = (operation.name, alias)
    return projected


def _live_prompt_seams() -> set[str]:
    seams: set[str] = set()
    source_root = ROOT / "src"
    for source in sorted((source_root / "beadhive").rglob("*.py")):
        module = ".".join(source.relative_to(source_root).with_suffix("").parts)
        tree = ast.parse(source.read_text())

        class PromptVisitor(ast.NodeVisitor):
            def __init__(self, module_name: str) -> None:
                self.functions: list[str] = []
                self.module_name = module_name

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node: ast.Call) -> None:
                function = node.func
                if (
                    self.functions
                    and isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "typer"
                    and function.attr in {"confirm", "prompt"}
                ):
                    seams.add(
                        f"{self.module_name}.{'.'.join(self.functions)}:typer.{function.attr}"
                    )
                self.generic_visit(node)

        PromptVisitor(module).visit(tree)
    return seams


def _retired_tokens(text: str) -> set[str]:
    return {match.group("token").lower() for match in RETIRED_SURFACE_TOKEN.finditer(text)}


def _live_cli_surface_texts() -> dict[str, str]:
    texts = {}
    for path, metadata in _cli_leaves(get_command(app)).items():
        command = metadata["command"]
        context = Context(command, info_name=path.rsplit(" ", 1)[-1])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rendered = command.get_help(context) or ""
        texts[f"cli:{path}:help"] = rendered + stdout.getvalue() + stderr.getvalue()
        texts[f"cli:{path}:docstring"] = inspect.getdoc(command.callback) or ""
    return texts


def _live_mcp_surface_texts() -> tuple[dict[str, str], dict[str, int]]:
    async def inventory():
        async with Client(build_server()) as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            probe = await client.read_resource("beadhive://probe/health")
            texts = {
                **{
                    f"mcp:tool:{tool.name}": "\n".join(
                        filter(None, (tool.name, tool.title, tool.description))
                    )
                    for tool in tools
                },
                **{
                    f"mcp:resource:{resource.uri}": "\n".join(
                        filter(
                            None,
                            (
                                resource.name,
                                resource.title,
                                str(resource.uri),
                                resource.description,
                            ),
                        )
                    )
                    for resource in resources
                },
                **{
                    f"mcp:resource-template:{template.uriTemplate}": "\n".join(
                        filter(
                            None,
                            (
                                template.name,
                                template.title,
                                str(template.uriTemplate),
                                template.description,
                            ),
                        )
                    )
                    for template in templates
                },
                "mcp:probe:health-payload": "\n".join(
                    block.text for block in probe if hasattr(block, "text")
                ),
            }
            return texts, {
                "tools": len(tools),
                "resources": len(resources) + len(templates),
                "probes": 1,
            }

    return asyncio.run(inventory())


def test_published_catalog_is_current_deterministic_and_schema_valid() -> None:
    published = json.loads((WIRE / "operation-catalog-v1.json").read_text())
    schema = json.loads((WIRE / "operation-catalog-v1.schema.json").read_text())

    assert published == document()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(published)
    release = json.loads((WIRE / "release.json").read_text())
    manifest_paths = {row["id"]: row["path"] for row in release["artifacts"]}
    assert schema["$id"] != published["$id"]
    assert manifest_paths[schema["$id"]] == "operation-catalog-v1.schema.json"
    assert manifest_paths[published["$id"]] == "operation-catalog-v1.json"
    assert [row["name"] for row in published["operations"]] == sorted(
        row["name"] for row in published["operations"]
    )


def test_every_cli_leaf_and_signature_is_declared_exactly_once() -> None:
    actual = _cli_leaves(get_command(app))
    projected = _projected_cli()
    by_name = {operation.name: operation for operation in operations()}

    assert set(projected) == set(actual) - MANIFEST_CLI_COMMANDS
    assert MANIFEST_CLI_COMMANDS == set(actual) - set(projected)
    for path, metadata in actual.items():
        if path in MANIFEST_CLI_COMMANDS:
            continue
        operation_name, projection = projected[path]
        operation = by_name[operation_name]
        declared = {parameter.name: parameter for parameter in operation.parameters}
        command = metadata["command"]
        actual_parameters = {parameter.name: parameter for parameter in command.params}
        assert projection["parameters"] == list(actual_parameters)
        for name, parameter in actual_parameters.items():
            canonical_name = projection["parameter_map"].get(name, name)
            assert declared[canonical_name].schema["type"] == _click_type(parameter), (path, name)
            assert declared[canonical_name].required is parameter.required, (path, name)
        assert projection["declared_hidden"] is metadata["declared_hidden"]
        assert projection["effective_hidden"] is metadata["effective_hidden"]


def test_every_projection_declares_granularity_progress_and_interactivity() -> None:
    cli = _projected_cli()
    assert len(cli) == 219
    for path, (_name, projection) in cli.items():
        assert projection["granularity"]["mode"] in {"fine", "coarse", "divergent"}
        if projection["granularity"]["mode"] == "fine":
            assert projection["granularity"]["reason"] is None
        else:
            assert projection["granularity"]["reason"], path
        assert projection["progress"]["mode"] in {"stdout", "structured", "none"}
        assert projection["progress"]["notification_uris"] == []
        assert projection["progress"]["reason"]
        assert projection["interactivity"]["mode"] in {"none", "guarded-prompt"}

    mcp = [operation.surfaces["mcp"] for operation in operations() if "mcp" in operation.surfaces]
    for projection in mcp:
        assert projection["granularity"]["mode"] in {"fine", "coarse", "divergent"}
        assert projection["progress"]["mode"] in {"none", "notifications"}
        assert projection["interactivity"] == {
            "mode": "none",
            "guard_parameters": [],
            "guard_conditions": [],
            "prompt_seams": [],
            "reason": None,
        }


def test_live_prompt_seams_are_guarded_and_mcp_projections_never_prompt() -> None:
    prompt_paths = {}
    seam_paths: dict[str, set[str]] = {}
    for path, (name, projection) in _projected_cli().items():
        policy = projection["interactivity"]
        if policy["mode"] != "guarded-prompt":
            continue
        prompt_paths[path] = policy
        assert policy["reason"]
        assert policy["prompt_seams"]
        assert policy["guard_parameters"] or policy["guard_conditions"]
        for guard in policy["guard_parameters"]:
            assert guard in projection["parameters"], (path, guard)
        for seam in policy["prompt_seams"]:
            seam_paths.setdefault(seam, set()).add(path)
        operation = next(op for op in operations() if op.name == name)
        if "mcp" in operation.surfaces:
            assert path == "doctor"
            assert "mcp-uses-pure-doctor-payload" in policy["guard_conditions"]
            assert operation.surfaces["mcp"]["interactivity"]["mode"] == "none"
            assert operation.surfaces["mcp"]["resource"] == "beadhive://doctor"
        else:
            assert "mcp" not in operation.surfaces

    assert set(prompt_paths) == {
        "dep install",
        "doctor",
        "escalate",
        "harness install",
        "host provision",
        "hq clone",
        "hq init",
        "setup guide",
    }
    assert set(seam_paths) == _live_prompt_seams()
    assert seam_paths["beadhive.harness.install:typer.confirm"] == {
        "dep install",
        "harness install",
    }
    assert seam_paths["beadhive.setup_guide.wizard:typer.prompt"] == {"setup guide"}

    interactive_operations = {
        operation.name for operation in operations() if operation.constraints["interactive"]
    }
    assert interactive_operations == {
        "dep.install",
        "escalate",
        "host.provision",
        "hq.clone",
        "hq.init",
        "setup.guide",
    }


def test_cli_parent_alias_and_passthrough_metadata_is_complete() -> None:
    root = get_command(app)
    actual_parents = _cli_parents(root)
    declared_parents = {row["path"]: row for row in document()["cli_parents"]}
    assert set(declared_parents) == set(actual_parents) - MANIFEST_CLI_PARENTS
    assert MANIFEST_CLI_PARENTS == set(actual_parents) - set(declared_parents)
    for path, actual in actual_parents.items():
        if path in MANIFEST_CLI_PARENTS:
            continue
        declared = declared_parents[path]
        for key in ("declared_hidden", "effective_hidden", "panel", "effective_panel"):
            assert declared[key] == actual[key], (path, key)

    actual_leaves = _cli_leaves(root)
    passthrough = {}
    aliases = {}
    for operation in operations():
        projection = operation.surfaces.get("cli")
        if not projection:
            continue
        if metadata := projection.get("passthrough"):
            passthrough[projection["path"]] = metadata
        for alias in projection["aliases"]:
            aliases[alias["path"]] = operation.name

    expected_passthrough = {
        path
        for path, metadata in actual_leaves.items()
        if path not in MANIFEST_CLI_COMMANDS
        if (metadata["command"].context_settings or {}).get("allow_extra_args")
        or (metadata["command"].context_settings or {}).get("ignore_unknown_options")
    }
    assert set(passthrough) == expected_passthrough
    assert len(passthrough) == 10
    for path, metadata in passthrough.items():
        settings = actual_leaves[path]["command"].context_settings
        assert metadata == {
            "mode": "opaque-argv",
            "allow_extra_args": settings["allow_extra_args"],
            "ignore_unknown_options": settings["ignore_unknown_options"],
        }

    assert aliases == {
        "harness auth": "dep.auth",
        "harness install": "dep.install",
        "harness list": "dep.list",
        "hive sync-remote": "hive.sync.remotes",
        "host adopt": "host.lease.adopt",
        "host daemon remove": "host.daemon.rm",
        "host packup": "host.lease.release",
        "host release": "host.lease.release",
        "wt add": "worktree.add",
        "wt init": "worktree.init",
        "wt list": "worktree.list",
        "wt mark-abandoned": "worktree.mark-abandoned",
        "wt mark-landed": "worktree.mark-landed",
        "wt path": "worktree.path",
        "wt prune": "worktree.prune",
        "wt rm": "worktree.rm",
        "wt status": "worktree.status",
    }
    assert {
        operation.name: operation.alias_of for operation in operations() if operation.alias_of
    } == {
        "work.finish": "work.merge",
        "work.start": "work.claim",
    }


def _mcp_inventory() -> tuple[dict[str, object], set[str]]:
    async def inventory():
        async with Client(build_server()) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            resources = {str(resource.uri) for resource in await client.list_resources()}
            resources.update(
                str(template.uriTemplate) for template in await client.list_resource_templates()
            )
            return tools, resources

    return asyncio.run(inventory())


def test_mcp_allowlist_exactly_covers_the_current_tools_resources_and_signatures() -> None:
    actual_tools, actual_resources = _mcp_inventory()
    projected_tools = {}
    projected_resources = set()
    for operation in operations():
        projection = operation.surfaces.get("mcp")
        if not projection:
            continue
        assert projection["allowlisted"] is True
        if tool := projection.get("tool"):
            projected_tools[tool] = projection["tool_parameters"]
        if resource := projection.get("resource"):
            projected_resources.add(resource)

    assert set(projected_tools) == set(actual_tools)
    assert projected_resources == actual_resources
    for name, parameter_names in projected_tools.items():
        assert parameter_names == list(actual_tools[name].inputSchema.get("properties", {}))


def test_mcp_allowlist_excludes_privilege_and_enforces_action_resource_split() -> None:
    for operation in operations():
        projection = operation.surfaces.get("mcp")
        if not projection:
            continue
        assert operation.privilege != "privileged"
        assert operation.constraints["hq_write"] is False
        assert operation.constraints["secret_material"] is False
        assert operation.constraints["interactive"] is False
        projected_parameters = set(projection.get("tool_parameters", ())) | set(
            projection.get("resource_parameters", ())
        )
        assert not projected_parameters.intersection(operation.constraints["override_parameters"])
        if "resource" in projection:
            assert operation.kind == "read-resource"
        if "tool" in projection and operation.kind != "action":
            assert projection.get("divergence")


def test_mcp_progress_notifications_are_explicit_and_complete() -> None:
    notifications = {
        operation.name: operation.surfaces["mcp"]["progress"]["notification_uris"]
        for operation in operations()
        if operation.surfaces.get("mcp", {}).get("progress", {}).get("mode") == "notifications"
    }
    assert notifications == {
        "bd.create": [
            "beadhive://work/ready",
            "beadhive://work/intake",
            "beadhive://alerts",
        ],
        "config.set": [
            "beadhive://config",
            "beadhive://config/{key}",
            "beadhive://alerts",
        ],
        "hive.add": [
            "beadhive://hive/status",
            "beadhive://hive/list",
            "beadhive://hive/survey",
            "beadhive://alerts",
        ],
        "hive.onboard": [
            "beadhive://hive/status",
            "beadhive://hive/list",
            "beadhive://hive/survey",
            "beadhive://alerts",
        ],
        "plan.file": [
            "beadhive://work/ready",
            "beadhive://plan/list",
            "beadhive://alerts",
        ],
    }


def test_result_refs_and_naming_divergences_are_explicit() -> None:
    release = json.loads((WIRE / "release.json").read_text())
    artifact_ids = {artifact["id"] for artifact in release["artifacts"]}
    for operation in operations():
        assert operation.result_schema in artifact_ids
        projection = operation.surfaces.get("mcp")
        if not projection:
            continue
        if tool := projection.get("tool"):
            assert tool == operation.name.replace(".", "_") or projection.get("divergence")
        if resource := projection.get("resource"):
            base = "beadhive://" + operation.name.replace(".", "/")
            suffix = "".join(f"/{{{name}}}" for name in projection["resource_parameters"])
            if resource != base + suffix:
                assert projection.get("divergence"), operation.name


def test_catalog_publishes_the_naming_adr_generation_rules() -> None:
    naming = document()["policy"]["naming"]
    rules = {row["convention"]: row["rule"] for row in naming["generation_rules"]}
    assert set(rules) == set(range(1, 9))
    assert "trace_verb" in rules[5]
    for target in ("names", "docstrings", "probe payloads", "test filenames"):
        assert target in rules[8]
    assert "retired workspace abbreviations" in rules[8]
    assert naming["cli"] == "singular kebab-case group/verb paths; collections use list"
    assert naming["mcp_tool"] == "group_verb for 1:1 projections"
    assert naming["mcp_resource"].startswith("beadhive://<singular-group>")
    assert naming["parameters"]["machine_output"] == {"flag": "--json", "name": "as_json"}
    assert naming["parameters"]["target_hive"]["short"] is None
    assert naming["parameters"]["row_limit"] == {
        "flag": "--limit",
        "name": "limit",
        "short": "-n",
    }
    assert naming["panels"] == [
        "Planning plane",
        "Integration plane",
        "Hive",
        "Fleet / HQ",
        "Admin / infra",
        "Passthrough",
    ]
    assert naming["hidden_groups"] == ["dolt", "harness", "otel", "wt"]
    assert naming["flag_scope"]["hive"].endswith("no short flag")
    assert naming["flag_scope"]["all"].startswith("passthrough routing")
    assert {row["name"] for row in naming["mcp_exceptions"]} == {"bd_create", "probe.health"}


def test_naming_generation_rules_cover_live_traces_and_reject_rename_residue() -> None:
    projected_names = {operation.name for operation in operations()}
    for group_name, group in (("work", work.app), ("plan", plan.app)):
        for command in group.registered_commands:
            verb = command.name or command.callback.__name__.rstrip("_")
            operation_name = f"{group_name}.{verb}"
            assert operation_name in projected_names
            assert getattr(command.callback, "__otel_verb__", None) == operation_name

    surface_values = []
    for operation in operations():
        surface_values.append(operation.name)
        if cli := operation.surfaces.get("cli"):
            surface_values.append(cli["path"])
            surface_values.extend(alias["path"] for alias in cli["aliases"])
        if mcp := operation.surfaces.get("mcp"):
            surface_values.extend(value for key in ("tool", "resource") if (value := mcp.get(key)))
    assert not {value for value in surface_values if _retired_tokens(value)}


def test_convention_8_detector_catches_the_reviewed_help_regression() -> None:
    assert _retired_tokens("that's the ws->bh rename") == {"ws"}
    assert _retired_tokens("that is the legacy command-name migration") == set()
    migrate_help = _live_cli_surface_texts()["cli:hive migrate-storage:help"]
    assert "legacy command-name migration" in migrate_help
    assert not _retired_tokens(migrate_help)


def test_convention_8_scans_every_live_description_probe_and_test_filename() -> None:
    cli_texts = _live_cli_surface_texts()
    mcp_texts, mcp_counts = _live_mcp_surface_texts()
    test_files = subprocess.run(
        ["git", "ls-files", "tests"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    filename_texts = {f"test-filename:{path}": Path(path).name for path in test_files}
    scanned = {**cli_texts, **mcp_texts, **filename_texts}

    # Core catalog and package manifests are the two explicit CLI authorities.
    assert len(cli_texts) == (219 + len(MANIFEST_CLI_COMMANDS)) * 2
    assert mcp_counts == {"tools": 10, "resources": 21, "probes": 1}
    assert len(filename_texts) == len(test_files) > 0
    assert set(RETIRED_SURFACE_EXCLUSIONS) <= set(scanned)
    assert all(reason.strip() for reason in RETIRED_SURFACE_EXCLUSIONS.values())

    findings = {
        descriptor: sorted(_retired_tokens(text))
        for descriptor, text in scanned.items()
        if descriptor not in RETIRED_SURFACE_EXCLUSIONS and _retired_tokens(text)
    }
    assert findings == {}
