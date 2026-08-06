"""validate_config() — check a loaded config dict against :class:`BeadhiveConfig` and turn
pydantic errors + known ws-era renames into an actionable ``{level, message}`` problem list.

A pure, read-only layer over the schema (bh-5cgm.2): it never writes. It reuses the same
``{level, message}`` problem shape as the config write-path (``config._problem`` — ``error``
rejects, ``warning`` proceeds) so the CLI can echo both through the existing
``_echo_problems``.

The ws→bh rename table (rig→hive cutover, home-dir move, env prefix) is the authoritative
old→new mapping from ``docs/design/rig-to-hive-rename.md`` and ``home_migration.py``: a
stale/ported config that still carries an old key gets an actionable "renamed to X" message
naming the current key, on top of the raw pydantic rejection.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence

from pydantic import ValidationError

from .config import _problem
from .config_schema import SCHEMA_VERSION, BeadhiveConfig

# Structurally-renamed config keys pydantic now rejects (extra="forbid" → extra_forbidden):
# old dotted key -> current dotted key. Source: docs/design/rig-to-hive-rename.md.
RENAMED_KEYS: dict[str, str] = {
    "otel.rig": "otel.hive",
    "git_workspace.rig_match": "git_workspace.hive_match",
}

# Keys DELETED outright (bh-hsus.4): pydantic rejects them the same `extra_forbidden` way a
# renamed key is rejected, but there is no "new" name to point at — `config.py`'s
# migrate_hive_keys_if_needed() silently drops them from the persisted file on the next real
# CLI invocation. A stray one seen here just means that migration hasn't run yet, so it is a
# non-blocking WARNING, not an error: "does not error" is the bh-hsus.4 acceptance bar.
REMOVED_KEYS: dict[str, str] = {
    "git_workspace.enabled": (
        "git-workspace is now a required dependency (bh-hsus.4) — this flag is ignored and "
        "safe to delete."
    ),
}

# Pre-rebrand home-dir markers (~/.ws) — a string VALUE still rooted here is accepted by the
# schema but points at the wrong home, so it warns rather than errors. See home_migration.
OLD_HOME_MARKERS: tuple[str, ...] = ("~/.ws/", "~/.ws", "/.ws/")

# Full ws→bh reference table the validator prints so a ported config gets the whole picture:
# the two renamed keys plus the home-dir move and the env-var prefix. (label, old, new).
RENAMES: tuple[tuple[str, str, str], ...] = (
    ("key", "otel.rig", "otel.hive"),
    ("key", "git_workspace.rig_match", "git_workspace.hive_match"),
    ("home dir", "~/.ws", "~/.beadhive"),
    ("env prefix", "WS_*", "BH_*"),
)


def renamed_key_table() -> list[str]:
    """The ws→bh old→new rename table as aligned display lines (header + rows)."""
    rows = [("what", "ws-era (old)", "current (bh)"), *RENAMES]
    w_kind = max(len(r[0]) for r in rows)
    w_old = max(len(r[1]) for r in rows)
    return [f"  {kind:<{w_kind}}  {old:<{w_old}}  →  {new}" for kind, old, new in rows]


def renamed_keys_present(cfg) -> list[tuple[str, str]]:
    """``(old, new)`` for each structurally-renamed ws-era key actually present in ``cfg``."""
    present: list[tuple[str, str]] = []
    if not isinstance(cfg, Mapping):
        return present
    for old, new in RENAMED_KEYS.items():
        section, _, leaf = old.partition(".")
        sec = cfg.get(section)
        if isinstance(sec, Mapping) and leaf in sec:
            present.append((old, new))
    return present


def _string_leaves(node, prefix: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(dotted_key, value)`` for every string leaf in a nested config mapping/list."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            yield from _string_leaves(value, dotted)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for i, value in enumerate(node):
            yield from _string_leaves(value, f"{prefix}[{i}]")
    elif isinstance(node, str):
        yield prefix, node


def _dotted(loc) -> str:
    return ".".join(str(part) for part in loc)


def _schema_version_problem(cfg: Mapping) -> dict | None:
    """A ``schema_version`` staleness problem, or None when it matches the current schema.

    A missing or older ``schema_version`` gates (``error``): a fresh ``bh config init`` always
    stamps the current version, so its absence means a hand-rolled/ported ws-era config that
    predates versioning — the exact case bh-5cgm.7's agentic-update offer addresses. A newer
    version also gates (this bh can't understand it)."""
    sv = cfg.get("schema_version")
    if sv is None:
        return _problem(
            "error",
            f"schema_version is not set — this config predates schema versioning; add "
            f"`schema_version: {SCHEMA_VERSION}` (the current schema).",
        )
    if isinstance(sv, int) and sv < SCHEMA_VERSION:
        return _problem(
            "error",
            f"schema_version {sv} is older than the current schema ({SCHEMA_VERSION}).",
        )
    if isinstance(sv, int) and sv > SCHEMA_VERSION:
        return _problem(
            "error",
            f"schema_version {sv} is newer than this bh understands ({SCHEMA_VERSION}) "
            "— upgrade bh.",
        )
    return None


def validate_config(cfg) -> list[dict]:
    """Validate ``cfg`` (a loaded config dict) against :class:`BeadhiveConfig`.

    Returns a list of ``{level, message}`` problems — ``error`` rejects, ``warning`` proceeds;
    an empty list means the config is clean. Pure/read-only: never writes.

    - Each renamed ws-era key present (``otel.rig`` → ``otel.hive`` …) becomes an actionable
      ``error`` naming the current key, replacing pydantic's opaque "extra_forbidden".
    - Each deleted-outright key present (``git_workspace.enabled`` …) becomes a non-blocking
      ``warning`` — no new key to rename to, and the CLI's own migration drops it silently.
    - Any other unknown key is an ``error`` (schema forbids extras at every level).
    - A wrong-type / out-of-enum value is an ``error`` carrying pydantic's message.
    - A missing/older ``schema_version`` is an ``error`` (see ``_schema_version_problem``);
      any value still rooted under the old ``~/.ws`` home is a ``warning`` (accepted, stale).
    """
    raw = dict(cfg) if isinstance(cfg, Mapping) else {}
    problems: list[dict] = []

    sv_problem = _schema_version_problem(raw)
    if sv_problem is not None:
        problems.append(sv_problem)

    try:
        BeadhiveConfig.model_validate(raw)
    except ValidationError as exc:
        for err in exc.errors():
            dotted = _dotted(err["loc"])
            if err["type"] == "extra_forbidden":
                new = RENAMED_KEYS.get(dotted)
                removed_note = REMOVED_KEYS.get(dotted)
                if new:
                    problems.append(
                        _problem("error", f"`{dotted}` was renamed to `{new}` — rename this key.")
                    )
                elif removed_note:
                    problems.append(_problem("warning", f"`{dotted}` was removed — {removed_note}"))
                else:
                    from . import config_schema

                    message = (
                        f"unknown config key `{dotted}` — not part of schema v{SCHEMA_VERSION}."
                    )
                    suggestion = config_schema.suggest_key(dotted)
                    if suggestion:
                        message += f" — did you mean `{suggestion}`?"
                    problems.append(_problem("error", message))
            else:
                problems.append(_problem("error", f"`{dotted}`: {err['msg']}"))

    for dotted, value in _string_leaves(raw):
        if any(marker in value for marker in OLD_HOME_MARKERS):
            problems.append(
                _problem(
                    "warning",
                    f"`{dotted}` points under the old home `~/.ws` — the new home is "
                    "`~/.beadhive`.",
                )
            )

    return problems


# ---- stale config → paste-ready agentic-update offer (bh-5cgm.7) --------------
# The operator's chosen stance while user count is low: instead of a deterministic migrator,
# offer to have a coding agent update a stale/ported config to the current schema. We describe
# the concrete deltas; an agent (or the user) applies them, preserving comments + formatting.


def stale_deltas(cfg) -> list[str]:
    """Concrete, human-readable deltas from ``cfg`` to the current schema (v{SCHEMA_VERSION}).

    Empty == not stale. Covers a missing/older ``schema_version``, each renamed ws-era key
    present, and any value still rooted under the old ``~/.ws`` home. A wrong-type value is a
    validation error, not staleness, so it is deliberately NOT a delta here."""
    deltas: list[str] = []
    if not isinstance(cfg, Mapping):
        return deltas

    sv = cfg.get("schema_version")
    if sv is None:
        deltas.append(f"add `schema_version: {SCHEMA_VERSION}` (this config predates versioning)")
    elif isinstance(sv, int) and sv < SCHEMA_VERSION:
        deltas.append(f"bump `schema_version` from {sv} to {SCHEMA_VERSION}")

    for old, new in renamed_keys_present(cfg):
        deltas.append(f"rename key `{old}` → `{new}`")

    for dotted, value in _string_leaves(cfg):
        if any(marker in value for marker in OLD_HOME_MARKERS):
            deltas.append(f"repoint `{dotted}` from the old home `~/.ws` to `~/.beadhive`")

    return deltas


def is_stale(cfg) -> bool:
    """Whether ``cfg`` carries any staleness (renamed key, missing/old schema_version, old home
    path) — i.e. whether an agentic-update offer applies."""
    return bool(stale_deltas(cfg))


def agentic_update_prompt(cfg) -> str | None:
    """A self-contained, paste-ready instruction for a coding agent to bring a stale config to
    schema v{SCHEMA_VERSION}. Returns None when the config is already current (no offer).

    NOT a deterministic migrator and NOT an auto-write: it just describes the concrete deltas
    (see :func:`stale_deltas`) for the user or an agent to apply, preserving comments/formatting.
    """
    deltas = stale_deltas(cfg)
    if not deltas:
        return None
    lines = [
        f"Update my Beadhive config (~/.beadhive/config.yaml) to schema version "
        f"{SCHEMA_VERSION}. Apply exactly these changes, preserving all comments, key order,",
        "and formatting — change only the keys listed:",
        "",
        *(f"  - {delta}" for delta in deltas),
        "",
        "Also update anything that drives bh from the environment: the `WS_*` env-var prefix",
        "is now `BH_*` (e.g. `WS_HOME` → `BH_HOME`).",
        "Do not add, remove, or reorder any other keys; leave every unlisted value untouched.",
    ]
    return "\n".join(lines)
