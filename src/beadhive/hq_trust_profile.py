"""The fleet trust profile a host runs: ``hq.trust_profile`` learned from HQ, plus its overrides
(trust-profiles ADR section 2, bh-bc2rj). The profile table itself is
:mod:`beadhive.trust_profiles`.

**Carrier.** ``hq.trust_profile`` (``open`` | ``trusted`` | ``verified`` | ``attested``) is a
FLEET key in the committed ``fleet.yaml``, published exactly like ``hq.default_authority_mode``
(``bh hq authority profile-<name>``, :mod:`beadhive.hq_authority_fleet_mode`). Absent means
``verified``, which is 0.24.0's behaviour. A host LEARNS the profile from the config snapshots
it reads (:func:`beadhive.hq_authority_enforce.observe_fleet_default` calls
:func:`observe`):

* a frame whose authority mode is ``trusted`` honours the committed profile as-is, raises
  included — trusted authority means unsigned content is honoured, as for every other fleet key;
* a ``signed`` frame learns a raise (towards ``open``) only from an operator-authorised
  snapshot — the operator-signed config commit on Git HQ, the head the signed authority is
  bound to on SQL HQ;
* lowering it (towards ``attested``) is always learned.

On SQL HQ ``hq.trust_profile`` is in the frame-relevant set of
:func:`beadhive.hq_hive_policy.config_head_tolerated`: a key-less edit that LOOSENS it fences
signed frames until the operator re-signs authority against the new head; one that tightens it
is tolerated and learned. The learned value is persisted per host (:data:`STATE`, under
``$BH_HOME/state``) and memoized on the file's mtime.

**Authority column.** The profile's first column is the 0.24.0 authority mode: when
``hq.default_authority_mode`` is not set explicitly, ``open`` and ``trusted`` run ``trusted``
and ``verified`` / ``attested`` run ``signed`` (:func:`committed_fleet_default`). An explicit
``hq.default_authority_mode`` (operator-signed ``mode-trusted`` / ``mode-signed``) reads as an
override of that column, and a frame's own ``hq.authority_mode`` pin as a pin.

**Overrides.** ``hq.placement_mode``, ``hive.write_policy`` and ``hq.tenure`` in this host's
``host.yaml`` override one knob each; :func:`beadhive.trust_profiles.resolve` refuses any that
would loosen the profile (they are unsigned) and names the profile in the refusal.

This module is a leaf below the HQ authority modules: it imports only
:mod:`beadhive.trust_profiles` (and :mod:`beadhive.config`, late). Callers that know the
authority mode pass it in (:func:`beadhive.hq_authority_enforce.trust_profile`).
"""

from __future__ import annotations

import importlib
import json
import os
import time

from . import trust_profiles

STATE = "hq-fleet-trust-profile.json"
FLEET_DOCUMENT = "fleet.yaml"
#: ``hq.default_authority_mode``: the 0.24.0 carrier of the authority column.
AUTHORITY_KEY = "default_authority_mode"
PROFILE_KEY = "trust_profile"
AUTHORITY_MODES = ("signed", "trusted")
#: Profiles whose authority column is unsigned (0.24.0 ``trusted``).
TRUSTED_PROFILES = tuple(
    name
    for name in trust_profiles.PROFILES
    if trust_profiles.resolve(name).authority_mode == "trusted"
)
#: :data:`beadhive.hq_operator_settings.ENV`, read here without importing the control plane.
OPERATOR_SETTINGS_ENV = "BH_HQ_OPERATOR_SETTINGS"

_cache: tuple[tuple, str | None] | None = None
#: committed fleet.yaml content -> its trust-posture hq keys (bounded memo).
_committed: dict[str, dict] = {}


def reset_cache() -> None:
    global _cache
    _cache = None
    _committed.clear()


def _config():
    # Resolved late: config's store reaches hq_authority_enforce, which imports this module.
    return importlib.import_module("beadhive.config")


# ---- the committed carrier -------------------------------------------------------------------


def committed_hq_keys(snapshot) -> dict:
    """The committed ``fleet.yaml`` ``hq`` keys that set the fleet's trust posture
    (``default_authority_mode`` and ``trust_profile``, each ``None`` when absent) from a config
    snapshot or a document sequence. Any doubt is ``{}``. Memoized on the ``fleet.yaml``
    content (verification reads it on every authority read)."""
    try:
        documents = getattr(snapshot, "documents", snapshot)
        content = next(
            (document.content for document in documents if document.path == FLEET_DOCUMENT),
            None,
        )
    except Exception:  # noqa: BLE001 - an unreadable carrier never raises trust
        return {}
    if not isinstance(content, str):
        return {}
    if content in _committed:
        return _committed[content]
    try:
        from ruamel.yaml import YAML

        parsed = YAML(typ="safe").load(content)
        hq = parsed.get("hq") if isinstance(parsed, dict) else None
        hq = hq if isinstance(hq, dict) else {}
        value = {key: hq.get(key) for key in (AUTHORITY_KEY, PROFILE_KEY)}
    except Exception:  # noqa: BLE001 - an unreadable carrier never raises trust
        value = {}
    if len(_committed) >= 16:
        _committed.pop(next(iter(_committed)))
    _committed[content] = value
    return value


def committed(snapshot) -> str | None:
    """``hq.trust_profile`` committed in a config snapshot (or a document sequence): ``None``
    when absent; an invalid value is ``None`` too (it never raises trust)."""
    value = committed_hq_keys(snapshot).get(PROFILE_KEY)
    return value if value in trust_profiles.PROFILES else None


def committed_fleet_default(snapshot) -> str:
    """The fleet's authority column committed in a config snapshot: ``hq.default_authority_mode``
    when set explicitly, otherwise the column of ``hq.trust_profile`` (``open`` / ``trusted``
    run ``trusted``; ``verified``, the default, and ``attested`` run ``signed``). Absent,
    invalid or unreadable is ``"signed"``."""
    keys = committed_hq_keys(snapshot)
    explicit = keys.get(AUTHORITY_KEY)
    if explicit in AUTHORITY_MODES:
        return explicit
    return "trusted" if keys.get(PROFILE_KEY) in TRUSTED_PROFILES else "signed"


# ---- the learned profile ---------------------------------------------------------------------


def _state_path() -> str | None:
    try:
        return str(_config().home() / "state" / STATE)
    except Exception:  # noqa: BLE001 - no home: nothing learned, verified
        return None


def _mtime(path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except (OSError, TypeError, ValueError):
        return None


def state() -> dict:
    """The learned record (``{}`` when nothing has been learned)."""
    path = _state_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def learned() -> str | None:
    """The profile this host has learned, or ``None`` (nothing learned: the default
    ``verified``). Unreadable or invalid state is ``None``. Memoized on the file's mtime."""
    global _cache
    path = _state_path()
    key = (path, _mtime(path))
    if _cache is not None and _cache[0] == key:
        return _cache[1]
    value = state().get("profile") if key[1] is not None else None
    value = value if value in trust_profiles.PROFILES else None
    _cache = (key, value)
    return value


def current() -> str:
    """The learned profile name, or :data:`beadhive.trust_profiles.DEFAULT`."""
    return learned() or trust_profiles.DEFAULT


def record(value: str, *, head: str = "", via: str = "") -> bool:
    """Persist the learned profile; returns whether it changed. Never raises."""
    global _cache
    if value not in trust_profiles.PROFILES or value == learned():
        return False
    path = _state_path()
    if not path:
        return False
    body = json.dumps(
        {"profile": value, "config_head": head, "via": via, "learned_at": time.time()},
        sort_keys=True,
    )
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(body)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
    _cache = None
    return True


def observe(snapshot, *, authorised: bool, via: str = "") -> str:
    """Learn the profile from a config snapshot this process just verified.

    A looser profile than the learned one is honoured only when *authorised* (the
    operator-signed/bound snapshot); an equal or stricter one is always honoured. Returns the
    learned profile name; never raises."""
    try:
        value = committed(snapshot) or trust_profiles.DEFAULT
        known = current()
        if value == known or (trust_profiles.looser(value, known) and not authorised):
            return known
        record(value, head=str(getattr(snapshot, "commit_revision", "")), via=via)
        return current()
    except Exception:  # noqa: BLE001 - learning is advisory to the verification it rides on
        return current()


# ---- overrides and the resolved profile ------------------------------------------------------


def _host_settings(cfg):
    if cfg is not None:
        return cfg if isinstance(cfg, dict) else {}
    try:
        return _config().load_host()
    except Exception:  # noqa: BLE001 - no host config: no host overrides
        return {}


def _section(settings, name: str) -> dict:
    value = settings.get(name) if isinstance(settings, dict) else None
    return value if isinstance(value, dict) else {}


def host_overrides(cfg=None) -> list[trust_profiles.Override]:
    """The host-local per-knob overrides in ``host.yaml`` (unsigned, so they can only tighten)."""
    settings = _host_settings(cfg)
    found = []
    for section, key, knob in (
        ("hq", "placement_mode", "placement"),
        ("hive", "write_policy", "write_policy"),
        ("hq", "tenure", "tenure"),
    ):
        value = _section(settings, section).get(key)
        if value is not None:
            found.append(trust_profiles.Override(knob, f"{section}.{key}", value, "host.yaml"))
    return found


def authority_overrides(
    profile: str, *, fleet_default: str, pin: str | None = None, pin_source: str = ""
) -> list[trust_profiles.Override]:
    """The authority column's overrides: the learned fleet default (``hq.default_authority_mode``)
    when it differs from the profile's column — it is learned only from operator-authorised
    reads, so it is signed — and the frame's own ``hq.authority_mode`` pin, 0.24.0's
    frame-local setting, reported as a pin."""
    column = trust_profiles.resolve(profile).authority_mode
    found = []
    if fleet_default != column:
        found.append(
            trust_profiles.Override(
                "authority", "hq.default_authority_mode", fleet_default, "fleet.yaml", signed=True
            )
        )
    if pin is not None and pin != fleet_default:
        found.append(
            trust_profiles.Override(
                "authority", "hq.authority_mode", pin, pin_source or "host.yaml", pin=True
            )
        )
    return found


def resolved(
    cfg=None,
    *,
    fleet_default: str = "signed",
    pin: str | None = None,
    pin_source: str = "",
    profile: str | None = None,
) -> trust_profiles.ResolvedProfile:
    """The profile this host runs (`profile`, else the learned one), with every override
    applied. Never raises on a bad override: it is refused and reported. `fleet_default` /
    `pin` are the authority mode facts (:func:`beadhive.hq_authority_enforce.trust_profile`
    supplies them)."""
    name = profile if profile is not None else learned()
    effective = name or trust_profiles.DEFAULT
    overrides = [
        *authority_overrides(
            effective, fleet_default=fleet_default, pin=pin, pin_source=pin_source
        ),
        *host_overrides(cfg),
    ]
    return trust_profiles.resolve(
        effective, overrides, source="fleet" if name is not None else "default"
    )


def holds_operator_key() -> bool:
    """Whether this host carries the off-frame operator binding (``$BH_HQ_OPERATOR_SETTINGS``):
    the operator key never moves onto a frame, so a frame never does."""
    return bool(os.environ.get(OPERATOR_SETTINGS_ENV))


def hq_write_access(cfg=None) -> bool:
    """Whether this host can write HQ's authority rows: the operator binding, an
    ``hq.sql.authority_writer`` in its own config, or a Git HQ checkout it pushes."""
    if holds_operator_key():
        return True
    hq = _section(_host_settings(cfg), "hq")
    if _section(hq, "sql").get("authority_writer"):
        return True
    if hq.get("mode", "git") == "git":
        try:
            return _config().hq_dir().is_dir()
        except Exception:  # noqa: BLE001 - no HQ checkout: no write access
            return False
    return False


def capability_policy(profile: trust_profiles.ResolvedProfile, cfg=None) -> dict:
    """What frame eligibility needs from `profile`: the capability ceiling, the capabilities
    granted regardless of roles, and the extra predicates per capability."""
    operator = holds_operator_key()
    return {
        "ceiling": profile.ceiling(operator_key=operator, director=operator),
        "grants": profile.grants(hq_write_access=profile.lab_posture and hq_write_access(cfg)),
        "capability_adds": profile.capability_adds(),
    }


# ---- doctor ----------------------------------------------------------------------------------

LAB_POSTURE = (
    "open trust profile: the single-operator lab posture — nothing is signed and any host "
    "with HQ write access holds admin"
)


def doctor_data(profile: trust_profiles.ResolvedProfile) -> dict:
    """The doctor section: the effective profile and every override on one line, the lab
    posture under ``open``, and each refused override."""
    notes = [outcome.message for outcome in profile.refusals]
    if profile.lab_posture:
        notes.insert(0, LAB_POSTURE)
    return {
        "level": "warn" if profile.refusals else "ok",
        "line": profile.doctor_line(),
        "notes": notes,
        "profile": profile.as_dict(),
    }
