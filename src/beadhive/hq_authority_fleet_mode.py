"""``bh hq authority mode | mode-signed | mode-trusted`` and ``bh hq authority join`` (bh-taa04.3).

The fleet default authority mode is the fleet-config key ``hq.default_authority_mode`` in the
committed ``fleet.yaml`` (see :mod:`beadhive.hq_authority_enforce` for why that carrier and how
frames learn it). Frames whose ``hq.authority_mode`` is ``inherit`` (the default) follow it.

* Raising to ``trusted`` needs the operator key ONCE: the config publication is followed by a
  signed ``rebind`` on SQL HQ (so signed frames verify the new head and learn the default), and
  the Git config carrier commit is itself operator-signed. Afterwards the operator works
  key-less, frames accept unsigned records, and registering frames join by open admission.
* Lowering to ``signed`` is always allowed (stricter) and removes the key from ``fleet.yaml``.
  Without the key the frames that followed the fleet fall back to signed and fence on the
  unsigned authority until the operator re-signs it (``bh hq authority rebind --operator-key``).
"""

from __future__ import annotations

import time
from io import StringIO
from pathlib import Path

from . import hq_authority_ceiling, hq_operator_settings
from . import hq_authority_enforce as authority_mode
from .hq_control_plane import ControlPlaneError
from .modules.config.domain.ports import FleetConfigDocument


def _sql(plane) -> bool:
    return hasattr(plane, "_runtime_authority")


def with_fleet_default(documents, value: str) -> tuple:
    """`documents` with ``fleet.yaml``'s ``hq.default_authority_mode`` set to `value`
    (``signed`` removes the key, so a fleet back on signed is readable by 0.23.x again)."""
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    if value not in authority_mode.FLEET_MODES:
        raise ControlPlaneError(f"fleet authority mode must be signed|trusted (got {value!r})")
    result, found = [], False
    for document in documents:
        if document.path != authority_mode.FLEET_DOCUMENT:
            result.append(document)
            continue
        found = True
        yaml = YAML()
        yaml.preserve_quotes = True
        data = yaml.load(document.content) or CommentedMap()
        hq = data.get("hq")
        if hq is None:
            hq = data["hq"] = CommentedMap()
        if value == "signed":
            hq.pop(authority_mode.FLEET_KEY, None)
        else:
            hq[authority_mode.FLEET_KEY] = value
        buffer = StringIO()
        yaml.dump(data, buffer)
        result.append(FleetConfigDocument(document.path, buffer.getvalue()))
    if not found:
        raise ControlPlaneError("committed fleet.yaml unavailable")
    return tuple(result)


def refresh(plane) -> None:
    """Best-effort re-learn of the fleet default through this host's normal verified read."""
    try:
        if not _sql(plane):
            plane.config_store().load_snapshot()  # the carrier read observes the default
        elif plane.settings.get("runtime") is not None:
            timeout = plane.settings["runtime"]["operation_timeout"]
            plane._runtime_authority().read_frame_composite(deadline=time.monotonic() + timeout)
        elif plane.settings.get("authority_writer") is not None:
            _head, state, crossref, policies = plane._operator().load(
                deadline=plane._operator_deadline()
            )
            bound, _latest, snapshot = plane.config_store().authority_binding(
                crossref, policies, expires_at=state["expires_at"]
            )
            if bound and snapshot is not None:
                authority_mode.observe_fleet_default(
                    snapshot, authorised=authority_mode.enforced(), via="operator"
                )
            latest = plane.config_store().load_snapshot()
            authority_mode.observe_fleet_default(latest, authorised=False, via="operator")
    except Exception:  # noqa: BLE001 - reporting falls back to what was learned before
        return


def show(plane) -> dict:
    refresh(plane)
    resolved = authority_mode.resolve(authority_mode._configured_mode())
    committed = None
    try:
        committed = authority_mode.committed_fleet_default(plane.config_store().load_snapshot())
    except Exception:  # noqa: BLE001 - an unreadable head only omits the committed value
        committed = None
    return {
        "mode": resolved.mode,
        "source": resolved.source,
        "pinned": authority_mode.pinned(),
        "fleet_default": authority_mode.fleet_default(),
        "committed_fleet_default": committed,
        "learned": authority_mode.fleet_state(),
    }


def set_fleet_mode(plane, value: str, *, operator_key: str = "", ceiling=None) -> dict:
    """Publish ``hq.default_authority_mode = value`` and make it take effect (see module doc)."""
    if value not in authority_mode.FLEET_MODES:
        raise ControlPlaneError(f"fleet authority mode must be signed|trusted (got {value!r})")
    key = str(operator_key) if operator_key else ""
    if value == "trusted" and not key and not authority_mode.fleet_trusted():
        raise ControlPlaneError(
            "raising the fleet default to trusted needs the operator key once (--operator-key): "
            "signed frames honour it only from an operator-signed configuration"
        )
    if authority_mode.key_required(key):
        raise ControlPlaneError("mutation requires separate --operator-key")
    store = plane.config_store(operator_key=key or None)
    snapshot = store.load_snapshot()
    published = authority_mode.committed_fleet_default(snapshot) != value
    if published:
        snapshot = store.publish_snapshot(
            with_fleet_default(snapshot.documents, value),
            expected_revision=snapshot.commit_revision,
        )
    rebound = ""
    if _sql(plane) and key and published:
        # SQL fleet config is bound by the signed authority: re-sign it against the new head
        # (no expiry change beyond rebind's default) so signed frames verify and learn it.
        expected = plane._operator().load(deadline=plane._operator_deadline())[0]
        rebound = plane.renew(expected=expected, operator_key=key, duration=None, ceiling=ceiling)
    authority_mode.record_fleet_default(value, head=snapshot.commit_revision, via="operator")
    result = {
        "fleet_default": value,
        "config_revision": snapshot.commit_revision,
        "published": published,
        "rebound": rebound,
    }
    if value == "signed" and published and not key:
        result["note"] = (
            "frames that follow the fleet verify signatures again and fence on unsigned "
            "authority until it is re-signed: bh hq authority rebind --operator-key <key> "
            "--confirm"
        )
    elif not _sql(plane) and published:
        result["note"] = (
            "Git HQ frames learn the new default on their next configuration read "
            "(bh hq authority mode on the frame) or when they meet an unsigned record"
        )
    return result


def mode_cmd(value, *, operator_settings=None, operator_key=None, confirm=False, max_duration=None):
    plane = hq_operator_settings.select_plane(operator_settings)
    if not value:
        return show(plane)
    if not confirm:
        raise ControlPlaneError("operator mutation requires --confirm")
    try:
        ceiling = hq_authority_ceiling.resolve_ceiling(
            cli=max_duration, settings=getattr(plane, "authority_max_duration_s", None)
        )
    except ValueError as exc:
        raise ControlPlaneError(str(exc)) from None
    return set_fleet_mode(
        plane,
        value,
        operator_key=str(operator_key) if operator_key is not None else "",
        ceiling=ceiling,
    )


def _local_public_key() -> str:
    from . import host

    key = host.signing_key()
    if not key:
        raise ControlPlaneError("open admission needs this host's runtime signing key")
    public = Path(key + ".pub")
    return (public if public.exists() else Path(key)).read_text()


def join_cmd(frame, public_key, *, operator_settings=None, operator_key=None, confirm=False):
    """``bh hq authority join [--frame <host_id> --public-key <path>] --confirm``."""
    from . import host, hq_open_admission

    if not confirm:
        raise ControlPlaneError("operator mutation requires --confirm")
    if frame:
        if public_key is None:
            raise ControlPlaneError("join --frame requires the frame's --public-key")
        key_text = Path(public_key).read_text()
    else:
        frame = host.host_id()
        key_text = Path(public_key).read_text() if public_key is not None else _local_public_key()
    plane = hq_operator_settings.select_plane(operator_settings)
    return hq_open_admission.join(
        plane, frame, key_text, operator_key=str(operator_key) if operator_key else ""
    )
