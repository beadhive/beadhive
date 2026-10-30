"""``bh plugin tracker-github status`` and ``doctor``: read-only, offline, no secret rendered.

The CLI is driven through its Typer app with the config seam replaced, so no test reads the real
configuration, the real ``GITHUB_TOKEN``/``GH_TOKEN`` or ``gh auth token``. The probe path runs a
real ``TrackerAdapters`` over a ``MemoryTracker`` provider and an injected token source that
fails the test if it is called, and the package conftest already refuses every socket.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from beadhive import tracker_github_plugin as cli
from beadhive.bootstrap.tracker import TrackerAdapterProvider, TrackerAdapters
from beadhive.modules.tracker.contracts import Operation
from beadhive.plugin_runtime_catalog import PLUGIN_CLI_RUNTIME_CATALOG
from beadhive.testing.tracker import MemoryTracker, MemoryWorld

SECRET = "ghp_" + "A1b2C3d4E5" * 4
BOUND = {
    "provider": "github",
    "org": "octo",
    "repo": "widgets",
    "stores": {"public": {"source": "github-issues", "adapter": "tracker-github"}},
}
OTHER = {**BOUND, "repo": "gadgets"}


def plugin(enabled: bool = True, **extra) -> dict:
    return {
        "enabled": enabled,
        "read": {"issues": {"enabled": True}, "comments": {"enabled": True}},
        **extra,
    }


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    def no_process(*args, **kwargs):
        raise AssertionError("no process may be spawned (no `gh auth token`)")

    monkeypatch.setattr(subprocess, "run", no_process)
    monkeypatch.setattr(subprocess, "Popen", no_process)


def use_config(monkeypatch, cfg: dict) -> None:
    monkeypatch.setattr(cli, "_load_config", lambda: cfg)


def memory_adapters(cfg: dict) -> TrackerAdapters:
    world = MemoryWorld(locator="octo/widgets")

    def loader(request):
        return MemoryTracker(world, enabled=request.enabled)

    def never() -> None:
        raise AssertionError("the probe must not resolve a credential through this source")

    return TrackerAdapters(
        plugins=cfg.get("plugins"),
        plugin_kernel=cfg.get("plugin_kernel"),
        token_source=never,
        providers=(TrackerAdapterProvider("tracker-github", "beadhive[github]", loader),),
    )


def invoke(*args: str):
    return CliRunner().invoke(cli.app, list(args))


def test_the_cli_is_declared_by_the_manifest_and_mounted_from_the_runtime_catalog():
    entry = next(row for row in PLUGIN_CLI_RUNTIME_CATALOG if row.plugin_id == "tracker-github")
    assert (entry.module, entry.object_name) == ("beadhive.tracker_github_plugin", "app")
    from beadhive.kernel.plugins.builtins import builtin_manifest_documents

    manifest = next(
        json.loads(doc.payload)
        for doc in builtin_manifest_documents()
        if json.loads(doc.payload)["plugin_id"] == "tracker-github"
    )
    declared = {row["command"] for row in manifest["presentation"]["cli"]}
    assert declared == {"plugin tracker-github status", "plugin tracker-github doctor"}


def test_status_defaults_to_disabled_offline_and_names_credential_sources_only(monkeypatch):
    use_config(monkeypatch, {"managed_repos": [BOUND]})
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    result = invoke("status")
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "tracker-github  disabled"
    assert "  credential   env:GH_TOKEN unset, env:GITHUB_TOKEN set; gh not found" in lines
    assert "  identity     not probed (bh plugin tracker-github doctor --probe)" in lines
    assert "  stores       1 bound" in lines
    assert "               github/octo/widgets [public] -> octo/widgets" in lines
    assert all("probe=not-probed" in line for line in lines if "probe=" in line)
    assert sum("probe=" in line for line in lines) == 4
    assert SECRET not in result.output


def test_status_json_is_the_generic_document(monkeypatch):
    use_config(monkeypatch, {"managed_repos": [BOUND], "plugins": {"tracker-github": plugin()}})
    result = invoke("status", "--json")
    assert result.exit_code == 0
    doc = json.loads(result.output)
    assert doc["schema"] == "tracker-adapter-status/1"
    assert (doc["plugin"], doc["state"], doc["enabled"]) == ("tracker-github", "ready", True)
    assert doc["identity"] is None
    probes = {row["operation"]: row["probe"] for row in doc["operations"]}
    assert set(probes.values()) == {"not-probed"}
    assert {row["operation"]: row["enabled"] for row in doc["operations"]} == {
        "issues.read": True,
        "comments.read": True,
        "comment.create": False,
        "labels.change": False,
    }


def test_doctor_on_the_disabled_default_is_informational_and_exits_zero(monkeypatch):
    use_config(monkeypatch, {})
    result = invoke("doctor")
    assert result.exit_code == 0
    assert "info     disabled-plugin: tracker-github: disabled" in result.output


def test_doctor_fails_only_on_an_error_while_enabled(monkeypatch):
    cfg = {
        "managed_repos": [BOUND],
        "plugins": {"tracker-github": plugin(write={"comment": {"enabled": True}})},
    }
    use_config(monkeypatch, cfg)
    result = invoke("doctor", "--json")
    assert result.exit_code == 1  # a write is enabled with no credential source at all
    codes = {(row["code"], row["severity"]) for row in json.loads(result.output)["diagnostics"]}
    assert ("tracker-credential", "error") in codes


def test_the_default_verbs_never_build_an_adapter_or_resolve_a_credential(monkeypatch):
    use_config(monkeypatch, {"managed_repos": [BOUND], "plugins": {"tracker-github": plugin()}})

    def forbidden(_cfg):
        raise AssertionError("status and doctor without --probe must build no adapter")

    monkeypatch.setattr(cli, "_build_adapters", forbidden)
    assert invoke("status").exit_code == 0
    assert invoke("doctor").exit_code == 0


def test_probe_renders_identity_and_the_tri_state_through_the_bound_port(monkeypatch):
    cfg = {"managed_repos": [BOUND], "plugins": {"tracker-github": plugin()}}
    use_config(monkeypatch, cfg)
    monkeypatch.setattr(cli, "_build_adapters", memory_adapters)
    result = invoke("doctor", "--probe", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["identity"]["kind"] == "human"
    assert doc["error"] is None
    by_op = {row["operation"]: row for row in doc["operations"]}
    assert by_op["issues.read"]["probe"] in {"yes", "no", "unknown"}
    assert by_op["issues.read"]["enabled"] is True
    assert by_op["comment.create"]["enabled"] is False
    text = invoke("doctor", "--probe").output
    assert "identity     id=" in text
    assert "not probed" not in text


def test_probe_on_a_disabled_plugin_is_refused_before_any_credential_read(monkeypatch):
    use_config(monkeypatch, {"managed_repos": [BOUND]})
    monkeypatch.setattr(cli, "_build_adapters", memory_adapters)
    result = invoke("doctor", "--probe", "--json")
    assert result.exit_code == 0  # a disabled plugin never fails a gate
    doc = json.loads(result.output)
    assert doc["state"] == "disabled" and doc["identity"] is None
    assert "tracker-github" in doc["error"] and "enabled" in doc["error"]


def test_probe_with_no_bound_store_or_an_ambiguous_one_says_so(monkeypatch):
    cfg = {"plugins": {"tracker-github": plugin()}}
    use_config(monkeypatch, {**cfg, "managed_repos": []})
    monkeypatch.setattr(cli, "_build_adapters", memory_adapters)
    assert "none found" in json.loads(invoke("doctor", "--probe", "--json").output)["error"]

    use_config(monkeypatch, {**cfg, "managed_repos": [BOUND, OTHER]})
    error = json.loads(invoke("doctor", "--probe", "--json").output)["error"]
    assert "github/octo/gadgets, github/octo/widgets" in error and "pass --hive" in error

    picked = json.loads(
        invoke("doctor", "--probe", "--hive", "github/octo/widgets", "--json").output
    )
    assert picked["error"] is None and picked["identity"] is not None
    missing = json.loads(invoke("doctor", "--probe", "--hive", "github/x/y", "--json").output)
    assert "none found" in missing["error"]


def test_the_probe_credential_chain_follows_the_configured_source_names(monkeypatch):
    seen: list[tuple[str, ...]] = []
    from beadhive import run

    monkeypatch.setattr(run, "github_token_source", lambda names: seen.append(tuple(names)) or None)
    cfg = {"plugins": {"tracker-github": plugin(credential={"sources": ["env"]})}}
    assert isinstance(cli._build_adapters(cfg), TrackerAdapters)
    cfg = {"plugins": {"tracker-github": plugin(credential={"sources": []})}}
    cli._build_adapters(cfg)
    assert seen == [("env",), ()]


def test_no_output_form_contains_a_planted_token(monkeypatch):
    cfg = {"managed_repos": [BOUND], "plugins": {"tracker-github": plugin()}}
    use_config(monkeypatch, cfg)
    monkeypatch.setattr(cli, "_build_adapters", memory_adapters)
    monkeypatch.setenv("GITHUB_TOKEN", SECRET)
    monkeypatch.setenv("GH_TOKEN", SECRET[::-1])
    outputs = [
        invoke(*args).output
        for args in (
            ("status",),
            ("status", "--json"),
            ("doctor",),
            ("doctor", "--json"),
            ("doctor", "--probe"),
            ("doctor", "--probe", "--json"),
        )
    ]
    assert all(outputs)
    for text in outputs:
        assert SECRET not in text and SECRET[::-1] not in text
    assert "both set and differ" in outputs[0]


def test_no_write_operation_is_requested_by_the_probe(monkeypatch):
    requested: list[frozenset] = []
    cfg = {"managed_repos": [BOUND], "plugins": {"tracker-github": plugin()}}
    use_config(monkeypatch, cfg)

    def adapters(c):
        real = memory_adapters(c)
        original = real.for_store
        real.for_store = lambda entry, *a, **k: (
            requested.append(k.get("requested", frozenset())) or original(entry, *a, **k)
        )
        return real

    monkeypatch.setattr(cli, "_build_adapters", adapters)
    assert invoke("doctor", "--probe").exit_code == 0
    assert requested == [frozenset()]
    assert Operation.COMMENT_CREATE not in requested[0]
