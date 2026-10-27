"""Composition root for the ``tracker.adapter`` plugin category (tracker-adapter-plugin-adr.md).

A bead store is served by a tracker adapter only when every layer agrees, and the first layer
that disagrees is reported as a typed refusal, in the ADR's order (section 4, rules R6 to R13):

1. the store binds an adapter (``managed_repos[].stores.public.adapter``), else R13, which is
   the consumer's "tracker unconfigured" (:class:`TrackerUnconfigured`), not an error;
2. the hive identity resolves to the tracker project, else R12;
3. a discovered manifest has the bound plugin id, else R9;
4. no ownership conflict (R11) and the bound plugin is the selected owner (R10);
5. the plugin is enabled: its own ``plugins.<id>.enabled`` is exactly ``True`` AND
   ``plugin_kernel.enabled`` does not turn it off, else R6;
6. every operation the consumer is about to use is enabled, else R8 (a read) or R7 (a write),
   so a write that is not enabled fails before any approval prompt.

Only after all of that is the selected provider's package imported, lazily, and bound through
:func:`~beadhive.kernel.plugins.bind_application_port` (the kernel is used unchanged). The
bound object is narrowed structurally: a writer Protocol (``CommentWriter``, ``LabelWriter``)
is present on it only when that write operation is enabled AND the adapter implements it, and
even then each call must carry a :class:`~beadhive.modules.tracker.contracts.WriteGrant` for
that operation. Enabling a write is necessary, never sufficient.

One adapter per (hive, store scope) per :class:`TrackerAdapters` instance, which is the
process's composition root for one configuration snapshot: it is never re-created per call,
because the adapter holds in-memory state (its grant ledger) that must not be forked.

Nothing here reads a credential: the token source is injected, and every refusal names only
plugin ids, operations, configuration keys and the hive triplet.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import import_module
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ValidationError

from beadhive_plugins.credentials import TokenSource

from ..kernel.plugins import (
    BEADHIVE_COMPATIBILITY_VERSION,
    PLUGIN_KERNEL_VERSION,
    CapabilityBindingError,
    CapabilityKey,
    CapabilityRef,
    DiagnosticCode,
    DiagnosticSeverity,
    DiscoveredPlugin,
    DiscoveryResult,
    ManifestSource,
    PluginDiagnostic,
    PluginKernelConfig,
    ProviderBinding,
    ProviderKey,
    bind_application_port,
    builtin_manifest_source,
    discover_plugins,
    parse_kernel_config,
)
from ..modules.config.contracts import KNOWN_TRACKER_ADAPTERS, TrackerGithubConfig
from ..modules.tracker.contracts import (
    PORT_API_VERSION,
    WRITE_OPERATIONS,
    CommentWriter,
    IssueResolver,
    LabelWriter,
    Operation,
    OperationNotEnabled,
    ProjectRef,
    TrackerAdapter,
    WriteGrant,
)

#: The category's capability: ``tracker.adapter``, API major equal to the port's version.
TRACKER_ADAPTER = CapabilityRef("tracker.adapter", PORT_API_VERSION)

#: The typed request bootstrap makes for ``tracker.adapter``: the capability plus its port.
TRACKER_ADAPTER_PORT = CapabilityKey(TRACKER_ADAPTER, TrackerAdapter)

#: Where each operation is switched on inside a tracker plugin's ``plugins.<id>`` namespace (the
#: category's key convention, ratified for ``tracker-github`` in the ADR section 3).
OPERATION_FLAG_KEYS: Mapping[Operation, str] = MappingProxyType(
    {
        Operation.ISSUES_READ: "read.issues.enabled",
        Operation.COMMENTS_READ: "read.comments.enabled",
        Operation.COMMENT_CREATE: "write.comment.enabled",
        Operation.LABELS_CHANGE: "write.labels.enabled",
    }
)

#: ``labels.change`` also needs a non-empty owned-label set: an empty one means no label write
#: is possible, so the operation is not enabled.
LABEL_OWNERSHIP_KEY = "write.labels.owned_prefixes"

#: Plugin-config models for the tracker plugins root knows; any other tracker plugin's
#: fragment is read as raw data under the same exact-``True`` rule.
TRACKER_FRAGMENT_MODELS: Mapping[str, type[BaseModel]] = MappingProxyType(
    {"tracker-github": TrackerGithubConfig}
)

_ADAPTER_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_SOURCE_TRACKER = MappingProxyType({"github-issues": "github"})


# --------------------------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------------------------


class TrackerRule(StrEnum):
    """Stable identities of the composition root's refusals (ADR section 4)."""

    NOT_BOUND = "not-bound"  # R13
    INVALID_BINDING = "invalid-binding"  # R2/R4, re-checked on an unvalidated document
    IDENTITY = "identity"  # R12
    NOT_INSTALLED = "not-installed"  # R9, no discovered manifest has the id
    CONFLICT = "conflict"  # R11
    NOT_OWNER = "not-owner"  # R10
    DISABLED = "disabled"  # R6
    INVALID_CONFIG = "invalid-config"  # the plugin's fragment or plugin_kernel is invalid
    READ_NOT_ENABLED = "read-not-enabled"  # R8
    WRITE_NOT_ENABLED = "write-not-enabled"  # R7
    PACKAGE_MISSING = "package-missing"  # R9, selected but the package is not importable
    NOT_CONFORMING = "not-conforming"  # the package built something that is not the port


_RULE_IDS: Mapping[TrackerRule, str] = MappingProxyType(
    {
        TrackerRule.NOT_BOUND: "R13",
        TrackerRule.INVALID_BINDING: "R2",
        TrackerRule.IDENTITY: "R12",
        TrackerRule.NOT_INSTALLED: "R9",
        TrackerRule.CONFLICT: "R11",
        TrackerRule.NOT_OWNER: "R10",
        TrackerRule.DISABLED: "R6",
        TrackerRule.READ_NOT_ENABLED: "R8",
        TrackerRule.WRITE_NOT_ENABLED: "R7",
        TrackerRule.PACKAGE_MISSING: "R9",
    }
)

_RULE_CODES: Mapping[TrackerRule, DiagnosticCode] = MappingProxyType(
    {
        TrackerRule.NOT_BOUND: DiagnosticCode.TRACKER_CONFIG,
        TrackerRule.INVALID_BINDING: DiagnosticCode.TRACKER_CONFIG,
        TrackerRule.IDENTITY: DiagnosticCode.TRACKER_CONFIG,
        TrackerRule.NOT_INSTALLED: DiagnosticCode.TRACKER_PACKAGE,
        TrackerRule.CONFLICT: DiagnosticCode.DUPLICATE_CAPABILITY,
        TrackerRule.NOT_OWNER: DiagnosticCode.TRACKER_CONFIG,
        TrackerRule.DISABLED: DiagnosticCode.DISABLED_PLUGIN,
        TrackerRule.INVALID_CONFIG: DiagnosticCode.TRACKER_CONFIG,
        TrackerRule.READ_NOT_ENABLED: DiagnosticCode.TRACKER_CAPABILITY,
        TrackerRule.WRITE_NOT_ENABLED: DiagnosticCode.TRACKER_CAPABILITY,
        TrackerRule.PACKAGE_MISSING: DiagnosticCode.TRACKER_PACKAGE,
        TrackerRule.NOT_CONFORMING: DiagnosticCode.TRACKER_PACKAGE,
    }
)


class TrackerBindingError(Exception):
    """The composition root refused to serve a store. ``message`` is what the operator sees;
    ``rule`` is stable; ``code`` is the diagnostic code a readiness surface renders."""

    def __init__(self, rule: TrackerRule, message: str, *, plugin_id: str = "?") -> None:
        super().__init__(message)
        self.rule = rule
        self.message = message
        self.plugin_id = plugin_id

    @property
    def rule_id(self) -> str | None:
        """The ADR rule number (``R6`` to ``R13``), when the refusal is one of them."""
        return _RULE_IDS.get(self.rule)

    @property
    def code(self) -> DiagnosticCode:
        return _RULE_CODES[self.rule]

    def diagnostic(self, severity: DiagnosticSeverity) -> PluginDiagnostic:
        return PluginDiagnostic(
            self.code, severity, self.message, plugin_id=self.plugin_id, capability=TRACKER_ADAPTER
        )


class TrackerUnconfigured(TrackerBindingError):
    """R13: the store binds no tracker adapter. Not an error: the consumer's "unconfigured"."""


# --------------------------------------------------------------------------------------------
# Providers: the lazy composition-root rows
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackerAdapterRequest:
    """Everything a provider's factory receives. ``settings`` is the validated plugin fragment
    with every operation flag folded to the resolved ``enabled`` set; ``project`` is derived
    from the hive identity. No credential value is held: ``token_source`` is called by the
    adapter once per operation."""

    plugin_id: str
    project: ProjectRef
    settings: Mapping[str, Any]
    enabled: frozenset[Operation]
    token_source: TokenSource = field(repr=False, compare=False)
    sleep: Callable[[float], None] = field(repr=False, compare=False)


@dataclass(frozen=True)
class TrackerAdapterProvider:
    """A first-party ``tracker.adapter`` runtime binding. ``loader`` imports the plugin package
    lazily and is called only after discovery selected ``plugin_id`` and every rule passed."""

    plugin_id: str
    #: What the operator installs to get the package, named in the R9 refusal.
    install_hint: str
    loader: Callable[[TrackerAdapterRequest], object] = field(repr=False, compare=False)


def _github_adapter(request: TrackerAdapterRequest) -> object:
    build = import_module("beadhive_tracker_github.factory").build
    return build(
        request.settings, request.token_source, project=request.project, sleep=request.sleep
    )


#: Built-in runtime bindings for manifests that provide ``tracker.adapter``. A row is inert
#: until a discovered, enabled, selected manifest has its id.
BUILTIN_TRACKER_PROVIDERS: tuple[TrackerAdapterProvider, ...] = (
    TrackerAdapterProvider("tracker-github", "beadhive[github]", _github_adapter),
)


# --------------------------------------------------------------------------------------------
# Settings and enablement
# --------------------------------------------------------------------------------------------


def _plain(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    return value


def _lookup(settings: Mapping[str, Any], dotted: str) -> object:
    node: object = settings
    for part in dotted.split("."):
        if not isinstance(node, Mapping):
            return None
        node = node.get(part)
    return node


@dataclass(frozen=True)
class TrackerPluginSettings:
    """One tracker plugin's ``plugins.<id>`` fragment as the root reads it."""

    plugin_id: str
    #: The validated fragment (defaults applied), or the raw one when root has no model for it.
    #: Empty when the fragment is invalid: an invalid fragment is retained inactive.
    values: Mapping[str, Any]
    #: ``plugins.<id>.enabled`` is exactly ``True`` (an absent flag, ``false`` and a
    #: non-boolean such as ``"true"`` are all off).
    own_enabled: bool
    valid: bool

    def operations(self) -> frozenset[Operation]:
        """The operations the fragment switches on, before the plugin and binding gates."""
        enabled = {
            op for op, key in OPERATION_FLAG_KEYS.items() if _lookup(self.values, key) is True
        }
        prefixes = _lookup(self.values, LABEL_OWNERSHIP_KEY)
        if not (isinstance(prefixes, Sequence) and not isinstance(prefixes, str) and prefixes):
            enabled.discard(Operation.LABELS_CHANGE)
        return frozenset(enabled)


def plugin_settings(
    plugin_id: str, plugins: Mapping[str, object] | BaseModel | None
) -> TrackerPluginSettings:
    """Read ``plugins.<plugin_id>`` from the ``plugins`` section (raw mapping or model)."""
    section = _plain(plugins) or {}
    raw = section.get(plugin_id) if isinstance(section, Mapping) else None
    raw = _plain(raw)
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        return TrackerPluginSettings(plugin_id, MappingProxyType({}), False, False)
    own_enabled = raw.get("enabled") is True
    model = TRACKER_FRAGMENT_MODELS.get(plugin_id)
    if model is None:
        return TrackerPluginSettings(plugin_id, MappingProxyType(dict(raw)), own_enabled, True)
    try:
        values = model.model_validate(dict(raw)).model_dump(by_alias=True)
    except ValidationError:
        return TrackerPluginSettings(plugin_id, MappingProxyType({}), own_enabled, False)
    return TrackerPluginSettings(plugin_id, MappingProxyType(values), own_enabled, True)


def _folded(settings: Mapping[str, Any], enabled: frozenset[Operation]) -> Mapping[str, Any]:
    """``settings`` with every operation flag set to whether the operation is in ``enabled``."""

    def copy(node: object) -> object:
        if isinstance(node, Mapping):
            return {key: copy(value) for key, value in node.items()}
        if isinstance(node, list):
            return [copy(item) for item in node]
        return node

    folded = copy(settings)
    assert isinstance(folded, dict)
    for op, key in OPERATION_FLAG_KEYS.items():
        *parents, leaf = key.split(".")
        node = folded
        for part in parents:
            child = node.get(part)
            if not isinstance(child, dict):
                child = node[part] = {}
            node = child
        node[leaf] = op in enabled
    return MappingProxyType(folded)


# --------------------------------------------------------------------------------------------
# Discovery: the category's composition, shared by binding and status
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackerComposition:
    """Metadata-only view of the category on this host: no package is imported for it."""

    #: Every discovered manifest providing ``tracker.adapter``, ignoring enablement.
    installed: tuple[DiscoveredPlugin, ...]
    #: Discovery with every tracker plugin's own flag folded into the kernel policy.
    discovery: DiscoveryResult
    owner: str | None
    conflict: bool
    #: The ``plugin_kernel`` policy failed to parse; nothing is selected.
    policy_error: str | None
    policy: PluginKernelConfig
    settings: Mapping[str, TrackerPluginSettings]

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

    def enabled_operations(self, plugin_id: str) -> frozenset[Operation]:
        """The plugin-level effective operations (the per-store gates are the caller's)."""
        if not self.plugin_enabled(plugin_id):
            return frozenset()
        return self.settings[plugin_id].operations()

    def diagnostics_for(self, plugin_id: str) -> tuple[PluginDiagnostic, ...]:
        return tuple(
            d
            for d in self.discovery.diagnostics
            if d.plugin_id == plugin_id or d.capability == TRACKER_ADAPTER
        )


def _discover(sources: Sequence[ManifestSource], policy: PluginKernelConfig) -> DiscoveryResult:
    return discover_plugins(
        list(sources),
        config=policy,
        beadhive_version=BEADHIVE_COMPATIBILITY_VERSION,
        kernel_version=PLUGIN_KERNEL_VERSION,
    )


def compose_tracker_category(
    *,
    plugins: Mapping[str, object] | BaseModel | None,
    plugin_kernel: Mapping[str, object] | PluginKernelConfig | BaseModel | None,
    sources: Sequence[ManifestSource] | None = None,
) -> TrackerComposition:
    """Discover the ``tracker.adapter`` providers with each plugin's own flag folded in.

    The fold is the legacy facade's: ``enabled[id] = own_enabled AND kernel.enabled.get(id,
    True)``, applied to EVERY manifest providing the capability, because discovery otherwise
    enables a built-in manifest by default. An invalid fragment folds to off.
    """
    sources = tuple(sources) if sources is not None else (builtin_manifest_source(),)
    try:
        policy = parse_kernel_config(_plain(plugin_kernel))  # type: ignore[arg-type]
        policy_error = None
    except (TypeError, ValueError) as exc:
        policy, policy_error = PluginKernelConfig(), str(exc)
    inventory = _discover(
        sources, PluginKernelConfig(allow_external_entry_points=policy.allow_external_entry_points)
    )
    installed = tuple(p for p in inventory.plugins if TRACKER_ADAPTER in p.manifest.capabilities)
    # Settings for every installed tracker plugin plus every known adapter id, so status can
    # tell "enabled but not installed" from "off"; only installed ones are folded.
    ids = sorted({p.manifest.plugin_id for p in installed} | set(KNOWN_TRACKER_ADAPTERS))
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
    if policy_error is not None:
        discovery = DiscoveryResult((), (), ())
    else:
        discovery = _discover(sources, folded)
    conflict = any(
        d.code is DiagnosticCode.DUPLICATE_CAPABILITY and d.capability == TRACKER_ADAPTER
        for d in discovery.errors
    )
    return TrackerComposition(
        installed=installed,
        discovery=discovery,
        owner=discovery.owner_of(TRACKER_ADAPTER),
        conflict=conflict,
        policy_error=policy_error,
        policy=policy,
        settings=MappingProxyType(settings),
    )


# --------------------------------------------------------------------------------------------
# The store binding (hive identity, rules R12/R13)
# --------------------------------------------------------------------------------------------


def hive_label(entry: Mapping[str, Any]) -> str:
    parts = (str(entry.get(key, "") or "") for key in ("provider", "org", "repo"))
    return "/".join(parts)


def _entry(entry: Mapping[str, Any] | BaseModel) -> Mapping[str, Any]:
    plain = _plain(entry)
    if not isinstance(plain, Mapping):
        raise TypeError("a managed_repos entry must be a mapping")
    return plain


def bound_adapter_id(entry: Mapping[str, Any] | BaseModel, scope: str = "public") -> str | None:
    """The adapter id ``stores.<scope>`` binds, or ``None`` (R13). Raises on a malformed or
    misplaced binding without repeating the value."""
    entry = _entry(entry)
    stores = entry.get("stores")
    store = stores.get(scope) if isinstance(stores, Mapping) else None
    adapter = store.get("adapter") if isinstance(store, Mapping) else None
    if adapter is None:
        return None
    hive = hive_label(entry)
    if not isinstance(adapter, str) or not _ADAPTER_ID_RE.fullmatch(adapter):
        raise TrackerBindingError(
            TrackerRule.INVALID_BINDING,
            f"{hive}: stores.{scope}.adapter must be a plugin id (lowercase letters, digits and "
            "hyphens, at most 64 characters)",
        )
    if scope != "public":
        raise TrackerBindingError(
            TrackerRule.INVALID_BINDING,
            f"{hive}: stores.{scope}: adapter is only valid on a public store",
            plugin_id=adapter,
        )
    return adapter


def derive_project(entry: Mapping[str, Any] | BaseModel, adapter_id: str) -> ProjectRef:
    """The tracker project from the hive identity triplet (never from plugin config, never
    from a store key). Only GitHub is derivable today (ADR S2); anything else is R12."""
    entry = _entry(entry)
    hive = hive_label(entry)
    tracker = _SOURCE_TRACKER.get(KNOWN_TRACKER_ADAPTERS.get(adapter_id, ""), "github")
    provider = str(entry.get("provider", "") or "")
    if provider != tracker:
        raise TrackerBindingError(
            TrackerRule.IDENTITY,
            f"{hive}: cannot derive a GitHub repository — the hive provider is {provider!r}, "
            "not 'github'",
            plugin_id=adapter_id,
        )
    owner, repo = str(entry.get("org", "") or ""), str(entry.get("repo", "") or "")
    if not owner or not repo:
        raise TrackerBindingError(
            TrackerRule.IDENTITY,
            f"{hive}: the hive identity names no owner/repository",
            plugin_id=adapter_id,
        )
    return ProjectRef(tracker, f"{owner}/{repo}")


# --------------------------------------------------------------------------------------------
# The bound object: structurally narrowed, grant-checked
# --------------------------------------------------------------------------------------------


class _Bound:
    """Delegates the required port (reader plus identity) to the adapter."""

    def __init__(self, inner: Any, enabled: frozenset[Operation]) -> None:
        self._inner = inner
        self._enabled = enabled

    def describe(self):
        return self._inner.describe()

    def list_issues(self, query):
        return self._inner.list_issues(query)

    def list_comments(self, query):
        return self._inner.list_comments(query)

    def list_ids(self, query):
        return self._inner.list_ids(query)

    def whoami(self):
        return self._inner.whoami()

    def probe_capabilities(self):
        return self._inner.probe_capabilities()

    def _check_grant(self, grant: object, operation: Operation) -> None:
        # Static messages: a grant is never interpolated.
        if not isinstance(grant, WriteGrant):
            raise OperationNotEnabled(
                f"{operation.value} needs an operator-approved single-use WriteGrant"
            )
        if grant.operation is not operation:
            raise OperationNotEnabled(f"the grant does not cover {operation.value}")


class _CommentWriting:
    def post_comment(self, issue, body, *, marker, grant):
        self._check_grant(grant, Operation.COMMENT_CREATE)  # type: ignore[attr-defined]
        return self._inner.post_comment(issue, body, marker=marker, grant=grant)  # type: ignore[attr-defined]


class _LabelWriting:
    def change_labels(self, issue, *, add, remove, ownership, grant):
        self._check_grant(grant, Operation.LABELS_CHANGE)  # type: ignore[attr-defined]
        return self._inner.change_labels(  # type: ignore[attr-defined]
            issue, add=add, remove=remove, ownership=ownership, grant=grant
        )


class _Resolving:
    def resolve(self, url):
        return self._inner.resolve(url)  # type: ignore[attr-defined]

    def get(self, key):
        return self._inner.get(key)  # type: ignore[attr-defined]


_VIEW_TYPES: dict[tuple[bool, bool, bool], type] = {}


def _view_type(comment: bool, labels: bool, resolver: bool) -> type:
    key = (comment, labels, resolver)
    if key not in _VIEW_TYPES:
        mixins = tuple(
            mixin
            for present, mixin in (
                (comment, _CommentWriting),
                (labels, _LabelWriting),
                (resolver, _Resolving),
            )
            if present
        )
        name = "BoundTracker" + "".join(m.__name__.strip("_") for m in mixins)
        _VIEW_TYPES[key] = type(name, (*mixins, _Bound), {})
    return _VIEW_TYPES[key]


def narrow(adapter: object, enabled: frozenset[Operation]) -> TrackerAdapter:
    """The adapter as consumers see it: a writer Protocol is present only when the operation is
    enabled AND the adapter implements it; ``IssueResolver`` passes through when implemented."""
    view = _view_type(
        Operation.COMMENT_CREATE in enabled and isinstance(adapter, CommentWriter),
        Operation.LABELS_CHANGE in enabled and isinstance(adapter, LabelWriter),
        isinstance(adapter, IssueResolver),
    )
    return view(adapter, enabled)


# --------------------------------------------------------------------------------------------
# The composition root
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BoundTrackerAdapter:
    """One store's tracker adapter. ``port`` is the narrowed, bound object."""

    hive: str
    scope: str
    adapter_id: str
    project: ProjectRef
    enabled: frozenset[Operation]
    port: TrackerAdapter = field(repr=False, compare=False)


def _not_enabled(
    hive: str,
    scope: str,
    adapter_id: str,
    missing: Sequence[Operation],
    settings: TrackerPluginSettings,
) -> str:
    writes = [op for op in missing if op in WRITE_OPERATIONS]
    kind = "write" if writes and len(writes) == len(missing) else "capability"
    keys = ", ".join(f"plugins.{adapter_id}.{OPERATION_FLAG_KEYS[op]}" for op in missing)
    text = (
        f"{hive}: {kind} {', '.join(op.value for op in missing)} was requested for "
        f"stores.{scope} but is not enabled — set {keys}: true"
    )
    if (
        Operation.LABELS_CHANGE in missing
        and settings.values
        and (_lookup(settings.values, OPERATION_FLAG_KEYS[Operation.LABELS_CHANGE]) is True)
    ):
        text += f" and plugins.{adapter_id}.{LABEL_OWNERSHIP_KEY} to a non-empty list"
    if writes:
        text += (
            " (enabling a write is necessary, never sufficient: each write still needs an "
            "operator-approved single-use grant)"
        )
    return text


class TrackerAdapters:
    """The process's ``tracker.adapter`` composition root for one configuration snapshot.

    ``plugins`` and ``plugin_kernel`` are the ``plugins`` and ``plugin_kernel`` sections (raw
    mappings or their config models). ``token_source`` is the injected credential port; it is
    handed to the adapter and never called here. Discovery runs once, lazily; each (hive,
    store scope) gets one adapter, cached for this instance's lifetime.
    """

    def __init__(
        self,
        *,
        plugins: Mapping[str, object] | BaseModel | None,
        plugin_kernel: Mapping[str, object] | PluginKernelConfig | BaseModel | None,
        token_source: TokenSource,
        sources: Sequence[ManifestSource] | None = None,
        providers: Sequence[TrackerAdapterProvider] = BUILTIN_TRACKER_PROVIDERS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._plugins = plugins
        self._plugin_kernel = plugin_kernel
        self._token_source = token_source
        self._sources = tuple(sources) if sources is not None else None
        self._providers = tuple(providers)
        self._sleep = sleep
        self._composition: TrackerComposition | None = None
        self._bound: dict[tuple[str, str], BoundTrackerAdapter] = {}

    @property
    def composition(self) -> TrackerComposition:
        if self._composition is None:
            self._composition = compose_tracker_category(
                plugins=self._plugins, plugin_kernel=self._plugin_kernel, sources=self._sources
            )
        return self._composition

    def _install_hint(self, adapter_id: str) -> str:
        rows = (*self._providers, *BUILTIN_TRACKER_PROVIDERS)
        row = next((p for p in rows if p.plugin_id == adapter_id), None)
        return row.install_hint if row else f"the package that provides plugin {adapter_id!r}"

    def for_store(
        self,
        entry: Mapping[str, Any] | BaseModel,
        scope: str = "public",
        *,
        requested: frozenset[Operation] = frozenset(),
    ) -> BoundTrackerAdapter:
        """The adapter serving ``entry``'s ``stores.<scope>``, or a typed refusal (R6 to R13).

        ``requested`` is the operations the caller is about to use; any that is not enabled is
        refused here (R8 for a read, R7 for a write), before any approval prompt.
        """
        entry = _entry(entry)
        hive = hive_label(entry)
        adapter_id = bound_adapter_id(entry, scope)
        if adapter_id is None:
            raise TrackerUnconfigured(
                TrackerRule.NOT_BOUND,
                f"{hive}: stores.{scope} binds no tracker adapter — declare "
                f"`stores.{scope}.adapter` to opt in",
            )
        project = derive_project(entry, adapter_id)
        cached = self._bound.get((hive, scope))
        if cached is None or (cached.adapter_id, cached.project) != (adapter_id, project):
            cached = self._compose(hive, scope, adapter_id, project, requested)
            self._bound[(hive, scope)] = cached
        else:
            self._check_requested(hive, scope, adapter_id, cached.enabled, requested)
        return cached

    def _check_requested(
        self,
        hive: str,
        scope: str,
        adapter_id: str,
        enabled: frozenset[Operation],
        requested: frozenset[Operation],
    ) -> None:
        order = list(Operation)
        missing = sorted(set(requested) - enabled, key=order.index)
        if not missing:
            return
        rule = (
            TrackerRule.WRITE_NOT_ENABLED
            if any(op in WRITE_OPERATIONS for op in missing)
            else TrackerRule.READ_NOT_ENABLED
        )
        settings = self.composition.settings[adapter_id]
        raise TrackerBindingError(
            rule,
            _not_enabled(hive, scope, adapter_id, missing, settings),
            plugin_id=adapter_id,
        )

    def _compose(
        self,
        hive: str,
        scope: str,
        adapter_id: str,
        project: ProjectRef,
        requested: frozenset[Operation],
    ) -> BoundTrackerAdapter:
        composition = self.composition
        binds = f"{hive}: stores.{scope} binds tracker adapter {adapter_id!r}"
        if composition.policy_error is not None:
            raise TrackerBindingError(
                TrackerRule.INVALID_CONFIG,
                f"{binds} but plugin_kernel is invalid — run `bh config validate`",
                plugin_id=adapter_id,
            )
        if composition.manifest(adapter_id) is None:
            raise TrackerBindingError(
                TrackerRule.NOT_INSTALLED,
                f"{binds} but no discovered plugin has that id — install "
                f"`{self._install_hint(adapter_id)}`",
                plugin_id=adapter_id,
            )
        if composition.conflict:
            raise TrackerBindingError(
                TrackerRule.CONFLICT,
                f"{hive}: two enabled plugins provide {TRACKER_ADAPTER.render()} — select one "
                "with plugin_kernel.capability_owners",
                plugin_id=adapter_id,
            )
        owner = composition.owner
        configured = composition.policy.capability_owners.get(TRACKER_ADAPTER.render())
        other = owner if owner is not None else configured
        if other is not None and other != adapter_id:
            raise TrackerBindingError(
                TrackerRule.NOT_OWNER,
                f"{binds} but {other!r} is the selected owner of {TRACKER_ADAPTER.render()}",
                plugin_id=adapter_id,
            )
        settings = composition.settings[adapter_id]
        if owner is None:
            if settings.own_enabled and not settings.valid:
                raise TrackerBindingError(
                    TrackerRule.INVALID_CONFIG,
                    f"{binds} but plugins.{adapter_id} is invalid — run `bh config validate` "
                    "(an invalid plugin configuration is retained inactive)",
                    plugin_id=adapter_id,
                )
            raise TrackerBindingError(
                TrackerRule.DISABLED,
                f"{binds} but the plugin is disabled — set plugins.{adapter_id}.enabled: true "
                "on this host (a binding never enables a plugin)",
                plugin_id=adapter_id,
            )
        enabled = composition.enabled_operations(adapter_id)
        self._check_requested(hive, scope, adapter_id, enabled, requested)
        return self._bind(hive, scope, adapter_id, project, enabled, settings)

    def _bind(
        self,
        hive: str,
        scope: str,
        adapter_id: str,
        project: ProjectRef,
        enabled: frozenset[Operation],
        settings: TrackerPluginSettings,
    ) -> BoundTrackerAdapter:
        provider = next((p for p in self._providers if p.plugin_id == adapter_id), None)
        missing_package = TrackerBindingError(
            TrackerRule.PACKAGE_MISSING,
            f"{hive}: stores.{scope} binds tracker adapter {adapter_id!r} but its "
            f"package is not installed — install `{self._install_hint(adapter_id)}`",
            plugin_id=adapter_id,
        )
        if provider is None:
            raise missing_package
        request = TrackerAdapterRequest(
            adapter_id,
            project,
            _folded(settings.values, enabled),
            enabled,
            token_source=self._token_source,
            sleep=self._sleep,
        )
        try:
            adapter = provider.loader(request)
        except ModuleNotFoundError:
            raise missing_package from None
        except Exception as exc:  # noqa: BLE001 - a factory failure is a typed refusal
            # The exception text is not repeated: a factory error may echo configuration.
            raise TrackerBindingError(
                TrackerRule.NOT_CONFORMING,
                f"{hive}: tracker adapter {adapter_id!r} could not be built ({type(exc).__name__})",
                plugin_id=adapter_id,
            ) from None
        if not isinstance(adapter, TrackerAdapter):
            raise TrackerBindingError(
                TrackerRule.NOT_CONFORMING,
                f"{hive}: tracker adapter {adapter_id!r} does not implement TrackerAdapter",
                plugin_id=adapter_id,
            )
        binding = ProviderBinding(
            ProviderKey(adapter_id, TRACKER_ADAPTER), narrow(adapter, enabled)
        )
        try:
            port = bind_application_port(
                TRACKER_ADAPTER_PORT, self.composition.discovery, (binding,)
            )
        except CapabilityBindingError:
            raise TrackerBindingError(
                TrackerRule.NOT_CONFORMING,
                f"{hive}: tracker adapter {adapter_id!r} could not be bound to "
                f"{TRACKER_ADAPTER.render()}",
                plugin_id=adapter_id,
            ) from None
        return BoundTrackerAdapter(hive, scope, adapter_id, project, enabled, port=port)


__all__ = [
    "BUILTIN_TRACKER_PROVIDERS",
    "LABEL_OWNERSHIP_KEY",
    "OPERATION_FLAG_KEYS",
    "TRACKER_ADAPTER",
    "TRACKER_ADAPTER_PORT",
    "TRACKER_FRAGMENT_MODELS",
    "BoundTrackerAdapter",
    "TrackerAdapterProvider",
    "TrackerAdapterRequest",
    "TrackerAdapters",
    "TrackerBindingError",
    "TrackerComposition",
    "TrackerPluginSettings",
    "TrackerRule",
    "TrackerUnconfigured",
    "bound_adapter_id",
    "compose_tracker_category",
    "derive_project",
    "hive_label",
    "narrow",
    "plugin_settings",
]
