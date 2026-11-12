"""``bh backfill judge setup``: one step to enable the opt-in haiku judge.

Steps, in order, each reported (and each a no-op under ``--dry-run``):

1. **extra**: verify ``beadlog-sources[haiku]`` (the ``httpx`` dependency and the judge command)
   and install it when missing (``--no-install`` only verifies);
2. **key**: resolve ``ANTHROPIC_API_KEY`` through the host's fnox broker, the same seam HQ's SQL
   transport and the XTDB store password use (:class:`beadhive.hq_sql_transport.FnoxBroker`), or
   take it from the environment when no fnox reference is configured;
3. **probe**: one ``bh-judge/1`` call through :class:`beadlog_sources.CommandJudge`, reporting the
   model and latency; a fallback (offline, bad key, no extra) is a failed probe;
4. **enable**: only with ``--enable`` and only after every earlier step passed, write
   ``backfill.judge.enabled`` (and the fnox *reference*, never a value) to the host config.

The key lives in this process only: it is handed to the probe subprocess through its environment
and never printed, logged or written; any text that reaches the output is scrubbed of it first.
A failure at any step leaves the config untouched.
"""

from __future__ import annotations

import importlib.util
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

API_KEY_ENV = "ANTHROPIC_API_KEY"
EXTRA_SPEC = "beadlog-sources[haiku]"
PROBE_DEADLINE_SECONDS = 15.0
INSTALL_TIMEOUT_SECONDS = 300.0

Installer = Callable[[], tuple[bool, str]]
ProbeFn = Callable[[str, Mapping[str, Any]], "ProbeResult"]


@dataclass(frozen=True)
class ProbeResult:
    """The outcome of the one-call probe: ``ok`` only when the judge command itself answered."""

    ok: bool
    model: str | None = None
    latency_ms: int | None = None
    detail: str = ""


@dataclass
class Step:
    name: str
    status: str  # ok | planned | skipped | failed
    detail: str = ""


@dataclass
class SetupReport:
    steps: list[Step] = field(default_factory=list)
    enabled: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(step.status != "failed" for step in self.steps)


def extra_present(command: str) -> bool:
    """The ``[haiku]`` extra is usable: ``httpx`` imports and the judge command resolves."""
    argv = shlex.split(command)
    return bool(importlib.util.find_spec("httpx") is not None and argv and shutil.which(argv[0]))


def pip_installer() -> tuple[bool, str]:
    """Install the extra into the interpreter running ``bh`` (uv when present, else pip)."""
    uv = shutil.which("uv")
    argv = (
        [uv, "pip", "install", "--python", sys.executable, EXTRA_SPEC]
        if uv
        else [sys.executable, "-m", "pip", "install", EXTRA_SPEC]
    )
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"installer could not run ({type(exc).__name__})"
    if done.returncode != 0:
        return False, f"installer exited {done.returncode}"
    return True, "installed"


def fnox_reference(section: Mapping[str, Any]) -> dict[str, str] | None:
    """The configured ``credential`` reference when it names a complete fnox lookup."""
    ref = section.get("credential")
    if isinstance(ref, Mapping) and all(ref.get(k) for k in ("config_path", "profile", "key")):
        return {k: str(ref[k]) for k in ("config_path", "profile", "key")}
    return None


def fnox_key(reference: Mapping[str, str], *, broker: Any = None) -> str:
    """Resolve the key through the fnox broker; failures carry its static message only."""
    from .hq_sql_transport import FnoxBroker, SqlTransportError  # lazy: only when resolving

    try:
        value = (broker or FnoxBroker()).get(
            dict(reference), deadline=time.monotonic() + PROBE_DEADLINE_SECONDS
        )
    except SqlTransportError as exc:
        raise KeyError(str(exc)) from None
    if not value:
        raise KeyError("the secret store returned no value")
    return value


def probe_judge(key: str, settings: Mapping[str, Any]) -> ProbeResult:
    """One real ``bh-judge/1`` call (the seam tests replace); a fallback is a failure."""
    from beadlog_sources import ChoiceQuery, CommandJudge
    from beadlog_sources.judges import CommandResult

    def runner(argv: Sequence[str], stdin: bytes, timeout: float) -> CommandResult:
        import os

        env = {**os.environ, API_KEY_ENV: key}
        done = subprocess.run(  # noqa: S603 - the operator configures the command
            list(argv),
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=timeout,
            check=False,
        )
        return CommandResult(done.returncode, done.stdout)

    judge = CommandJudge(
        settings["command"],
        timeout_seconds=float(settings["timeout_seconds"]),
        model=settings["model"],
        max_calls_per_run=1,
        runner=runner,
    )
    started = time.monotonic()
    verdict = judge.choose(
        ChoiceQuery(
            question="Which option is a fruit?",
            options={"apple": "an apple", "chair": "a chair"},
            context="Connectivity probe; answer with the true option.",
            family="probe",
        )
    )
    latency = int((time.monotonic() - started) * 1000)
    if verdict.fallback is not None:
        return ProbeResult(False, detail=f"fell back ({verdict.fallback.reason})")
    return ProbeResult(True, model=verdict.model or settings["model"], latency_ms=latency)


def _scrub(text: str, key: str | None) -> str:
    return text.replace(key, "<redacted>") if key else text


def run_setup(
    section: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
    dry_run: bool = False,
    install: bool = True,
    enable: bool = False,
    reference: dict[str, str] | None = None,
    installer: Installer | None = None,
    present: Callable[[str], bool] | None = None,
    resolve_key: Callable[[Mapping[str, str]], str] | None = None,
    probe: ProbeFn | None = None,
    persist: Callable[[Mapping[str, str] | None], None] | None = None,
) -> SetupReport:
    """Run the setup steps; ``persist`` is called once, only on full success with ``enable``."""
    # Bound at call time so the module-level seams stay replaceable.
    installer = installer or pip_installer
    present = present or extra_present
    resolve_key = resolve_key or fnox_key
    probe = probe or probe_judge
    report = SetupReport(dry_run=dry_run)
    steps = report.steps
    command = str(section.get("command") or "bh-judge-haiku")
    settings = {
        "command": command,
        "timeout_seconds": section.get("timeout_seconds", 30.0),
        "model": section.get("model", "claude-haiku-5-5"),
    }
    reference = reference or fnox_reference(section)

    # 1. the extra
    if present(command):
        steps.append(Step("extra", "ok", f"{EXTRA_SPEC} is installed"))
    elif dry_run:
        steps.append(Step("extra", "planned", f"would install {EXTRA_SPEC}"))
    elif not install:
        steps.append(Step("extra", "failed", f"{EXTRA_SPEC} is not installed (--no-install)"))
        return report
    else:
        done, detail = installer()
        if not done or not present(command):
            steps.append(Step("extra", "failed", detail if not done else "still missing"))
            return report
        steps.append(Step("extra", "ok", f"{EXTRA_SPEC} {detail}"))

    # 2. the key
    key: str | None = None
    source = f"fnox profile {reference['profile']}" if reference else API_KEY_ENV
    if dry_run:
        steps.append(Step("key", "planned", f"would resolve {API_KEY_ENV} from {source}"))
    else:
        try:
            key = resolve_key(reference) if reference else (environ.get(API_KEY_ENV) or "")
            if not key:
                raise KeyError(
                    f"{API_KEY_ENV} is not set and no credential reference is configured"
                )
        except KeyError as exc:
            steps.append(Step("key", "failed", _scrub(str(exc.args[0]), key)))
            return report
        steps.append(Step("key", "ok", f"{API_KEY_ENV} resolved from {source}"))

    # 3. the probe
    if dry_run:
        steps.append(Step("probe", "planned", f"would make one {settings['model']} call"))
    else:
        assert key is not None
        try:
            result = probe(key, settings)
        except Exception as exc:  # noqa: BLE001 - a probe never aborts with a traceback
            result = ProbeResult(False, detail=f"probe raised {type(exc).__name__}")
        if not result.ok:
            steps.append(Step("probe", "failed", _scrub(result.detail, key) or "probe failed"))
            return report
        steps.append(
            Step("probe", "ok", f"model {result.model}, {result.latency_ms} ms (bh-judge/1)")
        )

    # 4. enable
    if not enable:
        steps.append(Step("enable", "skipped", "pass --enable to set backfill.judge.enabled"))
    elif dry_run:
        steps.append(Step("enable", "planned", "would set backfill.judge.enabled = true"))
    else:
        if persist is not None:
            persist(reference)
        report.enabled = True
        steps.append(Step("enable", "ok", "backfill.judge.enabled = true"))
    return report


__all__ = [
    "API_KEY_ENV",
    "EXTRA_SPEC",
    "ProbeResult",
    "SetupReport",
    "Step",
    "extra_present",
    "fnox_key",
    "fnox_reference",
    "pip_installer",
    "probe_judge",
    "run_setup",
]
