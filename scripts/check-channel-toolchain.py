#!/usr/bin/env python3
"""Refuse a release channel target whose Nix toolchain is below policy."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MINIMUM_BEADS_VERSION = (1, 3, 0)
_RELEASE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_BEADS_DERIVATION = re.compile(
    r'beadsRelease\s*=\s*pkgs:.*?pname\s*=\s*"beads";\s*version\s*=\s*"([^"]+)";'
    r"(?P<body>.*?)(?=\n\s*doltReleaseCommit\s*=)",
    re.DOTALL,
)
_BEADS_ASSETS = re.compile(
    r"beadsReleaseAssets\s*=\s*\{(?P<body>.*?)};\s*beadsRelease\s*=", re.DOTALL
)
_TOOLCHAIN = re.compile(r"toolchainFor\s*=\s*pkgs:\s*\[(?P<body>.*?)\n\s*\];", re.DOTALL)


def _nix_code(text: str) -> str:
    """Remove Nix comments and indented-string bodies before structural checks."""
    output: list[str] = []
    index = 0
    block_depth = 0
    state = "code"
    while index < len(text):
        pair = text[index : index + 2]
        char = text[index]
        if state == "line":
            if char == "\n":
                output.append(char)
                state = "code"
            else:
                output.append(" ")
            index += 1
        elif state == "block":
            if pair == "/*":
                output.extend("  ")
                block_depth += 1
                index += 2
            elif pair == "*/":
                output.extend("  ")
                block_depth -= 1
                index += 2
                if block_depth == 0:
                    state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
        elif state == "double":
            output.append(char)
            index += 1
            if char == "\\" and index < len(text):
                output.append(text[index])
                index += 1
            elif char == '"':
                state = "code"
        elif state == "indented":
            if pair == "''":
                output.extend("  ")
                index += 2
                state = "code"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
        elif char == "#":
            output.append(" ")
            index += 1
            state = "line"
        elif pair == "/*":
            output.extend("  ")
            index += 2
            block_depth = 1
            state = "block"
        elif pair == "''":
            output.extend("  ")
            index += 2
            state = "indented"
        else:
            output.append(char)
            index += 1
            if char == '"':
                state = "double"
    if state == "block":
        raise ValueError("flake.nix contains an unterminated block comment")
    if state in {"double", "indented"}:
        raise ValueError("flake.nix contains an unterminated string")
    return "".join(output)


def _version(value: object, *, source: str) -> tuple[int, int, int]:
    match = _RELEASE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"{source} must be a final MAJOR.MINOR.PATCH release, got {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _read(root: Path, ref: str | None, relative: str) -> str:
    if ref is None:
        return (root / relative).read_text()
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{ref}:{relative}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "path is absent from the target"
        raise ValueError(f"cannot read {relative} from {ref}: {detail}")
    return result.stdout


def check(root: Path, ref: str | None = None) -> str:
    label = ref or str(root)
    try:
        rows = json.loads(_read(root, ref, "docker/toolchain-metadata.json"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: toolchain metadata is not valid JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError(f"{label}: toolchain metadata must be a list")

    beads = [row for row in rows if isinstance(row, dict) and row.get("name") == "bd"]
    if len(beads) != 1:
        raise ValueError(f"{label}: expected exactly one bd toolchain row, found {len(beads)}")
    if beads[0].get("package") != "beads":
        raise ValueError(f"{label}: bd toolchain row must come from the beads package")
    metadata_text = beads[0].get("version")
    metadata_version = _version(metadata_text, source=f"{label}: bd metadata version")

    flake = _nix_code(_read(root, ref, "flake.nix"))
    matches = list(_BEADS_DERIVATION.finditer(flake))
    if len(matches) != 1:
        raise ValueError(
            f"{label}: expected exactly one Beads release derivation, found {len(matches)}"
        )
    flake_text = matches[0].group(1)
    flake_version = _version(flake_text, source=f"{label}: flake Beads version")
    if flake_version != metadata_version:
        raise ValueError(
            f"{label}: flake Beads {flake_text} disagrees with toolchain metadata {metadata_text}"
        )
    if flake_version < MINIMUM_BEADS_VERSION:
        minimum = ".".join(map(str, MINIMUM_BEADS_VERSION))
        raise ValueError(f"{label}: Beads {flake_text} is below channel minimum {minimum}")

    derivation = matches[0].group("body")
    expected_url = (
        f'url = "https://github.com/gastownhall/beads/releases/download/'
        f'v{flake_text}/${{release.asset}}";'
    )
    fetch_url = re.search(
        r'src\s*=\s*pkgs\.fetchurl\s*\{.*?url\s*=\s*"([^"]+)";', derivation, re.DOTALL
    )
    if fetch_url is None or fetch_url.group(1) != expected_url.removeprefix('url = "').removesuffix(
        '";'
    ):
        raise ValueError(f"{label}: Beads derivation URL is not bound to release v{flake_text}")

    assets_match = _BEADS_ASSETS.search(flake)
    assets = re.findall(
        r'asset\s*=\s*"([^"]+)";', assets_match.group("body") if assets_match else ""
    )
    expected_prefix = f"beads_{flake_text}_"
    if not assets or any(not asset.startswith(expected_prefix) for asset in assets):
        raise ValueError(f"{label}: Beads assets are not all bound to release v{flake_text}")

    toolchain_match = _TOOLCHAIN.search(flake)
    toolchain = toolchain_match.group("body") if toolchain_match else ""
    if toolchain.count("(beadsRelease pkgs)") != 1:
        raise ValueError(f"{label}: toolchain must install exactly one beadsRelease derivation")

    return flake_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ref", help="Git tree/tag to inspect instead of the working tree")
    args = parser.parse_args()
    try:
        version = check(args.root.resolve(), args.ref)
    except (OSError, ValueError) as exc:
        print(f"channel toolchain refused: {exc}", file=sys.stderr)
        return 1
    print(f"channel toolchain eligible: Beads {version} >= 1.3.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
