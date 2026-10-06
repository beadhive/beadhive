"""Off-frame operator binding for protected SQL authority verbs.

A released ``bh`` selects its control plane from the running host's ``host.yaml``. An
operator host (for example a laptop) may not carry ``hq.sql.authority_writer`` there.
``$BH_HQ_OPERATOR_SETTINGS`` (a path) supplies that binding from a JSON or YAML file instead.

Trust delta: none. The file carries the same ``authority_writer`` credential reference the
operator already holds off-frame, and is refused whenever it binds ``runtime``, so it can never
turn a frame into an authority writer. Validation reuses ``HqSqlConfig``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .hq_control_plane import ControlPlaneError, SqlControlPlane

#: Environment variable naming the settings file (no new CLI parameter on published operations).
ENV = "BH_HQ_OPERATOR_SETTINGS"
CEILING_KEY = "authority_max_duration_s"
#: Operator-only retention margin for ``bh hq authority prune-inbox`` (bh-ce886).
RETENTION_KEY = "inbox_retention_s"

#: Option help shared by every verb that accepts the binding.
OPTION_HELP = (
    "JSON/YAML file with an `hq.sql` section (reader, authority_writer, "
    "runtime_operator_public_key; runtime must be absent) used instead of this host's host.yaml"
)


def load_settings(path) -> dict:
    """Read and validate an operator settings file, returning the normalized ``hq.sql`` dict."""
    from pydantic import ValidationError
    from ruamel.yaml import YAML

    from .modules.config.contracts import HqSqlConfig

    try:
        raw = YAML(typ="safe").load(Path(path).read_text())
    except OSError as exc:
        raise ControlPlaneError(f"operator settings unreadable: {exc.strerror or exc}") from None
    except Exception:  # noqa: BLE001 - parser messages may echo credential-adjacent values
        raise ControlPlaneError("operator settings file is not valid JSON or YAML") from None
    if not isinstance(raw, dict):
        raise ControlPlaneError("operator settings must be an object with an `hq.sql` section")
    hq = raw.get("hq", raw)
    sql = hq.get("sql") if isinstance(hq, dict) else None
    if not isinstance(sql, dict):
        raise ControlPlaneError("operator settings must contain an `hq.sql` section")
    if sql.get("runtime") is not None:
        raise ControlPlaneError(
            "operator settings key hq.sql.runtime must be null: a runtime binding makes the "
            "file a frame, not an authority writer"
        )
    if not sql.get("authority_writer"):
        raise ControlPlaneError("operator settings key hq.sql.authority_writer is required")
    sql = dict(sql)
    ceiling = sql.pop("authority_max_duration_s", None)
    if ceiling is not None:
        from .hq_authority_ceiling import parse_duration

        try:
            parse_duration(ceiling)
        except ValueError as exc:
            raise ControlPlaneError(
                f"operator settings key hq.sql.authority_max_duration_s invalid: {exc}"
            ) from None
    retention = sql.pop(RETENTION_KEY, None)
    if retention is not None:
        from .hq_authority_ceiling import parse_duration

        try:
            parse_duration(retention)
        except ValueError as exc:
            raise ControlPlaneError(
                f"operator settings key hq.sql.{RETENTION_KEY} invalid: {exc}"
            ) from None
    try:
        validated = HqSqlConfig.model_validate({**sql, "runtime": None})
    except (ValidationError, TypeError, ValueError):
        # Never render raw rejected values: they may be credential material.
        raise ControlPlaneError("operator settings hq.sql binding invalid") from None
    settings = validated.model_dump()
    if ceiling is not None:
        # Hook for the authority duration ceiling (bh-od8ve): resolve_ceiling(settings=...)
        # consumes this when present in the base; otherwise it is carried, unused.
        settings[CEILING_KEY] = ceiling
    if retention is not None:
        settings[RETENTION_KEY] = retention
    return settings


def operator_plane(path, *, broker=None, clock=time.time) -> SqlControlPlane:
    """Build a ``SqlControlPlane`` straight from the file, bypassing ``control_plane()``."""
    settings = load_settings(path)
    ceiling = settings.pop(CEILING_KEY, None)
    retention = settings.pop(RETENTION_KEY, None)
    plane = SqlControlPlane(settings, broker=broker, clock=clock)
    # Passed to hq_authority_ceiling.resolve_ceiling(settings=...) once that lands (bh-od8ve).
    plane.authority_max_duration_s = ceiling
    plane.inbox_retention_s = retention
    return plane


def configured() -> bool:
    return bool(os.environ.get(ENV))


def select_plane(operator_settings=None):
    """The file's plane when a settings file is named (argument or $BH_HQ_OPERATOR_SETTINGS),
    else the host's own."""
    operator_settings = operator_settings or os.environ.get(ENV) or None
    if operator_settings is None:
        from .hq_control_plane import control_plane

        return control_plane()
    return operator_plane(operator_settings)
