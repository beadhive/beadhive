"""INSTALL.md must UPGRADE a machine that already has `bh`, not silently no-op (bh-6x5xj).

Measured on macOS 2026-08-06, minutes after the v0.8.0 release: `uv tool install
'beadhive[otel]'` run verbatim on a box carrying 0.7.1 printed "Installed 2 executables: bh,
bh-mcp", exited 0, and left `bh --version` at 0.7.1. Only `--force` produced 0.8.0. The control
case — the same command on a host with no bh at all — installed 0.8.0 correctly, so the defect
is exclusively the already-installed case, which is precisely the population the managed path's
upgrade posture targets.

The failure shape is the bad one: a command that succeeds, appears to complete the migration,
and leaves a version-skewed machine. It is INVISIBLE without checking the version afterwards —
which is why it needs a test rather than a careful reader.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

INSTALL_MD = Path(__file__).resolve().parents[1] / "INSTALL.md"

# installer -> the flag that makes it replace an existing install instead of no-opping.
UPGRADE_SAFE = {
    "uv tool install": "--force",
    "pipx install": "--force",
    "pip install": "--upgrade",
}


def _frontmatter() -> dict:
    text = INSTALL_MD.read_text()
    assert text.startswith("---\n"), "INSTALL.md must open with YAML frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


def _install_commands() -> list[str]:
    """Every command string in the `install:` methods, plus every shell line in the prose —
    the two routes a reader can take, which bh-vmdq.2 requires to reach the same posture."""
    cmds = [str(m.get("command", "")) for m in _frontmatter()["install"]["methods"]]
    for block in re.findall(r"```sh\n(.*?)```", INSTALL_MD.read_text(), re.S):
        cmds.extend(line.split("#")[0].strip() for line in block.splitlines())
    return [c for c in cmds if c]


@pytest.mark.parametrize("installer,flag", sorted(UPGRADE_SAFE.items()))
def test_every_install_command_replaces_an_existing_bh(installer, flag):
    offenders = [
        c for c in _install_commands() if installer in c and flag not in c and "bh " not in c
    ]
    assert not offenders, (
        f"`{installer}` without `{flag}` no-ops on a machine that already has bh and still "
        f"exits 0, leaving it version-skewed (bh-6x5xj): {offenders}"
    )


def test_the_prose_tells_the_reader_to_verify_the_version_not_the_exit_code():
    """The acceptance bar is "ends on the released version, verified by `bh --version` rather
    than by the command exiting 0" — so the prose has to actually say to run it."""
    body = INSTALL_MD.read_text().split("---\n", 2)[2]
    assert body.count("bh --version") >= 2, "each install route must end in a version check"


def test_frontmatter_and_prose_do_not_disagree_about_the_uv_command():
    """bh-vmdq.2's requirement: an agent following the frontmatter and a human following the
    prose reach the SAME posture. They disagreed here — `upgrade: ask` offered the upgrade
    while the prose named a command that could not perform one."""
    fm = [c for c in (str(m.get("command", "")) for m in _frontmatter()["install"]["methods"])]
    assert any("uv tool install --force" in c for c in fm)
    body = INSTALL_MD.read_text().split("---\n", 2)[2]
    assert "uv tool install --force" in body
    assert re.search(r"uv tool install(?! --force)", body) is None
