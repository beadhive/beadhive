"""Release channels refuse tags with an obsolete or ambiguous Beads toolchain."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-channel-toolchain.py"
SPEC = importlib.util.spec_from_file_location("channel_toolchain_policy", SCRIPT)
assert SPEC and SPEC.loader
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


def _candidate(tmp_path: Path, *, metadata: object, flake_version: str = "1.3.0") -> Path:
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / "toolchain-metadata.json").write_text(json.dumps(metadata))
    (tmp_path / "flake.nix").write_text(
        'beadsReleaseAssets = { x86_64-linux.asset = "beads_'
        f'{flake_version}_linux_amd64.tar.gz"; }};\n'
        "beadsRelease = pkgs: let release = {}; in pkgs.stdenvNoCC.mkDerivation {\n"
        '  pname = "beads";\n'
        f'  version = "{flake_version}";\n'
        '  src = pkgs.fetchurl { url = "https://github.com/gastownhall/beads/releases/download/'
        f'v{flake_version}/${{release.asset}}"; }};\n'
        "};\n"
        'doltReleaseCommit = "unused";\n'
        "toolchainFor = pkgs: [\n"
        "  (beadsRelease pkgs)\n"
        "];\n"
    )
    return tmp_path


def test_current_release_candidate_is_channel_eligible() -> None:
    assert POLICY.check(ROOT) == "1.3.0"


@pytest.mark.parametrize("version", ["1.2.99", "1.3.0-rc.1", "dev", ""])
def test_old_prerelease_or_malformed_beads_is_refused(tmp_path: Path, version: str) -> None:
    root = _candidate(tmp_path, metadata=[{"name": "bd", "package": "beads", "version": version}])
    with pytest.raises(ValueError):
        POLICY.check(root)


def test_metadata_must_agree_with_the_flake(tmp_path: Path) -> None:
    root = _candidate(
        tmp_path,
        metadata=[{"name": "bd", "package": "beads", "version": "1.3.1"}],
        flake_version="1.3.0",
    )
    with pytest.raises(ValueError, match="disagrees"):
        POLICY.check(root)


def test_artifact_and_toolchain_are_bound_to_the_qualified_derivation(tmp_path: Path) -> None:
    root = _candidate(
        tmp_path,
        metadata=[{"name": "bd", "package": "beads", "version": "1.3.0"}],
    )
    flake = (root / "flake.nix").read_text()
    (root / "flake.nix").write_text(flake.replace("beads_1.3.0_", "beads_1.2.0_"))
    with pytest.raises(ValueError, match="assets"):
        POLICY.check(root)

    (root / "flake.nix").write_text(flake.replace("(beadsRelease pkgs)", "pkgs.beads"))
    with pytest.raises(ValueError, match="toolchain"):
        POLICY.check(root)


def test_comments_and_strings_cannot_spoof_structural_binding(tmp_path: Path) -> None:
    root = _candidate(
        tmp_path,
        metadata=[{"name": "bd", "package": "beads", "version": "1.3.0"}],
    )
    valid = (root / "flake.nix").read_text()
    expected_url = (
        'url = "https://github.com/gastownhall/beads/releases/download/v1.3.0/${release.asset}";'
    )
    stale_url = expected_url.replace("v1.3.0", "v1.2.0")
    (root / "flake.nix").write_text(valid.replace(expected_url, f"{stale_url} # {expected_url}"))
    with pytest.raises(ValueError, match="URL"):
        POLICY.check(root)

    active_asset = 'asset = "beads_1.3.0_linux_amd64.tar.gz";'
    stale_asset = 'asset = "beads_1.2.0_linux_amd64.tar.gz";'
    (root / "flake.nix").write_text(
        valid.replace(active_asset, f"{stale_asset} /* {active_asset} */")
    )
    with pytest.raises(ValueError, match="assets"):
        POLICY.check(root)

    (root / "flake.nix").write_text(
        valid.replace("(beadsRelease pkgs)", "pkgs.beads # (beadsRelease pkgs)")
    )
    with pytest.raises(ValueError, match="toolchain"):
        POLICY.check(root)


def test_ref_reads_the_tag_tree_instead_of_the_working_tree(tmp_path: Path) -> None:
    root = _candidate(
        tmp_path,
        metadata=[{"name": "bd", "package": "beads", "version": "1.3.0"}],
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "add", "flake.nix", "docker/toolchain-metadata.json"], check=True
    )
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "eligible"], check=True)
    subprocess.run(["git", "-C", str(root), "tag", "eligible"], check=True)
    (root / "docker" / "toolchain-metadata.json").write_text("[]")

    assert POLICY.check(root, "eligible") == "1.3.0"


@pytest.mark.parametrize(
    "metadata",
    [[], [{"name": "dolt", "package": "dolt", "version": "2.3.5"}]],
)
def test_missing_beads_row_is_refused(tmp_path: Path, metadata: object) -> None:
    root = _candidate(tmp_path, metadata=metadata)
    with pytest.raises(ValueError, match="exactly one bd"):
        POLICY.check(root)


def test_tagged_nix_bh_must_exist_and_match_project_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "package"
    (package / "bin").mkdir(parents=True)
    for name in ("bh", "bh-host-daemon", "beadhive-frame-bridge"):
        (package / "bin" / name).touch()
    monkeypatch.setattr(POLICY, "_read", lambda root, ref, path: '[project]\nversion = "0.20.2"')
    observed: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append(argv)
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\n", "")
        if argv[0] == "nix":
            return subprocess.CompletedProcess(argv, 0, str(package) + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "0.20.2\n", "")

    monkeypatch.setattr(POLICY.subprocess, "run", run)
    assert POLICY.verify_bh(tmp_path, "v0.20.2") == "0.20.2"
    assert observed[1][0:4] == ["nix", "build", "--no-link", "--print-out-paths"]
    assert observed[1][-1].endswith("?rev=" + "a" * 40 + "#bh")

    (package / "bin" / "bh-host-daemon").unlink()
    with pytest.raises(ValueError, match="omits bh-host-daemon"):
        POLICY.verify_bh(tmp_path, "v0.20.2")
    (package / "bin" / "bh-host-daemon").touch()

    def wrong_version(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        result = run(argv, **kwargs)
        if argv[0] == str(package / "bin" / "bh"):
            return subprocess.CompletedProcess(argv, 0, "0.20.1\n", "")
        return result

    monkeypatch.setattr(POLICY.subprocess, "run", wrong_version)
    with pytest.raises(ValueError, match="reports '0.20.1'"):
        POLICY.verify_bh(tmp_path, "v0.20.2")
