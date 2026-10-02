"""Complete, domain-separated SQL FrameLease payload signatures.

The frame owns its private signing key.  The trusted receiver supplies the
operator-granted current public key; sender-provided key IDs are never trust.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .hq_framelease_contracts import HeartbeatLease

DOMAIN = b"beadhive/sql-framelease/v1\x00"
AUTHORITY_DOMAIN = b"beadhive/sql-authority/v1\x00"
HIVE_LEASE_DOMAIN = b"beadhive/sql-hive-lease/v1\x00"
REGISTRATION_DOMAIN = b"beadhive/sql-registration/v1\x00"
MAX_BYTES = 65536


class SqlSignatureError(ValueError):
    """The SQL carrier is malformed or not signed by current operator-granted identity."""


def canonical(document: dict, *, limit: int = MAX_BYTES) -> bytes:
    if not isinstance(document, dict):
        raise SqlSignatureError("signed SQL document must be an object")
    try:
        body = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError):
        raise SqlSignatureError("signed SQL document encoding invalid") from None
    if len(body) > limit:
        raise SqlSignatureError("signed SQL document exceeds size bound")
    return body


def fingerprint(public_key: str) -> str:
    """OpenSSH SHA256 fingerprint of an Ed25519 public key, without shell tools."""
    try:
        parts = public_key.strip().split()
        if len(parts) not in (2, 3) or parts[0] != "ssh-ed25519":
            raise ValueError()
        wire = base64.b64decode(parts[1], validate=True)
        key = serialization.load_ssh_public_key(" ".join(parts[:2]).encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError()
    except (ValueError, TypeError, binascii.Error):
        raise SqlSignatureError("invalid granted Ed25519 public key") from None
    return "SHA256:" + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip("=")


def _unsigned_envelope(lease) -> dict:
    if not isinstance(lease, HeartbeatLease):
        lease = HeartbeatLease.model_validate(lease)
    return {
        "apiVersion": "frame.beadhive.ai/v1alpha1",
        "kind": "FrameLease",
        "metadata": {"name": lease.frame_id},
        "spec": lease.model_dump(mode="json", exclude_none=True),
    }


def sign_heartbeat(lease, *, signing_key: str) -> dict:
    """Sign the entire canonical envelope from an explicitly supplied SSH key path."""
    if not isinstance(signing_key, str) or not Path(signing_key).is_absolute():
        raise SqlSignatureError("explicit absolute frame signing key required")
    try:
        private = serialization.load_ssh_private_key(Path(signing_key).read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError()
        public = (
            private.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        envelope = _unsigned_envelope(lease)
        if envelope["spec"]["key_id"] != fingerprint(public):
            raise SqlSignatureError("frame key does not match lease key identity")
        signature = private.sign(DOMAIN + canonical(envelope))
    except (OSError, TypeError, ValueError):
        raise SqlSignatureError("frame signing key unavailable or invalid") from None
    envelope["spec"]["signature"] = {
        "keyId": fingerprint(public),
        "algorithm": "ed25519",
        "scope": "payload",
        "value": base64.b64encode(signature).decode(),
    }
    canonical(envelope)
    return envelope


def sign_authority(record: dict, *, signing_key: str) -> str:
    """Sign exact protected authority metadata and state for operator publication."""
    if not isinstance(signing_key, str) or not Path(signing_key).is_absolute():
        raise SqlSignatureError("explicit absolute operator signing key required")
    try:
        private = serialization.load_ssh_private_key(Path(signing_key).read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError()
        return base64.b64encode(
            private.sign(AUTHORITY_DOMAIN + canonical(record, limit=4 * 1024 * 1024))
        ).decode()
    except (OSError, TypeError, ValueError):
        raise SqlSignatureError("operator signing key unavailable or invalid") from None


def verify_authority(record: dict, signature: str, *, granted_public_key: str) -> None:
    try:
        public = serialization.load_ssh_public_key(granted_public_key.encode())
        if not isinstance(public, Ed25519PublicKey):
            raise SqlSignatureError("operator key type unsupported")
        public.verify(
            base64.b64decode(signature, validate=True),
            AUTHORITY_DOMAIN + canonical(record, limit=4 * 1024 * 1024),
        )
    except SqlSignatureError:
        raise
    except (ValueError, TypeError, InvalidSignature, binascii.Error):
        raise SqlSignatureError("protected SQL authority signature invalid") from None


def sign_hive_request(request: dict, *, signing_key: str) -> dict:
    """Bind request UUID, original CAS, operation and full incarnation to one signer."""
    if not isinstance(signing_key, str) or not Path(signing_key).is_absolute():
        raise SqlSignatureError("explicit absolute frame signing key required")
    try:
        private = serialization.load_ssh_private_key(Path(signing_key).read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError()
        public = (
            private.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        if request.get("key_fingerprint") != fingerprint(public):
            raise SqlSignatureError("hive request signer differs from granted key identity")
        if request.get("domain") != HIVE_LEASE_DOMAIN.rstrip(b"\x00").decode():
            raise SqlSignatureError("wrong signed hive lease request domain")
        signature = private.sign(HIVE_LEASE_DOMAIN + canonical(request))
        envelope = {
            "request": request,
            "signature": {
                "keyId": fingerprint(public),
                "algorithm": "ed25519",
                "scope": "payload",
                "value": base64.b64encode(signature).decode(),
            },
        }
        canonical(envelope)
        return envelope
    except (OSError, TypeError, ValueError):
        raise SqlSignatureError("hive request signing key unavailable or invalid") from None


def verify_hive_request(envelope: dict, *, granted_public_key: str) -> tuple[dict, str]:
    try:
        encoded = canonical(envelope)
        if not isinstance(envelope, dict) or set(envelope) != {"request", "signature"}:
            raise SqlSignatureError("hive request envelope shape invalid")
        request, signature = envelope["request"], envelope["signature"]
        if (
            not isinstance(request, dict)
            or not isinstance(signature, dict)
            or set(signature) != {"keyId", "algorithm", "scope", "value"}
            or signature["keyId"] != fingerprint(granted_public_key)
            or signature["algorithm"] != "ed25519"
            or signature["scope"] != "payload"
            or request.get("domain") != HIVE_LEASE_DOMAIN.rstrip(b"\x00").decode()
            or request.get("key_fingerprint") != signature["keyId"]
        ):
            raise SqlSignatureError("hive request signature identity or domain mismatch")
        public = serialization.load_ssh_public_key(granted_public_key.encode())
        if not isinstance(public, Ed25519PublicKey):
            raise SqlSignatureError("hive request signer key type unsupported")
        public.verify(
            base64.b64decode(signature["value"], validate=True),
            HIVE_LEASE_DOMAIN + canonical(request),
        )
        return request, hashlib.sha256(encoded).hexdigest()
    except SqlSignatureError:
        raise
    except (KeyError, TypeError, ValueError, InvalidSignature, binascii.Error):
        raise SqlSignatureError("hive request authentication failed") from None


def sign_registration(request: dict, *, signing_key: str) -> dict:
    """Sign the complete canonical manifest plus original grant revision."""
    if not isinstance(signing_key, str) or not Path(signing_key).is_absolute():
        raise SqlSignatureError("explicit absolute frame signing key required")
    try:
        private = serialization.load_ssh_private_key(Path(signing_key).read_bytes(), password=None)
        if not isinstance(private, Ed25519PrivateKey):
            raise ValueError()
        public = (
            private.public_key()
            .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
            .decode()
        )
        if (
            request.get("key_fingerprint") != fingerprint(public)
            or request.get("domain") != REGISTRATION_DOMAIN.rstrip(b"\x00").decode()
        ):
            raise SqlSignatureError("registration identity or domain mismatch")
        result = {
            "request": request,
            "signature": {
                "keyId": fingerprint(public),
                "algorithm": "ed25519",
                "scope": "payload",
                "value": base64.b64encode(
                    private.sign(REGISTRATION_DOMAIN + canonical(request))
                ).decode(),
            },
        }
        canonical(result)
        return result
    except (OSError, TypeError, ValueError):
        raise SqlSignatureError("registration signing key unavailable or invalid") from None


def verify_registration(envelope: dict, *, granted_public_key: str) -> tuple[dict, str]:
    try:
        encoded = canonical(envelope)
        if not isinstance(envelope, dict) or set(envelope) != {"request", "signature"}:
            raise SqlSignatureError("registration envelope shape invalid")
        request, signature = envelope["request"], envelope["signature"]
        if (
            not isinstance(request, dict)
            or not isinstance(signature, dict)
            or set(signature) != {"keyId", "algorithm", "scope", "value"}
            or signature["keyId"] != fingerprint(granted_public_key)
            or signature["algorithm"] != "ed25519"
            or signature["scope"] != "payload"
            or request.get("domain") != REGISTRATION_DOMAIN.rstrip(b"\x00").decode()
            or request.get("key_fingerprint") != signature["keyId"]
        ):
            raise SqlSignatureError("registration signer or domain mismatch")
        public = serialization.load_ssh_public_key(granted_public_key.encode())
        if not isinstance(public, Ed25519PublicKey):
            raise SqlSignatureError("registration key type unsupported")
        public.verify(
            base64.b64decode(signature["value"], validate=True),
            REGISTRATION_DOMAIN + canonical(request),
        )
        return request, hashlib.sha256(encoded).hexdigest()
    except SqlSignatureError:
        raise
    except (KeyError, TypeError, ValueError, InvalidSignature, binascii.Error):
        raise SqlSignatureError("registration authentication failed") from None


def verify_heartbeat(envelope: dict, *, granted_public_key: str):
    """Return only a fully authenticated heartbeat and exact carrier digest."""
    try:
        encoded = canonical(envelope)
        schema = json.loads(
            (Path(__file__).parent / "schemas/frame/v1alpha1/framelease.schema.json").read_text()
        )
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(envelope)
        signature = envelope["spec"]["signature"]
        if (
            signature["algorithm"] != "ed25519"
            or signature["scope"] != "payload"
            or "signedObject" in signature
            or signature["keyId"] != fingerprint(granted_public_key)
        ):
            raise SqlSignatureError("SQL FrameLease signature identity or scope mismatch")
        unsigned = {
            "apiVersion": envelope["apiVersion"],
            "kind": envelope["kind"],
            "metadata": envelope["metadata"],
            "spec": {key: value for key, value in envelope["spec"].items() if key != "signature"},
        }
        lease = HeartbeatLease.model_validate(unsigned["spec"])
        if unsigned != _unsigned_envelope(lease):
            raise SqlSignatureError("SQL FrameLease payload is not canonical")
        if lease.key_id != signature["keyId"]:
            raise SqlSignatureError("SQL FrameLease key identity mismatch")
        public = serialization.load_ssh_public_key(granted_public_key.encode())
        if not isinstance(public, Ed25519PublicKey):
            raise SqlSignatureError("granted FrameLease key type unsupported")
        public.verify(
            base64.b64decode(signature["value"], validate=True), DOMAIN + canonical(unsigned)
        )
        return lease, "sha256:" + hashlib.sha256(encoded).hexdigest()
    except SqlSignatureError:
        raise
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        ValidationError,
        InvalidSignature,
        binascii.Error,
    ):
        raise SqlSignatureError("SQL FrameLease authentication failed") from None
