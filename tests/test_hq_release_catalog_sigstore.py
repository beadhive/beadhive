"""``SigstoreBundleVerifier.verify`` and the ``verify_dsse`` call it wraps (bh-vfrem.8).

The optional extra ``sigstore`` is not part of the gate environment, so the library is replaced
by a stand-in that records exactly what the adapter hands ``Verifier.verify_dsse`` (the bundle,
and the certificate-identity policy built from the SAN and issuer). Everything on our side of
that boundary is real: the certificates are generated here with ``cryptography`` and carry
Fulcio's GitHub claims as DER ``UTF8String`` extensions, so claim extraction, the identity
arguments, the trusted-root choice and every error mapping run for real. No Sigstore, GitHub or
network, and no clock (the certificate validity is never consulted: the stand-in is the verifier).
"""

from __future__ import annotations

import datetime
import json
import sys
import types

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from beadhive.hq_release_catalog import CatalogError, VerifiedAttestation

_FULCIO = "1.3.6.1.4.1.57264.1."
SAN = "https://github.com/beadhive/beadhive/.github/workflows/release.yml@refs/tags/v0.28.5"
ISSUER = "https://token.actions.githubusercontent.com"
CLAIMS = {
    "8": ISSUER,
    "12": "https://github.com/beadhive/beadhive",
    "13": "c" * 40,
    "14": "refs/tags/v0.28.5",
    "15": "123456",
}
STATEMENT = {"_type": "https://in-toto.io/Statement/v1", "predicate": {"version": "0.28.5"}}


def _utf8(text: str) -> bytes:
    raw = text.encode()
    assert len(raw) < 128
    return b"\x0c" + bytes([len(raw)]) + raw


def _certificate(*, claims=None, sans=(SAN,)):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-leaf")])
    now = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)  # fixed, never "now"
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(minutes=10))
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(s) for s in sans]),
            critical=False,
        )
    for suffix, value in (CLAIMS if claims is None else claims).items():
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(_FULCIO + suffix), _utf8(value)),
            critical=False,
        )
    certificate = builder.sign(key, hashes.SHA256())
    # Round-trip through DER, as a certificate inside a real bundle is parsed.
    from cryptography.hazmat.primitives.serialization import Encoding

    return x509.load_der_x509_certificate(certificate.public_bytes(Encoding.DER))


class _SigstoreError(Exception):
    pass


class _Calls:
    def __init__(self):
        self.verify_dsse: list[tuple] = []
        self.roots: list[str] = []
        self.production = 0


@pytest.fixture
def stand_in(monkeypatch):
    """Install a fake ``sigstore`` and return ``(calls, configure)``."""
    calls = _Calls()
    state = types.SimpleNamespace(
        certificate=_certificate(),
        payload=json.dumps(STATEMENT).encode(),
        raises=None,
        bundle_error=None,
    )

    class Bundle:
        signing_certificate = None

        @classmethod
        def from_json(cls, raw):
            if state.bundle_error is not None:
                raise state.bundle_error
            bundle = cls()
            bundle.raw = raw
            bundle.signing_certificate = state.certificate
            return bundle

    class Verifier:
        def verify_dsse(self, bundle, policy):
            calls.verify_dsse.append((bundle, policy))
            if state.raises is not None:
                raise state.raises
            return "application/vnd.in-toto+json", state.payload

        @classmethod
        def production(cls):
            calls.production += 1
            return cls()

        def __init__(self, trusted_root=None):
            self.trusted_root = trusted_root

    class TrustedRoot:
        @classmethod
        def from_file(cls, path):
            calls.roots.append(path)
            return cls()

    class Identity:
        def __init__(self, *, identity, issuer):
            self.identity, self.issuer = identity, issuer

    modules = {
        "sigstore": types.ModuleType("sigstore"),
        "sigstore.errors": types.ModuleType("sigstore.errors"),
        "sigstore.models": types.ModuleType("sigstore.models"),
        "sigstore.verify": types.ModuleType("sigstore.verify"),
        "sigstore.verify.policy": types.ModuleType("sigstore.verify.policy"),
    }
    modules["sigstore.errors"].Error = _SigstoreError
    modules["sigstore.models"].Bundle = Bundle
    modules["sigstore.models"].TrustedRoot = TrustedRoot
    modules["sigstore.verify"].Verifier = Verifier
    modules["sigstore.verify.policy"].Identity = Identity
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return calls, state


def _verify(trusted_root=None, bundle=b'{"bundle": 1}'):
    from beadhive.hq_release_catalog_sigstore import SigstoreBundleVerifier

    return SigstoreBundleVerifier().verify(
        bundle, identity=SAN, issuer=ISSUER, trusted_root=trusted_root
    )


def test_verify_dsse_gets_the_exact_identity_and_the_claims_come_from_the_certificate(stand_in):
    calls, _state = stand_in
    result = _verify()
    assert result == VerifiedAttestation(
        statement=STATEMENT,
        issuer=ISSUER,
        san=SAN,
        repository_uri="https://github.com/beadhive/beadhive",
        repository_id="123456",
        ref="refs/tags/v0.28.5",
        source_digest="c" * 40,
    )
    [(bundle, policy)] = calls.verify_dsse
    assert bundle.raw == b'{"bundle": 1}'
    assert (policy.identity, policy.issuer) == (SAN, ISSUER)  # the caller's, never widened


def test_online_uses_the_production_trust_root_and_offline_pins_the_given_file(stand_in, tmp_path):
    calls, _state = stand_in
    _verify()
    assert (calls.production, calls.roots) == (1, [])
    pinned = tmp_path / "trusted_root.json"
    _verify(trusted_root=pinned)
    assert (calls.production, calls.roots) == (1, [str(pinned)])  # no TUF refresh when pinned


@pytest.mark.parametrize("failure", [_SigstoreError("detail with s3cr3t"), ValueError("bad json")])
def test_library_failures_become_a_catalog_error_without_their_message(stand_in, failure):
    _calls, state = stand_in
    state.raises = failure
    with pytest.raises(CatalogError, match=r"bundle verification failed \(") as caught:
        _verify()
    assert "s3cr3t" not in str(caught.value) and "bad json" not in str(caught.value)


def test_an_unparseable_bundle_is_refused_before_any_verification(stand_in):
    calls, state = stand_in
    state.bundle_error = ValueError("not a bundle")
    with pytest.raises(CatalogError, match="bundle verification failed"):
        _verify()
    assert calls.verify_dsse == []


def test_a_signed_payload_that_is_not_json_is_refused(stand_in):
    _calls, state = stand_in
    state.payload = b"\xff not json"
    with pytest.raises(CatalogError, match="bundle verification failed"):
        _verify()


def test_a_statement_that_is_not_an_object_is_malformed(stand_in):
    _calls, state = stand_in
    state.payload = b"[1, 2]"
    with pytest.raises(CatalogError, match="malformed"):
        _verify()


@pytest.mark.parametrize("sans", [(), (SAN, SAN + "2")])
def test_a_certificate_without_exactly_one_uri_san_is_refused(stand_in, sans):
    _calls, state = stand_in
    state.certificate = _certificate(sans=sans)
    with pytest.raises(CatalogError):
        _verify()


def test_a_certificate_without_the_github_claims_is_refused(stand_in):
    _calls, state = stand_in
    state.certificate = _certificate(claims={k: v for k, v in CLAIMS.items() if k != "15"})
    with pytest.raises(CatalogError, match="lacks the GitHub repository claims"):
        _verify()


def test_a_claim_that_is_not_a_utf8_string_is_refused(stand_in):
    _calls, state = stand_in
    # The repository-id claim is a DER OCTET STRING, not the UTF8String Fulcio uses.
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "t")])
    when = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(when)
        .not_valid_after(when + datetime.timedelta(minutes=10))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(SAN)]), critical=False
        )
    )
    for suffix, value in CLAIMS.items():
        raw = b"\x04\x01x" if suffix == "15" else _utf8(value)
        builder = builder.add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier(_FULCIO + suffix), raw),
            critical=False,
        )
    state.certificate = builder.sign(key, hashes.SHA256())
    with pytest.raises(CatalogError, match="not a UTF8String"):
        _verify()


def test_the_real_library_still_has_the_surface_the_adapter_calls():
    """Runs only where the optional extra is installed: guards the stand-in against drift."""
    pytest.importorskip("sigstore")
    import inspect

    from sigstore.errors import Error
    from sigstore.models import Bundle, TrustedRoot
    from sigstore.verify import Verifier
    from sigstore.verify.policy import Identity

    assert issubclass(Error, Exception)
    assert list(inspect.signature(Verifier.verify_dsse).parameters) == ["self", "bundle", "policy"]
    assert callable(Verifier.production) and callable(Bundle.from_json)
    assert callable(TrustedRoot.from_file) and hasattr(Bundle, "signing_certificate")
    assert {"identity", "issuer"} <= set(inspect.signature(Identity).parameters)
