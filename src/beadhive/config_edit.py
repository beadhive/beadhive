"""Dotted config editing, literal parsing, and write-time validation."""

from __future__ import annotations

import json
import re
from collections.abc import MutableMapping

from pydantic import TypeAdapter
from ruamel.yaml.comments import CommentedMap


def problem(level: str, message: str) -> dict:
    return {"level": level, "message": message}


def not_set_message(dotted: str) -> str:
    from . import config_schema

    suggestion = config_schema.suggest_key(dotted)
    message = f"{dotted} is not set"
    return f"{message} — did you mean '{suggestion}'?" if suggestion else message


def has_errors(problems) -> bool:
    return any(item["level"] == "error" for item in problems)


def split_key(dotted: str) -> list[str]:
    parts = [part for part in str(dotted).split(".") if part != ""]
    if not parts:
        raise ValueError(f"empty config key: {dotted!r}")
    return parts


def coerce_value(raw: str, as_json: bool = False):
    if as_json:
        return json.loads(raw)
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def validate(api, parts: list[str], value) -> list[dict]:
    from . import config_schema

    problems: list[dict] = []
    dotted = ".".join(parts)
    literal_checked = False
    if dotted == "otel.protocol" and value not in api.OTEL_PROTOCOLS:
        problems.append(
            problem(
                "error",
                f"otel.protocol must be one of {list(api.OTEL_PROTOCOLS)}, got {value!r}",
            )
        )
        literal_checked = True
    if parts[-1] == "enabled" and not isinstance(value, bool):
        problems.append(problem("error", f"{dotted} must be a boolean (true|false), got {value!r}"))
    if dotted == "archive.window_days" and (not isinstance(value, int) or value <= 0):
        problems.append(
            problem("error", f"archive.window_days must be a positive integer, got {value!r}")
        )
    if parts[-1] == "validate_subset" and value:
        if config_schema.SUBSET_PLACEHOLDER not in str(value):
            problems.append(
                problem(
                    "error",
                    f"{dotted} must contain the {config_schema.SUBSET_PLACEHOLDER} placeholder "
                    f"(where bh substitutes the failing test names), got {value!r}",
                )
            )
    if dotted == "work.routing.tiers":
        try:
            TypeAdapter(list[config_schema.RoutingTierConfig]).validate_python(value)
        except ValueError as exc:
            problems.append(problem("error", f"{dotted} is invalid: {exc}"))
    if not literal_checked:
        choices = config_schema.literal_choices(dotted)
        if choices is not None and value not in choices:
            allowed = "|".join(str(choice) for choice in choices)
            problems.append(problem("error", f"{dotted} must be one of {allowed}, got {value!r}"))
    if parts[0] not in api.KNOWN_SECTIONS:
        message = f"unknown config section '{parts[0]}' — writing it anyway"
        suggestion = config_schema.suggest_key(dotted)
        if suggestion:
            message += f" (did you mean '{suggestion}'?)"
        problems.append(problem("warning", message))
    return problems


def descend(cfg, parts: list[str]):
    node = cfg
    for part in parts:
        if not isinstance(node, MutableMapping) or part not in node:
            return (False, None)
        node = node[part]
    return (True, node)


def literal_violations(api, cfg=None) -> list[dict]:
    from . import config_schema

    cfg = cfg if cfg is not None else api.load()
    violations: list[dict] = []
    for dotted in api._leaf_paths(cfg):
        choices = config_schema.literal_choices(dotted)
        if choices is None:
            continue
        found, value = api._descend(cfg, dotted.split("."))
        if found and value not in choices:
            violations.append(
                {
                    "key": dotted,
                    "value": value,
                    "choices": choices,
                    "default": config_schema.field_default(dotted),
                }
            )
    return violations


def warn_literal_violations(api) -> None:
    try:
        cfg = api.load()
    except FileNotFoundError:
        return
    violations = api.literal_violations(cfg)
    if not violations:
        return
    for item in violations:
        allowed = "|".join(str(choice) for choice in item["choices"])
        api._warning(
            "config_literal_value_invalid",
            logger_name=api.__name__,
            key=item["key"],
            value=item["value"],
            allowed=allowed,
            effective=item["default"],
            hint=(
                f"config: {item['key']} = {item['value']!r} is not one of {allowed} "
                f"(using default {item['default']!r})"
            ),
        )


def get_value(api, dotted: str, cfg=None, scope: str | None = None) -> dict:
    parts = api._split_key(dotted)
    if cfg is None:
        if scope == api.SCOPE_HOST:
            cfg = api.load_host()
        elif scope == api.SCOPE_FLEET:
            cfg = api.load_fleet()
        else:
            cfg = api.load()
    found, value = api._descend(cfg, parts)
    if not found:
        return {
            "ok": False,
            "problems": [api._problem("error", api._not_set_message(dotted))],
            "value": None,
        }
    return {"ok": True, "problems": [], "value": value}


def _set_in(api, dotted: str, raw: str, as_json: bool, cfg, *, persist: bool, scope: str) -> dict:
    parts = api._split_key(dotted)
    value = api.coerce_value(raw, as_json)
    problems = api._validate(parts, value)
    if persist and scope == api.SCOPE_HOST:
        try:
            api._reject_fleet_override_for_key(parts, value)
        except api.ConfigError as exc:
            problems.append(api._problem("error", str(exc)))
            return {"ok": False, "problems": problems, "old": None, "new": None}
    if persist:
        cfg = api.load_fleet() if scope == api.SCOPE_FLEET else api.load_host()
    node = cfg
    for index, part in enumerate(parts[:-1]):
        child = node.get(part)
        if child is None:
            child = CommentedMap()
            node[part] = child
        elif not isinstance(child, MutableMapping):
            here = ".".join(parts[: index + 1])
            problems.append(api._problem("error", f"cannot descend into '{here}': it is a scalar"))
            return {"ok": False, "problems": problems, "old": None, "new": None}
        node = child
    leaf = parts[-1]
    old = node.get(leaf)
    if api._has_errors(problems):
        return {"ok": False, "problems": problems, "old": old, "new": None}
    node[leaf] = value
    if persist:
        api.save_fleet(cfg) if scope == api.SCOPE_FLEET else api.save(cfg)
    return {"ok": True, "problems": problems, "old": old, "new": value}


def set_value(api, dotted: str, raw: str, as_json: bool = False, cfg=None, scope=None) -> dict:
    persist = cfg is None
    scope = scope or api.SCOPE_HOST
    if not persist:
        return _set_in(api, dotted, raw, as_json, cfg, persist=False, scope=scope)
    with api._write_transaction(scope):
        return _set_in(api, dotted, raw, as_json, None, persist=True, scope=scope)


def _unset_in(api, dotted: str, cfg, *, persist: bool, scope: str) -> dict:
    parts = api._split_key(dotted)
    if persist:
        cfg = api.load_fleet() if scope == api.SCOPE_FLEET else api.load_host()
    chain = [cfg]
    node = cfg
    for part in parts[:-1]:
        child = node.get(part) if isinstance(node, MutableMapping) else None
        if not isinstance(child, MutableMapping):
            return {
                "ok": False,
                "problems": [api._problem("error", api._not_set_message(dotted))],
                "old": None,
                "new": None,
            }
        node = child
        chain.append(node)
    leaf = parts[-1]
    if not isinstance(node, MutableMapping) or leaf not in node:
        return {
            "ok": False,
            "problems": [api._problem("error", f"{dotted} is not set")],
            "old": None,
            "new": None,
        }
    old = node[leaf]
    del node[leaf]
    for ancestor in range(len(parts) - 1, 0, -1):
        if chain[ancestor]:
            break
        del chain[ancestor - 1][parts[ancestor - 1]]
    if persist:
        api.save_fleet(cfg) if scope == api.SCOPE_FLEET else api.save(cfg)
    return {"ok": True, "problems": [], "old": old, "new": None}


def unset_value(api, dotted: str, cfg=None, scope: str | None = None) -> dict:
    persist = cfg is None
    scope = scope or api.SCOPE_HOST
    if not persist:
        return _unset_in(api, dotted, cfg, persist=False, scope=scope)
    with api._write_transaction(scope):
        return _unset_in(api, dotted, None, persist=True, scope=scope)


def set_hive_feature_flag(api, entry, feature: str, enabled: bool) -> dict:
    problems = api._validate([feature, "enabled"], enabled)
    if api._has_errors(problems):
        return {"ok": False, "problems": problems, "old": None, "new": None}
    sub = entry.get(feature)
    if sub is None:
        sub = CommentedMap()
        sub.fa.set_flow_style()
        entry[feature] = sub
    elif not isinstance(sub, MutableMapping):
        error = api._problem("error", f"cannot descend into '{feature}': it is a scalar")
        return {"ok": False, "problems": problems + [error], "old": None, "new": None}
    old = sub.get("enabled")
    sub["enabled"] = enabled
    return {"ok": True, "problems": problems, "old": old, "new": enabled}
