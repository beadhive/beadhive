"""Capture the pinned Beads canonical-record schema as a vendored contract.

This is an upgrade-time command, not a runtime probe.  Runtime record models import generated
Python source and never invoke ``bd schema``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

app = typer.Typer(no_args_is_help=True, help="Pinned Beads contract maintenance.")
schema_app = typer.Typer(no_args_is_help=True, help="Capture the canonical bd record schema.")
app.add_typer(schema_app, name="schema")

_REPO_OPTION = typer.Option(
    None,
    "--repo",
    file_okay=False,
    resolve_path=True,
    help="repository checkout containing the authoritative flake.nix pin (default: cwd)",
)
_BD_OPTION = typer.Option("bd", "--bd", help="bd executable to verify and invoke")

SCHEMA_VERSION = 1
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class CaptureError(ValueError):
    """The binary, pin, or emitted schema is not the contract we can safely vendor."""


@dataclass(frozen=True)
class BeadsPin:
    version: str
    commit: str


@dataclass(frozen=True)
class CaptureResult:
    artifact: Path
    provenance: Path
    sha256: str
    version: str
    commit: str


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[bytes]]


def _run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(command, cwd=cwd, capture_output=True, check=False)  # noqa: S603


def read_beads_pin(flake: Path) -> BeadsPin:
    """Read the Beads release version and commit from the local-install toolchain pin."""

    try:
        text = flake.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaptureError(f"cannot read Beads pin {flake}: {exc}") from exc

    commit_match = re.search(r'^\s*beadsReleaseCommit\s*=\s*"([^"]+)"\s*;', text, re.MULTILINE)
    release_match = re.search(
        r"beadsRelease\s*=\s*pkgs:\s*.*?pname\s*=\s*\"beads\"\s*;"
        r".*?version\s*=\s*\"([^\"]+)\"\s*;",
        text,
        re.DOTALL,
    )
    if commit_match is None or release_match is None:
        raise CaptureError(f"cannot locate the Beads version and release commit in pin {flake}")

    pin = BeadsPin(version=release_match.group(1), commit=commit_match.group(1))
    if _VERSION.fullmatch(pin.version) is None:
        raise CaptureError(f"invalid Beads version in pin {flake}: {pin.version!r}")
    if _COMMIT.fullmatch(pin.commit) is None:
        raise CaptureError(f"invalid Beads commit in pin {flake}: {pin.commit!r}")
    return pin


def _decode_json(raw: bytes, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureError(f"{source} did not emit a valid UTF-8 JSON document: {exc}") from exc
    if not isinstance(value, dict):
        raise CaptureError(f"{source} emitted {type(value).__name__}, expected a JSON object")
    return value


def _invoke_json(
    runner: Runner, command: Sequence[str], cwd: Path, *, source: str
) -> dict[str, Any]:
    try:
        result = runner(command, cwd)
    except OSError as exc:
        raise CaptureError(f"cannot invoke {command[0]!r}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise CaptureError(
            f"{source} failed with exit {result.returncode}: {detail or 'no output'}"
        )
    return _decode_json(result.stdout, source=source)


def validate_schema_document(document: dict[str, Any]) -> None:
    """Fail closed unless *document* supports strict generated canonical-record models."""

    observed_version = document.get("schema_version", "<missing>")
    if observed_version != SCHEMA_VERSION:
        raise CaptureError(
            f"schema_version: expected {SCHEMA_VERSION}, observed {observed_version!r}"
        )

    types = document.get("types")
    if not isinstance(types, dict):
        raise CaptureError("types.issue missing: document has no object-valued types member")
    for name in ("issue", "dependency"):
        schema = types.get(name)
        if not isinstance(schema, dict):
            raise CaptureError(f"types.{name} missing: expected an object schema")
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise CaptureError(
                f"types.{name} failed Draft202012Validator.check_schema: {exc.message}"
            ) from exc
        observed_additional = schema.get("additionalProperties", "<missing>")
        if observed_additional is not False:
            raise CaptureError(
                f"types.{name} additionalProperties must be false; observed {observed_additional!r}"
            )


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def capture_schema(
    repo_root: Path,
    *,
    bd_binary: str = "bd",
    runner: Runner = _run,
    captured_at: datetime | None = None,
) -> CaptureResult:
    """Capture the exact raw schema from the binary matching ``repo_root/flake.nix``."""

    repo_root = repo_root.resolve()
    pin = read_beads_pin(repo_root / "flake.nix")
    version_document = _invoke_json(
        runner, [bd_binary, "version", "--json"], repo_root, source="bd version --json"
    )
    observed_version = str(version_document.get("version", "<missing>"))
    observed_commit = str(version_document.get("commit", "<missing>"))
    if observed_version != pin.version or observed_commit != pin.commit:
        raise CaptureError(
            "Beads binary pin mismatch: "
            f"expected version={pin.version} commit={pin.commit}; "
            f"observed version={observed_version} commit={observed_commit}"
        )

    try:
        result = runner([bd_binary, "schema"], repo_root)
    except OSError as exc:
        raise CaptureError(f"cannot invoke {bd_binary!r}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise CaptureError(
            f"bd schema failed with exit {result.returncode}: {detail or 'no output'}"
        )
    raw_document = result.stdout
    document = _decode_json(raw_document, source="bd schema")
    validate_schema_document(document)

    digest = hashlib.sha256(raw_document).hexdigest()
    timestamp = captured_at or datetime.now(UTC)
    provenance_document = {
        "version": pin.version,
        "commit": pin.commit,
        "schema_version": SCHEMA_VERSION,
        "captured_at": timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "document_sha256": digest,
    }
    provenance_bytes = (json.dumps(provenance_document, indent=2) + "\n").encode()

    contract_dir = repo_root / "src" / "beadhive" / "schemas" / "beads" / f"v{pin.version}"
    artifact = contract_dir / "schema.json"
    provenance = contract_dir / "provenance.json"
    contract_dir.mkdir(parents=True, exist_ok=True)
    _write_atomic(artifact, raw_document)
    _write_atomic(provenance, provenance_bytes)
    return CaptureResult(artifact, provenance, digest, pin.version, pin.commit)


@schema_app.command("capture")
def capture_command(
    repo: Path | None = _REPO_OPTION,
    bd_binary: str = _BD_OPTION,
) -> None:
    """Verify the pinned bd binary and vendor its byte-exact canonical schema."""

    try:
        captured = capture_schema(repo or Path.cwd(), bd_binary=bd_binary)
    except CaptureError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"✓ captured Beads v{captured.version} schema: {captured.artifact}")
    typer.echo(f"  sha256: {captured.sha256}")
