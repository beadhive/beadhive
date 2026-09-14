#!/usr/bin/env python3
"""Create or verify the signed, exact-version local release transaction."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE = "just check-all"
RELEASE_FILES = {
    "CHANGELOG.md",
    "docs/proof/bh-j5uyb.1-modularization-closeout.json",
    "pyproject.toml",
    "uv.lock",
}


class Refusal(RuntimeError):
    pass


@dataclass(frozen=True)
class SigningIdentity:
    name: str
    email: str
    fingerprint: str


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=ROOT,
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise Refusal(f"{shlex.join(args)} failed ({result.returncode}): {detail}")
    return result


def _git(*args: str, check: bool = True) -> str:
    return _run("git", *args, check=check).stdout.strip()


def _config(key: str) -> str:
    return _git("config", "--get", key, check=False)


def _signing_identity() -> SigningIdentity:
    name = _config("user.name")
    email = _config("user.email")
    key_value = _config("user.signingkey")
    signing_format = _config("gpg.format")
    commit_signing = _git("config", "--bool", "--get", "commit.gpgsign", check=False)
    missing = [
        label
        for label, value in (
            ("user.name", name),
            ("user.email", email),
            ("user.signingkey", key_value),
            ("gpg.format", signing_format),
            ("commit.gpgsign", commit_signing),
        )
        if not value
    ]
    if missing:
        raise Refusal(f"missing signing configuration: {', '.join(missing)}")
    if signing_format != "ssh":
        raise Refusal(f"gpg.format must be 'ssh' for this release policy, found {signing_format!r}")
    if commit_signing != "true":
        raise Refusal("commit.gpgsign must be true before creating a release commit")

    key_path = Path(os.path.expanduser(key_value))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    if not key_path.is_file():
        raise Refusal(f"configured SSH signing key does not exist: {key_path}")
    fingerprint_output = _run("ssh-keygen", "-lf", str(key_path), "-E", "sha256").stdout
    fields = fingerprint_output.split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise Refusal(f"could not derive fingerprint for configured signing key {key_path}")
    return SigningIdentity(name=name, email=email, fingerprint=fields[1])


def _project_version() -> str:
    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        return str(project["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise Refusal(f"could not read [project].version: {exc}") from exc


def _signature_fields(revision: str) -> tuple[str, str, str, str]:
    raw = _git("show", "-s", "--format=%G?%x00%GF%x00%cn%x00%ce", revision)
    fields = raw.split("\0")
    if len(fields) != 4:
        raise Refusal(f"could not read commit signature identity for {revision}")
    return fields[0], fields[1], fields[2], fields[3]


def verify(expected: str, tag: str = "") -> None:
    identity = _signing_identity()
    pinned = _project_version()
    if pinned != expected:
        raise Refusal(f"project version is {pinned!r}, expected {expected!r}")

    expected_tag = f"v{expected}"
    tag = tag or expected_tag
    if tag != expected_tag:
        raise Refusal(
            f"release tag is {tag!r}, but project version {expected!r} requires {expected_tag!r}"
        )
    tag_type = _git("cat-file", "-t", f"refs/tags/{tag}", check=False)
    if tag_type != "tag":
        kind = tag_type or "missing"
        raise Refusal(f"local release tag {tag!r} must be a signed annotated tag, found {kind}")

    head = _git("rev-parse", "HEAD")
    target = _git("rev-parse", f"refs/tags/{tag}^{{commit}}", check=False)
    if target != head:
        raise Refusal(
            f"local release tag {tag!r} targets {target[:12] or 'nothing'}, not HEAD {head[:12]}"
        )

    grade, fingerprint, committer_name, committer_email = _signature_fields("HEAD")
    if grade not in {"G", "U"}:
        raise Refusal(
            f"release commit {head[:12]} does not have a valid signature (grade {grade!r})"
        )
    if fingerprint != identity.fingerprint:
        raise Refusal(
            f"release commit uses signing key {fingerprint or 'none'}, not configured key "
            f"{identity.fingerprint}"
        )
    if (committer_name, committer_email) != (identity.name, identity.email):
        raise Refusal("release commit identity does not match configured user.name and user.email")

    tag_verification = _run("git", "verify-tag", tag, check=False)
    valid_signature = re.search(
        r'^Good "git" signature .* key (SHA256:\S+)$', tag_verification.stderr, re.MULTILINE
    )
    if not valid_signature:
        raise Refusal(f"local release tag {tag!r} does not have a valid signature")
    tag_fingerprint = valid_signature.group(1)
    if tag_fingerprint != identity.fingerprint:
        raise Refusal(
            f"release tag uses signing key {tag_fingerprint or 'none'}, not configured key "
            f"{identity.fingerprint}"
        )

    tagger = _git("for-each-ref", "--format=%(taggername)%00%(taggeremail)", f"refs/tags/{tag}")
    tagger_fields = tagger.split("\0")
    if len(tagger_fields) != 2:
        raise Refusal(f"could not read tagger identity for local release tag {tag!r}")
    tagger_email = tagger_fields[1].strip().removeprefix("<").removesuffix(">")
    if (tagger_fields[0], tagger_email) != (identity.name, identity.email):
        raise Refusal("release tag identity does not match configured user.name and user.email")


def _next_version() -> str:
    result = _run(str(ROOT / "scripts/next-version.sh"), "--get-next", check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise Refusal(f"Commitizen could not predict the next version: {detail}")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise Refusal("Commitizen dry-run did not return exactly one predicted version")
    return lines[0]


def _cz(*args: str) -> None:
    configured = os.environ.get("CZ_EXEC", "").strip()
    command = [configured] if configured else ["uv", "run", "cz"]
    _run(*command, *args)


def _preflight(gate: str) -> None:
    command = shlex.split(os.environ.get("BH_EXEC", "bh"))
    _run(*command, "release", "preflight", "--gate", gate)


def bump(expected: str, gate: str) -> None:
    status = _git("status", "--porcelain=v1", "--untracked-files=normal")
    if status:
        raise Refusal("release bump requires a clean working tree")
    if (
        not expected
        or expected.startswith("v")
        or any(character.isspace() for character in expected)
    ):
        raise Refusal(f"expected version must be an unprefixed version string, found {expected!r}")

    predicted = _next_version()
    if predicted != expected:
        raise Refusal(
            f"expected version {expected!r}, but Commitizen predicts {predicted!r}; nothing changed"
        )
    _signing_identity()
    tag = f"v{expected}"
    tag_exists = _run(
        "git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}", check=False
    )
    if tag_exists.returncode == 0:
        raise Refusal(f"local tag {tag!r} already exists")
    _preflight(gate)

    start = _git("rev-parse", "HEAD")
    complete = False
    armed = True
    try:
        _cz("bump", "--changelog", "--gpg-sign", expected)
        head = _git("rev-parse", "HEAD")
        parents = _git("show", "-s", "--format=%P", head).split()
        if parents != [start]:
            raise Refusal(
                "Commitizen did not create exactly one release commit from the start HEAD"
            )
        changed = set(_git("diff-tree", "--no-commit-id", "--name-only", "-r", head).splitlines())
        if changed != RELEASE_FILES:
            raise Refusal(
                "release commit changed the wrong files: "
                f"expected {sorted(RELEASE_FILES)!r}, found {sorted(changed)!r}"
            )
        verify(expected, tag)
        complete = True
    finally:
        if armed and not complete:
            _run("git", "update-ref", "-d", f"refs/tags/{tag}", check=False)
            _run("git", "reset", "--hard", start, check=False)
    print(f"✓ local release transaction {tag} -> {_git('rev-parse', '--short=12', 'HEAD')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bump_parser = commands.add_parser("bump", help="create a verified local bump commit and tag")
    bump_parser.add_argument("expected")
    bump_parser.add_argument("--gate", default=GATE)
    verify_parser = commands.add_parser("verify", help="verify the local release commit and tag")
    verify_parser.add_argument("expected")
    verify_parser.add_argument("--tag", default="")
    args = parser.parse_args()

    try:
        if args.command == "bump":
            bump(args.expected, args.gate)
        else:
            verify(args.expected, args.tag)
    except Refusal as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
