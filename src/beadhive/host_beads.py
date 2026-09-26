"""The host runtime's owner of one supervised loopback ``bd serve`` per server-mode hive.

``docs/spikes/bh-ie41e.5-bd-serve-adoption-decision.md`` makes the Beadhive host runtime the
product-level owner of the Beads API service: one process bound to exactly one workspace,
loopback only, never started per request or per agent. This module binds the package-level
contract in :mod:`beadhive_beads_client.service` to Beadhive's hive identity and runtime
directory, and exposes three ways to own a service without choosing a Factory owner:

* ``bh host beads start|stop|status`` — a detached service for local/dev use;
* ``bh host beads run`` — a foreground supervisor (a terminal, or any service manager that runs
  it as its main process) with bounded-backoff restart and graceful ``SIGTERM``;
* ``bh host beads enable|disable`` — durable intent that the host daemon, while running,
  supervises the hive's service as a child (:class:`DaemonBeadsSupervision`).

Every owner publishes the same token-free endpoint record under
``$BH_HOME/run/beads-serve/<hive-slug>/endpoint.json``. Consumers never spawn ``bd serve``:
they call :func:`resolve_endpoint` (or :func:`resolve_session`), which fails closed with
``start it with `bh host beads start --hive <hive>``` when the service is absent, stale, or bound
to another workspace.

The client package is resolved lazily by name (``scripts/check_package_imports.py``), so plain
``bh`` commands never import it.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import registry, store_locator
from .config_consumer_ports import daemon_settings as config

INTENT_FILE = "supervise.json"
_RUNTIME = Path("run") / "beads-serve"


class HiveNotServable(RuntimeError):
    """The hive cannot be served over the Beads API (embedded Dolt, no ``bd``, not a hive)."""


def service_module() -> Any:
    """``beadhive_beads_client.service``, imported on first use only.

    httpx logs every request at INFO; on this path those lines are per-probe transport noise
    (and would interleave with ``--json`` output), so they are raised to WARNING.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return importlib.import_module("beadhive_beads_client.service")


def runtime_root(home: Path | None = None) -> Path:
    """Where every per-hive service directory lives: ``$BH_HOME/run/beads-serve``."""
    return (home if home is not None else config.home()) / _RUNTIME


def start_command(hive: str) -> str:
    return f"{config.BINARY_ALIAS} host beads start --hive {hive}"


def service_spec(main: Path, hive: str, *, root: Path | None = None) -> Any:
    """The :class:`ServiceSpec` for hive ``hive`` whose main clone is ``main``.

    Refuses embedded-Dolt hives: Beads 1.3 serves its HTTP API only from a Dolt SQL server, and
    this path deliberately does not support embedded Dolt.
    """
    main = Path(main)
    if store_locator.dolt_mode(main) is None:
        raise HiveNotServable(f"{main} has no recorded Beads store mode")
    if store_locator.is_embedded_mode(main):
        raise HiveNotServable(
            f"{hive} uses embedded Dolt; the Beads API service needs a Dolt SQL server — "
            f"migrate the hive to server mode first"
        )
    executable = shutil.which("bd")
    if executable is None:
        raise HiveNotServable("bd is not on PATH")
    service = service_module()
    return service.ServiceSpec(
        workspace=hive,
        repo_root=main,
        project_id=store_locator.project_id(main),
        database=store_locator.server_database(main),
        bd_executable=Path(executable).resolve(),
        paths=service.ServicePaths((root or runtime_root()) / registry.sanitize(hive)),
        start_command=start_command(hive),
    )


def locate(cfg: dict, hive: str = "") -> tuple[Path, str]:
    """``(main clone, hive key)`` for ``--hive`` (default: the hive owning cwd)."""
    main = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, main)
    if not entry:
        raise HiveNotServable(f"{main} belongs to no managed hive; pass --hive")
    return Path(main), registry.hive_key(entry)


def spec_for_hive(cfg: dict, hive: str = "") -> Any:
    main, key = locate(cfg, hive)
    return service_spec(main, key)


def spec_for_dir(main: Path, *, cfg: dict | None = None, entry: dict | None = None) -> Any:
    """The spec for the hive whose main clone (or managed worktree) is ``main``.

    Pass the hive's registry ``entry`` when the caller already holds it (``worktree.locate``
    returns one): that skips the cwd/workspace identity lookup, the slow part of resolution.
    """
    if not entry:
        entry = registry.entry_for_dir(cfg if cfg is not None else config.load(), Path(main))
    if not entry:
        raise HiveNotServable(f"{main} belongs to no managed hive")
    return service_spec(registry.hive_dir(entry), registry.hive_key(entry))


# ---- consumer API ----------------------------------------------------------------------------


def resolve_endpoint(
    main: Path,
    required_capabilities: frozenset[str] | None = None,
    *,
    cfg: dict | None = None,
    entry: dict | None = None,
) -> Any:
    """A context-verified loopback ``RemoteEndpoint`` for the hive at ``main``.

    Raises ``beadhive_beads_client.service.ServiceUnavailable`` (a ``RuntimeError`` whose
    message ends ``start it with `bh host beads start --hive <hive>```) when the service is
    absent, stale, unready, or bound to another workspace; :class:`HiveNotServable` for an
    embedded-Dolt hive. Never spawns ``bd serve``.
    """
    return service_module().resolve(spec_for_dir(main, cfg=cfg, entry=entry), required_capabilities)


def resolve_session(
    main: Path,
    required_capabilities: frozenset[str] | None = None,
    *,
    cfg: dict | None = None,
    entry: dict | None = None,
) -> Any:
    """An unopened ``BeadsSession`` over :func:`resolve_endpoint`, expecting this hive's context.

    Opening it re-verifies project, database, and ``required_capabilities``.
    """
    spec = spec_for_dir(main, cfg=cfg, entry=entry)
    endpoint = service_module().resolve(spec, required_capabilities)
    client = importlib.import_module("beadhive_beads_client")
    return client.BeadsSession(endpoint, spec.expected(required_capabilities))


# ---- host-daemon ownership intent ------------------------------------------------------------


def enable(spec: Any) -> Path:
    """Record durable intent that the host daemon supervises ``spec``'s service."""
    directory: Path = spec.paths.directory
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    intent = directory / INTENT_FILE
    temporary = directory / f".{INTENT_FILE}.{os.getpid()}"
    temporary.write_text(
        json.dumps({"hive": spec.workspace, "main": str(spec.repo_root)}, sort_keys=True) + "\n"
    )
    os.replace(temporary, intent)
    return intent


def disable(spec: Any) -> bool:
    intent: Path = spec.paths.directory / INTENT_FILE
    existed = intent.exists()
    intent.unlink(missing_ok=True)
    return existed


def is_enabled(spec: Any) -> bool:
    return (spec.paths.directory / INTENT_FILE).is_file()


@dataclass(frozen=True)
class Intent:
    hive: str
    main: Path


def iter_intents(root: Path) -> Iterator[Intent]:
    """Every readable ``supervise.json`` intent under ``root`` (malformed ones are skipped)."""
    if not root.is_dir():
        return
    for intent in sorted(root.glob(f"*/{INTENT_FILE}")):
        try:
            value = json.loads(intent.read_text())
            yield Intent(str(value["hive"]), Path(value["main"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue


def iter_published(root: Path) -> Iterator[str]:
    """Hive keys with an endpoint record or a supervision intent under ``root``."""
    if not root.is_dir():
        return
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        for name, key in (("endpoint.json", "workspace"), (INTENT_FILE, "hive")):
            try:
                hive = json.loads((directory / name).read_text())[key]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(hive, str) and hive:
                yield hive
                break


class DaemonBeadsSupervision:
    """Host-daemon child ownership: one :class:`ServiceSupervisor` per enabled hive.

    :meth:`reconcile` is one synchronous pass (run off the event loop): it starts supervisors
    for new intents, ticks each one (bounded-backoff restart), and shuts down supervisors whose
    intent was disabled. A lock serializes passes with :meth:`shutdown`, so the daemon's drain
    never races an in-flight start.
    """

    def __init__(
        self,
        root: Path,
        *,
        interval: float = 2.0,
        spec_factory: Callable[[Path, str], Any] | None = None,
        supervisor_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.root = root
        self.interval = interval
        self._spec_factory = spec_factory or (
            lambda main, hive: service_spec(main, hive, root=root)
        )
        self._supervisor_factory = supervisor_factory or (
            lambda spec: service_module().ServiceSupervisor(spec, kind="host-daemon")
        )
        self.supervisors: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self._lock = threading.Lock()
        self._closed = False

    def reconcile(self) -> None:
        with self._lock:
            if self._closed:
                return
            wanted = {intent.hive: intent for intent in iter_intents(self.root)}
            for hive in [hive for hive in self.supervisors if hive not in wanted]:
                self.supervisors.pop(hive).shutdown()
            for hive, intent in wanted.items():
                supervisor = self.supervisors.get(hive)
                if supervisor is None:
                    try:
                        spec = self._spec_factory(intent.main, hive)
                    except (HiveNotServable, OSError) as exc:
                        self.errors[hive] = str(exc)
                        continue
                    self.errors.pop(hive, None)
                    supervisor = self.supervisors[hive] = self._supervisor_factory(spec)
                supervisor.tick()

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            supervisors, self.supervisors = self.supervisors, {}
        for supervisor in supervisors.values():
            supervisor.shutdown()

    async def run(self) -> None:
        while True:
            await asyncio.to_thread(self.reconcile)
            await asyncio.sleep(self.interval)

    def component(self) -> Any:
        from .kernel.lifecycle import HostDaemonLifespanComponent, HostDaemonShutdownPhase

        @asynccontextmanager
        async def lifespan(_app: Any):
            task = asyncio.create_task(self.run(), name="beads-service-supervision")
            try:
                yield
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                await asyncio.to_thread(self.shutdown)

        return HostDaemonLifespanComponent(
            name="beads-service-supervision",
            lifespan=lifespan,
            shutdown_phase=HostDaemonShutdownPhase.CANCEL_PROCESSES,
        )


__all__ = [
    "INTENT_FILE",
    "DaemonBeadsSupervision",
    "HiveNotServable",
    "Intent",
    "disable",
    "enable",
    "is_enabled",
    "iter_intents",
    "iter_published",
    "locate",
    "resolve_endpoint",
    "resolve_session",
    "runtime_root",
    "service_module",
    "service_spec",
    "spec_for_dir",
    "spec_for_hive",
    "start_command",
]
