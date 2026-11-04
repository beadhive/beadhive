"""Composition root for the ``beadlog.store`` capability (beadlog-format-adr.md amendment A6).

``bh backfill apply|export --store <name>`` asks this module for a store. ``bd`` (the default) is
root's built-in bd StoreLoader and never reaches here. Any other name is served only when every
layer agrees, and the first that disagrees is a typed :class:`BeadlogStoreUnavailable` whose
message says what to do:

1. the name is a known store (a provider row below maps ``xtdb`` to plugin ``beadlog-xtdb``);
2. ``plugin_kernel`` parses and a discovered manifest provides ``beadlog.store@1`` under that id;
3. the plugin's fragment is valid and enabled: ``plugins.<id>.enabled`` is exactly ``True`` and
   ``plugin_kernel.enabled`` does not turn it off (installing the package enables nothing);
4. no other enabled plugin owns the capability (or ``plugin_kernel.capability_owners`` names it);
5. only then is the provider's package imported, lazily, and its factory's result bound through
   :func:`~beadhive.kernel.plugins.bind_application_port`. A missing package is an install hint.

So with the plugin disabled or not installed nothing here imports it, and nothing imports
psycopg. The store's password is never read here: the factory receives a
:data:`~beadhive.modules.beadlog_store.contracts.PasswordSource` (injected by the caller; the CLI
backs it with the host's fnox secret broker, :mod:`beadhive.beadlog_store_credentials`) that the
store calls when it connects, and every message names keys, never values.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from importlib.util import find_spec
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from ..kernel.plugins import (
    BEADHIVE_COMPATIBILITY_VERSION,
    PLUGIN_KERNEL_VERSION,
    CapabilityBindingError,
    CapabilityKey,
    CapabilityRef,
    DiagnosticCode,
    DiscoveredPlugin,
    DiscoveryResult,
    ManifestSource,
    PluginKernelConfig,
    ProviderBinding,
    ProviderKey,
    bind_application_port,
    builtin_manifest_source,
    discover_plugins,
    parse_kernel_config,
)
from ..modules.beadlog_store.contracts import (
    BEADLOG_STORE_API_VERSION,
    BEADLOG_STORE_CAPABILITY_ID,
    BUILTIN_STORE,
    BeadlogStore,
    BeadlogStoreUnavailable,
    PasswordSource,
)
from ..modules.config.contracts import BeadlogXtdbConfig

#: The capability: ``beadlog.store``, API major 1.
BEADLOG_STORE = CapabilityRef(BEADLOG_STORE_CAPABILITY_ID, BEADLOG_STORE_API_VERSION)
#: The typed request bootstrap makes for ``beadlog.store``.
BEADLOG_STORE_PORT = CapabilityKey(BEADLOG_STORE, BeadlogStore)

#: Plugin-config models for the store plugins root knows (validated before anything is built).
STORE_FRAGMENT_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {"beadlog-xtdb": BeadlogXtdbConfig}
)


@dataclass(frozen=True)
class BeadlogStoreRequest:
    """What a provider's factory receives: the validated ``plugins.<id>`` fragment and the
    credential port (called by the store only when it connects; never here)."""

    plugin_id: str
    settings: Mapping[str, Any]
    password_source: PasswordSource = field(repr=False, compare=False)


@dataclass(frozen=True)
class BeadlogStoreProvider:
    """A first-party ``beadlog.store`` runtime binding. ``loader`` imports the plugin package
    lazily and is called only after discovery selected ``plugin_id`` and it is enabled."""

    store: str
    plugin_id: str
    #: What the operator installs to get the package, named in the refusal.
    install_hint: str
    loader: Callable[[BeadlogStoreRequest], object] = field(repr=False, compare=False)
    #: Third-party top-level modules the store needs at run time. The plugin's own import
    #: packages ship in the root wheel, so their presence says nothing: a missing requirement is
    #: the "extra not installed" signal, probed before the loader runs.
    requires: tuple[str, ...] = ()

    def missing_requirements(self) -> tuple[str, ...]:
        """The :attr:`requires` modules that cannot be found (located, never imported)."""
        return tuple(name for name in self.requires if module_missing(name))


def module_missing(name: str) -> bool:
    """Whether top-level module *name* cannot be found (located, never imported)."""
    try:
        return find_spec(name) is None
    except (ImportError, ValueError):
        return True


def _xtdb_store(request: BeadlogStoreRequest) -> object:
    build = import_module("beadhive_beadlog_xtdb.factory").build
    return build(request.settings, request.password_source)


#: Built-in runtime bindings for manifests that provide ``beadlog.store``. A row is inert until a
#: discovered, enabled, selected manifest has its id.
BUILTIN_STORE_PROVIDERS: tuple[BeadlogStoreProvider, ...] = (
    BeadlogStoreProvider(
        "xtdb", "beadlog-xtdb", "beadhive[xtdb]", _xtdb_store, requires=("psycopg",)
    ),
)


def store_names(
    providers: Sequence[BeadlogStoreProvider] = BUILTIN_STORE_PROVIDERS,
) -> tuple[str, ...]:
    """Every ``--store`` value: the built-in ``bd`` first, then each provider's store."""
    return (BUILTIN_STORE, *(provider.store for provider in providers))


# --------------------------------------------------------------------------------------------
# Settings and the credential port
# --------------------------------------------------------------------------------------------


def _plain(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    return value


@dataclass(frozen=True)
class StorePluginSettings:
    """One store plugin's ``plugins.<id>`` fragment as root reads it."""

    plugin_id: str
    #: The validated fragment (defaults applied); empty when the fragment is invalid.
    values: Mapping[str, Any]
    #: ``plugins.<id>.enabled`` is exactly ``True``.
    own_enabled: bool
    valid: bool


def plugin_settings(
    plugin_id: str, plugins: Mapping[str, object] | BaseModel | None
) -> StorePluginSettings:
    """Read and validate ``plugins.<plugin_id>`` from the ``plugins`` section."""
    section = _plain(plugins) or {}
    raw = _plain(section.get(plugin_id)) if isinstance(section, Mapping) else None
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return StorePluginSettings(plugin_id, MappingProxyType({}), False, False)
    own_enabled = raw.get("enabled") is True
    model = STORE_FRAGMENT_MODELS.get(plugin_id)
    if model is None:
        return StorePluginSettings(plugin_id, MappingProxyType(dict(raw)), own_enabled, True)
    try:
        values = model.model_validate(dict(raw)).model_dump(by_alias=True)
    except ValidationError:
        return StorePluginSettings(plugin_id, MappingProxyType({}), own_enabled, False)
    return StorePluginSettings(plugin_id, MappingProxyType(values), own_enabled, True)


def credential_configured(settings: Mapping[str, Any]) -> bool:
    """``credential`` names a complete fnox lookup (``config_path``, ``profile``, ``key``)."""
    ref = settings.get("credential")
    return isinstance(ref, Mapping) and all(
        ref.get(key) for key in ("config_path", "profile", "key")
    )


# --------------------------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreComposition:
    """Metadata-only view of the category on this host: no plugin package is imported for it."""

    #: Every discovered manifest providing ``beadlog.store``, ignoring enablement.
    installed: tuple[DiscoveredPlugin, ...]
    #: Discovery with every store plugin's own flag folded into the kernel policy.
    discovery: DiscoveryResult
    owner: str | None
    conflict: bool
    policy_error: str | None
    policy: PluginKernelConfig
    settings: Mapping[str, StorePluginSettings]

    def manifest(self, plugin_id: str) -> DiscoveredPlugin | None:
        return next((p for p in self.installed if p.manifest.plugin_id == plugin_id), None)

    def plugin_enabled(self, plugin_id: str) -> bool:
        """Own flag exactly ``True``, fragment valid, and ``plugin_kernel.enabled`` not false."""
        settings = self.settings.get(plugin_id) or plugin_settings(plugin_id, None)
        return (
            settings.own_enabled
            and settings.valid
            and self.policy.enabled.get(plugin_id, True) is not False
        )


def _discover(sources: Sequence[ManifestSource], policy: PluginKernelConfig) -> DiscoveryResult:
    return discover_plugins(
        list(sources),
        config=policy,
        beadhive_version=BEADHIVE_COMPATIBILITY_VERSION,
        kernel_version=PLUGIN_KERNEL_VERSION,
    )


def compose_store_category(
    *,
    plugins: Mapping[str, object] | BaseModel | None,
    plugin_kernel: Mapping[str, object] | PluginKernelConfig | BaseModel | None,
    sources: Sequence[ManifestSource] | None = None,
) -> StoreComposition:
    """Discover the ``beadlog.store`` providers with each plugin's own flag folded in (a built-in
    manifest is otherwise enabled by default); an invalid fragment folds to off."""
    sources = tuple(sources) if sources is not None else (builtin_manifest_source(),)
    try:
        policy = parse_kernel_config(_plain(plugin_kernel))  # type: ignore[arg-type]
        policy_error = None
    except (TypeError, ValueError) as exc:
        policy, policy_error = PluginKernelConfig(), str(exc)
    inventory = _discover(
        sources, PluginKernelConfig(allow_external_entry_points=policy.allow_external_entry_points)
    )
    installed = tuple(p for p in inventory.plugins if BEADLOG_STORE in p.manifest.capabilities)
    ids = sorted({p.manifest.plugin_id for p in installed} | set(STORE_FRAGMENT_MODELS))
    settings = {plugin_id: plugin_settings(plugin_id, plugins) for plugin_id in ids}
    enabled = dict(policy.enabled)
    for plugin in installed:
        item = settings[plugin.manifest.plugin_id]
        enabled[plugin.manifest.plugin_id] = (
            item.own_enabled
            and item.valid
            and policy.enabled.get(plugin.manifest.plugin_id, True) is not False
        )
    folded = PluginKernelConfig(
        enabled=MappingProxyType(enabled),
        capability_owners=policy.capability_owners,
        allow_external_entry_points=policy.allow_external_entry_points,
    )
    discovery = (
        DiscoveryResult((), (), ()) if policy_error is not None else _discover(sources, folded)
    )
    conflict = any(
        d.code is DiagnosticCode.DUPLICATE_CAPABILITY and d.capability == BEADLOG_STORE
        for d in discovery.errors
    )
    return StoreComposition(
        installed=installed,
        discovery=discovery,
        owner=discovery.owner_of(BEADLOG_STORE),
        conflict=conflict,
        policy_error=policy_error,
        policy=policy,
        settings=MappingProxyType(settings),
    )


# --------------------------------------------------------------------------------------------
# The composition root
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundBeadlogStore:
    """One composed store: ``port`` is the bound object."""

    store: str
    plugin_id: str
    port: BeadlogStore = field(repr=False, compare=False)


PasswordSourceFactory = Callable[[Mapping[str, Any]], PasswordSource]


def _no_password(_settings: Mapping[str, Any]) -> PasswordSource:
    return lambda: None


class BeadlogStores:
    """The process's ``beadlog.store`` composition root for one configuration snapshot.

    ``plugins`` and ``plugin_kernel`` are the config sections (raw mappings or models).
    ``password_source_for`` turns a validated fragment into the store's credential port (the
    CLI passes :func:`beadhive.beadlog_store_credentials.fnox_password_source`); the port is
    handed to the factory and never called here. Without one the store gets no password.
    Discovery runs once, lazily.
    """

    def __init__(
        self,
        *,
        plugins: Mapping[str, object] | BaseModel | None,
        plugin_kernel: Mapping[str, object] | PluginKernelConfig | BaseModel | None,
        password_source_for: PasswordSourceFactory | None = None,
        sources: Sequence[ManifestSource] | None = None,
        providers: Sequence[BeadlogStoreProvider] = BUILTIN_STORE_PROVIDERS,
    ) -> None:
        self._plugins = plugins
        self._plugin_kernel = plugin_kernel
        self._password_source_for = password_source_for
        self._sources = tuple(sources) if sources is not None else None
        self._providers = tuple(providers)
        self._composition: StoreComposition | None = None

    @property
    def composition(self) -> StoreComposition:
        if self._composition is None:
            self._composition = compose_store_category(
                plugins=self._plugins, plugin_kernel=self._plugin_kernel, sources=self._sources
            )
        return self._composition

    def names(self) -> tuple[str, ...]:
        return store_names(self._providers)

    def resolve(self, name: str) -> BoundBeadlogStore:
        """The store answering to ``--store <name>``, or a :class:`BeadlogStoreUnavailable`."""
        provider = next((p for p in self._providers if p.store == name), None)
        if name == BUILTIN_STORE or provider is None:
            raise BeadlogStoreUnavailable(
                f"--store must be one of {', '.join(self.names())}"
                + ("" if name == BUILTIN_STORE else f" (got {name!r})")
                + (
                    "; bd is the built-in store and needs no plugin"
                    if name == BUILTIN_STORE
                    else ""
                )
            )
        plugin_id = provider.plugin_id
        what = f"--store {name} needs the {plugin_id} plugin (capability {BEADLOG_STORE.render()})"
        install = (
            f"install `{provider.install_hint}` (for example `uv sync --extra xtdb` in a checkout "
            f"or `pip install '{provider.install_hint}'`)"
        )
        enable = (
            f"set plugins.{plugin_id}.enabled: true on this host "
            f"(`bh config set plugins.{plugin_id}.enabled true`), then check the node with "
            f"`bh plugin {plugin_id} doctor`"
        )
        composition = self.composition
        if composition.policy_error is not None:
            raise BeadlogStoreUnavailable(
                f"{what}, but plugin_kernel is invalid — run `bh config validate`"
            )
        if composition.manifest(plugin_id) is None:
            raise BeadlogStoreUnavailable(
                f"{what}, which is not installed — {install}, then {enable}"
            )
        if composition.conflict:
            raise BeadlogStoreUnavailable(
                f"{what}, but two enabled plugins provide {BEADLOG_STORE.render()} — select one "
                "with plugin_kernel.capability_owners"
            )
        owner = composition.owner
        configured = composition.policy.capability_owners.get(BEADLOG_STORE.render())
        other = owner if owner is not None else configured
        if other is not None and other != plugin_id:
            raise BeadlogStoreUnavailable(
                f"{what}, but {other!r} is the selected owner of {BEADLOG_STORE.render()}"
            )
        settings = composition.settings[plugin_id]
        if owner is None:
            if settings.own_enabled and not settings.valid:
                raise BeadlogStoreUnavailable(
                    f"{what}, but plugins.{plugin_id} is invalid — run `bh config validate` "
                    "(an invalid plugin configuration is retained inactive)"
                )
            raise BeadlogStoreUnavailable(
                f"{what}, which is disabled (the default) — {enable}; if `bh plugin {plugin_id} "
                f"status` reports it not installed, first {install}"
            )
        return self._bind(provider, settings, what, install, enable)

    def _bind(
        self,
        provider: BeadlogStoreProvider,
        settings: StorePluginSettings,
        what: str,
        install: str,
        enable: str,
    ) -> BoundBeadlogStore:
        plugin_id = provider.plugin_id
        password_source_for = self._password_source_for or _no_password
        request = BeadlogStoreRequest(
            plugin_id, settings.values, password_source_for(settings.values)
        )
        if provider.missing_requirements():
            raise BeadlogStoreUnavailable(f"{what}, but its package is not installed — {install}")
        try:
            port = provider.loader(request)
        except ModuleNotFoundError:
            raise BeadlogStoreUnavailable(
                f"{what}, but its package is not installed — {install}"
            ) from None
        except Exception as exc:  # noqa: BLE001 - a factory failure is a typed refusal
            # The exception text is not repeated: a factory error may echo configuration.
            raise BeadlogStoreUnavailable(
                f"{what}, but the store could not be built ({type(exc).__name__}) — run "
                f"`bh plugin {plugin_id} doctor`"
            ) from None
        binding = ProviderBinding(ProviderKey(plugin_id, BEADLOG_STORE), port)
        try:
            bound = bind_application_port(
                BEADLOG_STORE_PORT, self.composition.discovery, (binding,)
            )
        except CapabilityBindingError:
            raise BeadlogStoreUnavailable(
                f"{what}, but its store does not implement {BEADLOG_STORE.render()}"
            ) from None
        return BoundBeadlogStore(provider.store, plugin_id, port=bound)


__all__ = [
    "BEADLOG_STORE",
    "BEADLOG_STORE_PORT",
    "BUILTIN_STORE_PROVIDERS",
    "STORE_FRAGMENT_MODELS",
    "BeadlogStoreProvider",
    "BeadlogStoreRequest",
    "BeadlogStores",
    "BoundBeadlogStore",
    "StoreComposition",
    "StorePluginSettings",
    "compose_store_category",
    "credential_configured",
    "plugin_settings",
    "store_names",
]
