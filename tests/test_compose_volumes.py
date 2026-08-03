"""Guards on the compose topology (docker-compose.yml + docker/Dockerfile, together).

`tests/test_image_build.py` holds the build definition still on its own. What THIS file holds
still is the seam BETWEEN the two files, which no single-file test can see and which fails
silently rather than loudly:

A named volume mounted at a path the image already contains inherits that directory's
ownership. A named volume mounted at a path the image does NOT contain gets a mount point
Docker creates as root — the agent user then cannot write to it, and the container comes up
looking healthy while being inert. So every mount target in the compose file must be created,
owned by the agent user, in the Dockerfile.

The runtime user is a build arg (AGENT_USER), which gives that invariant a second half: both
files must DERIVE the home-relative paths from it. Writing `/home/bee` in either one restores
exactly the root-owned-mount-point failure for anyone who overrides AGENT_USER — through a
path no default-user test would ever exercise.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
DOCKERFILE = (ROOT / "docker" / "Dockerfile").read_text()

# The two files spell the same substitution differently — compose interpolates its own `.env`
# (`${BH_AGENT_USER:-bee}`), the Dockerfile takes a build arg (`${AGENT_USER}`). Normalize both
# to one token so the paths can be compared as paths.
AGENT_HOME = "/home/<agent>"


def _normalize(path: str) -> str:
    return re.sub(r"/home/\$\{(?:BH_)?AGENT_USER(?::-\w+)?\}", AGENT_HOME, path)


def _compose_mount_targets() -> set[str]:
    """Container-side target of every volume the `bh` service mounts.

    Split on the first `:` that begins an absolute path, not on the first `:` — both halves of
    a short-syntax mount carry `${VAR:-default}` colons of their own.
    """
    return {_normalize(re.search(r":(/.*)$", m)[1]) for m in COMPOSE["services"]["bh"]["volumes"]}


def _dockerfile_prepared_dirs() -> set[str]:
    """Directories the image creates owned by the agent user, ready to be mounted over."""
    install = re.search(
        r"install -d -o \"\$\{AGENT_UID\}\" -g \"\$\{AGENT_GID\}\"(.*?)\nUSER ",
        DOCKERFILE,
        re.S,
    )
    assert install, "the `install -d` that pre-creates the mount points has moved or changed"
    return {_normalize(p.strip().strip('"')) for p in install[1].split("\\") if p.strip()}


def test_every_compose_mount_point_is_pre_created_in_the_image():
    """Otherwise Docker creates it root-owned and the agent cannot write to its own home."""
    assert _compose_mount_targets() == _dockerfile_prepared_dirs()


def test_the_four_split_areas_are_exactly_what_is_mounted():
    """The split is the feature: four areas, so four volumes and no fifth smuggled in."""
    assert _compose_mount_targets() == {
        f"{AGENT_HOME}/.beadhive",
        f"{AGENT_HOME}/.claude",
        "/workspace",
        "/worktrees",
    }


def test_no_literal_home_survives_in_either_file():
    """A hardcoded /home/bee silently strands every AGENT_USER-overridden build."""
    assert "/home/bee" not in re.sub(r"^\s*#.*$", "", COMPOSE_TEXT, flags=re.M)
    assert "/home/bee" not in re.sub(r"^\s*#.*$", "", DOCKERFILE, flags=re.M)


def test_bh_reads_each_area_from_the_env_var_that_names_it():
    """The whole split is these five variables — bh already honors them, so no bh code change.

    Each must point AT its mount, not merely near it: BH_HQ nests inside BH_HOME's volume by
    design, and CLAUDE_CONFIG_DIR must equal the harness mount or ~/.claude.json lands outside
    the volume and sign-in does not survive a recreate.
    """
    env = COMPOSE["services"]["bh"]["environment"]
    assert _normalize(env["BH_HOME"]) == f"{AGENT_HOME}/.beadhive"
    assert _normalize(env["BH_HQ"]).startswith(f"{AGENT_HOME}/.beadhive/")
    assert _normalize(env["CLAUDE_CONFIG_DIR"]) == f"{AGENT_HOME}/.claude"
    assert env["GIT_WORKSPACE"] == "/workspace"
    assert env["BH_WORKTREES"] == "/worktrees"


def test_codex_home_lands_inside_the_harness_volume():
    """Codex defaults to ~/.codex, which is NOT on any volume — a rebuild would silently log
    you out of Codex while Claude, on the harness volume, stayed signed in. The asymmetry is
    the bug; CODEX_HOME must nest inside the harness mount for auth.json to survive."""
    env = COMPOSE["services"]["bh"]["environment"]
    assert _normalize(env["CODEX_HOME"]).startswith(f"{AGENT_HOME}/.claude/")


def test_gh_config_dir_lands_inside_the_harness_volume():
    """gh had the identical defect: it writes credentials to GH_CONFIG_DIR (default
    ~/.config/gh/hosts.yml), on no volume — so `gh auth login` in the container was silently
    lost on rebuild while Claude and Codex stayed signed in. All three must persist alike."""
    env = COMPOSE["services"]["bh"]["environment"]
    assert _normalize(env["GH_CONFIG_DIR"]).startswith(f"{AGENT_HOME}/.claude/")


def test_headless_tokens_are_passed_through_and_never_defaulted():
    """A factory host with no browser supplies one of these instead of an interactive login.
    They must be PASSTHROUGH with an empty default: a literal value here would bake a
    credential into the compose file, and no default at all would break interactive hosts."""
    env = COMPOSE["services"]["bh"]["environment"]
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN"):
        assert var in env, f"{var} must be declared for the headless path"
        # This file is read UNINTERPOLATED, so the value must literally be the passthrough
        # form. Asserting the exact string catches both failure modes at once: a hardcoded
        # secret, and a missing `:-` default (which would make compose warn and blank it on
        # every interactive host that has not set the var).
        assert env[var] == f"${{{var}:-}}", f"{var} must be `${{{var}:-}}` passthrough"


def test_the_docker_socket_is_never_mounted():
    """Mounting it hands the container host root; the sibling stacks are separate projects."""
    assert "docker.sock" not in str(COMPOSE["services"]["bh"]["volumes"])
