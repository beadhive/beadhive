"""Host-local credentials for HQ-declared Dolt remotes (bh-b1scz.4).

The boundary (docs/design/hq-dolt-remote-descriptor-adr.md, "Credential boundary"): HQ names at
most a credential *label*; this host maps the label to a source in its own
``dolt.remote_credentials.<label>``; resolved values live only in the environment handed to the
one bd child that needs them. Nothing here prints, logs, or stores a value — every message names
what is missing, never what was found — and :class:`CredentialResolution` hides its environment
from ``repr``.

Resolution happens BEFORE any remote mutation, so a missing credential fails fast with nothing
changed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .dolt_remote import RemoteTarget

#: How long a fnox lookup may take before the resolution fails closed.
FNOX_TIMEOUT = 15.0

_AWS_FILE_ENV = (
    ("AWS_SHARED_CREDENTIALS_FILE", ".aws/credentials"),
    ("AWS_CONFIG_FILE", ".aws/config"),
)


@dataclass(frozen=True)
class CredentialResolution:
    """Outcome of resolving one remote's credentials on this host.

    ``source`` is a provenance label (never a value). ``env`` is secret-bearing: pass it only as
    an Engine ``extra_env``; it is excluded from ``repr`` so an accidental log line cannot leak
    it."""

    ok: bool
    source: str
    missing: str = ""
    env: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)


def _set(environ: Mapping[str, str], name: str) -> bool:
    return bool((environ.get(name) or "").strip())


def _shared_files(environ: Mapping[str, str], home: Path) -> list[str]:
    found = []
    for variable, default in _AWS_FILE_ENV:
        path = Path(environ[variable]) if _set(environ, variable) else home / default
        if path.is_file():
            found.append(variable if _set(environ, variable) else f"~/{default}")
    return found


def _ambient(environ: Mapping[str, str], home: Path, label_hint: str) -> CredentialResolution:
    key, secret = _set(environ, "AWS_ACCESS_KEY_ID"), _set(environ, "AWS_SECRET_ACCESS_KEY")
    if key and secret:
        return CredentialResolution(True, "ambient:AWS_ACCESS_KEY_ID")
    if key != secret:
        present, absent = (
            ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
            if key
            else ("AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID")
        )
        return CredentialResolution(False, "ambient", f"{present} is set but {absent} is not")
    for name in (
        "AWS_PROFILE",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        if _set(environ, name):
            return CredentialResolution(True, f"ambient:{name}")
    files = _shared_files(environ, home)
    if files:
        return CredentialResolution(True, f"ambient:{files[0]}")
    return CredentialResolution(
        False,
        "ambient",
        "no AWS credentials found on this host: set AWS_PROFILE or "
        "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, provide ~/.aws/credentials, or configure "
        f"dolt.remote_credentials.{label_hint} (source: instance-role on an EC2 host)",
    )


def _profile_config(cfg, label: str):
    dolt = (cfg or {}).get("dolt") or {}
    profiles = dolt.get("remote_credentials") or {}
    return profiles.get(label)


def resolve(
    target: RemoteTarget,
    label: str,
    cfg,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    broker=None,
) -> CredentialResolution:
    """Resolve credentials for ``target`` (whose descriptor names ``label``) on this host."""

    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    if target.capabilities.credentials != "aws":
        return CredentialResolution(True, "not-required")
    profile = _profile_config(cfg, label) if label else None
    if label and profile is None:
        return CredentialResolution(
            False,
            f"profile:{label}",
            f"credential profile '{label}' is not configured on this host "
            f"(dolt.remote_credentials.{label})",
        )
    source = str((profile or {}).get("source") or "ambient")
    if source == "ambient":
        return _ambient(environ, home, label or "<label>")
    if source == "instance-role":
        return CredentialResolution(True, "instance-role")
    if source == "aws-profile":
        name = str(profile.get("aws_profile") or "")
        if not _shared_files(environ, home):
            return CredentialResolution(
                False,
                f"aws-profile:{name}",
                f"AWS profile '{name}' needs ~/.aws/config or ~/.aws/credentials on this host",
            )
        return CredentialResolution(True, f"aws-profile:{name}", env={"AWS_PROFILE": name})
    if source == "fnox":
        return _fnox(profile, label, broker)
    return CredentialResolution(False, source, f"unknown credential source '{source}'")


def _fnox(profile, label: str, broker) -> CredentialResolution:
    from .hq_sql_transport import FnoxBroker, SqlTransportError

    broker = broker or FnoxBroker()
    deadline = time.monotonic() + FNOX_TIMEOUT
    env: dict[str, str] = {}
    wanted = (
        ("access_key_id", "AWS_ACCESS_KEY_ID", True),
        ("secret_access_key", "AWS_SECRET_ACCESS_KEY", True),
        ("session_token", "AWS_SESSION_TOKEN", False),
    )
    for field_name, variable, required in wanted:
        reference = dict(profile.get(field_name) or {})
        if not reference.get("key"):
            if required:
                return CredentialResolution(
                    False, f"fnox:{label}", f"fnox reference {field_name} is not configured"
                )
            continue
        try:
            env[variable] = broker.get(reference, deadline=deadline)
        except SqlTransportError:
            return CredentialResolution(
                False, f"fnox:{label}", f"fnox credential {field_name} for '{label}' unavailable"
            )
    return CredentialResolution(True, f"fnox:{label}", env=env)
