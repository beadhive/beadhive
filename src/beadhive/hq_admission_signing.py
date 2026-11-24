"""The signed admission hand-off of the ``hq.route: api`` join (bh-j7pdz).

hq-service v0.1.4 (bh-hq-gr9) never replaces an operator-signed authority with an unsigned one:
over a signed authority ``POST /admissions/{frame}`` refuses the unsigned admission
(``operator_signature_required``) and instead plans it (``"plan": true``) and applies it with
the plan's ``issued_at`` / ``config_head`` / ``expires_at`` and the operator's signature over
the plan's ``signing_input``.

The route is frame-bound (Gateway v0.1.1: the ``lifecycle:admission`` scope needs a frame
principal, and ``hqOwnFrame`` binds the path to the caller's own frame), so the operator can
neither plan nor apply it, and the frame never holds the operator key. The signature therefore
travels as a file, and only the signing happens off the frame:

1. the frame plans its own admission (:func:`write_plan`, from
   :func:`beadhive.hq_open_admission.join`) into a hand-off file (default
   ``$BH_HOME/state/hq-admission-<host_id>.json``);
2. the operator reviews and signs it (:func:`sign`, ``bh hq authority sign-admission --record
   <file> --operator-key <key> --confirm``) with no call to the frame's route: it reads the
   current authority and checks that the plan changes nothing but this frame's grant;
3. the frame applies it (:func:`apply_body`, ``bh hq authority join --confirm`` again) inside
   the plan window (15 minutes from the plan's ``issued_at``).

The file carries no secret: the plan, the frame's public key and, once signed, the signature.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from pathlib import Path

from .hq_control_plane import ControlPlaneError

FORMAT = "beadhive/admission-signing/v1"

#: The plan fields the hand-off keeps (hq-service ``planOutcome``).
PLAN_FIELDS = (
    "revision",
    "config_head",
    "record",
    "signing_input",
    "signing_sha256",
    "issued_at",
    "expires_at",
    "plan_expires_at",
    "signature_needed",
    "summary",
)

#: The authority state keys an admission may change besides the admitted frame's entry
#: (hq-service ``prepareAdmission``: the revision, the issue and expiry times and the state
#: domain, which moves to v2 once a record names a beadyard).
STAMPED = frozenset({"revision", "issued_at", "expires_at", "domain"})


def now() -> float:
    """The clock both seats judge a plan's window by (a test replaces it)."""
    return time.time()


class AdmissionSigningError(ControlPlaneError):
    """The admission hand-off is unusable: absent, malformed, for another frame, stale,
    expired, or not what the operator may sign."""


def default_path(host_id: str, state_dir: Path) -> Path:
    """``<state_dir>/hq-admission-<host_id>.json``; the CLI passes ``$BH_HOME/state``."""
    return Path(state_dir) / f"hq-admission-{host_id}.json"


def sign_command(path: Path | str) -> str:
    """The operator command that signs the hand-off at *path* (on the operator's copy)."""
    return f"bh hq authority sign-admission --record {path} --operator-key <key> --confirm"


def _write(path: Path, handoff: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(handoff, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_plan(
    path: Path, *, host_id: str, frame_id: str, public_key: str, request: dict, plan: dict
) -> dict:
    """Record the frame's admission plan at *path* for the operator to sign; returns it."""
    from .hq_sql_signatures import fingerprint

    handoff = {
        "format": FORMAT,
        "host_id": host_id,
        "frame_id": frame_id,
        "public_key": public_key.strip(),
        "key_fingerprint": fingerprint(public_key),
        "request": dict(request),
        "plan": {field: plan.get(field) for field in PLAN_FIELDS},
    }
    _write(Path(path), handoff)
    return handoff


def load(path: Path | str) -> dict | None:
    """The hand-off at *path*, or ``None`` when there is none."""
    path = Path(path)
    try:
        handoff = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise AdmissionSigningError(f"admission plan {path} is unreadable: {exc}") from None
    plan = handoff.get("plan") if isinstance(handoff, dict) else None
    if (
        not isinstance(handoff, dict)
        or handoff.get("format") != FORMAT
        or not isinstance(plan, dict)
        or not isinstance(plan.get("record"), dict)
        or not isinstance(handoff.get("request"), dict)
        or not all(isinstance(handoff.get(k), str) for k in ("host_id", "frame_id", "public_key"))
    ):
        raise AdmissionSigningError(f"{path} is not a {FORMAT} admission plan")
    return handoff


def discard(path: Path | str) -> None:
    Path(path).unlink(missing_ok=True)


def matches(handoff: dict, *, host_id: str, frame_id: str, public_key: str) -> bool:
    """Whether *handoff* is this frame's admission with this runtime key."""
    return (
        handoff.get("host_id") == host_id
        and handoff.get("frame_id") == frame_id
        and handoff.get("public_key") == public_key.strip()
    )


def expired(handoff: dict, now: float) -> bool:
    """Whether the plan's window (hq-service: 15 minutes from ``issued_at``) has passed."""
    try:
        return now > float(handoff["plan"]["plan_expires_at"])
    except (KeyError, TypeError, ValueError):
        return True


def apply_body(handoff: dict) -> dict:
    """The planned apply: the admission request plus the plan's inputs and the signature."""
    plan = handoff["plan"]
    return {
        **handoff["request"],
        "issued_at": plan["issued_at"],
        "config_head": plan["config_head"],
        "expires_at": plan["expires_at"],
        "record": plan["record"],
        "signature": handoff["signature"],
    }


def facts(handoff: dict, path: Path | str) -> dict:
    """What a frame and an operator both see of a hand-off."""
    plan = handoff["plan"]
    summary = plan.get("summary") if isinstance(plan.get("summary"), dict) else {}
    state = plan["record"].get("state") if isinstance(plan["record"].get("state"), dict) else {}
    return {
        "admission_plan": str(path),
        "frame_id": handoff["frame_id"],
        "host_id": handoff["host_id"],
        "key_fingerprint": handoff.get("key_fingerprint"),
        "principal": summary.get("principal"),
        "epoch": summary.get("epoch"),
        "revision": plan.get("revision"),
        "next_revision": state.get("revision"),
        "config_head": plan["record"].get("config_head"),
        "issued_at": plan.get("issued_at"),
        "expires_at": plan.get("expires_at"),
        "plan_expires_at": plan.get("plan_expires_at"),
        "signing_sha256": plan.get("signing_sha256"),
        "signed": bool(handoff.get("signature")),
    }


def review(handoff: dict, *, head: str, state: dict, now: float) -> None:
    """Refuse a hand-off the operator must not sign.

    The record must be the plan's signing input, the admission of the hand-off's own frame,
    host and runtime key, made at the current authority ``head`` and inside its window, and it
    may change nothing of ``state`` (the current authority) but that frame's entry and the
    stamped fields (:data:`STAMPED`). The service rebuilds the record and verifies the
    signature over its own bytes, so this check guards the operator's signature itself: it is
    never given to a record that grants more than this one frame."""
    from .hq_sql_signatures import AUTHORITY_DOMAIN, SqlSignatureError, canonical, fingerprint

    plan, frame = handoff["plan"], handoff["frame_id"]
    host_id, key = handoff["host_id"], handoff["public_key"]
    record = plan["record"]
    try:
        expected = base64.b64encode(AUTHORITY_DOMAIN + canonical(record)).decode()
    except SqlSignatureError:
        expected = None
    if plan.get("signing_input") != expected:
        raise AdmissionSigningError("the plan's signing_input is not its record's; plan again")
    request = handoff["request"]
    if (
        request.get("kind") != "open"
        or request.get("host_id") != host_id
        or str(request.get("public_key", "")).strip() != key
        or request.get("expected_revision") != plan.get("revision")
    ):
        raise AdmissionSigningError("the admission request is not the hand-off's frame and key")
    new = record.get("state") if isinstance(record.get("state"), dict) else {}
    active = ((new.get("frames") or {}).get(frame) or {}).get("active") or {}
    authority = active.get("authority") if isinstance(active.get("authority"), dict) else {}
    if (
        authority.get("frame_id") != frame
        or authority.get("holder_identity") != host_id
        or authority.get("key_fingerprint") != fingerprint(key)
        or str(active.get("public_key", "")).strip() != key
        or active.get("state") != "active"
    ):
        raise AdmissionSigningError(
            f"the plan does not admit {host_id} to {frame} with the hand-off's key"
        )
    if plan.get("revision") != head:
        raise AdmissionSigningError(
            f"the authority moved since the frame planned (head {head}, plan "
            f"{plan.get('revision')}): the frame plans again with `bh hq authority join --confirm`"
        )
    if expired(handoff, now):
        raise AdmissionSigningError(
            "the plan's window has passed: the frame plans again with "
            "`bh hq authority join --confirm`"
        )
    if new.get("revision") != (state.get("revision") or 0) + 1:
        raise AdmissionSigningError("the plan is not the next authority revision")
    before, after = dict(state.get("frames") or {}), dict(new.get("frames") or {})
    before.pop(frame, None)
    after.pop(frame, None)
    if before != after:
        raise AdmissionSigningError(f"the plan changes frames other than {frame}")
    changed = sorted(
        key for key in {*state, *new} - STAMPED - {"frames"} if state.get(key) != new.get(key)
    )
    if changed:
        raise AdmissionSigningError(f"the plan changes the authority's {', '.join(changed)}")


def sign(path, *, authority, operator_key="", confirm=False, frame="", clock=None) -> dict:
    """``bh hq authority sign-admission --record <path>``: review the frame's admission plan
    against the current authority and, with ``confirm`` and an operator key, sign it in place.
    Without ``confirm`` nothing is written (the review only). ``authority`` reads the current
    ``(head, state)`` with the operator's binding
    (:func:`beadhive.hq_open_admission.operator_state`)."""
    from .hq_sql_signatures import SqlSignatureError, sign_authority

    if path is None:
        raise AdmissionSigningError(
            "sign-admission requires --record <the frame's admission plan file>"
        )
    handoff = load(path)
    if handoff is None:
        raise AdmissionSigningError(f"no admission plan at {path}")
    if frame and frame not in (handoff["host_id"], handoff["frame_id"]):
        raise AdmissionSigningError(
            f"{path} is the admission of {handoff['host_id']} ({handoff['frame_id']}), "
            f"not --frame {frame}"
        )
    head, state = authority()
    review(handoff, head=head, state=state or {}, now=(clock or now)())
    if not confirm:
        return {**facts(handoff, path), "plan": True}
    if not operator_key:
        raise AdmissionSigningError("sign-admission requires --operator-key")
    try:
        handoff["signature"] = sign_authority(
            handoff["plan"]["record"], signing_key=str(operator_key)
        )
    except SqlSignatureError as exc:
        raise AdmissionSigningError(str(exc)) from None
    _write(Path(path), handoff)
    return {
        **facts(handoff, path),
        "plan": False,
        "next": "copy the file back to the frame and run `bh hq authority join --confirm` "
        "there before plan_expires_at",
    }
