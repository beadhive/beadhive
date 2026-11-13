"""The effective ``backfill`` block of one hive (ADR ``backfill-rerun-control-adr.md`` Q1).

``backfill.hives.<prefix>`` in the **host** config overlays the host block for the hive whose
registered ``prefix`` is ``<prefix>``. One resolver, :func:`effective_backfill`, serves every
backfill read site: ``effective = host block <- overlay``, mappings merging key by key, lists and
scalars replacing. A hive with no overlay gets the host block unchanged (``hives`` aside). The
overlay never carries ``judge`` (rejected at validation), so the credit-spending command stays
one host-wide setting. A prefix is never derived: a hive without one cannot have an overlay.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from . import registry
from .modules.config.contracts import BackfillHiveOverlay

HOST_LAYER = "host"


class BackfillHiveError(ValueError):
    """A hive's backfill overlay cannot be resolved (no prefix, or an invalid overlay)."""


def overlay_layer(prefix: str) -> str:
    """The layer name an overlaid value is attributed to."""
    return f"backfill.hives.{prefix}"


def hive_prefix_of(cfg: Mapping[str, Any], hive: str | Mapping[str, Any] | None) -> str | None:
    """The registered ``prefix`` of ``hive`` (an id, a registry entry, or ``""``/``None`` for the
    cwd's hive), or ``None`` when it has none. Never derived or guessed."""
    if hive is None:
        return None
    if isinstance(hive, Mapping):
        entry: Mapping[str, Any] | None = hive
    else:
        entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    return str((entry or {}).get("prefix") or "") or None


def merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """``base`` <- ``over``: mappings merge key by key; lists and scalars replace."""
    out = copy.deepcopy(dict(base))
    for key, value in over.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _host_block(cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    block = cfg.get("backfill") or {}
    return block if isinstance(block, Mapping) else {}


def overlay_of(cfg: Mapping[str, Any], prefix: str) -> Mapping[str, Any]:
    """The validated raw overlay of ``prefix`` (empty when the hive has none)."""
    hives = _host_block(cfg).get("hives") or {}
    raw = hives.get(prefix) if isinstance(hives, Mapping) else None
    if not raw:
        return {}
    try:
        BackfillHiveOverlay.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise BackfillHiveError(
            f"{overlay_layer(prefix)}.{where}: {first['msg']} (the overlay is BackfillConfig "
            "minus judge; judge stays host-wide)"
        ) from None
    return raw if isinstance(raw, Mapping) else {}


def effective_backfill(
    cfg: Mapping[str, Any], hive: str | Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The raw ``backfill`` mapping in effect for ``hive`` (an id, an entry, ``""`` = the cwd's
    hive, or ``None`` = no hive: the host block). The host block without its ``hives`` map when
    the hive has no overlay -- the same content every read site saw before overlays existed.

    Raises :class:`BackfillHiveError` for an invalid overlay, and for a hive without a registered
    prefix when the host configured overlays (the error says to set the registry prefix)."""
    host = {k: v for k, v in _host_block(cfg).items() if k != "hives"}
    hives = _host_block(cfg).get("hives")
    if hive is None or not hives:
        return host
    prefix = hive_prefix_of(cfg, hive)
    if prefix is None:
        raise BackfillHiveError(
            "the hive has no registered prefix, so a backfill overlay cannot apply "
            "(prefixes are never derived): set the hive's registry prefix first"
        )
    overlay = overlay_of(cfg, prefix)
    return merge(host, overlay) if overlay else host


def effective_cfg(
    cfg: Mapping[str, Any], hive: str | Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """``cfg`` with its ``backfill`` block replaced by :func:`effective_backfill`, so a read site
    that takes a whole config keeps its code and sees the hive's values."""
    out = dict(cfg)
    out["backfill"] = effective_backfill(cfg, hive)
    return out


def layer_of(cfg: Mapping[str, Any], hive: str | Mapping[str, Any], parts: list[str]) -> str:
    """The layer that decides the effective value at ``parts`` (a path below ``backfill``):
    the hive overlay when it sets that key or any key below it, else ``host``."""
    prefix = hive_prefix_of(cfg, hive)
    if prefix is None:
        return HOST_LAYER
    node: Any = overlay_of(cfg, prefix)
    for part in parts:
        if not isinstance(node, Mapping) or part not in node:
            return HOST_LAYER
        node = node[part]
    return overlay_layer(prefix)


def continuous_may_run(cfg: Mapping[str, Any]) -> bool:
    """Cheap gate: is ``backfill.continuous`` enabled on the host block or on any hive overlay?
    (A hive's own settings are then resolved per hive by :func:`settings`.)"""
    block = cfg.get("backfill") or {}
    if not isinstance(block, Mapping):
        return False

    def on(section: Any) -> bool:
        return isinstance(section, Mapping) and section.get("enabled") is True

    if on(block.get("continuous")):
        return True
    hives = block.get("hives") or {}
    return isinstance(hives, Mapping) and any(
        isinstance(over, Mapping) and on(over.get("continuous")) for over in hives.values()
    )


def effective_view(
    cfg: Mapping[str, Any], hive: str, parts: list[str]
) -> tuple[Mapping[str, Any], str]:
    """``(config view, layer)`` for ``bh config get <key> --hive <hive>``: a ``backfill.*`` key
    reads the hive's effective block (layer ``host`` or ``backfill.hives.<prefix>``); any other
    key reads ``cfg`` itself (layer ``config``). Raises :class:`BackfillHiveError`."""
    try:
        entry = registry.resolve_hive(cfg, hive)
    except (KeyError, ValueError) as exc:
        raise BackfillHiveError(str(exc)) from None
    if parts[0] != "backfill":
        return cfg, "config"
    if not hive_prefix_of(cfg, entry):
        raise BackfillHiveError(
            "the hive has no registered prefix, so it has no backfill overlay "
            "(prefixes are never derived): set the hive's registry prefix first"
        )
    return {"backfill": effective_backfill(cfg, entry)}, layer_of(cfg, entry, parts[1:])
