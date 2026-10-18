"""StoreTarget — the explicit seam between a repository checkout and the bead store it uses.

docs/design/two-store-scope-adr.md (bh-lniwf.1) lets one managed repository carry two bead
stores: a private, operational ``internal`` store and an optional ``public`` projection store.
Before this module every caller equated "the hive" with "the ``.beads`` under the hive's
checkout". That stays true for the default — an entry with no ``stores:`` block resolves the
one LEGACY store exactly as before — but the two notions are now separate values:

* ``repo_dir`` — where Git and ``bd -C`` run (the main checkout, or a linked worktree);
* ``beads_dir`` — which bead metadata ``bd`` opens. A DETACHED store (the public one) is
  selected through ``BEADS_DIR``, the supported discovery override, never the proxied-server-
  only ``--database`` flag and never by changing the process cwd to the store.

:func:`resolve_store` is deliberately distinct from :func:`registry.resolve_hive`: the latter
maps a hive NAME to a registry entry; this maps an entry plus a SCOPE to a store, and it never
falls back. A public request against a hive without a public store raises
:class:`StoreNotConfigured`; it is never answered from internal, nor internal from public.

Two bd behaviours measured in the spike (docs/spikes/bh-5lpl9.4-store-partitioning.md) shape
the guards here: a ``BEADS_DIR`` that does not exist SILENTLY falls back to cwd discovery (so
:meth:`StoreTarget.require_present` checks first), and an inherited ``BEADS_DIR`` pointing at a
different store would silently win (so :meth:`StoreTarget.env` refuses that conflict). A third,
measured for bh-lniwf.6 (bd 1.3.0): ``bd -C <dir>`` re-discovers the store from ``<dir>`` and
IGNORES ``BEADS_DIR`` — ``bd -C <checkout>`` with ``BEADS_DIR=<public>/.beads`` reads and writes
the checkout's INTERNAL store. :class:`StoreTransport` therefore scopes every call to a detached
store with ``-C <store root>`` (and still sets ``BEADS_DIR``), whatever cwd the caller passed.

Typer-free and subprocess-free: :func:`routes` hands the package's named ``bd`` routes a
:class:`StoreTransport` over the one shared ``bd.run`` seam.
"""

from __future__ import annotations

import json as _json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from . import registry
from .modules.config.contracts import (
    DOLT_REMOTE_DEFAULT_NAME,
    GIT_ORIGIN_REMOTE,
    ManagedRepoStores,
)

SCOPE_LEGACY = "legacy"
SCOPE_INTERNAL = "internal"
SCOPE_PUBLIC = "public"
SCOPES = (SCOPE_LEGACY, SCOPE_INTERNAL, SCOPE_PUBLIC)

#: Transport labels for diagnostics: ``git-ref`` rides the code origin's ``refs/dolt/data``;
#: ``descriptor`` is an HQ remote descriptor (bh-b1scz) whose transport bh cannot prove from
#: here; ``none`` has no remote at all.
TRANSPORT_GIT_REF = "git-ref"
TRANSPORT_DESCRIPTOR = "descriptor"
TRANSPORT_NONE = "none"

BEADS_DIR_ENV = "BEADS_DIR"
_BEADS = ".beads"


class StoreResolutionError(RuntimeError):
    """A requested store cannot be resolved or used as asked."""


class StoreConfigInvalid(StoreResolutionError):
    """The entry's ``stores`` block fails the two-store rules."""


class StoreNotConfigured(StoreResolutionError):
    """The requested scope has no store on this hive — and no fallback is ever taken."""


class StoreMissing(StoreResolutionError):
    """The store's beads directory does not exist (bd would silently fall back)."""


class StoreEnvConflict(StoreResolutionError):
    """An inherited ``BEADS_DIR`` names a different store than the one requested."""


class PublicStoreNotOperational(StoreResolutionError):
    """An operational verb (claim, assign, submit, review, merge, dispatch) got a public store."""


@dataclass(frozen=True)
class StoreTarget:
    """One resolved bead store: where to run, which metadata to open, and its policy."""

    hive: str
    scope: str
    repo_dir: Path
    beads_dir: Path
    prefix: str
    authority: str
    sync: str
    remote: str | None
    source: str | None = None
    detached: bool = False

    @property
    def operational(self) -> bool:
        """Only the internal (or legacy) store issues and dispatches work."""
        return self.scope != SCOPE_PUBLIC

    @property
    def transport(self) -> str:
        if self.scope == SCOPE_LEGACY or self.remote == GIT_ORIGIN_REMOTE:
            return TRANSPORT_GIT_REF
        return TRANSPORT_DESCRIPTOR if self.remote else TRANSPORT_NONE

    @property
    def store_root(self) -> Path:
        return self.beads_dir.parent

    def require_present(self) -> StoreTarget:
        """Refuse a store whose beads directory is absent rather than let bd fall back."""
        if not self.beads_dir.is_dir():
            raise StoreMissing(
                f"{self.scope} store for {self.hive} is not initialized at {self.beads_dir} — "
                "bd would silently fall back to the checkout's own store"
            )
        return self

    def env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment a bd subprocess for this store must run with.

        A detached store sets ``BEADS_DIR``. A non-detached store leaves the environment as it
        was (the legacy behaviour), but in both cases an inherited ``BEADS_DIR`` that names a
        DIFFERENT store is refused, because bd would honour it over everything else."""
        environ = dict(os.environ if base is None else base)
        inherited = environ.get(BEADS_DIR_ENV)
        if inherited and _norm(inherited) != _norm(self.beads_dir):
            raise StoreEnvConflict(
                f"{BEADS_DIR_ENV}={inherited} conflicts with the requested {self.scope} store "
                f"{self.beads_dir} for {self.hive}; unset it or select that store explicitly"
            )
        if self.detached:
            environ[BEADS_DIR_ENV] = str(self.beads_dir)
        return environ

    def describe(self) -> dict[str, Any]:
        """A JSON-able summary for diagnostics (never includes credentials — there are none)."""
        return {
            "hive": self.hive,
            "scope": self.scope,
            "repo_dir": str(self.repo_dir),
            "beads_dir": str(self.beads_dir),
            "prefix": self.prefix,
            "authority": self.authority,
            "sync": self.sync,
            "remote": self.remote,
            "transport": self.transport,
            "source": self.source,
            "operational": self.operational,
            "detached": self.detached,
        }


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def _plain(node: Any) -> Any:
    """ruamel CommentedMap/Seq -> plain dict/list for model validation."""
    if isinstance(node, Mapping):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, list | tuple):
        return [_plain(v) for v in node]
    return node


def configured_stores(entry: Mapping[str, Any]) -> ManagedRepoStores | None:
    """The entry's validated ``stores`` block, or None for a legacy one-store hive."""
    raw = (entry or {}).get("stores")
    if raw is None:
        return None
    try:
        return ManagedRepoStores.model_validate(_plain(raw))
    except ValidationError as exc:
        raise StoreConfigInvalid(f"{_hive(entry)}: invalid stores block: {exc}") from exc


def is_partitioned(entry: Mapping[str, Any]) -> bool:
    return configured_stores(entry) is not None


def _hive(entry: Mapping[str, Any]) -> str:
    try:
        return registry.hive_key(entry)
    except (KeyError, TypeError):
        return "<hive>"


def resolve_store(
    entry: Mapping[str, Any],
    scope: str | None = None,
    *,
    cwd: Path | str | None = None,
) -> StoreTarget:
    """Resolve ``entry``'s store for ``scope`` without ever falling back to another scope.

    ``scope=None`` means "the operational store": internal on a partitioned hive, the legacy
    store otherwise. ``internal`` on a legacy hive resolves the legacy store (it IS the
    internal store); ``public`` on a hive without a public store raises
    :class:`StoreNotConfigured`. ``cwd`` is the checkout to run in (a linked worktree passes
    its own path); the beads directory of the checkout-local store is always the MAIN
    checkout's, exactly as bd resolves it today."""
    if scope is not None and scope not in SCOPES:
        raise StoreResolutionError(f"unknown store scope {scope!r} (use one of {SCOPES})")
    hive = _hive(entry)
    checkout = registry.hive_dir(entry)
    repo_dir = Path(cwd) if cwd is not None else checkout
    prefix = str((entry or {}).get("prefix", "") or "")
    stores = configured_stores(entry)

    if stores is None:
        if scope == SCOPE_PUBLIC:
            raise StoreNotConfigured(
                f"{hive} has no public store (managed_repos[*].stores.public is not set); "
                "refusing to answer a public request from the internal store"
            )
        return StoreTarget(
            hive=hive,
            scope=SCOPE_LEGACY,
            repo_dir=repo_dir,
            beads_dir=checkout / _BEADS,
            prefix=prefix,
            authority="local",
            sync="push-pull",
            remote=None,
        )

    if scope == SCOPE_PUBLIC:
        public = stores.public
        if public is None:
            raise StoreNotConfigured(
                f"{hive} declares stores but no public store; refusing to fall back to internal"
            )
        root = Path(os.path.expanduser(str(public.path)))
        if _norm(root) == _norm(checkout) or Path(_norm(root)).is_relative_to(_norm(checkout)):
            raise StoreConfigInvalid(
                f"{hive}: stores.public.path {public.path} is inside the code checkout — a "
                "detached store lives outside it (bd refuses to init inside a workspace)"
            )
        return StoreTarget(
            hive=hive,
            scope=SCOPE_PUBLIC,
            repo_dir=repo_dir,
            beads_dir=root / _BEADS,
            prefix=str(public.prefix),
            authority=public.effective_authority(SCOPE_PUBLIC),
            sync=public.effective_sync(SCOPE_PUBLIC),
            remote=public.remote,
            source=public.source,
            detached=True,
        )

    internal = stores.internal
    assert internal is not None  # ManagedRepoStores rejects a stores block without internal
    return StoreTarget(
        hive=hive,
        scope=SCOPE_INTERNAL,
        repo_dir=repo_dir,
        beads_dir=checkout / _BEADS,
        prefix=str(internal.prefix or prefix),
        authority=internal.effective_authority(SCOPE_INTERNAL),
        sync=internal.effective_sync(SCOPE_INTERNAL),
        remote=internal.remote or hive_descriptor_name(entry),
    )


def hive_descriptor_name(entry: Mapping[str, Any]) -> str | None:
    """The name of the hive's own ``dolt_remote`` descriptor (bh-b1scz; default ``origin``),
    or None when the hive declares none. That descriptor IS the internal store's remote
    (bh-rz379), so an internal store that names no remote still resolves to it."""
    raw = (entry or {}).get("dolt_remote")
    if not isinstance(raw, Mapping) or not raw.get("url"):
        return None
    return str(raw.get("name") or DOLT_REMOTE_DEFAULT_NAME)


def operational_store(entry: Mapping[str, Any], *, cwd: Path | str | None = None) -> StoreTarget:
    """The one store plan/work/report/gates/memories/guards and default ``bh bd`` use.

    Never public: this is the subject-and-visibility table's implicit row
    (docs/design/beads-methodology-divergence-adr.md)."""
    return resolve_store(entry, None, cwd=cwd)


def all_stores(entry: Mapping[str, Any]) -> list[StoreTarget]:
    """Every configured store of ``entry``: the operational one first, then public if any."""
    targets = [operational_store(entry)]
    stores = configured_stores(entry)
    if stores is not None and stores.public is not None:
        targets.append(resolve_store(entry, SCOPE_PUBLIC))
    return targets


def require_operational(target: StoreTarget, verb: str) -> StoreTarget:
    """Refuse an operational verb against a public projection store."""
    if not target.operational:
        raise PublicStoreNotOperational(
            f"`{verb}` refuses the public store of {target.hive}: public records are a "
            "projection and never claim, assign, submit, review, merge or dispatch work"
        )
    return target


#: Operations the public store supports at all — everything else needs the internal store.
PUBLIC_OPERATIONS: frozenset[str] = frozenset(
    {"list", "show", "search", "count", "export", "sync", "status", "doctor", "init"}
)


def require_explicit_public(target: StoreTarget, operation: str) -> StoreTarget:
    """Admit ``operation`` against a public target only when it is a supported read/sync/admin
    operation; an operational target passes through untouched."""
    if target.operational:
        return target
    if operation not in PUBLIC_OPERATIONS:
        allowed = ", ".join(sorted(PUBLIC_OPERATIONS))
        raise PublicStoreNotOperational(
            f"`{operation}` is not supported on the public store of {target.hive} "
            f"(supported: {allowed})"
        )
    return target


def bead_scope(cfg: Mapping[str, Any], bead: str) -> tuple[Mapping[str, Any], str] | None:
    """``(entry, scope)`` when ``bead``'s prefix belongs to a configured PUBLIC store, else None.

    Store prefixes are unique fleet-wide (bh-lniwf.2), so a public id is recognisable by its
    prefix alone; operational resolution uses this to refuse a public id outright instead of
    reading it from any internal store (`registry.public_store_owner`)."""
    entry = registry.public_store_owner(cfg, bead)
    return None if entry is None else (entry, SCOPE_PUBLIC)


def _refuse_database_flags(args: list[str]) -> None:
    for arg in args:
        text = str(arg)
        if text in ("--database", "--db") or text.startswith(("--database=", "--db=")):
            raise StoreResolutionError(
                "store selection goes through BEADS_DIR, never --database/--db "
                "(proxied-server only)"
            )


class StoreTransport:
    """A ``beadhive_bd_cli`` ``BdTransport`` bound to ONE store.

    It forwards to the shell's own bd seam (``beadhive.bd``: ``-C`` scoping, ``--actor``, the
    configured engine) and adds only the store selection: :meth:`StoreTarget.env` (``BEADS_DIR``
    for a detached store) after :meth:`StoreTarget.require_present`. ``--database`` / ``--db``
    are refused — they are the proxied-server database switch, not a store selector. Every
    named route in ``beadhive_bd_cli.CliRoutes`` therefore works against a detached store
    without a single new argv builder in the shell."""

    def __init__(
        self, target: StoreTarget, inner: Any, *, extra_env: Mapping[str, str] | None = None
    ) -> None:
        self._target = target
        self._inner = inner
        self._extra_env = dict(extra_env or {})
        if BEADS_DIR_ENV in self._extra_env:
            raise StoreResolutionError(f"{BEADS_DIR_ENV} is selected by the store, not extra_env")

    def scoped_cwd(self, cwd):
        """The ``-C`` directory: a detached store's own root (bd ignores ``BEADS_DIR`` under
        ``-C <checkout>``), else the caller's ``cwd``."""
        return self._target.store_root if self._target.detached else cwd

    def run(self, args, cwd, actor="", capture=False, text_input=None, *, timeout=None):
        _refuse_database_flags(list(args))
        self._target.require_present()
        env = self._target.env()
        env.update(self._extra_env)
        kwargs: dict[str, Any] = {
            "actor": actor,
            "capture": capture,
            "text_input": text_input,
            "env": env,
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self._inner.run(list(args), self.scoped_cwd(cwd), **kwargs)

    def json(self, args, cwd, *, strict=False):
        res = self.run([*args, "--json"], cwd, capture=True)
        if res.returncode != 0:
            return None
        try:
            return _json.loads(res.stdout or "null")
        except ValueError:
            return None


def routes(
    target: StoreTarget,
    *,
    cwd: Path | str | None = None,
    extra_env: Mapping[str, str] | None = None,
):
    """Named ``beadhive_bd_cli`` routes against ``target``'s store, run in ``cwd`` (default:
    the target's repository checkout). A detached store always runs in its own root — see
    :meth:`StoreTransport.scoped_cwd`. ``extra_env`` adds variables to the child environment
    only (never ``BEADS_DIR``; that is the store's)."""
    from . import bd, bd_cli  # lazy: bd imports registry; keep this module import-light

    transport = StoreTransport(target, bd, extra_env=extra_env)
    return bd_cli.package().CliRoutes(transport, cwd or target.repo_dir)
