"""The `work.runtime` name registry (gas-city-runtime-seam-adr.md Decision 1).

Lives in the config module's domain so the config schema can check `work.runtime` without
importing the runtime seam (which reads config). Config LOAD checks the name's SYNTAX only
(:func:`check_runtime_name_syntax`): a hive's runtime choice must not break config load on a host
that lacks the package providing it. The registry is checked where the value is USED
(:func:`validate_runtime_name`: `get_runtime`, `bh config set` / `validate`, hive-ready,
doctor). `beadhive.runtime` is the public face: it re-exports `register_runtime` /
`available_runtimes` and binds the built-in factories. Mirrors `claim_authority`'s authority
registry: lookup is by name and a plugin registers itself — either by calling `register_runtime`
at import, or by declaring a `beadhive.runtimes` entry point (see :func:`load_entry_point`),
which is imported lazily and ONLY for the one name asked for.

A built-in name is seeded with no factory (`None`) so it validates before `beadhive.runtime` has
been imported; importing that module binds the real factory.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import logging
import re
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

RuntimeFactory = Callable[[Any, Any], Any]

BUILTIN_RUNTIMES = ("claude", "local", "temporal")

RUNTIMES: dict[str, RuntimeFactory | None] = dict.fromkeys(BUILTIN_RUNTIMES)


def register_runtime(name: str, factory: RuntimeFactory, *, replace: bool = False) -> None:
    """Register `factory` under `name`. A name that already has a factory is refused unless
    ``replace=True``, so two plugins cannot silently shadow each other or a built-in. A built-in
    name that is still unbound (`beadhive.runtime` not imported yet) may be bound once."""
    if not name or not isinstance(name, str):
        raise ValueError("a runtime name must be a non-empty string")
    if RUNTIMES.get(name) is not None and not replace:
        raise ValueError(f"runtime {name!r} is already registered (pass replace=True to override)")
    RUNTIMES[name] = factory


def available_runtimes() -> list[str]:
    """Every registered name, sorted — surfaced in the unknown-runtime error."""
    return sorted(RUNTIMES)


def unknown_runtime(name: str) -> ValueError:
    names = ", ".join(available_runtimes())
    return ValueError(
        f"unknown runtime {name!r}; registered: {names}; install the package that provides it "
        f"(a {ENTRY_POINT_GROUP!r} entry point named {name!r})"
    )


#: The allowed-syntax statement, value-free so config load can surface it without echoing input.
RUNTIME_NAME_SYNTAX_HINT = (
    "work.runtime must be a runtime name (letters, digits, '_', '.', '-'; starting alphanumeric)"
)

_NAME_SYNTAX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def check_runtime_name_syntax(name: object) -> str:
    """`name` itself when it is a well-formed runtime name (non-empty, starts alphanumeric, then
    alphanumerics / `_` / `.` / `-` — the shape an entry-point name can have), else `ValueError`.
    Does NOT consult the registry: this is the load-time check ("syntax at load")."""
    if not isinstance(name, str) or _NAME_SYNTAX.fullmatch(name) is None:
        raise ValueError(f"{RUNTIME_NAME_SYNTAX_HINT}, got {name!r}")
    return name


ENTRY_POINT_GROUP = "beadhive.runtimes"


class RuntimeEntryPointError(ValueError):
    """A `beadhive.runtimes` entry point could not provide the requested runtime. `kind` is one
    of `not_installed` | `import_error` | `name_mismatch` | `bad_entry_point` so callers and
    tests can tell the cases apart; the message always names the runtime and the group."""

    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


def _dist_name(ep: Any) -> str:
    dist = getattr(ep, "dist", None)
    return str(getattr(dist, "name", "") or "<unknown distribution>")


def _is_register_callable(obj: Callable[..., Any]) -> bool:
    """True when `obj` can be called with no arguments (a `register()` callable); False when it
    needs positional arguments (a `(cfg, entry)` factory)."""
    try:
        params = inspect.signature(obj).parameters.values()
    except (TypeError, ValueError):
        return False
    return not any(
        p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        for p in params
    )


def load_entry_point(
    name: str,
    *,
    entry_points: Callable[..., Any] = importlib.metadata.entry_points,
) -> None:
    """Make runtime `name` available by importing ONLY the `beadhive.runtimes` entry point
    whose name equals `name`. Every other entry point in the group is left unimported.

    Declaring an entry point (a third-party package's `pyproject.toml`)::

        [project.entry-points."beadhive.runtimes"]
        gascity = "my_package.runtime:register"

    The entry-point NAME is the `work.runtime` value it provides. Its object is either

    - a **register callable** taking no required arguments, which calls
      `beadhive.runtime.register_runtime("gascity", factory)` itself; or
    - a **factory** taking `(cfg, entry)` and returning a `Runtime`, which bh registers under the
      entry-point name.

    Raises :class:`RuntimeEntryPointError` with a distinct `kind`: `not_installed` (no entry
    point of that name), `import_error` (the import or the register call raised),
    `name_mismatch` (the register callable ran but did not register `name`), or
    `bad_entry_point` (the object is not callable). The providing distribution is logged."""
    matches = [ep for ep in entry_points(group=ENTRY_POINT_GROUP) if ep.name == name]
    if not matches:
        raise RuntimeEntryPointError(
            "not_installed",
            f"runtime {name!r} is not registered and no {ENTRY_POINT_GROUP!r} entry point named "
            f"{name!r} is installed; install the package that provides it",
        )
    ep = matches[0]
    dist = _dist_name(ep)
    where = f"{ENTRY_POINT_GROUP!r} entry point {name!r} (from {dist})"
    try:
        obj = ep.load()
    except Exception as exc:
        raise RuntimeEntryPointError(
            "import_error", f"runtime {name!r}: {where} failed to import: {exc!r}"
        ) from exc
    if not callable(obj):
        raise RuntimeEntryPointError(
            "bad_entry_point",
            f"runtime {name!r}: {where} is not callable; it must be a register() callable "
            "or a (cfg, entry) factory",
        )
    log.debug("loading runtime %r from %s via %s", name, dist, ENTRY_POINT_GROUP)
    if _is_register_callable(obj):
        try:
            obj()
        except Exception as exc:
            raise RuntimeEntryPointError(
                "import_error", f"runtime {name!r}: {where} register() failed: {exc!r}"
            ) from exc
    else:
        register_runtime(name, obj, replace=True)
    if RUNTIMES.get(name) is None:
        raise RuntimeEntryPointError(
            "name_mismatch",
            f"runtime {name!r}: {where} did not register a runtime named {name!r}; "
            f"registered: {', '.join(available_runtimes())}",
        )


def resolve_factory(
    name: str,
    *,
    entry_points: Callable[..., Any] = importlib.metadata.entry_points,
) -> RuntimeFactory:
    """The factory for `name`, loading its entry point on a registry miss; raises
    `ValueError` (the unknown-runtime message, or a :class:`RuntimeEntryPointError`)."""
    factory = RUNTIMES.get(name)
    if factory is None:
        try:
            load_entry_point(name, entry_points=entry_points)
        except RuntimeEntryPointError as exc:
            if exc.kind != "not_installed":
                raise
            raise unknown_runtime(name) from exc
        factory = RUNTIMES.get(name)
    if factory is None:
        raise unknown_runtime(name)
    return factory


def validate_runtime_name(
    name: str,
    *,
    entry_points: Callable[..., Any] = importlib.metadata.entry_points,
) -> str:
    """`name` itself when registered (loading its `beadhive.runtimes` entry point on a miss),
    else `ValueError` listing the registered names. The strict "at use" check; config load uses
    :func:`check_runtime_name_syntax` instead. A built-in name validates without a factory."""
    if name in RUNTIMES:
        return name
    resolve_factory(name, entry_points=entry_points)
    return name
