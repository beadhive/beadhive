"""Prototype of binding a bead STORE to a tracker adapter plugin (spike bh-6bkun).

Three layers, one per place a rule can run (spike section "Where validation runs"):

* **T1, config load.** ``Proto*`` pydantic models subclass the REAL config models and add exactly
  one store key, ``adapter`` (plus the rktrh C1 gap: ``plugins`` and ``plugin_kernel``). They are
  exercised through the REAL ``config_validate.validate_config`` with the model swapped in, so
  message wrapping, ``schema_version`` handling and unknown-key reporting are the shipped ones.
  Rules here read the document only: no discovery, no environment, no network.
* **T2, ``hive ready`` / ``doctor``.** :func:`binding_check` is offline and environment-aware and
  NEVER fails a gate while the plugin is disabled.
* **T3, composition root.** :func:`resolve_store_adapter` is what a consumer (the intake module)
  calls. It runs the REAL kernel discovery and ``bind_application_port`` via the rktrh
  ``compose_proto`` and fails closed with a :class:`BindingError` whose ``message`` is the
  operator-facing text.

Nothing here reads a credential value, the environment, a clock or the network.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from compose_proto import (
    TRACKER_ADAPTER,
    TrackerAdapterProvider,
    TrackerComposition,
    compose_tracker_adapter,
)
from credentials_proto import TokenSource
from pydantic import ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator
from stub_adapters import enabled_operations
from tracker_contracts import WRITE_OPERATIONS, Operation

from beadhive.kernel.plugins import discover_plugins
from beadhive.modules.config.contracts import (
    BeadhiveConfig,
    ManagedRepoEntry,
    ManagedRepoStores,
    StoreDescriptor,
    _Section,
)
from beadhive_plugins.contracts import ManifestSource

# --------------------------------------------------------------------------------------------
# The binding vocabulary
# --------------------------------------------------------------------------------------------

#: adapter plugin id -> the ``stores.<scope>.source`` it serves. A closed table in the config
#: layer, exactly as ``source`` is a closed ``Literal`` today; a drift test ties it to the
#: built-in manifests (spike section "Validation runs in three places"). One row today.
KNOWN_ADAPTERS: Mapping[str, str] = {"tracker-github": "github-issues"}

_ADAPTER_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")

#: operation -> the plugin key that enables it (for the operator-facing message).
ENABLING_KEY: Mapping[Operation, str] = {
    Operation.ISSUES_READ: "read.issues.enabled",
    Operation.COMMENTS_READ: "read.comments.enabled",
    Operation.COMMENT_CREATE: "write.comment.enabled",
    Operation.LABELS_CHANGE: "write.labels.enabled",
}


# --------------------------------------------------------------------------------------------
# T1: the config models (REAL ones, plus one key)
# --------------------------------------------------------------------------------------------


def adapter_problems(
    scope: str, adapter: str | None, source: str | None, known: Mapping[str, str] = KNOWN_ADAPTERS
) -> list[str]:
    """Every document-only rule for one store's ``adapter`` (empty when it is unset)."""
    if adapter is None:
        return []
    if scope != "public":
        return [f"stores.{scope}: adapter is only valid on a public store"]
    if adapter not in known:
        return [
            f"stores.{scope}: unknown tracker adapter {adapter!r} — known adapters: "
            f"{', '.join(sorted(known))}"
        ]
    if source is None:
        return [
            f"stores.{scope}: adapter {adapter!r} needs `source: {known[adapter]}` — a bound "
            "store must name the tracker it mirrors"
        ]
    if source != known[adapter]:
        return [
            f"stores.{scope}: adapter {adapter!r} serves source {known[adapter]!r}, not {source!r}"
        ]
    return []


class ProtoStoreDescriptor(StoreDescriptor):
    """``StoreDescriptor`` plus the one new key. Everything else is inherited unchanged."""

    adapter: str | None = Field(
        None,
        description="Plugin id of the tracker adapter bound to this store (public only). Names "
        "a plugin; never a URL, repository or credential. Absent = today's behaviour.",
    )

    @field_validator("adapter")
    @classmethod
    def _adapter_has_plugin_id_shape(cls, value):
        # The value is deliberately NOT echoed: anything that is not shaped like a plugin id
        # might be a pasted secret, and a validation message must never repeat it.
        if value is not None and not _ADAPTER_ID_RE.fullmatch(value):
            raise ValueError(
                "adapter must be a plugin id (lowercase letters, digits and hyphens, at most "
                "64 characters)"
            )
        return value


class ProtoManagedRepoStores(ManagedRepoStores):
    # ``store_target.configured_stores`` embeds ``str(ValidationError)``, which by default repeats
    # the offending INPUT value; a binding key makes a pasted secret more likely, so hide it.
    model_config = ConfigDict(hide_input_in_errors=True)

    internal: ProtoStoreDescriptor | None = None
    public: ProtoStoreDescriptor | None = None

    @model_validator(mode="after")
    def _adapter_binding(self):
        problems: list[str] = []
        for scope in ("internal", "public"):
            store = getattr(self, scope)
            if store is not None:
                problems += adapter_problems(scope, store.adapter, store.source)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class ProtoManagedRepoEntry(ManagedRepoEntry):
    stores: ProtoManagedRepoStores | None = None


class _ReadFlag(_Section):
    enabled: StrictBool = False


class _Read(_Section):
    issues: _ReadFlag = Field(default_factory=_ReadFlag)
    comments: _ReadFlag = Field(default_factory=_ReadFlag)
    include_closed: StrictBool = True
    page_size: StrictInt | None = Field(None, ge=1, le=100)


class _Credential(_Section):
    sources: list[str] = Field(default_factory=lambda: ["env", "gh"])

    @field_validator("sources")
    @classmethod
    def _names(cls, value):
        if len(set(value)) != len(value) or not set(value) <= {"env", "gh"}:
            raise ValueError("credential sources must be unique members of: env, gh")
        return value


class _WriteLabels(_Section):
    enabled: StrictBool = False
    owned_prefixes: list[str] = Field(default_factory=list)


class _Write(_Section):
    comment: _ReadFlag = Field(default_factory=_ReadFlag)
    labels: _WriteLabels = Field(default_factory=_WriteLabels)


class TrackerGithubConfig(_Section):
    """``plugins.tracker-github`` as amended by this spike: the bh-rktrh table WITHOUT ``repo``.

    The repository is a property of the store binding (derived from the hive identity), not of
    a process-wide plugin namespace, so there is no key to set it and ``extra="forbid"`` rejects
    one. A subset of the keys is enough to exercise the layering; the full table is rktrh's."""

    enabled: StrictBool = False
    base_url: str | None = None
    credential: _Credential = Field(default_factory=_Credential)
    read: _Read = Field(default_factory=_Read)
    write: _Write = Field(default_factory=_Write)

    @field_validator("base_url")
    @classmethod
    def _https_no_userinfo(cls, value):
        if value is not None and (not value.startswith("https://") or "@" in value):
            raise ValueError("base_url must be an https URL without credentials")
        return value


class ProtoBeadhiveConfig(BeadhiveConfig):
    """The root model with the store key AND the rktrh C1 gap closed (``plugins`` and
    ``plugin_kernel`` accepted, ``plugins.tracker-github`` validated)."""

    managed_repos: list[ProtoManagedRepoEntry] = Field(default_factory=list)
    plugins: dict[str, Any] | None = None
    plugin_kernel: dict[str, Any] | None = None

    @field_validator("plugins")
    @classmethod
    def _tracker_fragment(cls, value):
        if value and "tracker-github" in value:
            TrackerGithubConfig.model_validate(value["tracker-github"])
        return value


def validate_with_proto(cfg: Mapping) -> list[dict]:
    """The REAL ``validate_config`` over the prototype model (swapped in for one call)."""
    from beadhive import config_validate

    original = config_validate.BeadhiveConfig
    config_validate.BeadhiveConfig = ProtoBeadhiveConfig
    try:
        return config_validate.validate_config(cfg)
    finally:
        config_validate.BeadhiveConfig = original


# --------------------------------------------------------------------------------------------
# T3: the composition root
# --------------------------------------------------------------------------------------------


class BindingError(Exception):
    """A binding refused. ``rule`` is stable; ``message`` is what the operator sees."""

    def __init__(self, rule: str, message: str):
        super().__init__(message)
        self.rule = rule
        self.message = message


@dataclass(frozen=True)
class StoreAdapter:
    hive: str
    scope: str
    adapter_id: str
    repo: str
    port: object
    enabled: frozenset[Operation]


def hive_label(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('provider', '')}/{entry.get('org', '')}/{entry.get('repo', '')}"


def derive_repo(entry: Mapping[str, Any]) -> str:
    """``owner/name`` from the hive identity triplet, as ``github_projection.resolve_projection``
    does (``github_projection.py:109-114``): never from the plugin namespace, never from a
    store key."""
    hive = hive_label(entry)
    if str(entry.get("provider", "")) != "github":
        raise BindingError(
            "identity",
            f"{hive}: cannot derive a GitHub repository — the hive provider is "
            f"{str(entry.get('provider', ''))!r}, not 'github'",
        )
    owner, repo = str(entry.get("org", "") or ""), str(entry.get("repo", "") or "")
    if not owner or not repo:
        raise BindingError("identity", f"{hive}: the hive identity names no owner/repository")
    return f"{owner}/{repo}"


def _bound_store(entry: Mapping[str, Any], scope: str) -> tuple[Mapping[str, Any], str]:
    store = ((entry.get("stores") or {}).get(scope)) or None
    if store is None:
        raise BindingError("not-bound", f"{hive_label(entry)}: no {scope} store is declared")
    adapter = store.get("adapter")
    if not adapter:
        raise BindingError(
            "not-bound",
            f"{hive_label(entry)}: stores.{scope} binds no tracker adapter — declare "
            f"`stores.{scope}.adapter` to opt in",
        )
    return store, str(adapter)


def resolve_store_adapter(
    entry: Mapping[str, Any],
    scope: str = "public",
    *,
    plugin_configs: Mapping[str, Mapping],
    sources: Sequence[ManifestSource],
    providers: Sequence[TrackerAdapterProvider],
    token_source: TokenSource,
    requested: frozenset[Operation] = frozenset(),
    kernel: Mapping[str, object] | None = None,
) -> StoreAdapter:
    """What the intake module calls. Fails closed, in this order, with these rules."""
    hive = hive_label(entry)
    _store, adapter_id = _bound_store(entry, scope)
    repo = derive_repo(entry)

    # The repository is a property of the BINDING: root adds it to the settings it hands the
    # package factory; the plugin namespace has no ``repo`` key to carry it.
    bound = [TrackerAdapterProvider(p.plugin_id, _with_repo(p, repo)) for p in providers]
    composition = compose_tracker_adapter(
        plugin_configs, sources=sources, providers=bound, token_source=token_source, kernel=kernel
    )
    if adapter_id not in _installed_ids(sources):
        raise BindingError(
            "adapter-not-installed",
            f"{hive}: stores.{scope} binds tracker adapter {adapter_id!r} but no discovered "
            "plugin has that id — install `beadhive[github]`",
        )
    if composition.state == "conflict":
        raise BindingError(
            "adapter-conflict",
            f"{hive}: two enabled plugins provide tracker.adapter@1 — select one with "
            "plugin_kernel.capability_owners",
        )
    if composition.owner != adapter_id:
        if composition.owner is None:
            raise BindingError(
                "adapter-disabled",
                f"{hive}: stores.{scope} binds tracker adapter {adapter_id!r} but the plugin is "
                f"disabled — set plugins.{adapter_id}.enabled: true on this host (a binding "
                "never enables a plugin)",
            )
        raise BindingError(
            "adapter-not-owner",
            f"{hive}: stores.{scope} binds tracker adapter {adapter_id!r} but "
            f"{composition.owner!r} is the selected owner of tracker.adapter@1",
        )
    enabled = enabled_operations(plugin_configs[adapter_id])
    missing = sorted(op.value for op in requested - enabled)
    if missing:
        raise BindingError("operation-not-enabled", _not_enabled(hive, scope, adapter_id, missing))
    return StoreAdapter(hive, scope, adapter_id, repo, composition.port, enabled)


def _installed_ids(sources: Sequence[ManifestSource]) -> set[str]:
    """Plugin ids the sources provide. A DISABLED plugin is absent from ``discovery.plugins``,
    so "installed" needs its own pass with no enablement policy (root would use the static
    built-in catalog, which is data)."""
    everything = discover_plugins(
        list(sources),
        config=None,
        beadhive_version="0.15.1",
        kernel_version="1.0.0",
        host_executables={},
    )
    return {plugin.manifest.plugin_id for plugin in everything.plugins}


def _not_enabled(hive: str, scope: str, adapter_id: str, missing: list[str]) -> str:
    ops = [Operation(m) for m in missing]
    writes = [op for op in ops if op in WRITE_OPERATIONS]
    kind = "write" if writes and len(writes) == len(ops) else "capability"
    keys = ", ".join(f"plugins.{adapter_id}.{ENABLING_KEY[op]}" for op in ops)
    text = (
        f"{hive}: {kind} {', '.join(missing)} was requested for stores.{scope} but is not "
        f"enabled — set {keys}: true"
    )
    if writes:
        text += (
            " (enabling a write is necessary, never sufficient: each write still needs an "
            "operator-approved single-use grant)"
        )
    return text


def _with_repo(provider: TrackerAdapterProvider, repo: str):
    loader = provider.loader

    def load(settings: Mapping, token_source: TokenSource):
        return loader({**settings, "repo": repo}, token_source)

    return load


# --------------------------------------------------------------------------------------------
# T2: the hive ready / doctor row (offline, never fails a disabled plugin)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BindingCheck:
    state: str
    severity: str  # info | warning | error
    detail: str


def binding_check(
    entry: Mapping[str, Any],
    scope: str = "public",
    **resolve_kwargs: Any,
) -> BindingCheck | None:
    """``None`` when the store binds no adapter (nothing to say: today's hive). Otherwise one
    row. A binding whose plugin is off is ``info``, never a failure, so a fleet push of the
    ``adapter`` key cannot turn any host's ``hive ready`` red."""
    try:
        bound = resolve_store_adapter(entry, scope, **resolve_kwargs)
    except BindingError as error:
        if error.rule == "not-bound":
            return None
        plugin_enabled = (
            resolve_kwargs["plugin_configs"].get(_adapter_id(entry, scope), {}).get("enabled")
            is True
        )
        severity = {
            "adapter-disabled": "info",
            "adapter-not-installed": "error" if plugin_enabled else "info",
            "identity": "warning",
        }.get(error.rule, "error")
        return BindingCheck(error.rule, severity, error.message)
    ops = ", ".join(sorted(op.value for op in bound.enabled)) or "none"
    return BindingCheck(
        "ready", "info", f"{bound.hive}: {bound.adapter_id} -> {bound.repo}; operations: {ops}"
    )


def _adapter_id(entry: Mapping[str, Any], scope: str) -> str:
    return str((((entry.get("stores") or {}).get(scope)) or {}).get("adapter") or "")


__all__ = [
    "BindingCheck",
    "BindingError",
    "KNOWN_ADAPTERS",
    "ProtoBeadhiveConfig",
    "StoreAdapter",
    "TRACKER_ADAPTER",
    "TrackerComposition",
    "TrackerGithubConfig",
    "adapter_problems",
    "binding_check",
    "derive_repo",
    "resolve_store_adapter",
    "validate_with_proto",
]
