"""Raw HQ documents must satisfy their declared contracts without changing bytes."""

from __future__ import annotations

import base64
import subprocess

import pytest

from beadhive.hive_schema import HiveSchemaRecord as LegacyHiveSchemaRecord
from beadhive.hive_schema_contracts import HiveSchemaRecord
from beadhive.hq_document_validation import (
    DocumentValidationError,
    validate_document,
    validate_documents,
    validate_repair_carrier,
    validate_settings_mapping,
)
from beadhive.modules.config.contracts import BeadhiveConfig
from beadhive.modules.config.domain.ports import FleetConfigDocument
from beadhive.signer_policy import _KEY_TYPES, SignerPolicyError, validate_allowed_signers


def test_hive_record_reexport_is_same_pure_contract():
    assert LegacyHiveSchemaRecord is HiveSchemaRecord
    assert HiveSchemaRecord.model_fields["schema_version"].description.startswith(
        "The real bd/Dolt migration-count"
    )


@pytest.mark.parametrize("version", [None, False, True, "1", 0, -1, 2, 1.0])
@pytest.mark.parametrize("scope", ["host", "fleet"])
def test_explicit_settings_version_must_be_current_integer(scope, version):
    with pytest.raises(DocumentValidationError) as captured:
        validate_settings_mapping({"schema_version": version}, scope=scope)
    assert captured.value.path == "schema_version"
    assert captured.value.code == "unsupported_schema_version"


def test_legacy_missing_version_checks_full_shape_without_stamping():
    document = {"work": {"max_commits": 3}}
    validate_settings_mapping(document, scope="fleet")
    assert "schema_version" not in document
    with pytest.raises(DocumentValidationError, match="schema_type"):
        validate_settings_mapping({"work": {"max_commits": "3"}}, scope="fleet")


def test_legacy_beads_section_validates_live_engine_and_keeps_opaque_extensions():
    for beads in (None, {}, {"engine": None}, {"engine": ""}, {"engine": "bd"}):
        validate_settings_mapping({"beads": beads}, scope="host")
    validate_settings_mapping({"beads": {"engine": "bd", "extension": {"future": 1}}}, scope="host")
    for beads in (3, "bd", [], {"engine": 3}, {"engine": "br"}):
        with pytest.raises(DocumentValidationError):
            validate_settings_mapping({"beads": beads}, scope="host")


def test_validation_does_not_consult_ambient_settings(monkeypatch):
    monkeypatch.setenv("BH_DOLT__BACKEND", "invalid-ambient-backend")
    validate_settings_mapping({}, scope="fleet")
    validate_settings_mapping({"dolt": {"backend": "docker"}}, scope="host")


def test_ordered_snapshot_requires_exact_unique_documents():
    fleet = FleetConfigDocument("fleet.yaml", "schema_version: 1\n")
    workspace = FleetConfigDocument("workspace.toml", "# legacy empty provider list\n")
    validate_documents((fleet, workspace))
    with pytest.raises(DocumentValidationError, match="duplicate_path"):
        validate_documents((fleet, fleet))
    with pytest.raises(DocumentValidationError, match="required"):
        validate_documents((workspace,))


def test_raw_repair_view_retains_invalid_schema_but_rejects_unsafe_carriers():
    invalid_version = (FleetConfigDocument("fleet.yaml", "schema_version: true\n"),)
    validate_repair_carrier(invalid_version)
    with pytest.raises(DocumentValidationError, match="schema_version"):
        validate_documents(invalid_version)
    for unsafe in (
        FleetConfigDocument("fleet.yaml", "password: dummy-canary\n"),
        FleetConfigDocument("fleet.yaml", "url: https://user:dummy-canary@example.invalid\n"),
        FleetConfigDocument("../escape.yaml", "safe: true\n"),
        FleetConfigDocument("fleet.yaml", "invalid: [\n"),
    ):
        with pytest.raises(DocumentValidationError) as caught:
            validate_repair_carrier((unsafe,))
        assert "dummy-canary" not in str(caught.value)


def test_raw_schema_blocks_coercion_but_preserves_canonical_enum_strings_and_defaults():
    validate_settings_mapping({}, scope="fleet")
    validate_settings_mapping({"dolt": {"backend": "docker"}}, scope="host")
    validate_settings_mapping(
        {"work": {"routing": {"tiers": [{"model": "openai/gpt-5", "floor": "SIMPLE"}]}}},
        scope="fleet",
    )
    validate_settings_mapping({"managed_repos": [{"kind": ""}]}, scope="fleet")
    for document in (
        {"otel": {"enabled": "false"}},
        {"work": {"max_commits": "3"}},
        {"dolt": {"backend": "not-an-enum"}},
        {"work": {"routing": {"tiers": [{"model": "openai/gpt-5", "floor": "simple"}]}}},
        {
            "work": {
                "routing": {
                    "tiers": [{"model": "openai/gpt-5", "floor": "REASONING", "ceiling": "SIMPLE"}]
                }
            }
        },
    ):
        with pytest.raises(DocumentValidationError):
            validate_settings_mapping(document, scope="fleet")


def test_generated_schema_is_actual_canonical_contract():
    schema = BeadhiveConfig.model_json_schema()
    assert schema["properties"]["schema_version"]["default"] == 1
    # Published v1 keeps its historical output schema. The private HQ raw
    # validator derives its accepted input exceptions from these model owners.
    assert schema["$defs"]["RoutingTierConfig"]["properties"]["floor"]["$ref"] == (
        "#/$defs/ComplexityTier"
    )
    assert schema["$defs"]["ManagedRepoEntry"]["properties"]["kind"]["anyOf"][0]["enum"] == [
        "org-native",
        "personal",
        "prototype",
        "fork",
        "external",
        "hq",
    ]


def test_failure_never_renders_rejected_values_or_dynamic_keys():
    canary = "secret-canary-should-not-appear"
    with pytest.raises(DocumentValidationError) as captured:
        validate_settings_mapping({"metadata": {canary: [canary]}}, scope="host")
    assert canary not in str(captured.value)
    assert canary not in repr(captured.value)


def test_workspace_provider_contract_matches_pinned_git_workspace_1101():
    valid = (
        '[[provider]]\nprovider = "github"\nname = "one"\npath = "github"\n'
        'include = ["org/*"]\n[provider.extension]\nfuture = "opaque"\n'
    )
    validate_document("workspace.toml", valid)
    for invalid in (
        '[[provider]]\nprovider = "github"\nname = "one"\n',
        '[[provider]]\ntype = "github"\nname = "one"\npath = "github"\n',
        '[[provider]]\nprovider = ["github"]\nname = "one"\npath = "github"\n',
        '[[provider]]\nprovider = "github"\nname = 4\npath = "github"\n',
    ):
        with pytest.raises(DocumentValidationError):
            validate_document("workspace.toml", invalid)


def test_manifest_contracts_validate_identity_and_observed_beads_count():
    host = (
        "host_id: frame-1\nlabel: Frame\nos: linux\narch: x86_64\n"
        "role: executor\nidentity:\n  kind: none\n"
    )
    validate_document("hosts/frame-1.yaml", host)
    with pytest.raises(DocumentValidationError, match="schema_type"):
        validate_document("hosts/frame-1.yaml", host + "remote_only_hives: not-a-list\n")
    with pytest.raises(DocumentValidationError, match="path_identity_mismatch"):
        validate_document("hosts/frame-2.yaml", host)
    hive = (
        "provider: github\norg: acme\nrepo: app\nschema_version: 59\n"
        'observed_at: "2026-08-01T00:00:00Z"\n'
    )
    validate_document("hives/github/acme/app.yaml", hive)
    with pytest.raises(DocumentValidationError, match="schema_type"):
        validate_document(
            "hives/github/acme/app.yaml", hive.replace("schema_version: 59", "schema_version: '59'")
        )
    with pytest.raises(DocumentValidationError, match="path_identity_mismatch"):
        validate_document("hives/github/acme/other.yaml", hive)


def _generated_public_key(tmp_path, algorithm: str) -> tuple[str, str]:
    private = tmp_path / algorithm
    subprocess.run(
        ["ssh-keygen", "-q", "-t", algorithm, "-N", "", "-f", str(private)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    key_type, encoded, *_ = private.with_suffix(".pub").read_text().split()
    return key_type, encoded


@pytest.mark.parametrize("algorithm", ["ed25519", "rsa", "ecdsa"])
@pytest.mark.usefixtures("runtime_test_scope")
def test_openssh_generated_signer_key_and_options(tmp_path, algorithm):
    key_type, key = _generated_public_key(tmp_path, algorithm)
    policy = (
        f'# policy\noperator@example.invalid namespaces="file,git",valid-after="20240101Z" '
        f"{key_type} {key} optional comment\n"
    )
    validate_allowed_signers(policy)
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(policy)
    result = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "match-principals",
            "-f",
            str(allowed),
            "-I",
            "operator@example.invalid",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "operator@example.invalid"


@pytest.mark.usefixtures("runtime_test_scope")
def test_openssh_signer_rejects_truncated_key_mismatch_and_bad_option(tmp_path):
    key_type, key = _generated_public_key(tmp_path, "ed25519")
    raw = base64.b64decode(key)
    truncated = base64.b64encode(raw[:-1]).decode()
    for invalid in (
        "operator@example.invalid ssh-ed25519 not-base64!",
        f"operator@example.invalid {key_type} {truncated}",
        f"operator@example.invalid ssh-rsa {key}",
        f"operator@example.invalid unknown-opt=yes {key_type} {key}",
        f"operator@example.invalid valid-after=20241399 {key_type} {key}",
    ):
        with pytest.raises(SignerPolicyError):
            validate_allowed_signers(invalid)


def _ssh_string(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


@pytest.mark.usefixtures("runtime_test_scope")
def test_openssh_certificate_and_security_key_carrier_shapes(tmp_path):
    _generated_public_key(tmp_path, "ed25519")
    ca = tmp_path / "ca"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ca)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    leaf = tmp_path / "ed25519.pub"
    subprocess.run(
        ["ssh-keygen", "-q", "-s", str(ca), "-I", "test", "-n", "operator", str(leaf)],
        check=True,
        capture_output=True,
        timeout=10,
    )
    cert_type, cert_blob, *_ = (tmp_path / "ed25519-cert.pub").read_text().split()
    validate_allowed_signers(f"operator@example.invalid {cert_type} {cert_blob}\n")
    cert_raw = base64.b64decode(cert_blob)
    with pytest.raises(SignerPolicyError, match="invalid_public_key"):
        validate_allowed_signers(
            f"operator@example.invalid {cert_type} {base64.b64encode(cert_raw[:-1]).decode()}\n"
        )

    _, encoded = _generated_public_key(tmp_path, "ecdsa")
    original = base64.b64decode(encoded)
    type_length = int.from_bytes(original[:4], "big")
    material = original[4 + type_length :]
    sk_type = b"sk-ecdsa-sha2-nistp256@openssh.com"
    sk_blob = _ssh_string(sk_type) + material + _ssh_string(b"ssh:")
    validate_allowed_signers(
        f"operator@example.invalid {sk_type.decode()} {base64.b64encode(sk_blob).decode()}\n"
    )


@pytest.mark.usefixtures("runtime_test_scope")
def test_signer_rejects_ecdsa_off_curve(tmp_path):
    key_type, encoded = _generated_public_key(tmp_path, "ecdsa")
    raw = bytearray(base64.b64decode(encoded))
    raw[-1] ^= 0x80
    with pytest.raises(SignerPolicyError, match="invalid_public_key"):
        validate_allowed_signers(
            f"operator@example.invalid {key_type} {base64.b64encode(raw).decode()}"
        )


@pytest.mark.usefixtures("runtime_test_scope")
def test_signer_parser_covers_installed_openssh_key_algorithms():
    result = subprocess.run(
        ["ssh", "-Q", "key"], check=True, capture_output=True, text=True, timeout=10
    )
    supported = set(result.stdout.splitlines())
    assert supported
    assert supported <= _KEY_TYPES
