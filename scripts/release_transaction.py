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
RELEASE_FILES = {
    "CHANGELOG.md",
    "pyproject.toml",
    "uv.lock",
    "server.json",
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
    if tag_verification.returncode != 0 or not valid_signature:
        raise Refusal(
            f"local release tag {tag!r} does not have a valid signature "
            f"(git verify-tag exited {tag_verification.returncode})"
        )
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


# Commitizen `bump` flags the operator may NOT pass through (bh-rgc6p). Each one skips or changes
# something this transaction guarantees: the single signed release commit, the changelog, the
# tag, or the RELEASE_FILES check. Everything else is forwarded unchanged to BOTH the prediction
# (`next-version.sh --get-next`) and the real `cz bump`, so what is predicted is what is cut.
# Matching is by unambiguous argparse prefix too (`--dry` is `--dry-run`), as cz itself accepts.
REFUSED_CZ_FLAGS = frozenset(
    {
        "--dry-run",  # writes nothing: not a release
        "--files-only",  # no commit, no tag
        "--version-files-only",  # no commit, no tag
        "--no-verify",  # bypasses the hooks the signed commit relies on
        "--changelog-to-stdout",  # changelog goes to stdout, not CHANGELOG.md
        "--get-next",  # prints a version, bumps nothing
        "--tag-format",  # the transaction verifies a `v<version>` tag
        "--help",
    }
)
REFUSED_CZ_SHORT = frozenset({"-h"})
# cz bump options that consume the next token; any OTHER bare token is a positional (a manual
# version), which would override the prediction, so it is refused.
CZ_VALUE_FLAGS = frozenset(
    {
        "--bump-message",
        "--prerelease",
        "-pr",
        "--devrelease",
        "-d",
        "--increment",
        "--increment-mode",
        "--annotated-tag-message",
        "-atm",
        "--template",
        "-t",
        "--extra",
        "-e",
        "--file-name",
        "--prerelease-offset",
        "--version-scheme",
        "--version-type",
        "--build-metadata",
    }
)


def validate_cz_flags(flags: list[str]) -> list[str]:
    """Refuse override flags that would break the transaction; return the flags unchanged."""
    skip_value = False
    for token in flags:
        if skip_value:
            skip_value = False
            continue
        if not token.startswith("-"):
            raise Refusal(
                f"cz override {token!r} is a positional (manual version); "
                "state the version as <expected> and pass only flags"
            )
        name = token.split("=", 1)[0]
        if name in REFUSED_CZ_SHORT or any(
            refused == name
            or (len(name) > 2 and name.startswith("--") and refused.startswith(name))
            for refused in REFUSED_CZ_FLAGS
        ):
            raise Refusal(
                f"cz override {token!r} would break the release transaction "
                f"(refused: {', '.join(sorted(REFUSED_CZ_FLAGS))}, -h, or a manual version)"
            )
        if name in CZ_VALUE_FLAGS and "=" not in token:
            skip_value = True
    return list(flags)


def _next_version(cz_flags: list[str] | None = None) -> str:
    result = _run(
        str(ROOT / "scripts/next-version.sh"), "--get-next", *(cz_flags or []), check=False
    )
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
    args = [*command, "release", "preflight"]
    if gate:
        args.extend(("--gate", gate))
    _run(*args)


def _rollback(start: str, tag: str) -> None:
    errors: list[str] = []
    delete = _run("git", "update-ref", "-d", f"refs/tags/{tag}", check=False)
    if delete.returncode != 0:
        errors.append(f"tag deletion exited {delete.returncode}")
    reset = _run("git", "reset", "--hard", start, check=False)
    if reset.returncode != 0:
        errors.append(f"worktree reset exited {reset.returncode}")

    head = _run("git", "rev-parse", "HEAD", check=False)
    if head.returncode != 0 or head.stdout.strip() != start:
        current = head.stdout.strip()[:12] if head.returncode == 0 else "unreadable"
        errors.append(f"HEAD is {current}, expected rollback target {start[:12]}")
    tag_state = _run("git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}", check=False)
    if tag_state.returncode == 0:
        errors.append(f"local release tag {tag!r} still exists")
    elif tag_state.returncode != 1:
        errors.append(f"could not verify local release tag removal (exit {tag_state.returncode})")
    status = _run("git", "status", "--porcelain=v1", "--untracked-files=normal", check=False)
    if status.returncode != 0:
        errors.append(f"could not verify rollback worktree state (exit {status.returncode})")
    elif status.stdout.strip():
        errors.append("rollback worktree is not clean")
    if errors:
        raise Refusal("release rollback failed: " + "; ".join(errors))


_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?")
_PRE_ORDER = ("a", "b", "rc")


def _parse_version(text: str) -> tuple[int, int, int, str, int] | None:
    """Canonical PEP 440 (the repo's `version_scheme`): X.Y.Z with optional a/b/rcN; else None."""
    hit = _VERSION.fullmatch(text)
    if not hit:
        return None
    major, minor, patch, kind, number = hit.groups()
    return int(major), int(minor), int(patch), kind or "", int(number or 0)


def allowed_successors(current: str) -> list[str]:
    """Every exact version `just bump <version>` accepts from `current` (pure, no I/O).

    From a final X.Y.Z: the next patch, minor or major, each also as its first pre-release
    (a0 / b0 / rc0). From a pre-release of base B: B itself, the next number of the same
    pre-release kind, or the first of a later kind (a -> b -> rc). Nothing else: no equal or lower
    version, no skipped range, and deliberately no escape hatch."""
    parsed = _parse_version(current)
    if parsed is None:
        raise Refusal(f"current version {current!r} is not a canonical version")
    major, minor, patch, kind, number = parsed
    if kind:
        base = f"{major}.{minor}.{patch}"
        out = [base, f"{base}{kind}{number + 1}"]
        out += [f"{base}{later}0" for later in _PRE_ORDER[_PRE_ORDER.index(kind) + 1 :]]
        return out
    bases = [f"{major}.{minor}.{patch + 1}", f"{major}.{minor + 1}.0", f"{major + 1}.0.0"]
    return [item for base in bases for item in (base, *(f"{base}{k}0" for k in _PRE_ORDER))]


def check_successor(current: str, target: str) -> None:
    """Refuse `target` unless it is an allowed immediate successor of `current`."""
    allowed = allowed_successors(current)
    if _parse_version(target) is None or target not in allowed:
        raise Refusal(
            f"version {target!r} is not an allowed successor of {current!r} "
            f"(unprefixed canonical versions only, no equal, lower or skipped versions); "
            f"allowed: {', '.join(allowed)}"
        )


def bump(raw: list[str], gate: str) -> None:
    """`raw` is the operator's choice of version: nothing (cz decides), cz bump flags (cz decides
    under them), or one exact version. A version AND flags are mutually exclusive (bh-rgc6p)."""
    version = raw[0] if raw and not raw[0].startswith("-") else ""
    cz_flags = validate_cz_flags(raw[1:] if version else raw)
    if version and cz_flags:
        raise Refusal(
            "give either an exact version or Commitizen flags, not both "
            f"(version {version!r} with {shlex.join(cz_flags)!r})"
        )
    status = _git("status", "--porcelain=v1", "--untracked-files=normal")
    if status:
        raise Refusal("release bump requires a clean working tree")
    if version:
        check_successor(_project_version(), version)
        expected = version
        cz_args = [version]
    else:
        expected = _next_version(cz_flags)
        cz_args = cz_flags
    if cz_flags:
        print(f"cz overrides (operator): {shlex.join(cz_flags)}")
    print(f"release version: {expected}" + (" (exact)" if version else " (Commitizen)"))
    _signing_identity()
    tag = f"v{expected}"
    tag_exists = _run("git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}", check=False)
    if tag_exists.returncode == 0:
        raise Refusal(f"local tag {tag!r} already exists")
    _preflight(gate)

    start = _git("rev-parse", "HEAD")
    try:
        # cz decides under THE SAME flags the prediction saw, or is handed the exact version as
        # its manual-version positional; verify() below pins the result to `expected`.
        _cz("bump", "--changelog", "--gpg-sign", *cz_args)
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
    except BaseException as failure:
        try:
            _rollback(start, tag)
        except Refusal as cleanup_failure:
            raise Refusal(f"release failed ({failure}); {cleanup_failure}") from failure
        raise
    print(f"✓ local release transaction {tag} -> {_git('rev-parse', '--short=12', 'HEAD')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bump_parser = commands.add_parser("bump", help="create a verified local bump commit and tag")
    bump_parser.add_argument(
        "--gate",
        default="",
        help="optional expected gate; defaults to the hive's work.validate.push-main command",
    )
    verify_parser = commands.add_parser("verify", help="verify the local release commit and tag")
    verify_parser.add_argument("expected")
    verify_parser.add_argument("--tag", default="")
    # Everything after `--`, and any option this parser does not know, is a Commitizen bump flag
    # forwarded unchanged to the prediction and the bump (bh-rgc6p).
    argv = sys.argv[1:]
    tail: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, tail = argv[:split], argv[split + 1 :]
    args, extras = parser.parse_known_args(argv)
    cz_flags = [*extras, *tail]
    if extras and args.command != "bump":
        parser.error(f"unrecognized arguments: {' '.join(extras)}")

    try:
        if args.command == "bump":
            bump(cz_flags, args.gate)
        else:
            verify(args.expected, args.tag)
    except Refusal as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
