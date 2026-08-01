"""Guards on the container build definition (docker-bake.hcl + docker/Dockerfile).

Building the image needs a docker daemon, so the build itself is proved by bh-pc2a.17's
local bake, not by this suite. What IS cheap to hold still are the contracts the build
definition makes, and each of these has a failure mode that only shows up at bake time:

- docker-bake.hcl is the SINGLE pin source: no version literal in the Dockerfile, and every
  ARG the Dockerfile consumes is fed from a target's args (so `--set <t>.args.<N>` lands)
- both platforms are declared on the shared target, so no caller passes a platform flag
- `agent` inherits `core` rather than duplicating shared config, and `default` builds both
- the image runs as non-root `bee` — both harnesses refuse bypass-permission mode as root
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BAKE = (ROOT / "docker-bake.hcl").read_text()
DOCKERFILE = (ROOT / "docker" / "Dockerfile").read_text()

# Provided by BuildKit, never declared in the bake file.
BUILTIN_ARGS = {"TARGETARCH", "TARGETOS", "TARGETPLATFORM", "TARGETVARIANT", "BUILDPLATFORM"}


def _block(text: str, opener: str) -> str:
    """The brace-balanced body that follows ``opener`` in ``text``."""
    start = text.index(opener) + len(opener)
    depth, i = 1, start
    while depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start : i - 1]


def _target_args(target: str) -> set[str]:
    body = _block(BAKE, f'target "{target}" {{')
    return set(re.findall(r"^\s*([A-Z0-9_]+)\s*=", _block(body, "args = {"), re.M))


def test_no_version_literal_in_the_dockerfile():
    """Every pin originates in an HCL variable — a literal here would be a second pin source."""
    literals = re.findall(r"\b\d+\.\d+[\w.]*", DOCKERFILE)
    assert literals == []


def test_every_dockerfile_arg_is_fed_from_a_bake_target():
    """An ARG with no target arg silently builds empty; this is that failure, at test time."""
    declared = set(re.findall(r"^ARG\s+([A-Z0-9_]+)", DOCKERFILE, re.M)) - BUILTIN_ARGS
    assert declared <= _target_args("core") | _target_args("agent")


def test_every_hcl_variable_is_referenced_by_a_target():
    """A pin that reaches no target arg is unreachable by `--set <target>.args.<NAME>`."""
    variables = set(re.findall(r'variable\s+"([A-Z0-9_]+)"', BAKE))
    reachable = _target_args("core") | _target_args("agent") | {"REGISTRY", "TAG"}
    assert variables <= reachable


def test_both_platforms_are_declared_on_the_shared_target():
    """Cross-platform is a property of the definition, so no caller passes --platform."""
    core = _block(BAKE, 'target "core" {')
    assert re.search(r'platforms\s*=\s*\["linux/amd64",\s*"linux/arm64"\]', core)


def test_agent_inherits_core():
    """Shared context/dockerfile/platforms/labels/args are declared exactly once."""
    assert re.search(r'inherits\s*=\s*\["core"\]', _block(BAKE, 'target "agent" {'))


def test_default_group_builds_both_targets():
    group = _block(BAKE, 'group "default" {')
    assert set(re.findall(r'"(\w+)"', group)) == {"core", "agent"}


@pytest.mark.parametrize("stage", ["core", "agent"])
def test_stage_ends_as_non_root_bee(stage):
    """Claude Code and the Codex CLI both refuse their in-container bypass mode as root."""
    body = DOCKERFILE.split(f"AS {stage}\n", 1)[1].split("\n# ---- ", 1)[0]
    assert body.rstrip().splitlines()[-3:].count("USER bee") == 1


def test_managed_settings_pins_the_harness():
    """Fleet policy baked at the highest-precedence settings path keeps a pinned CLI pinned."""
    settings = json.loads((ROOT / "docker" / "managed-settings.json").read_text())
    assert settings["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert "DISABLE_AUTOUPDATER=1" in DOCKERFILE
    assert "/etc/claude-code/managed-settings.json" in DOCKERFILE
