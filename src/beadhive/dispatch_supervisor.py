"""The unattended-dispatch **supervision backend seam** (bh-e7r9q.4).

"Keep this loop running" is a backend choice, not a hard-coded systemd assumption. Three
planes, three supervisors, and they do not share an answer: `systemd --user` on Linux,
`launchd` on macOS, and a container whose PID 1 is already an interactive bash with
`init: true`. This fleet already spans two of them — beadhive-factory (executor, Linux) and
xeno-mac.lan (transient, macOS) — so baking systemd in would fork the product the first time
someone runs a dispatcher on the Mac.

Follows :mod:`beadhive.engine` (`Engine` Protocol + `get_engine`) and :mod:`beadhive.dolt`
(container-backend dispatch) verbatim in shape AND in restraint: a config key selects ONE
thin implementation, not a plugin framework. `SystemdUserBackend` is the only real one; a
second implementation (`RecordingBackend`) exists purely to prove the seam is an abstraction
and not an assertion — see `tests/test_dispatch_supervisor.py`.

ONE INSTANCE PER HIVE, not one process for all hives. The host lease is per-hive
(`refs/bh/lease/<prefix>`), so a single process would renew N refs and reason about partial
ownership; per-hive instances make "what am I driving" answerable and keep a wedged store or
poisoned worktree in one hive from stopping every other one. Enforced here through systemd
**template units** (`bh-dispatch@.service`, instantiated as `bh-dispatch@<hive-slug>.service`)
so there is never a hand-edited unit file — the template is written/refreshed once and every
hive gets an *instance* of it.

THE LEASE-ABSENT DEGRADATION PATH lives one layer up, in :mod:`beadhive.dispatch_hive_run` (the
process this backend supervises): an enabled instance for a hive this host does NOT hold the
lease on IDLES READ-ONLY and says so in the aggregate log — that is the multi-host model's
specified degradation, not an error. This module only starts/stops/persists the OS-level
process; it has no opinion about what that process does once running.

WHAT THIS SEAM DOES NOT DO: it is not a general-purpose service manager and it is not the
director loop. It answers exactly one question — "is bh-dispatch-run for hive X installed /
running / persisted across reboot" — through `enable` / `disable` / `status`, idempotently.

macOS (`launchd`) and container backends are NOT implemented. Each would have to supply:

  * macOS/launchd  — a per-hive `LaunchAgent` plist under `~/Library/LaunchAgents/`, templated
    the same way the systemd unit is, `launchctl bootstrap`/`bootout` for install/remove, and
    `launchctl print` (or `kickstart`) for status. `KeepAlive` covers restart-on-crash;
    persistence across reboot is a LaunchAgent's default (no separate "enable" step exists).
  * container    — PID 1 in an `init: true` container already reaps zombies and has no unit
    concept at all; there is nothing for `enable`/`disable` to install. The real seam there is
    the CONTAINER'S OWN restart policy (`restart: always` / `RestartPolicy`) plus this backend
    reporting `running` from a liveness probe (e.g. a marker file the driver touches per pass)
    rather than from a supervisor query, since there IS no supervisor to query.
"""

from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import config, log
from .run import run as run_cmd

_LOG = log.get_logger(__name__)

#: The systemd `--user` unit name TEMPLATE (bh-dispatch@.service). `%i` is the systemd
#: template-instance placeholder; `enable`/`disable`/`status` fill it with the sanitized hive
#: slug, e.g. `bh-dispatch@github-beadhive-beadhive.service`. One template, N instances — never
#: a hand-rolled per-hive unit file.
SYSTEMD_TEMPLATE_NAME = "bh-dispatch@.service"

BACKEND_SYSTEMD = "systemd"
BACKEND_LAUNCHD = "launchd"
BACKEND_CONTAINER = "container"
#: Closed set — mirrors `dolt.backend`'s `colima | docker | podman | none` shape. Only
#: `systemd` is implemented; the others are documented, not built (see module docstring).
KNOWN_BACKENDS: tuple[str, ...] = (BACKEND_SYSTEMD, BACKEND_LAUNCHD, BACKEND_CONTAINER)


@dataclass(frozen=True)
class SupervisorState:
    """What `enable` / `disable` / `status` all return — the three questions an operator
    (or `bh host dispatch status`) actually has about one hive's supervised loop.

    ``installed`` — does a unit/equivalent exist at all for this hive.
    ``running``   — is the process live right now.
    ``persisted`` — will it come back after a reboot (systemd: `is-enabled`).
    ``detail``    — one human-readable line; never parsed, only displayed.
    """

    installed: bool = False
    running: bool = False
    persisted: bool = False
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "installed": self.installed,
            "running": self.running,
            "persisted": self.persisted,
            "detail": self.detail,
        }


class SupervisorBackend(Protocol):
    """The seam. One thin implementation per platform — config-selected, never a plugin
    registry (that restraint is the whole reason `engine.py`/`dolt.py` are cited as
    precedent)."""

    name: str

    def enable(
        self, hive_slug: str, *, exec_argv: list[str], env: dict[str, str]
    ) -> SupervisorState:
        """Install (if needed) + start + persist-across-reboot, idempotently. Converges a
        half-state (installed-but-stopped, started-but-not-persisted) rather than erroring on
        a second call."""
        ...

    def disable(self, hive_slug: str) -> SupervisorState:
        """Stop + de-persist. Destroys nothing — the unit stays installed so a later `enable`
        does not need to recreate it from scratch."""
        ...

    def status(self, hive_slug: str) -> SupervisorState:
        """Read-only: the current installed/running/persisted state, with no side effects."""
        ...


# ------------------------------------------------------------------------------------------
# systemd --user — the one real backend
# ------------------------------------------------------------------------------------------


def _systemd_user_dir() -> Path:
    """`~/.config/systemd/user/`, honoring `XDG_CONFIG_HOME` the same way systemd itself does."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or ""
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "systemd" / "user"


def _unit_instance(hive_slug: str) -> str:
    return f"bh-dispatch@{hive_slug}.service"


def _template_unit_text(bh_binary: str) -> str:
    """The template unit's contents. `%i` is systemd's own instance-name substitution — the
    hive slug — so ONE file serves every hive; nothing here is hive-specific."""
    parts = (bh_binary, "host", "dispatch", "run", "--hive", "%i")
    exec_start = " ".join(shlex.quote(a) for a in parts)
    return (
        "[Unit]\n"
        "Description=beadhive unattended dispatch supervisor for hive %i\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        # Restart-on-crash is intentional (loop-ownership ADR Decision 1: restart is a no-op
        # by construction, nothing is persisted outside beads), but a crash loop still SHOULD
        # eventually stop retrying rather than spin forever if the driver is fundamentally
        # broken (e.g. missing `bh` on PATH).
        "StartLimitIntervalSec=600\n"
        "StartLimitBurst=20\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


class SystemdUserBackend:
    """The real, complete Linux backend: `systemd --user` template units, one instance per
    hive. Ships first and real per bh-e7r9q.4's acceptance bar."""

    name = BACKEND_SYSTEMD

    def __init__(self, *, unit_dir: Path | None = None, bh_binary: str | None = None):
        self.unit_dir = unit_dir or _systemd_user_dir()
        self.bh_binary = bh_binary or sys.argv[0] or "bh"

    def _systemctl(self, *args: str, check: bool = False):
        return run_cmd(["systemctl", "--user", *args], check=check, capture=True)

    def _ensure_template(self) -> None:
        """Write/refresh the ONE template unit, idempotently — only touches disk when the
        content actually changed, so `enable` never spuriously bounces a running instance."""
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        path = self.unit_dir / SYSTEMD_TEMPLATE_NAME
        text = _template_unit_text(self.bh_binary)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            self._systemctl("daemon-reload")

    def enable(
        self, hive_slug: str, *, exec_argv: list[str], env: dict[str, str]
    ) -> SupervisorState:
        # exec_argv/env are unused: the systemd template's `%i` substitution already covers
        # the whole per-hive argv, so there is nothing per-call left to inject.
        self._ensure_template()
        unit = _unit_instance(hive_slug)
        res = self._systemctl("enable", "--now", unit)
        if res.returncode != 0:
            return SupervisorState(
                installed=True,
                running=False,
                persisted=False,
                detail=(
                    res.stderr or res.stdout or f"systemctl enable --now {unit} failed"
                ).strip(),
            )
        return self.status(hive_slug)

    def disable(self, hive_slug: str) -> SupervisorState:
        unit = _unit_instance(hive_slug)
        self._systemctl("disable", "--now", unit)
        return self.status(hive_slug)

    def _wants_symlink(self, hive_slug: str) -> Path:
        """The `default.target.wants/` symlink `systemctl --user enable` creates for ONE
        instance — the per-instance artifact, as opposed to the shared template file."""
        return self.unit_dir / "default.target.wants" / _unit_instance(hive_slug)

    def status(self, hive_slug: str) -> SupervisorState:
        """`installed` is PER INSTANCE, never the shared template.

        THE BUG THIS FIXES: this read used to be `(unit_dir / "bh-dispatch@.service").exists()`
        — ONE file for every hive on the host. Enable hive A and every OTHER hive on the box
        immediately reported `installed=True, running=False`, which `_classify` renders as
        `enabled_stopped`: "supervised but not running", the dead-loop alarm. That is precisely
        the distinction bh-e7r9q.6 exists to make, inverted into a permanent false positive on
        every hive but one.

        `systemctl --user is-enabled <instance>` is the authoritative per-instance answer; the
        `default.target.wants/` symlink is the same fact on disk and is checked as a fallback
        for the case systemctl is unavailable. A unit that is RUNNING is installed by
        definition, which also covers the half-state `enable` converges (started, not yet
        persisted).
        """
        unit = _unit_instance(hive_slug)
        active = self._systemctl("is-active", unit)
        running = (active.stdout or "").strip() == "active"
        enabled = self._systemctl("is-enabled", unit)
        enabled_txt = (enabled.stdout or "").strip()
        persisted = enabled_txt in ("enabled", "static", "enabled-runtime")
        installed = persisted or running or self._wants_symlink(hive_slug).exists()
        active_txt = (active.stdout or "").strip() or "unknown"
        detail = f"is-active={active_txt} is-enabled={enabled_txt or 'unknown'}"
        return SupervisorState(
            installed=installed, running=running, persisted=persisted, detail=detail
        )


# ------------------------------------------------------------------------------------------
# The test double — proves the seam is an abstraction, not an assertion
# ------------------------------------------------------------------------------------------


class RecordingBackend:
    """An in-memory second implementation of :class:`SupervisorBackend`, used ONLY by tests.

    Exists to prove `dispatch_supervisor` genuinely dispatches on an interface rather than
    hard-coding systemd calls somewhere a Protocol conformance check would miss — the same
    reason `engine.py`/`dolt.py` are cited as the pattern to copy "in shape AND restraint"."""

    name = "recording"

    def __init__(self) -> None:
        self._state: dict[str, SupervisorState] = {}
        self.calls: list[tuple[str, str]] = []

    def enable(
        self, hive_slug: str, *, exec_argv: list[str], env: dict[str, str]
    ) -> SupervisorState:  # noqa: ARG002
        self.calls.append(("enable", hive_slug))
        self._state[hive_slug] = SupervisorState(
            installed=True, running=True, persisted=True, detail="recording backend"
        )
        return self._state[hive_slug]

    def disable(self, hive_slug: str) -> SupervisorState:
        self.calls.append(("disable", hive_slug))
        prev = self._state.get(hive_slug, SupervisorState())
        self._state[hive_slug] = SupervisorState(
            installed=prev.installed, running=False, persisted=False, detail="recording backend"
        )
        return self._state[hive_slug]

    def status(self, hive_slug: str) -> SupervisorState:
        self.calls.append(("status", hive_slug))
        return self._state.get(hive_slug, SupervisorState(detail="never enabled"))


def get_supervisor_backend(cfg: dict | None = None) -> SupervisorBackend:
    """The configured backend (`host.dispatch.backend`, default `systemd`) — the ONE place a
    config key becomes an implementation, mirroring `engine.get_engine` / `dolt`'s
    `compose.backend()`. `launchd` and `container` are known names (so config validation
    accepts them) but not yet implemented; selecting either raises with a message naming what
    the implementation would need to supply (see the module docstring)."""
    if cfg is None:
        cfg = config.load()
    name = config.dispatch_supervisor_backend(cfg)
    if name == BACKEND_SYSTEMD:
        return SystemdUserBackend()
    if name in (BACKEND_LAUNCHD, BACKEND_CONTAINER):
        raise NotImplementedError(
            f"host.dispatch.backend={name!r} is a known backend name but not yet implemented "
            f"— see the module docstring on beadhive.dispatch_supervisor for what it would "
            f"need to supply. Only {BACKEND_SYSTEMD!r} ships today."
        )
    raise ValueError(f"unknown host.dispatch.backend {name!r} — expected one of {KNOWN_BACKENDS}")
