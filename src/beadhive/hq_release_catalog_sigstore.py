"""The Sigstore half of the release catalog (bh-vfrem.4): the only module that imports ``sigstore``.

Optional extra ``beadhive[release-verify]`` (sigstore, Apache-2.0). Imported lazily by the
``bh hq release-catalog`` verbs; core, frames and every other command import without it.

``verify_dsse`` checks the bundle's signature chain, the Fulcio certificate against the trusted
root, the transparency-log entry, and the certificate identity against the exact SAN and issuer
the caller passes. This module then reads the repository claims out of the verified certificate
(Fulcio's GitHub OIDC extensions) so :func:`beadhive.hq_release_catalog.check_attestation` can
apply the rest of the identity policy. With ``trusted_root`` the root is a pinned local file
(offline); without it the TUF root is refreshed online.
"""

from __future__ import annotations

import json
from pathlib import Path

from .hq_release_catalog import CatalogError, VerifiedAttestation

__all__ = ["SigstoreBundleVerifier", "available"]

#: Fulcio OID arc 1.3.6.1.4.1.57264.1 (https://github.com/sigstore/fulcio/blob/main/docs/oid-info.md).
_FULCIO = "1.3.6.1.4.1.57264.1."
_OID_ISSUER_V2 = _FULCIO + "8"
_OID_REPOSITORY_URI = _FULCIO + "12"
_OID_REPOSITORY_DIGEST = _FULCIO + "13"
_OID_REPOSITORY_REF = _FULCIO + "14"
_OID_REPOSITORY_ID = _FULCIO + "15"
_INSTALL_HINT = (
    "release verification needs the optional extra: install 'beadhive[release-verify]' "
    "(uv tool install 'beadhive[release-verify]')"
)


def available() -> bool:
    """Whether the optional extra is installed (does not import it into the caller)."""
    import importlib.util

    return importlib.util.find_spec("sigstore") is not None


def _der_utf8(raw: bytes) -> str:
    """The text of a DER ``UTF8String`` (the form Fulcio's v2 extensions use)."""
    if len(raw) < 2 or raw[0] != 0x0C:
        raise CatalogError("certificate extension is not a UTF8String")
    length, offset = raw[1], 2
    if length & 0x80:
        width = length & 0x7F
        if not 1 <= width <= 2 or len(raw) < 2 + width:
            raise CatalogError("certificate extension length is malformed")
        length, offset = int.from_bytes(raw[2 : 2 + width], "big"), 2 + width
    if len(raw) != offset + length:
        raise CatalogError("certificate extension length is malformed")
    return raw[offset:].decode("utf-8")


class SigstoreBundleVerifier:
    """A :class:`beadhive.hq_release_catalog.BundleVerifier` over ``sigstore-python``."""

    def __init__(self) -> None:
        try:
            import sigstore  # noqa: F401
        except ImportError:
            raise CatalogError(_INSTALL_HINT) from None

    def verify(
        self, bundle: bytes, *, identity: str, issuer: str, trusted_root: Path | None
    ) -> VerifiedAttestation:
        from cryptography import x509
        from sigstore.errors import Error as SigstoreError
        from sigstore.models import Bundle, TrustedRoot
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity

        try:
            parsed = Bundle.from_json(bundle)
            if trusted_root is not None:
                verifier = Verifier(trusted_root=TrustedRoot.from_file(str(trusted_root)))
            else:
                verifier = Verifier.production()
            _payload_type, payload = verifier.verify_dsse(
                parsed, Identity(identity=identity, issuer=issuer)
            )
            statement = json.loads(payload)
            certificate = parsed.signing_certificate
            claims = {
                extension.oid.dotted_string: bytes(extension.value.value)
                for extension in certificate.extensions
                if isinstance(extension.value, x509.UnrecognizedExtension)
            }
            alternative = certificate.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.UniformResourceIdentifier)
        except CatalogError:
            raise
        except (SigstoreError, ValueError, KeyError, x509.ExtensionNotFound) as exc:
            raise CatalogError(f"bundle verification failed ({type(exc).__name__})") from None
        if len(alternative) != 1 or not isinstance(statement, dict):
            raise CatalogError("bundle certificate or statement is malformed")
        try:
            return VerifiedAttestation(
                statement=statement,
                issuer=_der_utf8(claims[_OID_ISSUER_V2]),
                san=alternative[0],
                repository_uri=_der_utf8(claims[_OID_REPOSITORY_URI]),
                repository_id=_der_utf8(claims[_OID_REPOSITORY_ID]),
                ref=_der_utf8(claims[_OID_REPOSITORY_REF]),
                source_digest=_der_utf8(claims[_OID_REPOSITORY_DIGEST]),
            )
        except KeyError:
            raise CatalogError("certificate lacks the GitHub repository claims") from None
