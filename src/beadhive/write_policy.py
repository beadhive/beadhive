"""The off-primary write policy: ``fenced``, ``forward`` or ``open`` (trust-profiles ADR section 4).

bh-sxhnq. What a host that is not a hive's writer may do with bead writes:

* ``fenced`` — today's behaviour: only the writer frame (or lease holder) writes; every other
  host is refused by :func:`beadhive.guard.guard_capability`.
* ``forward`` — bh's bd points at the primary's hive server through a per-frame, address-pinned
  TLS account (``bh hive forward point``, ``docs/FORWARD-WRITE-PATH.md``); the primary's guard
  decides the write.
* ``open`` — **never forwards**. A hive's forward marker and ``host.forward`` are ignored, and
  ``bh hive forward point`` refuses naming the profile. A non-primary write lands on the
  replica's own Dolt data (its ``bh_local_ident`` carries ``role = 'branch'`` so the in-data
  guard accepts it) and is published only to the frame-private ref ``refs/dolt/frame/<id>``,
  never ``main`` (:mod:`beadhive.write_policy_publish`); the writer merges it (bh-sfp9o.5).
  Only :data:`OPEN_OPERATIONS` are allowed. Child create is refused until
  gastownhall/beads#4796 gives frame-scoped child ids — a correctness limit no profile lifts —
  and top-level create is refused until the ``bh-xwy5x`` decision confirms hash ids merge
  cleanly. Every such write prints one **readiness warning** per command and appends a counted
  event, which ``bh doctor`` reports per hive over ``hive.readiness_window``.

**Resolution** (:func:`resolve`). The trust profile supplies the default (``open`` for
``open``, ``forward`` for ``trusted``, ``verified`` and ``attested``). Then, in order:

1. the fleet layer — the hive's own ``managed_repos[].hive.write_policy``, else the fleet default
   ``hive.write_policy``. It is honoured as signed when the fleet runs ``trusted`` authority
   (unsigned content is honoured there, as for ``hq.trust_profile`` itself); otherwise it may
   only move the knob towards more trust (:func:`beadhive.trust_profiles.resolve`);
2. the host override — ``hive.write_policy`` in this host's ``host.yaml`` (bh-bc2rj), which may
   only require more trust than the result of step 1.

A ``forward`` result for a hive whose forwarder is not configured on this host (no forward
marker on its checkout and no ``host.forward`` endpoint) is ``fenced``.

This module decides and records; it never runs bd. The guard, the engine and the forward CLI
consult it, resolving it by name so it stays outside their legacy import cycle.
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

FENCED = "fenced"
FORWARD = "forward"
OPEN = "open"
POLICIES: tuple[str, ...] = (FENCED, FORWARD, OPEN)

#: The upstream issue whose frame-scoped child ids would let a replica allocate ``<epic>.N``.
CHILD_ID_ISSUE = "gastownhall/beads#4796"
CHILD_ID_URL = "https://github.com/gastownhall/beads/issues/4796"
#: The decision bead that opens top-level create under ``open`` once hash ids merge cleanly.
CREATE_DECISION = "bh-xwy5x"

#: The frame-private data ref a replica under ``open`` publishes to, and its Dolt remote name.
FRAME_REF = "refs/dolt/frame/{frame}"
FRAME_REMOTE = "frame-{frame}"

#: ADR section 4: what a non-primary host may do under ``open``.
OPEN_OPERATIONS: tuple[str, ...] = (
    "comment",
    "label",
    "note",
    "status",
    "dependency",
    "claim",
    "close",
)

#: ``hive.readiness_window`` when unset or unreadable: 7 days.
DEFAULT_WINDOW_S = 7 * 86400
#: The counted readiness events, under ``$BH_HOME/state``.
EVENTS = "write-policy-readiness.jsonl"
#: Compact the event log (drop events older than the window) once it grows past this.
EVENTS_COMPACT_BYTES = 1 << 20


# =============================================================================================
# Resolution
# =============================================================================================


@dataclass(frozen=True)
class EffectivePolicy:
    """The write policy one hive runs on this host, and why."""

    name: str
    #: The trust profile that supplied the default.
    profile: str
    #: The value after the override layers, before the forwarding fallback.
    resolved: str
    #: Where :attr:`resolved` came from: ``profile`` or the override's key and source.
    source: str = "profile"
    #: Whether this host has a forwarder configured for the hive.
    forwarding_configured: bool = True
    hive: str = ""
    #: Each override layer's outcome (:class:`beadhive.trust_profiles.OverrideOutcome`).
    outcomes: tuple = ()

    @property
    def forwards(self) -> bool:
        return self.name == FORWARD

    @property
    def open(self) -> bool:
        return self.name == OPEN

    @property
    def refusals(self) -> tuple:
        return tuple(outcome for outcome in self.outcomes if not outcome.accepted)

    def describe(self) -> str:
        """``open (trust profile open)`` / ``fenced (forward from the trusted profile; no
        forwarder configured on this host)``."""
        origin = (
            f"the {self.profile} trust profile"
            if self.source == "profile"
            else f"{self.source} under the {self.profile} trust profile"
        )
        if self.name != self.resolved:
            return (
                f"{self.name} ({self.resolved} from {origin}; no forwarder configured on this host)"
            )
        return f"{self.name} (from {origin})"

    def as_dict(self) -> dict:
        return {
            "hive": self.hive,
            "policy": self.name,
            "profile": self.profile,
            "resolved": self.resolved,
            "source": self.source,
            "forwarding_configured": self.forwarding_configured,
            "line": self.describe(),
            "refused_overrides": [outcome.message for outcome in self.refusals],
        }


def _trust_profiles():
    return importlib.import_module("beadhive.trust_profiles")


def _override(key: str, value, source: str, *, signed: bool):
    return _trust_profiles().Override("write_policy", key, value, source, signed=signed)


def resolve(
    profile,
    *,
    fleet_default: str | None = None,
    hive_override: str | None = None,
    host_override: str | None = None,
    forwarding_configured: bool = True,
    hive: str = "",
) -> EffectivePolicy:
    """The effective policy for one hive from its layers (module docstring). Pure.

    `profile` is a :class:`beadhive.trust_profiles.ResolvedProfile`; only its name and its
    authority column are read (an unsigned fleet layer under ``signed`` authority can only
    tighten). A layer whose value is invalid or would loosen is refused and reported, and the
    knob keeps its value."""
    trust = profile.authority_mode == "trusted"
    overrides = []
    if hive_override is not None:
        key = f"managed_repos[{hive or '?'}].hive.write_policy"
        overrides.append(_override(key, hive_override, "fleet.yaml", signed=trust))
    elif fleet_default is not None:
        overrides.append(_override("hive.write_policy", fleet_default, "fleet.yaml", signed=trust))
    if host_override is not None:
        overrides.append(_override("hive.write_policy", host_override, "host.yaml", signed=False))
    resolved = _trust_profiles().resolve(profile.name, overrides)
    value = resolved.write_policy
    source = "profile"
    for outcome in resolved.outcomes:
        if outcome.accepted and outcome.effect != "unchanged":
            source = f"{outcome.override.key} ({outcome.override.source})"
    name = FENCED if value == FORWARD and not forwarding_configured else value
    return EffectivePolicy(
        name=name,
        profile=profile.name,
        resolved=value,
        source=source,
        forwarding_configured=forwarding_configured,
        hive=hive,
        outcomes=resolved.outcomes,
    )


def _section(settings, name: str) -> dict:
    value = settings.get(name) if isinstance(settings, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _config():
    return importlib.import_module("beadhive.config")


def _host_settings() -> Mapping:
    """This host's own ``host.yaml`` (the host override layer). ``{}`` when unreadable."""
    try:
        return _config().load_host() or {}
    except Exception:  # noqa: BLE001 - no host config: no host override
        return {}


def _fleet_settings() -> Mapping:
    try:
        return _config().load_fleet() or {}
    except Exception:  # noqa: BLE001 - no fleet document: no fleet default
        return {}


def layers(entry, cfg, host: Mapping | None = None) -> dict:
    """``{fleet_default, hive_override, host_override}`` for `entry` from the merged `cfg` and
    this host's `host` settings. The merged config carries the host value when host.yaml sets
    it, so the fleet document is read only then."""
    host = _host_settings() if host is None else host
    host_value = _section(host, "hive").get("write_policy")
    if host_value is None:
        fleet_value = _section(cfg, "hive").get("write_policy")
    else:
        fleet_value = _section(_fleet_settings(), "hive").get("write_policy")
    return {
        "fleet_default": fleet_value,
        "hive_override": _section(entry or {}, "hive").get("write_policy"),
        "host_override": host_value,
    }


def forwarding_configured(cfg, hive_dir=None) -> bool:
    """Whether this host has a forwarder for the hive: a forward marker on its checkout, or
    ``host.forward`` enabled with at least one primary endpoint."""
    forward = _section(_section(cfg, "host"), "forward")
    if forward.get("enabled") and forward.get("endpoints"):
        return True
    if hive_dir is None:
        return False
    try:
        hive_forward = importlib.import_module("beadhive.hive_forward")
        return hive_forward.read_marker(hive_dir) is not None
    except Exception:  # noqa: BLE001 - an unreadable marker is no forwarder
        return False


def trust_profile(host: Mapping | None = None):
    """The trust profile this host runs (:func:`beadhive.hq_authority_enforce.trust_profile`)."""
    enforce = importlib.import_module("beadhive.hq_authority_enforce")
    return enforce.trust_profile(_host_settings() if host is None else host)


def for_entry(entry, *, cfg=None, hive_dir=None, profile=None) -> EffectivePolicy:
    """The effective policy for a registered hive `entry` on this host."""
    cfg = _config().load() if cfg is None else cfg
    host = _host_settings()
    profile = trust_profile(host) if profile is None else profile
    if hive_dir is None and entry:
        try:
            hive_dir = importlib.import_module("beadhive.registry").hive_dir(entry)
        except Exception:  # noqa: BLE001 - no directory: only host.forward can configure it
            hive_dir = None
    return resolve(
        profile,
        **layers(entry, cfg, host),
        forwarding_configured=forwarding_configured(cfg, hive_dir),
        hive=str((entry or {}).get("prefix") or ""),
    )


def for_dir(hive_dir, cfg=None) -> EffectivePolicy | None:
    """The effective policy for the hive checkout at `hive_dir`, or ``None`` when it is no
    registered hive (nothing to decide)."""
    cfg = _config().load() if cfg is None else cfg
    registry = importlib.import_module("beadhive.registry")
    try:
        entry = registry.entry_for_dir(cfg, Path(hive_dir))
    except Exception:  # noqa: BLE001 - an unresolvable directory is no hive
        return None
    if not entry or not entry.get("prefix"):
        return None
    return for_entry(entry, cfg=cfg, hive_dir=hive_dir)


def for_hive(hive: str = "", *, cfg=None) -> EffectivePolicy | None:
    """The effective policy for `hive` (``""``: the hive owning the process cwd)."""
    cfg = _config().load() if cfg is None else cfg
    registry = importlib.import_module("beadhive.registry")
    try:
        directory = registry.hive_dir_for(cfg, hive)
    except Exception:  # noqa: BLE001 - an unresolvable hive is no hive
        return None
    return for_dir(directory, cfg) if directory is not None else None


def ignores_forwarding(hive_dir, cfg=None) -> bool:
    """Whether the hive at `hive_dir` must not forward: its policy is ``open`` (never forwards)
    or ``fenced`` (the writer frame only). Fails towards today's behaviour (``False``) when the
    policy cannot be resolved, so the forward marker keeps deciding."""
    try:
        policy = for_dir(hive_dir, cfg)
    except Exception:  # noqa: BLE001 - unresolvable: the marker decides, as before
        return False
    return policy is not None and not policy.forwards


# =============================================================================================
# Off-primary writes
# =============================================================================================


@dataclass(frozen=True)
class OffPrimary:
    """This host is not the hive's writer: who is."""

    prefix: str
    writer: str
    this_frame: str
    #: Decided from the in-data ``bh_writer`` (cut over) or the host lease (legacy).
    cut_over: bool


def off_primary(*, cfg=None, hive_dir=None, hive: str = "") -> OffPrimary | None:
    """``None`` when this host is the hive's writer, or no writer is configured at all (a
    single-host factory). Raises :class:`beadhive.guard.WriterUnreadable` when the in-data
    fence cannot be read, so callers fail closed exactly as the guard does."""
    guard = importlib.import_module("beadhive.guard")
    state = guard.writer_state(hive, cfg=cfg, hive_dir=hive_dir)
    if state is not None:
        prefix, this_frame, writer = state
        if this_frame and writer.frame == this_frame:
            return None
        return OffPrimary(prefix, str(writer.frame or ""), this_frame, True)
    lease_state = guard.primary_state(hive, cfg=cfg, hive_dir=hive_dir)
    if lease_state is None:
        return None
    prefix, this_host, lease = lease_state
    if lease.held_by(this_host):
        return None
    return OffPrimary(prefix, str(lease.host_id or ""), this_host, False)


@dataclass(frozen=True)
class OpenWrite:
    """A write this host makes under ``open`` while it is not the hive's writer."""

    policy: EffectivePolicy
    where: OffPrimary
    #: The entry's ``hive`` section, for the per-hive readiness switch.
    entry: Mapping | None = None


def open_write(hive_dir, cfg=None) -> OpenWrite | None:
    """The ``open`` off-primary write context for `hive_dir`, else ``None`` (another policy,
    this host is the writer, or no hive).

    A policy that cannot be resolved (no config, no registry entry) is not ``open``: today's
    behaviour, whose guard decides. Raises like :func:`off_primary` when the hive's in-data
    fence cannot be read, so callers fail closed exactly as the guard does."""
    try:
        cfg = _config().load() if cfg is None else cfg
        entry = importlib.import_module("beadhive.registry").entry_for_dir(cfg, Path(hive_dir))
        if not entry or not entry.get("prefix"):
            return None
        policy = for_entry(entry, cfg=cfg, hive_dir=hive_dir)
    except Exception:  # noqa: BLE001 - unresolvable: not open, the guard decides as before
        return None
    if not policy.open:
        return None
    where = off_primary(cfg=cfg, hive_dir=Path(hive_dir))
    return OpenWrite(policy, where, entry) if where is not None else None


# ---- the allowed operations -----------------------------------------------------------------

_GLOBAL_VALUE_FLAGS = frozenset({"-C", "--directory", "--actor", "--db", "--database", "-r"})
_CREATE_VERBS = frozenset({"create", "q", "create-form", "new"})
_CLAIM_VERBS = frozenset({"claim", "assign", "unclaim", "heartbeat"})
#: ``bd update`` flags allowed under ``open``, by the operation each is.
_UPDATE_FLAGS: dict[str, str] = {
    "--status": "status",
    "-s": "status",
    "--notes": "note",
    "--append-notes": "note",
    "--claim": "claim",
    "--assignee": "claim",
    "-a": "claim",
    "--add-label": "label",
    "--remove-label": "label",
    "--set-labels": "label",
    "--session": "status",
    "--if-assignee": "claim",
    "--if-status": "status",
}
#: Flags that change nothing about the row (output, attribution, routing).
_NEUTRAL_FLAGS = frozenset(
    {
        "--json",
        "-q",
        "--quiet",
        "-v",
        "--verbose",
        "--no-color",
        "--actor",
        "-C",
        "--directory",
        "--db",
        "--database",
        "--sandbox",
        "--ignore-schema-skew",
    }
)


@dataclass(frozen=True)
class Operation:
    """What one bd invocation does, in :data:`OPEN_OPERATIONS` terms."""

    kind: str
    #: Why it is not allowed (the verb or flag), for :func:`open_refusal`.
    detail: str = ""


def _positionals(args: Iterable[str]) -> list[str]:
    out, skip = [], False
    for token in args or []:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            skip = token in _GLOBAL_VALUE_FLAGS
            continue
        out.append(token)
    return out


def _flag_names(args: Iterable[str]) -> list[str]:
    return [token.split("=", 1)[0] for token in args or [] if token.startswith("-")]


def _flag_value(args: list[str], flag: str) -> str | None:
    for index, token in enumerate(args):
        if token == flag and index + 1 < len(args):
            return args[index + 1]
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return None


def _is_child_id(value: str | None) -> bool:
    """``<epic>.N``: a dotted id whose last segment is a number."""
    if not value or "." not in value:
        return False
    return value.rsplit(".", 1)[1].isdigit()


def classify(args) -> Operation:
    """The :data:`OPEN_OPERATIONS` kind of a bd invocation, ``create`` / ``child-create`` for
    the two creates, or ``other`` with the verb or flag that falls outside the list."""
    args = list(args or [])
    positionals = _positionals(args)
    verb = positionals[0] if positionals else ""
    sub = positionals[1] if len(positionals) > 1 else ""
    if verb in _CREATE_VERBS or (verb == "todo" and sub == "add"):
        if _flag_value(args, "--parent") or _is_child_id(_flag_value(args, "--id")):
            return Operation("child-create")
        return Operation("create")
    if verb in ("comment",) or (verb == "comments" and sub == "add"):
        return Operation("comment")
    if verb in ("tag",) or (verb == "label" and sub in ("add", "remove")):
        return Operation("label")
    if verb == "note":
        return Operation("note")
    if verb == "reopen":
        return Operation("status")
    if verb == "close":
        return Operation("close")
    if verb in _CLAIM_VERBS:
        return Operation("claim")
    if verb == "link" or (verb == "dep" and sub in ("add", "remove", "rm")):
        return Operation("dependency")
    if verb == "update":
        kinds = []
        for flag in _flag_names(args):
            if flag in _NEUTRAL_FLAGS:
                continue
            kind = _UPDATE_FLAGS.get(flag)
            if kind is None:
                return Operation("other", f"bd update {flag}")
            kinds.append(kind)
        return Operation(kinds[0] if kinds else "status")
    return Operation("other", f"bd {' '.join(positionals[:2]) or shlex.join(args[:2])}")


def _bh() -> str:
    try:
        return _config().BINARY_ALIAS
    except Exception:  # noqa: BLE001
        return "bh"


def open_refusal(args, where: OffPrimary) -> str:
    """``""`` when `args` is allowed off the primary under ``open``, else the refusal."""
    operation = classify(args)
    if operation.kind in OPEN_OPERATIONS:
        return ""
    head = f"✗ {where.prefix}: write policy open, and this host is not the hive's writer"
    tail = (
        f"  Run it on the writer frame {where.writer or '?'}, where it is not limited.\n"
        f"  Which layer refused and the one command that fixes it: {_bh()} hq why {where.prefix}"
    )
    if operation.kind == "child-create":
        return (
            f"{head}: child create (<epic>.N) is refused.\n"
            f"  Two replicas would allocate the same child id and the frame-ref merge would "
            f"silently drop one bead; it stays refused until upstream {CHILD_ID_ISSUE} gives "
            f"frame-scoped child ids ({CHILD_ID_URL}). No profile or override lifts it.\n{tail}"
        )
    if operation.kind == "create":
        return (
            f"{head}: top-level create is refused until the {CREATE_DECISION} decision confirms "
            f"bd's hash ids merge cleanly across replicas ({_bh()} work issue "
            f"{CREATE_DECISION}).\n{tail}"
        )
    return (
        f"{head}: `{operation.detail}` is outside the operations open allows off the primary "
        f"({', '.join(OPEN_OPERATIONS)}).\n{tail}"
    )


# =============================================================================================
# The readiness warning
# =============================================================================================

#: Hives warned in this process: exactly one warning (and one counted event) per command.
_warned: set[str] = set()


def reset_warned() -> None:
    _warned.clear()


def _events_path() -> Path | None:
    try:
        return Path(_config().home()) / "state" / EVENTS
    except Exception:  # noqa: BLE001 - no home: nothing is counted
        return None


def readiness_enabled(entry, cfg) -> bool:
    """``hive.readiness_warning``: the hive's own override, else the fleet default (on)."""
    own = _section(entry or {}, "hive").get("readiness_warning")
    value = own if own is not None else _section(cfg, "hive").get("readiness_warning", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("off", "false", "no", "0")
    return bool(value)


def readiness_window_s(cfg) -> int:
    """``hive.readiness_window`` in seconds (an ISO-8601 duration; default 7 days)."""
    raw = _section(cfg, "hive").get("readiness_window")
    if not raw:
        return DEFAULT_WINDOW_S
    try:
        settings = importlib.import_module("beadhive.config_work_settings")
        seconds = settings.duration_seconds(str(raw), "P7D")
    except Exception:  # noqa: BLE001 - a bad window degrades to the default
        return DEFAULT_WINDOW_S
    return seconds if seconds > 0 else DEFAULT_WINDOW_S


def warning_text(prefix: str, writer: str, this_frame: str) -> str:
    ref = FRAME_REF.format(frame=this_frame or "<frame>")
    return (
        f"⚠ {prefix}: write policy open — this write lands on this replica's own data and is "
        f"published only to {ref}; the hive's writer frame {writer or '?'} merges it. Under "
        f"the trusted trust profile and above it would be forwarded to {writer or '?'}. "
        "(Readiness warning, never a refusal: hive.readiness_warning: off silences it.)"
    )


def record_event(event: Mapping, *, window_s: int = DEFAULT_WINDOW_S, path: Path | None = None):
    """Append one counted readiness event. Never raises; compacts an oversized log."""
    path = path or _events_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(event), sort_keys=True) + "\n")
        if path.stat().st_size > EVENTS_COMPACT_BYTES:
            _compact(path, float(event.get("at") or time.time()) - window_s)
    except OSError:
        return


def _read_events(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _compact(path: Path, cutoff: float) -> None:
    kept = [e for e in _read_events(path) if float(e.get("at") or 0) >= cutoff]
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in kept))
    temporary.replace(path)


def readiness(
    write: OpenWrite,
    *,
    cfg=None,
    verb: str = "",
    now: Callable[[], float] = time.time,
    echo: Callable[[str], None] | None = None,
) -> bool:
    """Print the readiness warning for `write` and count it — once per hive per process
    (command). Returns whether this call was the one that counted. The warning is suppressed
    by ``hive.readiness_warning: off``; the event is counted either way, so ``bh doctor`` still
    measures how much the fleet relies on ``open``."""
    where = write.where
    if where.prefix in _warned:
        return False
    _warned.add(where.prefix)
    cfg = cfg if cfg is not None else _safe_cfg()
    enabled = readiness_enabled(write.entry, cfg)
    if enabled:
        text = warning_text(where.prefix, where.writer, where.this_frame)
        (echo or _stderr)(text)
    record_event(
        {
            "at": now(),
            "hive": where.prefix,
            "writer": where.writer,
            "frame": where.this_frame,
            "verb": verb,
            "warned": enabled,
        },
        window_s=readiness_window_s(cfg),
    )
    return True


def _stderr(text: str) -> None:
    sys.stderr.write(text + "\n")


def _safe_cfg():
    try:
        return _config().load()
    except Exception:  # noqa: BLE001 - the warning never fails a write
        return {}


def readiness_counts(
    window_s: int, *, now: Callable[[], float] = time.time, path: Path | None = None
) -> dict[str, int]:
    """Readiness events per hive in the last `window_s` seconds."""
    path = path or _events_path()
    if path is None:
        return {}
    cutoff = now() - window_s
    counts: dict[str, int] = {}
    for event in _read_events(path):
        if float(event.get("at") or 0) < cutoff:
            continue
        hive = str(event.get("hive") or "")
        if hive:
            counts[hive] = counts.get(hive, 0) + 1
    return counts


# =============================================================================================
# The forward-point refusal
# =============================================================================================


def forward_point_refusal(policy: EffectivePolicy) -> str:
    """``""`` unless `policy` forbids pointing the hive at its primary (``open`` never
    forwards; ``fenced`` writes on the writer frame only)."""
    if policy.forwards or (policy.name == FENCED and policy.resolved == FORWARD):
        return ""  # forwarding allowed; pointing is how a forwarder gets configured
    if policy.open:
        return (
            f"{policy.hive}: the write policy is {policy.describe()}, and open never forwards: "
            f"`{_bh()} hive forward point` is refused. A non-primary write here lands on this "
            f"replica's own data and is published to {FRAME_REF.format(frame='<frame>')}, "
            "never main. To forward, run the trusted trust profile or above "
            f"({_bh()} hq authority profile-trusted)."
        )
    return (
        f"{policy.hive}: the write policy is {policy.describe()}: only the writer frame writes, "
        f"so `{_bh()} hive forward point` is refused. Remove the hive.write_policy override to "
        "forward."
    )


# =============================================================================================
# bh doctor
# =============================================================================================


def forwarder_login_problems(cfg) -> list[str]:
    """Why a configured forwarder login (``host.forward.endpoints``) is not a per-frame,
    address-pinned TLS login (ADR section 4: ``forward`` under ``trusted`` and ``verified``
    alike). Empty when every endpoint is one."""
    hive_forward = importlib.import_module("beadhive.hive_forward")
    forward = _section(_section(cfg, "host"), "forward")
    serve = _section(forward, "serve")
    shared = hive_forward.SYSTEM_ACCOUNTS | frozenset(
        str(o) for o in (serve.get("operators") or hive_forward.DEFAULT_OPERATORS)
    )
    problems = []
    for frame, endpoint in sorted(_section(forward, "endpoints").items()):
        endpoint = endpoint if isinstance(endpoint, Mapping) else {}
        user = str(endpoint.get("user") or "")
        where = f"host.forward.endpoints.{frame}"
        if not user:
            problems.append(f"{where}: no login of this frame's own (user is empty)")
        elif user in shared:
            problems.append(
                f"{where}: login {user!r} is a shared, root or operator login, not this frame's own"
            )
        if str(endpoint.get("tls_mode") or "required") != "required":
            problems.append(f"{where}: TLS is not required, so the login is not pinned")
        elif not (endpoint.get("ca_file") and endpoint.get("server_name")):
            problems.append(f"{where}: no ca_file/server_name pinning the primary's address")
    return problems


def _window_text(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def doctor_data(
    cfg,
    *,
    profile=None,
    host: Mapping | None = None,
    now: Callable[[], float] = time.time,
    events: Path | None = None,
    hive_dir: Callable[[Mapping], Path | None] | None = None,
    frame_state: Path | None = None,
) -> dict:
    """The doctor section: the effective write policy per hive, its readiness warnings over
    ``hive.readiness_window``, — under every profile that forwards — any forwarder login
    that is not per-frame and pinned, and the writer's recorded frame-ref merges per hive and
    frame (``frame_merges``, bh-sfp9o.5; ``warn`` while a ref is held by a conflict)."""
    host = _host_settings() if host is None else host
    profile = trust_profile(host) if profile is None else profile
    window = readiness_window_s(cfg)
    counts = readiness_counts(window, now=now, path=events)
    locate = hive_dir or _hive_dir_on_disk
    hives = []
    for entry in (cfg.get("managed_repos") if isinstance(cfg, Mapping) else None) or []:
        if not isinstance(entry, Mapping) or not entry.get("prefix"):
            continue
        if str(entry.get("kind") or "") == "hq":
            continue
        prefix = str(entry["prefix"])
        policy = resolve(
            profile,
            **layers(entry, cfg, host),
            forwarding_configured=forwarding_configured(cfg, locate(entry)),
            hive=prefix,
        )
        hives.append(
            {
                **policy.as_dict(),
                "readiness_warnings": counts.get(prefix, 0),
                "readiness_warning": readiness_enabled(entry, cfg),
            }
        )
    logins = forwarder_login_problems(cfg) if profile.name != "open" else []
    refused = any(item["refused_overrides"] for item in hives)
    frame_merges = importlib.import_module("beadhive.frame_merge").doctor_data(path=frame_state)
    return {
        "level": "warn" if logins or refused or frame_merges["held"] else "ok",
        "profile": profile.name,
        "window_s": window,
        "window": _window_text(window),
        "hives": hives,
        "readiness_total": sum(item["readiness_warnings"] for item in hives),
        "forwarder_logins": logins,
        "frame_merges": frame_merges,
    }


def _hive_dir_on_disk(entry: Mapping) -> Path | None:
    try:
        path = importlib.import_module("beadhive.registry").hive_dir(entry)
    except Exception:  # noqa: BLE001
        return None
    return path if path.exists() else None


def doctor_lines(data: Mapping) -> list[str]:
    """The text render of :func:`doctor_data`, one line per hive."""
    window = data.get("window", "7d")
    lines = [
        f"  {'⚠ WARN' if data.get('level') == 'warn' else '✓'} profile {data.get('profile')}; "
        f"readiness warnings counted over {window}"
    ]
    for item in data.get("hives", ()):
        count = int(item.get("readiness_warnings") or 0)
        glyph = "⚠" if item.get("refused_overrides") or (item["policy"] == OPEN and count) else "✓"
        tail = ""
        if item["policy"] == OPEN:
            tail = (
                f" · {count} readiness warning(s) in {window}: non-primary writes the trusted "
                "profile would forward"
            )
            if not item.get("readiness_warning", True):
                tail += " (console warning off)"
        lines.append(f"  {glyph} {item['hive']}: {item['line']}{tail}")
        lines += [f"      refused: {message}" for message in item.get("refused_overrides", ())]
    lines += [
        f"  ✗ forwarder login not per-frame and pinned — {problem}"
        for problem in data.get("forwarder_logins", ())
    ]
    frame_merges = data.get("frame_merges") or {}
    if frame_merges.get("hives"):
        lines.append("  frame refs merged by this writer (refs/dolt/frame/<id>):")
        lines += importlib.import_module("beadhive.frame_merge").doctor_lines(
            frame_merges, bh=_bh()
        )
    return lines
