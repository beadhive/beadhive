"""Bootstrap precedence for partitioned hives (bh-lniwf.4).

docs/design/two-store-scope-adr.md section 6. The ORDER is the security property:

1. **internal first** — a managed hive whose internal store names an HQ remote descriptor
   clones it (``bd init --remote <url>``: a plain full-history clone) BEFORE anything probes the
   code origin's ``refs/dolt/data``. A private hive therefore never bootstraps from a public
   ref by accident. The origin probe is not even called on that path.
2. **origin probe only when no internal remote is declared** — today's unmanaged / public-clone
   behaviour (``onboard._act_bd_init``), unchanged. :func:`plan_bootstrap` reports that case as
   ``legacy`` and leaves it to the caller.
3. **public second** — initialised independently at its detached path with its own prefix,
   never replacing the internal store. It is always MINTED fresh in this release: until a
   repository is migrated, its origin ``refs/dolt/data`` still holds the internal corpus, so
   cloning the public store from origin could copy private beads into the public store.

Each phase names itself on failure and touches nothing outside its own store: a failed public
init removes only the public root IT created; the internal store and the config are left as
they were. An already-initialised store is skipped, so a retry is idempotent.

Resolving a descriptor NAME to a URL is bh-b1scz's contract (the HQ remote-descriptor ADR); it
reaches this module only through the injected ``resolve_remote`` callable, and secrets never
do. :func:`entry_remote_url` is the default resolver: it reads the per-hive descriptor
bh-b1scz declares (``managed_repos[*].dolt_remote: {url, name, credentials}``, contract
``dolt-remote-descriptor/v1``; ``name`` defaults to ``origin``), and fails closed when absent.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store_target
from .modules.config.contracts import GIT_ORIGIN_REMOTE

PHASE_INTERNAL = "internal-clone"
PHASE_LEGACY = "legacy"
PHASE_PUBLIC = "public-init"

ACTION_CLONE = "clone"
ACTION_INIT = "init"
ACTION_SKIP = "skip-present"
ACTION_DELEGATE = "delegate"


class BootstrapError(RuntimeError):
    """A bootstrap phase failed; ``phase`` names which one."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(f"[{phase}] {message}")
        self.phase = phase


class RemoteUnresolved(BootstrapError):
    """The internal store names a descriptor no resolver can turn into a URL."""


@dataclass(frozen=True)
class Phase:
    name: str
    action: str
    target: store_target.StoreTarget
    remote: str | None = None

    @property
    def mutates(self) -> bool:
        return self.action in (ACTION_CLONE, ACTION_INIT)


def entry_remote_url(entry: Mapping[str, Any], name: str) -> str:
    """Default descriptor resolver: the per-hive HQ remote descriptor (bh-b1scz,
    ``dolt_remote``) whose ``name`` (default ``origin``) matches. Fails closed — a declared
    internal remote that cannot be resolved must stop the bootstrap, never fall through to
    probing the code origin. Credentials are not read here; bh-b1scz resolves them on the host."""
    descriptor = (entry or {}).get("dolt_remote")
    if (
        isinstance(descriptor, Mapping)
        and str(descriptor.get("name", "origin") or "origin") == name
    ):
        url = str(descriptor.get("url", "") or "")
        if url:
            return url
    raise RemoteUnresolved(
        PHASE_INTERNAL,
        f"internal store remote {name!r} has no HQ remote descriptor with a URL "
        "(managed_repos[*].dolt_remote, bh-b1scz) — refusing to fall back to the code origin",
    )


def plan_bootstrap(entry: Mapping[str, Any]) -> list[Phase]:
    """The ordered phases for ``entry``. Pure apart from checking which stores exist."""
    internal = store_target.operational_store(entry)
    phases: list[Phase] = []
    remote = internal.remote
    if internal.scope == store_target.SCOPE_INTERNAL and remote and remote != GIT_ORIGIN_REMOTE:
        action = ACTION_SKIP if internal.beads_dir.is_dir() else ACTION_CLONE
        phases.append(Phase(PHASE_INTERNAL, action, internal, remote))
    else:
        phases.append(Phase(PHASE_LEGACY, ACTION_DELEGATE, internal, remote))
    stores = store_target.configured_stores(entry)
    if stores is not None and stores.public is not None:
        public = store_target.resolve_store(entry, store_target.SCOPE_PUBLIC)
        action = ACTION_SKIP if public.beads_dir.is_dir() else ACTION_INIT
        phases.append(Phase(PHASE_PUBLIC, action, public, public.remote))
    return phases


Runner = Callable[[list[str], Path, dict[str, str]], Any]


def _default_runner(argv: list[str], cwd: Path, env: dict[str, str]):
    from . import bd  # lazy: keep this module import-light

    return bd.run(argv, cwd, env=env, hive_aware=False, pin_process_cwd=True)


def _clean_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """A bd environment with no inherited store selector: each phase names its own store."""
    out = dict(os.environ if env is None else env)
    out.pop(store_target.BEADS_DIR_ENV, None)
    out["BD_NON_INTERACTIVE"] = "1"
    return out


def execute_bootstrap(
    entry: Mapping[str, Any],
    *,
    resolve_remote: Callable[[Mapping[str, Any], str], str] = entry_remote_url,
    origin_probe: Callable[[], bool] | None = None,
    legacy: Callable[[], None] | None = None,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
    internal_args: tuple[str, ...] = (),
) -> list[Phase]:
    """Run :func:`plan_bootstrap`'s phases in order and return them.

    ``legacy`` is the caller's existing one-store path (it may consult ``origin_probe``);
    it runs only for a hive with no declared internal remote. ``origin_probe`` is accepted so a
    caller can prove it is never consulted on the managed path. ``internal_args`` are extra
    ``bd init`` flags for the internal clone (the caller's storage-mode / footprint flags)."""
    del origin_probe  # never consulted here: the managed path must not probe origin at all
    phases = plan_bootstrap(entry)
    base_env = _clean_env(env)
    runner = runner or _default_runner
    for phase in phases:
        if phase.action == ACTION_DELEGATE:
            if legacy is not None:
                legacy()
            continue
        if phase.action == ACTION_SKIP:
            continue
        if phase.action == ACTION_CLONE:
            _clone_internal(entry, phase, resolve_remote, runner, base_env, internal_args)
        elif phase.action == ACTION_INIT:
            _init_public(phase, runner, base_env)
    return phases


def _clone_internal(entry, phase: Phase, resolve_remote, runner: Runner, env, extra) -> None:
    target = phase.target
    url = resolve_remote(entry, str(phase.remote))
    argv = ["init", "--prefix", target.prefix, "--remote", url, *extra]
    argv += ["--skip-agents", "--skip-hooks", "--non-interactive"]
    res = runner(argv, target.repo_dir, env)
    if getattr(res, "returncode", 1) != 0:
        _discard(target.beads_dir)
        raise BootstrapError(
            PHASE_INTERNAL,
            f"cloning the internal store of {target.hive} from descriptor {phase.remote!r} "
            f"failed (exit {getattr(res, 'returncode', '?')}); nothing was probed on the code "
            "origin and no partial store was left behind — fix the remote and retry",
        )


def _init_public(phase: Phase, runner: Runner, env) -> None:
    target = phase.target
    root = target.store_root
    created = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    argv = ["init", "--prefix", target.prefix, "--skip-agents", "--skip-hooks"]
    argv += ["--non-interactive"]
    res = runner(argv, root, env)
    if getattr(res, "returncode", 1) != 0:
        if created:
            shutil.rmtree(root, ignore_errors=True)
        else:
            _discard(target.beads_dir)
        raise BootstrapError(
            PHASE_PUBLIC,
            f"initialising the public store of {target.hive} at {root} failed (exit "
            f"{getattr(res, 'returncode', '?')}); the internal store was not touched — retry",
        )


def _discard(beads_dir: Path) -> None:
    if beads_dir.is_dir():
        shutil.rmtree(beads_dir, ignore_errors=True)
