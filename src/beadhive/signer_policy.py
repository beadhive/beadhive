"""Pure structural validation for OpenSSH ``allowed_signers`` documents.

This checks the public-key carrier and option grammar before HQ publication.
OpenSSH remains the authority for signature, principal-pattern, and time-policy
evaluation; validation neither resolves keys nor invokes ssh-keygen.
"""

from __future__ import annotations

import base64
import binascii
import re
import shlex
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa


class SignerPolicyError(ValueError):
    """An allowed-signers line has invalid public syntax, without echoing it."""

    def __init__(self, line: int, code: str):
        self.line, self.code = line, code
        super().__init__(f"allowed_signers line {line}: {code}")


_CURVES = {
    "nistp256": ec.SECP256R1,
    "nistp384": ec.SECP384R1,
    "nistp521": ec.SECP521R1,
}
_PLAIN_KEYS = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        *(f"ecdsa-sha2-{curve}" for curve in _CURVES),
    }
)
_CERT_SUFFIX = "-cert-v01@openssh.com"
_CERT_KEYS = frozenset(key.removesuffix("@openssh.com") + _CERT_SUFFIX for key in _PLAIN_KEYS)
_KEY_TYPES = _PLAIN_KEYS | _CERT_KEYS
_OPTION_VALUE = frozenset({"namespaces", "valid-after", "valid-before"})
_TIME = re.compile(r"(?:\d{8}|\d{12}|\d{14})Z?\Z")


class _Wire:
    """Bounded SSH wire reader; every field must consume its entire declared body."""

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def take(self, length: int) -> bytes:
        if length < 0 or length > len(self.data) - self.offset:
            raise ValueError("short wire field")
        value = self.data[self.offset : self.offset + length]
        self.offset += length
        return value

    def u32(self) -> int:
        return int.from_bytes(self.take(4), "big")

    def u64(self) -> int:
        return int.from_bytes(self.take(8), "big")

    def string(self) -> bytes:
        return self.take(self.u32())

    def mpint(self) -> int:
        raw = self.string()
        if not raw or raw[0] & 0x80 or (len(raw) > 1 and raw[0] == 0 and not raw[1] & 0x80):
            raise ValueError("invalid positive mpint")
        value = int.from_bytes(raw, "big")
        if value <= 0:
            raise ValueError("invalid positive mpint")
        return value

    def done(self) -> None:
        if self.offset != len(self.data):
            raise ValueError("trailing key data")


def _key_material(wire: _Wire, key_type: str) -> None:
    plain_type = key_type.removesuffix(_CERT_SUFFIX)
    if plain_type.startswith("sk-") and not plain_type.endswith("@openssh.com"):
        plain_type += "@openssh.com"
    if plain_type == "ssh-ed25519":
        ed25519.Ed25519PublicKey.from_public_bytes(wire.string())
    elif plain_type == "ssh-rsa":
        exponent, modulus = wire.mpint(), wire.mpint()
        rsa.RSAPublicNumbers(exponent, modulus).public_key()
    elif plain_type.startswith("ecdsa-sha2-") or plain_type.startswith("sk-ecdsa-sha2-"):
        curve_name = (
            plain_type.removeprefix("sk-").removeprefix("ecdsa-sha2-").removesuffix("@openssh.com")
        )
        if wire.string().decode("ascii") != curve_name:
            raise ValueError("curve name mismatch")
        ec.EllipticCurvePublicKey.from_encoded_point(_CURVES[curve_name](), wire.string())
    elif plain_type == "sk-ssh-ed25519@openssh.com":
        ed25519.Ed25519PublicKey.from_public_bytes(wire.string())
    else:
        raise ValueError("unsupported key type")
    if plain_type.startswith("sk-") and not wire.string():
        raise ValueError("security key application missing")


def _wire_sequence(data: bytes, *, pairs: bool = False) -> None:
    nested = _Wire(data)
    while nested.offset < len(data):
        if not nested.string():
            raise ValueError("empty certificate field")
        if pairs:
            nested.string()
    nested.done()


def _parse_key_blob(raw: bytes, *, allow_cert: bool = True) -> str:
    wire = _Wire(raw)
    key_type = wire.string().decode("ascii")
    if key_type not in _KEY_TYPES or (not allow_cert and key_type in _CERT_KEYS):
        raise ValueError("unsupported key type")
    if key_type in _CERT_KEYS and not wire.string():
        raise ValueError("certificate nonce missing")
    _key_material(wire, key_type)
    if key_type in _CERT_KEYS:
        wire.u64()  # serial
        if wire.u32() not in (1, 2):
            raise ValueError("certificate type invalid")
        wire.string()  # key ID may be empty
        _wire_sequence(wire.string())  # valid principals may be empty
        wire.u64()  # valid after
        wire.u64()  # valid before
        _wire_sequence(wire.string(), pairs=True)  # critical options
        _wire_sequence(wire.string(), pairs=True)  # extensions
        if wire.string():
            raise ValueError("reserved certificate field not empty")
        _parse_key_blob(wire.string(), allow_cert=False)  # signing CA public key
        signature = _Wire(wire.string())
        if not signature.string() or not signature.string():
            raise ValueError("certificate signature missing")
        signature.done()
    wire.done()
    return key_type


def _words(line: str, line_number: int) -> list[str]:
    """Split OpenSSH's whitespace fields while keeping quoted option text."""

    words: list[str] = []
    start: int | None = None
    quote = False
    escape = False
    for index, char in enumerate(line):
        if start is None:
            if char.isspace():
                continue
            start = index
        if escape:
            escape = False
        elif char == "\\" and quote:
            escape = True
        elif char == '"':
            quote = not quote
        elif char.isspace() and not quote:
            words.append(line[start:index])
            start = None
    if quote or escape:
        raise SignerPolicyError(line_number, "unterminated_option_quote")
    if start is not None:
        words.append(line[start:])
    return words


def _options(token: str, line_number: int) -> None:
    lexer = shlex.shlex(token, posix=True)
    lexer.whitespace = ","
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        options = list(lexer)
    except ValueError:
        raise SignerPolicyError(line_number, "invalid_options") from None
    if not options or token.endswith(",") or ",," in token:
        raise SignerPolicyError(line_number, "invalid_options")
    seen: set[str] = set()
    for option in options:
        if option.lower() == "cert-authority":
            name = option.lower()
        elif "=" in option:
            name, value = option.split("=", 1)
            name = name.lower()
            if name not in _OPTION_VALUE or not value:
                raise SignerPolicyError(line_number, "invalid_options")
            if name == "namespaces":
                if any(
                    not pattern or any(char.isspace() for char in pattern)
                    for pattern in value.split(",")
                ):
                    raise SignerPolicyError(line_number, "invalid_namespace_patterns")
            elif not _valid_timestamp(value):
                raise SignerPolicyError(line_number, "invalid_validity_timestamp")
        else:
            raise SignerPolicyError(line_number, "invalid_options")
        if name in seen:
            raise SignerPolicyError(line_number, "duplicate_option")
        seen.add(name)


def _valid_timestamp(value: str) -> bool:
    if not _TIME.fullmatch(value):
        return False
    digits = value.removesuffix("Z")
    fmt = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}[len(digits)]
    try:
        datetime.strptime(digits, fmt)
    except ValueError:
        return False
    return True


def _key_blob(encoded: str, key_type: str, line_number: int) -> None:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise SignerPolicyError(line_number, "invalid_public_key") from None
    try:
        embedded = _parse_key_blob(raw)
    except (ValueError, UnicodeError):
        raise SignerPolicyError(line_number, "invalid_public_key") from None
    if embedded != key_type:
        raise SignerPolicyError(line_number, "public_key_type_mismatch")


def validate_allowed_signers(content: str) -> None:
    """Validate each non-comment line without changing public policy bytes."""

    if not isinstance(content, str):
        raise SignerPolicyError(0, "text_required")
    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words = _words(stripped, number)
        if (
            len(words) < 3
            or not words[0]
            or any(
                not pattern or any(char.isspace() for char in pattern)
                for pattern in words[0].split(",")
            )
        ):
            raise SignerPolicyError(number, "invalid_principals")
        offset = 1
        if words[offset] not in _KEY_TYPES:
            _options(words[offset], number)
            offset += 1
        if len(words) < offset + 2 or words[offset] not in _KEY_TYPES:
            raise SignerPolicyError(number, "invalid_key_type")
        _key_blob(words[offset + 1], words[offset], number)


__all__ = ("SignerPolicyError", "validate_allowed_signers")
