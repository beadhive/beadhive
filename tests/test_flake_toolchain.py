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

import json
import re
from pathlib import Path

from beadhive import deps

FLAKE = Path(__file__).resolve().parent.parent / "flake.nix"
METADATA = FLAKE.parent / "docker" / "toolchain-metadata.json"

#: dep name -> the nix attribute or override that supplies it. Hand-written on purpose: `bd`
#: is a `beadsRelease pkgs` archive derivation, not `pkgs.bd`, so no derivation
#: reproduces this map.
NIX_ATTR: dict[str, str] = {
    "git-workspace": "pkgs.git-workspace",
    "gh": "pkgs.gh",
    "bd": "(beadsRelease pkgs)",
    "dolt": "(doltRelease pkgs)",
    # `ps` (bh-x2yy0). Present in `toolchainFor` because it is a HOST requirement, and filtered
    # back out of `imageToolchainFor` for exactly git's reason: GPL, and the image's apt layer
    # already supplies it. See the delta comment block in flake.nix.
    "procps": "pkgs.procps",
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


def test_beads_is_the_final_immutable_130_release_archive():
    text = FLAKE.read_text()
    assert 'version = "1.3.0";' in text
    assert 'beadsReleaseCommit = "f45b249ce6b40ba62aecc03949e6371e8f7c79d8";' in text
    assert 'asset = "beads_1.3.0_linux_amd64.tar.gz";' in text
    assert 'hash = "sha256-L5K5BOzzW2B+RNxcOSKRc69pxU8Rg+jXCfF3NUDNzzs=";' in text
    assert "github.com/gastownhall/beads/releases/download/v1.3.0/" in text
    assert "fetchFromGitHub" not in text
    assert "vendorHash" not in text
    assert "1.3.0-rc" not in text
    assert "beadsRc" not in text
    for asset, hash_ in {
        "beads_1.3.0_linux_amd64.tar.gz": "sha256-L5K5BOzzW2B+RNxcOSKRc69pxU8Rg+jXCfF3NUDNzzs=",
        "beads_1.3.0_linux_arm64.tar.gz": "sha256-TOlEamjtwTICt2yEpH1m+z2tJ4e2RkyVIqEI+vnBxgg=",
        "beads_1.3.0_darwin_arm64.tar.gz": "sha256-fMdzZ9C4TFAkOhEIvB9zZIaZIRJX1BS5F1QL+Gjmu4U=",
    }.items():
        assert f'asset = "{asset}";' in text
        assert f'hash = "{hash_}";' in text


def test_dolt_is_the_immutable_235_release_archive():
    text = FLAKE.read_text()
    assert 'version = "2.3.5";' in text
    assert 'doltReleaseCommit = "ad65af6cc937d10fa3c88e2041fed4325968b581";' in text
    assert 'asset = "dolt-linux-amd64.tar.gz";' in text
    assert 'hash = "sha256-xJ1MPgBM8VgboNSgDFAjom+E6y7BXV/odu7TbVND9GM=";' in text
    assert "github.com/dolthub/dolt/releases/download/v2.3.5/" in text
    assert "dolt231" not in text
    assert "2.3.1" not in text
    for asset, hash_ in {
        "dolt-linux-amd64.tar.gz": "sha256-xJ1MPgBM8VgboNSgDFAjom+E6y7BXV/odu7TbVND9GM=",
        "dolt-linux-arm64.tar.gz": "sha256-nOcPyB5QE56XdY739NxX6Vg+Tl7wWtdddTXDDKoWE4c=",
        "dolt-darwin-arm64.tar.gz": "sha256-rR43cKzLt+igWQaSKOrSK99vJ1kTEhJFG0T1GGYbjkA=",
    }.items():
        assert f'asset = "{asset}";' in text
        assert f'hash = "{hash_}";' in text


def test_toolchain_uses_release_asset_derivations_and_metadata_matches():
    text = FLAKE.read_text()
    assert text.count("pkgs.stdenvNoCC.mkDerivation {") >= 2
    assert text.count("src = pkgs.fetchurl {") >= 2
    assert "pkgs.beads.overrideAttrs" not in text
    assert "pkgs.dolt.overrideAttrs" not in text

    rows = {row["name"]: row for row in json.loads(METADATA.read_text())}
    assert rows["bd"]["package"] == "beads"
    assert rows["bd"]["version"] == "1.3.0"
    assert rows["dolt"]["package"] == "dolt"
    assert rows["dolt"]["version"] == "2.3.5"


def test_no_group_member_is_supplied_by_the_flake():
    """A container runtime is an operator-supplied prerequisite (bh-q160.1) and an agent harness
    is a licence decision (`harness.py`) — neither belongs in the local-install toolchain, and
    both would be quietly wrong rather than loudly wrong."""
    block = _toolchain_block()
    for group in deps.GROUPS:
        for dep in deps.group_members(group):
            assert f"pkgs.{dep.name}" not in block, f"{dep.name} must not be in toolchainFor"


def test_the_justfile_and_bh_agree_on_the_toolchain_env_name():
    """TWO ENTRY POINTS, ONE DERIVATION (bh-vmdq.7). `just local-install` step 1 installs the
    toolchain from a CHECKOUT (`.#default`); `bh setup toolchain` installs it from a TAG ref for
    a machine with no checkout. They cannot share code — step 1 runs before step 2 installs bh,
    so the justfile cannot call bh — but they must agree on the buildEnv name, which is the
    idempotence probe BOTH use to decide "already installed".

    A drift gate rather than a comment asking people to remember, matching the argument this
    file's other tests already make."""
    from pathlib import Path

    from beadhive import setup

    justfile = Path(__file__).parents[1] / "justfile"
    assert setup.TOOLCHAIN_ENV_NAME in justfile.read_text(), (
        f"justfile's step-1 guard no longer greps {setup.TOOLCHAIN_ENV_NAME!r} — "
        "the two provisioning paths would disagree about what 'already installed' means"
    )


def test_the_toolchain_flake_ref_is_a_tag_and_carries_no_version_literal():
    """The version is DERIVED from the installed package, never typed here (bh-hqtt). A tag ref,
    not a branch ref: `github:owner/repo` resolves the default branch, measured 31 commits stale
    on 2026-08-06, which would install a toolchain the running bh does not match."""
    from beadhive import setup

    ref = setup.toolchain_flake_ref(version="9.9.9")
    assert ref == "github:beadhive/beadhive/v9.9.9#default"
    assert "#default" in ref

    src = (__import__("pathlib").Path(setup.__file__)).read_text()
    assert "/v0.8.0#default" not in src, "a hardcoded version would be a second place to be wrong"
