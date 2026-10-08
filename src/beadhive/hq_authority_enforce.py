"""HQ authority mode resolver (bh-dzb8m) and its verification policy (bh-mk97e).

``signed`` (default) verifies operator authority: signature, expiry, config-head binding.
``trusted`` (frame-local opt-in) skips exactly those three but still honours the content — see
:func:`trusted`. Unsigned records use the one marker shape documented at
:data:`UNSIGNED_SIGNATURE`.

Deprecated: the UNSUPPORTED, dev-only per-host switch for HQ runtime-authority enforcement
(bh-6pqul), now resolved as ``trusted`` plus the 0.23 content waiver (:func:`content_waived`).

``BH_HQ_AUTHORITY_ENFORCE=false`` makes this process skip HQ runtime-authority enforcement:
authority expiry, the config-revision binding, and the authority-derived frame eligibility
predicates (admission state, cordon, emergency review, desired release/caps/profile). Identity
checks, the heartbeat (which follows ``BH_FRAME_HEARTBEAT``), hive-lease ownership and
validation are unchanged. The frame becomes self-asserted — see docs/HQ.md for the trust delta.

The setting is an environment variable on purpose, following ``BH_FRAME_HEARTBEAT`` and
``BH_HQ_SQL_LIVENESS``: the strict host schema rejects unknown keys, and pre-0.22.x readers
(the 0.21.3 heartbeat sender) share the HOST file, so a ``host.yaml`` key would break them. A
fleet key would itself need an authority-bound publish. Unset, empty or ``true`` keeps
enforcement on; any other value is an error, never a fallback.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from contextlib import contextmanager
from typing import NamedTuple

ENFORCE_ENV = "BH_HQ_AUTHORITY_ENFORCE"
ENFORCE_VALUES = ("true", "false")
DOCS = "docs/HQ.md#unsupported-disabling-hq-authority-enforcement"
BANNER = (
    "⚠ HQ authority enforcement DISABLED on this host — unsupported, dev/prototype only "
    f"({ENFORCE_ENV}=false; re-enable before admitting new executors, see {DOCS})"
)

_banner_emitted = False


class AuthorityEnforcementError(ValueError):
    """``BH_HQ_AUTHORITY_ENFORCE`` holds a value other than ``true`` / ``false``."""


MODE_ENV = "BH_HQ_AUTHORITY_MODE"
MODES = ("signed", "trusted", "inherit")
DEFAULT_MODE = "inherit"
MODE_DOCS = "docs/CONFIGURATION.md"
TRUSTED_BANNER = (
    "⚠ HQ authority mode is TRUSTED on this frame — authority, heartbeat and config are not "
    f"signature-verified ({MODE_ENV} / hq.authority_mode; see {MODE_DOCS})"
)
DEPRECATION = (
    f"{ENFORCE_ENV} is deprecated; use {MODE_ENV}=trusted or hq.authority_mode: trusted "
    f"({MODE_DOCS})"
)

_deprecation_emitted = False
_invalid_emitted: set[str] = set()


class Resolution(NamedTuple):
    """The resolved authority mode and where it came from."""

    mode: str  # "signed" | "trusted" (never "inherit")
    source: str  # "env" | "enforce-env" | "host" | "default" | "invalid"


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def resolve(configured: str | None = None) -> Resolution:
    """The ONE resolver: ``$BH_HQ_AUTHORITY_MODE`` > deprecated ``BH_HQ_AUTHORITY_ENFORCE`` >
    *configured* (the frame-local ``hq.authority_mode`` from host config, or the operator
    settings file) > ``inherit``.

    ``inherit`` (or nothing set) follows the fleet default this host has LEARNED from an
    operator-authorised read (:func:`fleet_default`, bh-taa04.3): ``trusted`` resolves with
    source ``"fleet"``, anything else is ``signed``. An explicit ``signed``/``trusted`` pin
    always wins. An invalid mode value fails closed to ``signed`` with one error line.
    """
    global _deprecation_emitted
    raw = os.environ.get(MODE_ENV, "").strip()
    source = "env"
    if not raw:
        legacy = os.environ.get(ENFORCE_ENV, "").strip() or "true"
        if legacy not in ENFORCE_VALUES:
            raise AuthorityEnforcementError(
                f"{ENFORCE_ENV} must be 'true' or 'false' (got {legacy[:32]!r})"
            )
        if legacy == "false":
            if not _deprecation_emitted:
                _deprecation_emitted = True
                _warn(f"warning: {DEPRECATION}")
            return Resolution("trusted", "enforce-env")
        raw = (configured or "").strip()
        source = "host"
    if not raw or raw == "inherit":
        if fleet_default() == "trusted":
            return Resolution("trusted", "fleet")
        return Resolution("signed", "default" if not raw else source)
    if raw not in MODES:
        if raw not in _invalid_emitted:
            _invalid_emitted.add(raw)
            where = MODE_ENV if source == "env" else "hq.authority_mode"
            _warn(
                f"error: {where} must be one of {'|'.join(MODES)} (got {raw[:32]!r}); "
                "failing closed to signed"
            )
        return Resolution("signed", "invalid")
    return Resolution(raw, source)


_configured_cache: tuple[tuple, str | None] | None = None


def _mtime(path) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except (OSError, TypeError, ValueError):
        return None


_path_cache: tuple[tuple, str | None] | None = None


def _host_config_path(config) -> str | None:
    """This host's config path, memoized on the environment and the path resolvers in force:
    resolving it is milliseconds, and verification consults the mode on every read."""
    global _path_cache
    key = (frozenset(os.environ.items()), config.config_path, getattr(config, "home", None))
    if _path_cache is not None and _path_cache[0] == key:
        return _path_cache[1]
    try:
        path = str(config.config_path())
    except Exception:  # noqa: BLE001 - an unknown path only disables reuse
        path = None
    _path_cache = (key, path)
    return path


#: The operator CLI's ``--operator-settings`` file, in force for one command (bh-l4q0s); it
#: outranks ``$BH_HQ_OPERATOR_SETTINGS`` exactly as it does for plane selection.
_settings_override: str | None = None


@contextmanager
def operator_settings(path):
    """Resolve ``hq.authority_mode`` from the operator settings file *path* (``None``: no
    override) for the duration of one operator command."""
    global _settings_override
    previous = _settings_override
    if path:
        _settings_override = str(path)
    try:
        yield
    finally:
        _settings_override = previous


def _settings_path() -> str:
    return _settings_override or os.environ.get("BH_HQ_OPERATOR_SETTINGS", "")


def _configured_key(config) -> tuple:
    """What `_configured_mode` depends on: the operator settings file or this host's config
    file (path + mtime) and the reader in force (a monkeypatched ``load_host`` is a new key)."""
    settings = _settings_path()
    if settings:
        return ("operator", settings, _mtime(settings))
    path = _host_config_path(config)
    return ("host", config.load_host, path, _mtime(path))


def reset_cache() -> None:
    """Forget the per-process ``hq.authority_mode`` and fleet-default reads (tests and config
    writers)."""
    global _configured_cache, _path_cache, _fleet_cache, _fleet_path_cache
    _configured_cache = _path_cache = _fleet_cache = _fleet_path_cache = None
    _committed_defaults.clear()


def _configured_mode() -> str | None:
    """Frame-local ``hq.authority_mode``: the operator settings file when one is named, else
    this host's config. Unreadable config never changes the default.

    Memoized per process on :func:`_configured_key` (env + file path + mtime): verification
    hot paths consult the mode on every read. :func:`reset_cache` forgets it."""
    global _configured_cache
    try:
        # Resolved late on purpose: config's store already reaches this module, so a static
        # import would close an import cycle.
        config = importlib.import_module("beadhive.config")
        key = _configured_key(config)
    except Exception:  # noqa: BLE001 - selection must stay fail-closed to signed
        return None
    if _configured_cache is not None and _configured_cache[0] == key:
        return _configured_cache[1]
    try:
        if settings := _settings_path():
            from pathlib import Path

            from ruamel.yaml import YAML

            raw = YAML(typ="safe").load(Path(settings).read_text())
            hq = raw.get("hq", raw) if isinstance(raw, dict) else None
            value = hq.get("authority_mode") if isinstance(hq, dict) else None
            value = None if value is None else str(value)
        else:
            value = (config.load_host().get("hq") or {}).get("authority_mode")
    except Exception:  # noqa: BLE001 - selection must stay fail-closed to signed
        return None
    _configured_cache = (key, value)
    return value


def mode() -> str:
    """``"signed"`` or ``"trusted"`` for this process."""
    return resolve(_configured_mode()).mode


def enforced() -> bool:
    """True when this process verifies operator authority (``signed``): the operator
    signature, authority/policy expiry and the config-head binding. False in ``trusted``."""
    return mode() == "signed"


def trusted() -> bool:
    """``trusted``: skip the operator signature, expiry and config-head binding, but still
    read and HONOUR the content (admitted frames, cordon/drain/retire, desired release, caps,
    beadyard identity), the identity comparisons and the replay floor (revision monotonic).
    HQ write access is admin on such a frame."""
    return not enforced()


#: Trusted mode has no authority expiry; a heartbeat authority snapshot still needs a finite
#: ``valid_until`` > ``checked_at``, so trusted snapshots look this far ahead (re-read per use).
TRUSTED_SNAPSHOT_HORIZON_S = 86400


def snapshot_validity(now: float, expires_at: float, candidate_expires_at: float | None) -> float:
    """``valid_until`` for a heartbeat :class:`AuthoritySnapshot` (Git and SQL planes).

    Signed: the earlier of the authority expiry and the candidate window (unchanged). Trusted:
    the authority expiry is ignored; the candidate window (operator content: how long a pending
    incarnation may stay unadmitted) still bounds it."""
    if enforced():
        return min(expires_at, candidate_expires_at or expires_at)
    if candidate_expires_at is not None:
        return candidate_expires_at
    return max(expires_at, now + TRUSTED_SNAPSHOT_HORIZON_S)


# ---- fleet default authority mode (bh-taa04.3) ------------------------------------------------
#
# Carrier: the fleet-config key ``hq.default_authority_mode`` (``signed`` | ``trusted``,
# default ``signed``) in the committed ``fleet.yaml``. It is in the frame-relevant set of
# :func:`beadhive.hq_hive_policy.config_head_tolerated`, so on SQL HQ a change fences signed
# frames until the operator re-signs authority against the new head (``rebind``), and on Git HQ
# the config carrier commit is itself operator-signed. A signed or inherit frame therefore only
# ever LEARNS ``trusted`` from a config snapshot whose operator signature/binding it verified
# (``authorised``); a key-less HQ writer cannot raise it. Lowering to ``signed`` is always taken.
#
# The learned value is persisted per host (:data:`FLEET_STATE`, under ``$BH_HOME/state``) so the
# resolver stays one ``stat`` per call: verification consults the mode on every read.

FLEET_KEY = "default_authority_mode"
FLEET_MODES = ("signed", "trusted")
FLEET_STATE = "hq-fleet-authority-mode.json"
FLEET_DOCUMENT = "fleet.yaml"

_fleet_cache: tuple[tuple, str] | None = None
_fleet_path_cache: tuple[tuple, str | None] | None = None
#: committed fleet.yaml content -> its fleet default (bounded memo).
_committed_defaults: dict[str, str] = {}


class AuthorityModeConflict(ValueError):
    """This frame pins ``signed`` but its fleet runs ``trusted``: it refuses to work."""


def _fleet_state_path() -> str | None:
    """``$BH_HOME/state/hq-fleet-authority-mode.json``, memoized on the home variables."""
    global _fleet_path_cache
    try:
        config = importlib.import_module("beadhive.config")
    except Exception:  # noqa: BLE001 - no config: nothing learned, signed
        return None
    key = (
        tuple(os.environ.get(name) for name in ("BH_HOME", "WS_HOME", "HOME")),
        config.home,
    )
    if _fleet_path_cache is not None and _fleet_path_cache[0] == key:
        return _fleet_path_cache[1]
    try:
        path = str(config.home() / "state" / FLEET_STATE)
    except Exception:  # noqa: BLE001 - no home: nothing learned, signed
        path = None
    _fleet_path_cache = (key, path)
    return path


def fleet_state() -> dict:
    """The learned fleet-default record (``{}`` when nothing has been learned)."""
    path = _fleet_state_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def fleet_default() -> str:
    """The fleet default this host has learned: ``"trusted"`` or ``"signed"`` (nothing learned,
    unreadable or invalid state is ``signed``). Memoized on the state file's mtime."""
    global _fleet_cache
    path = _fleet_state_path()
    key = (path, _mtime(path))
    if _fleet_cache is not None and _fleet_cache[0] == key:
        return _fleet_cache[1]
    value = fleet_state().get("mode") if key[1] is not None else None
    value = value if value in FLEET_MODES else "signed"
    _fleet_cache = (key, value)
    return value


def fleet_trusted() -> bool:
    """Whether this host has learned that its fleet runs ``trusted`` (open admission)."""
    return fleet_default() == "trusted"


def committed_fleet_default(snapshot) -> str:
    """``hq.default_authority_mode`` committed in a config snapshot (or a document sequence):
    ``"trusted"`` only when explicitly so; absent, invalid or unreadable is ``"signed"``.
    Memoized on the ``fleet.yaml`` content (verification reads it on every authority read)."""
    try:
        documents = getattr(snapshot, "documents", snapshot)
        content = next(
            (document.content for document in documents if document.path == FLEET_DOCUMENT),
            None,
        )
    except Exception:  # noqa: BLE001 - an unreadable default never raises trust
        return "signed"
    if not isinstance(content, str):
        return "signed"
    if content in _committed_defaults:
        return _committed_defaults[content]
    try:
        from ruamel.yaml import YAML

        parsed = YAML(typ="safe").load(content)
        hq = parsed.get("hq") if isinstance(parsed, dict) else None
        raw = hq.get(FLEET_KEY) if isinstance(hq, dict) else None
        value = "trusted" if raw == "trusted" else "signed"
    except Exception:  # noqa: BLE001 - an unreadable default never raises trust
        value = "signed"
    if len(_committed_defaults) >= 16:
        _committed_defaults.pop(next(iter(_committed_defaults)))
    _committed_defaults[content] = value
    return value


def record_fleet_default(value: str, *, head: str = "", via: str = "") -> bool:
    """Persist the learned fleet default; returns whether it changed. Never raises."""
    global _fleet_cache
    if value not in FLEET_MODES or value == fleet_default():
        return False
    path = _fleet_state_path()
    if not path:
        return False
    body = json.dumps(
        {"mode": value, "config_head": head, "via": via, "learned_at": time.time()},
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
    _fleet_cache = None
    return True


def observe_fleet_default(snapshot, *, authorised: bool, via: str = "") -> str:
    """Learn the fleet default from a config snapshot this process just read.

    *authorised*: the snapshot is the operator-signed/bound one (verified in signed mode). A
    ``trusted`` value is honoured only then (or when already learned); a ``signed`` value is
    always honoured — lowering is stricter. Returns the learned default; never raises."""
    try:
        value = committed_fleet_default(snapshot)
        current = fleet_default()
        if value == current or (value == "trusted" and not authorised):
            return current
        record_fleet_default(value, head=str(getattr(snapshot, "commit_revision", "")), via=via)
        return fleet_default()
    except Exception:  # noqa: BLE001 - learning is advisory to the verification it rides on
        return "signed"


def pinned() -> str | None:
    """This process's EXPLICIT mode pin (``signed`` / ``trusted``), or ``None`` for inherit."""
    raw = os.environ.get(MODE_ENV, "").strip()
    if not raw:
        if os.environ.get(ENFORCE_ENV, "").strip() == "false":
            return "trusted"
        raw = (_configured_mode() or "").strip()
    return raw if raw in FLEET_MODES else None


def conflict_message() -> str:
    """The actionable refusal for a frame pinned ``signed`` in a ``trusted`` fleet."""
    return (
        "this frame pins hq.authority_mode: signed but its fleet default is trusted (bh hq "
        "authority mode-trusted): it refuses to work in a trusted fleet. Either unpin it (set "
        f"hq.authority_mode: inherit, or unset {MODE_ENV}) to follow the fleet, or have the "
        "operator return the fleet to signed (bh hq authority mode-signed --operator-key "
        "<key> --confirm)"
    )


def require_fleet_compatible() -> None:
    """Refuse fail-closed when this frame pins ``signed`` in a ``trusted`` fleet."""
    if fleet_trusted() and pinned() == "signed":
        raise AuthorityModeConflict(conflict_message())


def unsigned_refusal(carrier: str) -> str:
    """The message for an unsigned record read in signed mode, naming a pin conflict first."""
    if fleet_trusted() and pinned() == "signed":
        return conflict_message()
    return unsigned_rejection(carrier)


def discoverable() -> bool:
    """Whether an unsigned record may trigger fleet-default discovery: this process resolves
    signed only because it inherits (no explicit pin) or pins signed (to name the conflict)."""
    return enforced() and not fleet_trusted()


def content_waived() -> bool:
    """Deprecated ``BH_HQ_AUTHORITY_ENFORCE=false`` only: trusted verification PLUS the 0.23
    content waiver (admission, cordon, emergency review, desired release/caps/profile are
    satisfied-with-warning), kept unchanged for the deprecated switch until its removal
    (bh-ihckx). ``hq.authority_mode: trusted`` / ``BH_HQ_AUTHORITY_MODE=trusted`` never waive
    content."""
    return resolve(_configured_mode()).source == "enforce-env"


# ---- unsigned records (bh-mk97e) --------------------------------------------------------------
#
# The ONE unsigned-record marker shape, written by key-less trusted publication (bh-l4q0s) and
# accepted only by a trusted frame; a signed frame rejects it fail-closed with
# :func:`unsigned_rejection`. Trusted frames also accept signed records (and do not check them).
#
# * SQL carriers: the ``hq_authority.operator_signature`` column (``BLOB NOT NULL``, so no
#   NULL) holds exactly the ASCII bytes of :data:`UNSIGNED_SIGNATURE` instead of a base64
#   Ed25519 signature. ``:`` is outside the base64 alphabet, so the marker never collides with a
#   real signature.
# * Git carriers (``authority.json`` / ``config.json`` commits): an ordinary commit with NO
#   ``gpgsig`` header whose message ends with the trailer line :data:`UNSIGNED_TRAILER`. The
#   payload documents are byte-identical to a signed publication (same key set, same finite
#   ``expires_at``); only the signature is absent. Detection keys on the missing ``gpgsig``
#   header; the trailer is the writer's explicit intent marker for audit.
UNSIGNED_SIGNATURE = "unsigned:trusted"
UNSIGNED_TRAILER = "Bh-Authority-Signature: unsigned (trusted)"


def unsigned_signature(value) -> bool:
    """Whether a SQL signature column value is the :data:`UNSIGNED_SIGNATURE` marker."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    return isinstance(value, str) and value.strip() == UNSIGNED_SIGNATURE


def unsigned_commit(header: str) -> bool:
    """Whether a Git commit header (``git cat-file -p`` up to the blank line) carries no
    signature — the Git unsigned-record shape."""
    return not any(line.startswith("gpgsig") for line in header.split("\n"))


def unsigned_rejection(carrier: str) -> str:
    """The actionable fail-closed message for an unsigned record read in ``signed`` mode."""
    return (
        f"{carrier} is an UNSIGNED record (published key-less by a trusted-mode operator) but "
        f"this host's authority mode is signed: either set hq.authority_mode: trusted (or "
        f"{MODE_ENV}=trusted) on this frame, or have the operator republish with "
        "--operator-key"
    )


# ---- key-less publication (bh-l4q0s) ----------------------------------------------------------
#
# Trust delta: with ``hq.authority_mode: trusted`` in the operator's settings (or
# ``BH_HQ_AUTHORITY_MODE=trusted``), operator mutations publish without an operator key, as the
# unsigned marker above. Only trusted frames accept such a record. A supplied key still signs,
# which is how a fleet moves back to signed (rebind with the key). Outside trusted mode a
# key-less mutation refuses exactly as before.


def key_required(operator_key) -> bool:
    """Whether a key-less operator mutation must refuse: no key and this process is signed."""
    return not operator_key and enforced()


def commit_tree_args(tree: str, parent: str, operator_key, message: str) -> tuple[list, str]:
    """``git commit-tree`` arguments and message for a Git carrier commit: SSH-signed with the
    operator key, or — key-less, trusted only — the unsigned shape (no ``gpgsig`` header plus
    :data:`UNSIGNED_TRAILER`)."""
    if operator_key:
        args = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={operator_key}"]
        args += ["commit-tree", "-S", tree]
    else:
        args = ["commit-tree", "--no-gpg-sign", tree]
        message = f"{message.rstrip(chr(10))}\n\n{UNSIGNED_TRAILER}\n"
    if parent:
        args += ["-p", parent]
    return args, message


def status() -> str:
    """``"enabled"`` or ``"disabled"`` — the value surfaced by status/doctor JSON."""
    return "enabled" if enforced() else "disabled"


def emit_banner(stream=None) -> bool:
    """Print the trusted banner once per process; return whether enforcement is disabled."""
    global _banner_emitted
    resolved = resolve(_configured_mode())
    if resolved.mode == "signed":
        return False
    if not _banner_emitted:
        _banner_emitted = True
        text = BANNER if resolved.source == "enforce-env" else TRUSTED_BANNER
        print(text, file=stream if stream is not None else sys.stderr)
    return True


def doctor_warnings() -> list[str]:
    """Doctor WARN lines for this process's authority mode (never raises)."""
    try:
        resolved = resolve(_configured_mode())
    except AuthorityEnforcementError as exc:
        return [f"hq: {exc} — HQ authority enforcement setting is invalid"]
    if resolved.mode == "signed":
        try:
            require_fleet_compatible()
        except AuthorityModeConflict as exc:
            return [f"hq: {exc}"]
        return []
    if resolved.source == "enforce-env":
        return [
            f"hq: HQ runtime-authority enforcement is DISABLED on this host ({ENFORCE_ENV}=false)"
            " — UNSUPPORTED, dev/prototype only; frames here are self-asserted (expiry, config "
            "binding, cordon, release pins and caps are not enforced). Unset it before admitting "
            f"new executors ({DOCS})"
        ]
    source = "fleet default" if resolved.source == "fleet" else resolved.source
    return [
        f"hq: authority mode is TRUSTED ({source}) — the operator signature, authority "
        "expiry and config-head binding are not verified here; HQ write access is admin "
        f"(cordon, release pins and caps are still honoured) ({MODE_DOCS})"
    ]
