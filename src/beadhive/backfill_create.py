"""Create mode of ``bh backfill history``: the hive-side facts behind ``create = propose``.

``backfill.collate.create`` (``off`` by default, ``propose``) decides whether the history pass
(:class:`beadhive_backfill.HistoryPass`) also proposes the units no bead matches as closed
historical beads (``docs/design/backfill-history-pipeline-adr.md``, decision D1, #143.1). The
neutral projector knows nothing of the hive, so this module supplies what it needs:

- the **creation labels** of a created bead: ``origin:backfill`` (provenance; since bh-l1m3p an
  ``origin:`` label with no ``intake:`` value is *work*, never an adopted report) plus the hive's
  identity triplet ``provider:``, ``org:``, ``repo:``; the ``source:`` label is the projector's own;
- the **prefix**: the hive's explicit registry prefix, never derived (bh-ear6v). A hive that is not
  registered, or has no ``prefix``, has none; ``propose`` refuses it.

The ``create`` choice itself is read by the config models (bh-cyqgr); the CLI passes it in. Nothing
here adds a config key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from beadhive_backfill import BackfillError
from beadlog_sources import ProjectOptions

from . import registry

CREATE_OFF = "off"
CREATE_PROPOSE = "propose"
CREATE_MODES = (CREATE_OFF, CREATE_PROPOSE)
ORIGIN_LABEL = "origin:backfill"


def parse_create(create: Any) -> str:
    """``create`` as one of :data:`CREATE_MODES` (``None`` is ``off``); anything else is refused."""
    value = CREATE_OFF if create is None else create
    if value not in CREATE_MODES:
        raise BackfillError(f"backfill.collate.create must be one of {list(CREATE_MODES)}")
    return str(value)


def _entry(hive: str | Mapping[str, Any] | None, cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    """The registered ``managed_repos`` entry of ``hive`` (a name, ``""``/``None`` for the hive
    owning cwd, or an entry), or ``{}`` when the hive is not registered."""
    if isinstance(hive, Mapping):
        return hive
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    if not entry:
        return {}
    # current_hive synthesizes an entry (with a repo-derived prefix) for an unregistered clone;
    # only a registered one carries an explicit prefix.
    keys = (entry.get("provider"), entry.get("org"), entry.get("repo"))
    registered = registry.find_entry(cfg, *keys) if all(keys) else None
    return registered or {}


def create_mode_prefix(
    hive: str | Mapping[str, Any] | None, cfg: Mapping[str, Any], create: Any
) -> str | None:
    """The explicit bead id prefix of ``hive``; ``propose`` refuses a hive without one."""
    mode = parse_create(create)
    prefix = str(_entry(hive, cfg).get("prefix") or "") or None
    if mode == CREATE_PROPOSE and prefix is None:
        raise BackfillError(
            "create mode needs the hive's explicit bead id prefix (never derived): "
            "register the hive with a prefix"
        )
    return prefix


def identity_labels(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """The hive identity triplet labels ``provider:``, ``org:``, ``repo:`` of ``entry``."""
    keys = ("provider", "org", "repo")
    if not all(entry.get(key) for key in keys):
        raise BackfillError(
            "create mode needs the hive identity (provider, org, repo) to label created beads"
        )
    return tuple(f"{key}:{entry[key]}" for key in keys)


def create_mode_options(
    hive: str | Mapping[str, Any] | None, cfg: Mapping[str, Any], create: Any
) -> ProjectOptions:
    """The projector options for ``create``: none for ``off`` (link only), and for ``propose``
    the creation labels ``origin:backfill`` + the identity triplet (the ``source:`` label stays the
    projector's)."""
    if parse_create(create) == CREATE_OFF:
        return ProjectOptions()
    entry = _entry(hive, cfg)
    return ProjectOptions(creation_labels=(ORIGIN_LABEL, *identity_labels(entry)))


__all__ = [
    "CREATE_MODES",
    "CREATE_OFF",
    "CREATE_PROPOSE",
    "ORIGIN_LABEL",
    "create_mode_options",
    "create_mode_prefix",
    "identity_labels",
    "parse_create",
]
