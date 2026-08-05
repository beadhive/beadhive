"""flake.nix's toolchain vs `deps.py` — the drift gate the `# PROBE_TABLE` comments only
pretended to be (bh-hsus.2 Q4).

`flake.nix` is the LOCAL-INSTALL toolchain: on a Linux host nobody is sitting at, it is what
puts every unconditionally-required tool on the `PATH` that `shutil.which()` searches. A dep
added to `deps.py` and forgotten here fails at provisioning time as `✗ missing: <name>` — loud,
but only for whoever runs the install next.

Deliberately NOT codegen (see `flake.nix`'s own comment): deriving the list works, but trades a
hand-mirrored flake for a hand-mirrored generated file plus a codegen step, and the
name → nix-attribute map stays manual either way. So: keep the hand-written list, and make
drift a red test. Pure Python — this needs no `nix`, and so runs on macOS too.
"""

from __future__ import annotations

import re
from pathlib import Path

from beadhive import deps

FLAKE = Path(__file__).resolve().parent.parent / "flake.nix"

#: dep name -> the nix attribute or override that supplies it. Hand-written on purpose: `bd`
#: is a `beadsHead pkgs` override carrying its own rev/hash, not `pkgs.bd`, so no derivation
#: reproduces this map.
NIX_ATTR: dict[str, str] = {
    "git-workspace": "pkgs.git-workspace",
    "gh": "pkgs.gh",
    "bd": "(beadsHead pkgs)",
    "dolt": "pkgs.dolt",
}


def _toolchain_block() -> str:
    """The body of `toolchainFor`, comments stripped — the list of packages it actually supplies."""
    text = FLAKE.read_text()
    match = re.search(r"toolchainFor = pkgs: \[(.*?)\n      \];", text, re.DOTALL)
    assert match, "could not locate `toolchainFor` in flake.nix"
    return "\n".join(line.split("#")[0] for line in match.group(1).splitlines())


def test_every_always_required_dep_is_supplied_by_the_flake():
    block = _toolchain_block()
    for dep in deps.always_required():
        attr = NIX_ATTR.get(dep.name)
        assert attr, (
            f"{dep.name} is required always but has no nix attribute mapped here — add it to "
            f"flake.nix's toolchainFor and to NIX_ATTR, or it is invisible on a Linux host."
        )
        assert attr in block, f"flake.nix's toolchainFor does not supply {dep.name} ({attr})"


def test_the_attribute_map_covers_exactly_the_always_required_rows():
    """Both directions: a row dropped from `deps.py` should not leave a stale mapping behind."""
    assert set(NIX_ATTR) == {d.name for d in deps.always_required()}


def test_the_flake_points_at_deps_py_rather_than_annotating_each_line():
    """The per-package `# PROBE_TABLE` annotations were the eighth hand-mirrored registry, and
    they now name a DERIVATION rather than a source. One pointer to `deps.py` replaces them."""
    text = FLAKE.read_text()
    assert "deps.py" in text
    match = re.search(r"toolchainFor = pkgs: \[(.*?)\n      \];", text, re.DOTALL)
    assert match
    assert "PROBE_TABLE" not in match.group(1)


def test_no_group_member_is_supplied_by_the_flake():
    """A container runtime is an operator-supplied prerequisite (bh-q160.1) and an agent harness
    is a licence decision (`harness.py`) — neither belongs in the local-install toolchain, and
    both would be quietly wrong rather than loudly wrong."""
    block = _toolchain_block()
    for group in deps.GROUPS:
        for dep in deps.group_members(group):
            assert f"pkgs.{dep.name}" not in block, f"{dep.name} must not be in toolchainFor"
