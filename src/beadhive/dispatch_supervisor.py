"""Per-hive unattended dispatch supervision.

Systemd user units are the default. ``process`` runs the picker in the foreground
under container init, Kubernetes, frame-agent, or launchd. The legacy ``container``
and ``launchd`` backend names select process mode; they do not install services.
External supervisors own restart policy and send SIGTERM for bounded draining.
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
BACKEND_PROCESS = "process"
#: Closed set — mirrors `dolt.backend`'s `colima | docker | podman | none` shape. Only
#: Systemd owns service installation; process mode uses an external supervisor.
KNOWN_BACKENDS: tuple[str, ...] = (
    BACKEND_SYSTEMD,
    BACKEND_LAUNCHD,
    BACKEND_CONTAINER,
    BACKEND_PROCESS,
)


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


def _override_text(bh_binary: str, hive_slug: str, exec_argv: list[str]) -> str:
    """A systemd drop-in overriding ONE instance's `ExecStart` with *exec_argv* appended —
    e.g. `--dry-run` / `--seat-binary <path>` (bh-3xl60). The blank `ExecStart=` line first is
    load-bearing: systemd APPENDS repeated directives rather than replacing them, so without it
    the instance would run both the template's `ExecStart` and this one."""
    parts = (bh_binary, "host", "dispatch", "run", "--hive", hive_slug, *exec_argv)
    exec_start = " ".join(shlex.quote(a) for a in parts)
    return f"[Service]\nExecStart=\nExecStart={exec_start}\n"


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

    def _override_dir(self, hive_slug: str) -> Path:
        return self.unit_dir / f"{_unit_instance(hive_slug)}.d"

    def _override_path(self, hive_slug: str) -> Path:
        return self._override_dir(hive_slug) / "override.conf"

    def _ensure_override(self, hive_slug: str, *, exec_argv: list[str]) -> None:
        """Converge the PER-INSTANCE drop-in that carries *exec_argv* (bh-3xl60's `--dry-run` /
        `--seat-binary <path>`) — idempotently, like `_ensure_template`: write/refresh when
        *exec_argv* is non-empty, remove when it is (a plain `enable` after a dry-run `enable`
        should not leave a stale override behind), and only `daemon-reload` when something on
        disk actually changed."""
        path = self._override_path(hive_slug)
        if not exec_argv:
            if path.exists():
                path.unlink()
                self._systemctl("daemon-reload")
            return
        self._override_dir(hive_slug).mkdir(parents=True, exist_ok=True)
        text = _override_text(self.bh_binary, hive_slug, exec_argv)
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            self._systemctl("daemon-reload")

    def enable(
        self, hive_slug: str, *, exec_argv: list[str], env: dict[str, str]
    ) -> SupervisorState:
        # `env` is unused: nothing this backend forwards needs one today (bh-3xl60 forwards
        # `--dry-run` / `--seat-binary` as argv, not env). `exec_argv` is what the per-instance
        # override drop-in carries — the template's `%i` substitution covers everything ELSE.
        self._ensure_template()
        self._ensure_override(hive_slug, exec_argv=exec_argv)
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
    """Select systemd or the externally supervised foreground process runtime."""
    if cfg is None:
        cfg = config.load()
    name = config.dispatch_supervisor_backend(cfg)
    if name == BACKEND_SYSTEMD:
        return SystemdUserBackend()
    if name in (BACKEND_PROCESS, BACKEND_CONTAINER, BACKEND_LAUNCHD):
        return ProcessBackend()
    raise ValueError(f"unknown host.dispatch.backend {name!r} — expected one of {KNOWN_BACKENDS}")


class ProcessBackend:
    """Foreground dispatch owned by container init, Kubernetes, or launchd.

    No background process is created and no OS service is installed. The external
    supervisor invokes ``bh host dispatch run --hive <hive>`` and owns restart policy.
    ``container`` and ``launchd`` select this same runtime for compatibility.
    """

    name = BACKEND_PROCESS

    def enable(
        self, hive_slug: str, *, exec_argv: list[str], env: dict[str, str]
    ) -> SupervisorState:
        raise ValueError(
            "host.dispatch.backend=process requires an external supervisor: run "
            "`bh host dispatch run --hive <hive>` in the foreground; configure restart "
            "policy in container init, Kubernetes, or launchd"
        )

    def disable(self, hive_slug: str) -> SupervisorState:
        raise ValueError(
            "host.dispatch.backend=process is externally supervised; send SIGTERM to "
            "the foreground dispatch process to drain it"
        )

    def status(self, hive_slug: str) -> SupervisorState:
        return SupervisorState(
            detail="externally supervised; query container init, Kubernetes, or launchd"
        )
