from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRANSACTION_SCRIPTS = (
    "next-version.sh",
    "prepare-release-version.sh",
    "refresh_modularization_closeout.py",
    "release_transaction.py",
)


def _git(repo: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def _must_git(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@dataclass
class ReleaseRepo:
    root: Path
    environment: dict[str, str]
    start: str
    key: Path

    def transaction(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/release_transaction.py", *args],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            check=False,
        )


def _release_repo(tmp_path: Path, *, generator_fails: bool = False) -> ReleaseRepo:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    proof = repo / "docs/proof"
    binary = tmp_path / "bin"
    scripts.mkdir(parents=True)
    proof.mkdir(parents=True)
    binary.mkdir()
    for name in TRANSACTION_SCRIPTS:
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    if generator_fails:
        prepare = scripts / "prepare-release-version.sh"
        prepare.write_text(prepare.read_text() + "exit 42\n")

    key = tmp_path / "release-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    public = key.with_suffix(".pub").read_text().strip()
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"release@example.com {public}\n")

    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        """[project]
name = "release-fixture"
version = "0.16.1"

[tool.commitizen]
name = "cz_conventional_commits"
version_provider = "pep621"
version_scheme = "pep440"
tag_format = "v$version"
annotated_tag = true
pre_bump_hooks = ["scripts/prepare-release-version.sh $CZ_PRE_NEW_VERSION"]
"""
    )
    (repo / "uv.lock").write_text('version = "0.16.1"\n')
    (repo / "CHANGELOG.md").write_text("# Changelog\n")
    report = proof / "bh-j5uyb.1-modularization-closeout.json"
    report.write_text(
        json.dumps(
            {
                "evidence_inventory": {
                    "current_candidate": {
                        "package": {"path": "pyproject.toml", "sha256": "0" * 64}
                    }
                }
            },
            indent=2,
        )
        + "\n"
    )

    uv = binary / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'test "$1" = version && test "$2" = --no-sync || exit 64\n'
        "python3 - \"$3\" <<'PY'\n"
        "from pathlib import Path\n"
        "import sys\n"
        "version = sys.argv[1]\n"
        "for name in ('pyproject.toml', 'uv.lock'):\n"
        "    path = Path(name)\n"
        "    path.write_text(path.read_text().replace('0.16.1', version))\n"
        "PY\n"
    )
    uv.chmod(0o755)
    bh = binary / "bh"
    bh.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$BH_LOG\"\nexit ${BH_RC:-0}\n")
    bh.chmod(0o755)
    (binary / "python3").symlink_to(sys.executable)

    _must_git(repo, "init", "-q", "-b", "main")
    for key_name, value in (
        ("user.name", "Release Test"),
        ("user.email", "release@example.com"),
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key.with_suffix(".pub"))),
        ("gpg.ssh.allowedSignersFile", str(allowed)),
        ("commit.gpgsign", "true"),
        ("tag.gpgsign", "false"),
    ):
        _must_git(repo, "config", key_name, value)
    subprocess.run(
        [sys.executable, "scripts/refresh_modularization_closeout.py", "--write"],
        cwd=repo,
        check=True,
    )
    _must_git(repo, "add", "-A")
    _must_git(repo, "commit", "-qm", "chore: seed release fixture")
    _must_git(repo, "tag", "-a", "v0.16.1", "-m", "v0.16.1")
    (repo / "change.txt").write_text("fix\n")
    _must_git(repo, "add", "change.txt")
    _must_git(repo, "commit", "-qm", "fix: prepare patch release")
    start = _must_git(repo, "rev-parse", "HEAD")

    changelog_tool = Path(sys.executable).with_name("cz")
    assert changelog_tool.is_file()
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "BH_EXEC": str(bh),
        "BH_LOG": str(tmp_path / "bh.log"),
        "CZ_EXEC": str(changelog_tool),
        "UV_EXEC": str(uv),
    }
    return ReleaseRepo(repo, environment, start, key)


def test_bump_creates_one_exact_signed_commit_and_tag_with_refreshed_proof(tmp_path: Path) -> None:
    release_repo = _release_repo(tmp_path)

    result = release_repo.transaction("bump", "0.16.2")

    assert result.returncode == 0, result.stderr
    head = _must_git(release_repo.root, "rev-parse", "HEAD")
    assert _must_git(release_repo.root, "show", "-s", "--format=%P", head) == release_repo.start
    assert set(
        _must_git(
            release_repo.root, "diff-tree", "--no-commit-id", "--name-only", "-r", head
        ).splitlines()
    ) == {
        "CHANGELOG.md",
        "docs/proof/bh-j5uyb.1-modularization-closeout.json",
        "pyproject.toml",
        "uv.lock",
    }
    assert _must_git(release_repo.root, "cat-file", "-t", "v0.16.2") == "tag"
    assert _must_git(release_repo.root, "rev-parse", "v0.16.2^{commit}") == head
    assert release_repo.transaction("verify", "0.16.2", "--tag", "v0.16.2").returncode == 0
    report = json.loads(
        (release_repo.root / "docs/proof/bh-j5uyb.1-modularization-closeout.json").read_text()
    )
    expected = hashlib.sha256((release_repo.root / "pyproject.toml").read_bytes()).hexdigest()
    assert report["evidence_inventory"]["current_candidate"]["package"]["sha256"] == expected


def test_expected_version_mismatch_refuses_before_mutation(tmp_path: Path) -> None:
    release_repo = _release_repo(tmp_path)

    result = release_repo.transaction("bump", "0.16.3")

    assert result.returncode == 1
    assert "Commitizen predicts '0.16.2'" in result.stderr
    assert _must_git(release_repo.root, "rev-parse", "HEAD") == release_repo.start
    assert _must_git(release_repo.root, "status", "--porcelain") == ""
    assert _must_git(release_repo.root, "tag", "--list", "v0.16.3") == ""


def test_generator_failure_rolls_back_files_commit_and_tag(tmp_path: Path) -> None:
    release_repo = _release_repo(tmp_path, generator_fails=True)

    result = release_repo.transaction("bump", "0.16.2")

    assert result.returncode == 1
    assert _must_git(release_repo.root, "rev-parse", "HEAD") == release_repo.start
    assert _must_git(release_repo.root, "status", "--porcelain") == ""
    assert _must_git(release_repo.root, "tag", "--list", "v0.16.2") == ""


def test_signing_failure_rolls_back_commit_and_tag(tmp_path: Path) -> None:
    release_repo = _release_repo(tmp_path)
    release_repo.key.unlink()

    result = release_repo.transaction("bump", "0.16.2")

    assert result.returncode == 1
    assert _must_git(release_repo.root, "rev-parse", "HEAD") == release_repo.start
    assert _must_git(release_repo.root, "status", "--porcelain") == ""
    assert _must_git(release_repo.root, "tag", "--list", "v0.16.2") == ""


def test_missing_signing_configuration_refuses_before_mutation(tmp_path: Path) -> None:
    release_repo = _release_repo(tmp_path)
    _must_git(release_repo.root, "config", "--unset", "user.signingkey")

    result = release_repo.transaction("bump", "0.16.2")

    assert result.returncode == 1
    assert "missing signing configuration: user.signingkey" in result.stderr
    assert _must_git(release_repo.root, "rev-parse", "HEAD") == release_repo.start
    assert _must_git(release_repo.root, "status", "--porcelain") == ""
    assert _must_git(release_repo.root, "tag", "--list", "v0.16.2") == ""


@pytest.mark.parametrize("bad_tag", ["lightweight", "unsigned", "wrong-target", "invalid"])
def test_verify_refuses_unacceptable_local_release_tags(tmp_path: Path, bad_tag: str) -> None:
    release_repo = _release_repo(tmp_path)
    assert release_repo.transaction("bump", "0.16.2").returncode == 0
    repo = release_repo.root
    _must_git(repo, "tag", "-d", "v0.16.2")
    if bad_tag == "lightweight":
        _must_git(repo, "tag", "v0.16.2", "HEAD")
    elif bad_tag == "unsigned":
        _must_git(repo, "-c", "tag.gpgSign=false", "tag", "-a", "v0.16.2", "-m", "unsigned")
    elif bad_tag == "wrong-target":
        _must_git(repo, "tag", "-s", "v0.16.2", "HEAD^", "-m", "wrong target")
    else:
        payload = _must_git(repo, "cat-file", "tag", "v0.16.1")
        old_target = _must_git(repo, "rev-parse", "v0.16.1^{commit}")
        new_target = _must_git(repo, "rev-parse", "HEAD")
        payload = payload.replace(f"object {old_target}", f"object {new_target}")
        payload += "\n-----BEGIN SSH SIGNATURE-----\ninvalid\n-----END SSH SIGNATURE-----\n"
        object_result = _git(repo, "hash-object", "-t", "tag", "-w", "--stdin", input_text=payload)
        assert object_result.returncode == 0, object_result.stderr
        _must_git(repo, "update-ref", "refs/tags/v0.16.2", object_result.stdout.strip())

    result = release_repo.transaction("verify", "0.16.2", "--tag", "v0.16.2")

    assert result.returncode == 1


def test_verify_refuses_wrong_version_before_release(tmp_path: Path) -> None:
    release_repo = _release_repo(tmp_path)
    assert release_repo.transaction("bump", "0.16.2").returncode == 0

    result = release_repo.transaction("verify", "0.16.3", "--tag", "v0.16.2")

    assert result.returncode == 1
    assert "project version is '0.16.2', expected '0.16.3'" in result.stderr
