"""The signed release catalog's rules, verbs and CLI (bh-vfrem.4), with no network and no Sigstore.

The Sigstore boundary is a fake :class:`BundleVerifier` returning what a verified bundle would
carry; the storage is an in-memory store that runs the same :func:`plan_append` /
:func:`effective_active` rules the Dolt-backed store runs (its SQL is covered by
``test_hq_release_catalog_int``). Clocks are mocked. Never real Sigstore, PyPI or GitHub.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from beadhive import hq_release_catalog as catalog
from beadhive import release_catalog_auto_import as auto
from beadhive.hq_release_catalog import (
    ALGORITHM,
    PREDICATE_TYPE,
    CatalogConflict,
    CatalogError,
    CatalogRow,
    FileSource,
    HttpSource,
    IdentityPolicy,
    VerifiedAttestation,
    effective_active,
    import_release,
    measure_wheel,
    plan_append,
    revoke_release,
    verify_rows,
)

POLICY = IdentityPolicy()
VERSION = "0.28.5"
COMMIT = "c" * 40
NOW = 1_800_000_000.0


def _wheel(version=VERSION, *, extra=b"", name="beadhive") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("beadhive/__init__.py", b"VERSION = 1\n" + extra)
        archive.writestr("beadhive/tool.py", b"print('tool')\n")
        archive.writestr("beadhive/__pycache__/tool.cpython-312.pyc", b"ignored")
        archive.writestr("beadhive_core/other.py", b"not measured\n")
        archive.writestr(
            f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
    return buffer.getvalue()


def _statement(wheel: bytes, version=VERSION, **overrides):
    measured = measure_wheel(wheel)
    predicate = {
        "version": version,
        "measured_digest": measured.digest,
        "algorithm": ALGORITHM,
        "file_count": measured.file_count,
        "wheel_sha256": hashlib.sha256(wheel).hexdigest(),
        "commit": COMMIT,
    }
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "wheel", "digest": {"sha256": hashlib.sha256(wheel).hexdigest()}}],
        "predicateType": PREDICATE_TYPE,
        "predicate": predicate,
    }
    for key, value in overrides.items():
        statement["predicate"][key] = value
    return statement


def _verified(wheel: bytes, version=VERSION, **overrides) -> VerifiedAttestation:
    fields = {
        "statement": _statement(wheel, version),
        "issuer": POLICY.issuer,
        "san": POLICY.san(version),
        "repository_uri": POLICY.repository_uri(),
        "repository_id": POLICY.repository_id,
        "ref": POLICY.tag_ref(version),
        "source_digest": COMMIT,
    }
    fields.update(overrides)
    return VerifiedAttestation(**fields)


class FakeVerifier:
    """Stands in for the Sigstore boundary; records how it was called."""

    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def verify(self, bundle, *, identity, issuer, trusted_root):
        self.calls.append({"bundle": bundle, "identity": identity, "trusted_root": trusted_root})
        if self.error:
            raise self.error
        return self.result(bundle) if callable(self.result) else self.result


class MemoryStore:
    """The store's contract over a list, using the real pure rules and a mocked clock."""

    def __init__(self):
        self.table: list[CatalogRow] = []
        self.commits = 0

    def rows(self, version=None):
        return [r for r in self.table if version in (None, r.version)]

    def add(self, entry):
        if plan_append(self.table, entry) == "noop":
            return "unchanged"
        self.table.append(
            CatalogRow(
                entry.catalog,
                entry.version,
                entry.measured_digest,
                "active",
                entry.algorithm,
                entry.wheel_sha256,
                entry.commit,
                entry.bundle,
                "",
                NOW,
            )
        )
        self.commits += 1
        return "inserted"

    def revoke(self, version, reason):
        digests = sorted(effective_active(self.table, "beadhive", version))
        if not digests:
            raise CatalogError(f"version {version} has no active catalog entry")
        for digest in digests:
            source = next(r for r in self.table if r.measured_digest == digest)
            self.table.append(
                replace(source, status="revoked", reason=reason, bundle=b"", recorded_at=NOW)
            )
        return digests


def _import(tmp_path, wheel, verifier, store, *, version=VERSION, trusted_root=None):
    bundle, wheel_path = tmp_path / "bundle.json", tmp_path / f"beadhive-{version}.whl"
    bundle.write_bytes(b'{"bundle": true}')
    wheel_path.write_bytes(wheel)
    return import_release(
        version,
        source=FileSource(bundle, wheel_path),
        verifier=verifier,
        store=store,
        trusted_root=trusted_root,
    )


# ---- measurement -----------------------------------------------------------------------------


def test_measure_wheel_matches_installed_release_fold(tmp_path, monkeypatch):
    """The wheel fold must stay byte-for-byte what ``installed_release`` folds (no drift)."""
    from beadhive import release_measurement

    wheel = _wheel()
    root = tmp_path / "site"
    names = []
    with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
        for name in archive.namelist():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
            names.append(name)

    class Dist:
        version = VERSION
        files = [Path(name) for name in names]

        def locate_file(self, item):
            return root / item

    monkeypatch.setattr(importlib.metadata, "distribution", lambda _name: Dist())
    installed = release_measurement.installed_release()
    measured = measure_wheel(wheel)
    assert installed == {"id": VERSION, "digest": measured.digest}
    assert measured.file_count == 2  # __pycache__ and other top-level packages are not measured


@pytest.mark.parametrize("raw", [b"not a zip", b""])
def test_measure_wheel_refuses_garbage(raw):
    with pytest.raises(CatalogError):
        measure_wheel(raw)


def test_normalise_version_refuses_aliases():
    assert catalog.normalise_version("0.28.5") == "0.28.5"
    for bad in ("0.28.05", "v0.28.5", "latest", "0.28.5-1", ""):
        with pytest.raises(CatalogError):
            catalog.normalise_version(bad)


# ---- import ----------------------------------------------------------------------------------


def test_import_appends_the_verified_entry(tmp_path):
    wheel, store = _wheel(), MemoryStore()
    verifier = FakeVerifier(_verified(wheel))
    result = _import(tmp_path, wheel, verifier, store)
    assert result["result"] == "inserted" and result["commit"] == COMMIT
    assert store.table[0].measured_digest == measure_wheel(wheel).digest
    assert store.table[0].bundle == b'{"bundle": true}'
    # The verifier is asked for the EXACT identity of this version's tag run.
    assert verifier.calls[0]["identity"] == POLICY.san(VERSION)
    assert _import(tmp_path, wheel, verifier, store)["result"] == "unchanged"  # idempotent


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("issuer", "https://accounts.example.com", "issuer"),
        (
            "san",
            "https://github.com/evil/beadhive/.github/workflows/release.yml@refs/tags/v0.28.5",
            "identity",
        ),
        (
            "san",
            "https://github.com/beadhive/beadhive/.github/workflows/other.yml@refs/tags/v0.28.5",
            "identity",
        ),
        (
            "san",
            "https://github.com/beadhive/beadhive/.github/workflows/release.yml@refs/tags/v0.28.6",
            "identity",
        ),
        ("repository_uri", "https://github.com/evil/beadhive", "repository is not"),
        ("repository_id", "42", "repository ID"),
    ],
)
def test_import_refuses_a_bad_identity(tmp_path, field, value, match):
    wheel, store = _wheel(), MemoryStore()
    verifier = FakeVerifier(_verified(wheel, **{field: value}))
    with pytest.raises(CatalogError, match=match):
        _import(tmp_path, wheel, verifier, store)
    assert store.table == []


@pytest.mark.parametrize(
    "ref", ["refs/heads/main", "refs/pull/7/merge", "refs/tags/release-0.28.5"]
)
def test_import_refuses_a_non_tag_ref(tmp_path, ref):
    wheel, store = _wheel(), MemoryStore()
    with pytest.raises(CatalogError, match="refs/tags/v"):
        _import(tmp_path, wheel, FakeVerifier(_verified(wheel, ref=ref)), store)
    assert store.table == []


def test_import_refuses_a_tag_for_another_version(tmp_path):
    wheel, store = _wheel(), MemoryStore()
    verified = _verified(wheel, ref="refs/tags/v0.28.4")
    with pytest.raises(CatalogError, match="tag does not match"):
        _import(tmp_path, wheel, FakeVerifier(verified), store)


def test_import_refuses_a_subject_that_is_not_this_wheel(tmp_path):
    signed, served = _wheel(), _wheel(extra=b"# swapped\n")
    store = MemoryStore()
    with pytest.raises(CatalogError, match="subject is not this wheel"):
        _import(tmp_path, served, FakeVerifier(_verified(signed)), store)
    assert store.table == []


def test_import_refuses_a_recomputed_digest_mismatch(tmp_path):
    """A signed predicate that lies about the digest is caught by recomputing from the wheel."""
    wheel, store = _wheel(), MemoryStore()
    verified = _verified(wheel)
    verified.statement["predicate"]["measured_digest"] = "sha256:" + "0" * 64
    with pytest.raises(CatalogError, match="recomputed from the wheel"):
        _import(tmp_path, wheel, FakeVerifier(verified), store)
    assert store.table == []


def test_import_refuses_a_certificate_commit_that_differs_from_the_predicate(tmp_path):
    wheel, store = _wheel(), MemoryStore()
    with pytest.raises(CatalogError, match="source digest"):
        _import(tmp_path, wheel, FakeVerifier(_verified(wheel, source_digest="d" * 40)), store)


def test_import_refuses_wheel_metadata_for_another_version(tmp_path):
    wheel = _wheel(version="0.28.4")
    with pytest.raises(CatalogError, match="wheel metadata"):
        _import(tmp_path, wheel, FakeVerifier(_verified(wheel, VERSION)), MemoryStore())


def test_import_refuses_a_statement_of_the_wrong_type(tmp_path):
    wheel = _wheel()
    verified = _verified(wheel)
    verified.statement["predicateType"] = "https://slsa.dev/provenance/v1"
    with pytest.raises(CatalogError, match="not a beadhive release-manifest"):
        _import(tmp_path, wheel, FakeVerifier(verified), MemoryStore())


def test_import_surfaces_a_failed_signature_check_and_writes_nothing(tmp_path):
    store = MemoryStore()
    with pytest.raises(CatalogError, match="bundle verification failed"):
        _import(
            tmp_path,
            _wheel(),
            FakeVerifier(error=CatalogError("bundle verification failed")),
            store,
        )
    assert store.table == []


def test_a_second_digest_for_an_active_version_is_refused_until_revoked(tmp_path):
    first, second, store = _wheel(), _wheel(extra=b"# rebuilt\n"), MemoryStore()
    _import(tmp_path, first, FakeVerifier(_verified(first)), store)
    with pytest.raises(CatalogConflict, match="different active digest"):
        _import(tmp_path, second, FakeVerifier(_verified(second)), store)
    assert len(store.table) == 1
    revoke_release(VERSION, "re-pushed tag", store=store)
    assert _import(tmp_path, second, FakeVerifier(_verified(second)), store)["result"] == "inserted"
    assert effective_active(store.table, "beadhive", VERSION) == {measure_wheel(second).digest}


def test_revoke_appends_a_tombstone_and_the_digest_cannot_be_reactivated(tmp_path):
    wheel, store = _wheel(), MemoryStore()
    _import(tmp_path, wheel, FakeVerifier(_verified(wheel)), store)
    result = revoke_release(VERSION, "compromised build host", store=store)
    assert result["revoked"] == [measure_wheel(wheel).digest]
    assert [r.status for r in store.table] == ["active", "revoked"]  # append-only: nothing edited
    assert store.table[1].reason == "compromised build host"
    assert effective_active(store.table, "beadhive", VERSION) == set()
    with pytest.raises(CatalogConflict, match="revoked"):
        _import(tmp_path, wheel, FakeVerifier(_verified(wheel)), store)
    with pytest.raises(CatalogError, match="no active catalog entry"):
        revoke_release(VERSION, "again", store=store)


# ---- verify (offline) ------------------------------------------------------------------------


def _catalogued(tmp_path):
    wheel, store = _wheel(), MemoryStore()
    _import(tmp_path, wheel, FakeVerifier(_verified(wheel)), store)
    return wheel, store


def test_verify_reverifies_stored_bundles_offline_with_a_pinned_root(tmp_path):
    wheel, store = _catalogued(tmp_path)
    verifier = FakeVerifier(_verified(wheel))
    root = tmp_path / "trusted_root.json"
    result = verify_rows(store=store, verifier=verifier, trusted_root=root)
    assert result["verified"] == 1 and result["failed"] == 0
    # Only the stored bundle and the pinned root are used: no source, no network object exists.
    assert verifier.calls == [
        {"bundle": b'{"bundle": true}', "identity": POLICY.san(VERSION), "trusted_root": root}
    ]


def test_verify_flags_a_bundle_that_no_longer_matches_its_row(tmp_path):
    wheel, store = _catalogued(tmp_path)
    store.table[0] = replace(store.table[0], measured_digest="sha256:" + "9" * 64)
    result = verify_rows(store=store, verifier=FakeVerifier(_verified(wheel)))
    assert result["failed"] == 1
    assert "no longer matches" in result["entries"][0]["error"]


def test_verify_flags_a_bundle_that_fails_verification(tmp_path):
    _wheel_, store = _catalogued(tmp_path)
    result = verify_rows(
        store=store, verifier=FakeVerifier(error=CatalogError("certificate expired"))
    )
    assert result["failed"] == 1 and result["entries"][0]["error"] == "certificate expired"


def test_verify_skips_revoked_entries(tmp_path):
    wheel, store = _catalogued(tmp_path)
    revoke_release(VERSION, "yanked", store=store)
    verifier = FakeVerifier(_verified(wheel))
    result = verify_rows(store=store, verifier=verifier)
    assert result["failed"] == 0 and result["entries"][0]["status"] == "revoked"
    assert verifier.calls == []


# ---- sources ---------------------------------------------------------------------------------


def test_http_source_gets_the_bundle_from_the_attestation_api_by_wheel_sha():
    import base64

    wheel = _wheel()
    sha = hashlib.sha256(wheel).hexdigest()
    seen = []

    def attestation(predicate_type, tag):
        payload = base64.b64encode(json.dumps({"predicateType": predicate_type}).encode()).decode()
        return {"bundle": {"tag": tag, "dsseEnvelope": {"payload": payload}}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == f"/repos/beadhive/beadhive/attestations/sha256:{sha}":
            return httpx.Response(
                200,
                json={
                    "attestations": [
                        attestation("https://slsa.dev/provenance/v1", "slsa"),
                        attestation(PREDICATE_TYPE, "manifest"),
                    ]
                },
            )
        if request.url.path.endswith("/0.28.5/json"):
            return httpx.Response(
                200,
                json={
                    "urls": [{"packagetype": "bdist_wheel", "url": "https://files.example/w.whl"}]
                },
            )
        if request.url.host == "files.example":
            return httpx.Response(200, content=wheel)
        return httpx.Response(404)

    source = HttpSource(httpx.Client(transport=httpx.MockTransport(handler)))
    assert source.wheel(VERSION) == wheel
    assert json.loads(source.bundle(VERSION, sha))["tag"] == "manifest"
    assert all(url.startswith("https://") for url in seen)
    with pytest.raises(CatalogError, match="fetch failed"):
        source.bundle(VERSION, "0" * 64)
    with pytest.raises(CatalogError, match="malformed"):
        source.bundle(VERSION, "xyz")


def test_http_source_refuses_an_api_answer_without_the_release_manifest():
    def handler(request):
        return httpx.Response(200, json={"attestations": []})

    source = HttpSource(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(CatalogError, match="no release-manifest attestation"):
        source.bundle(VERSION, "a" * 64)


def test_http_source_refuses_plain_http_wheel_urls():
    def handler(request):
        return httpx.Response(
            200,
            json={"urls": [{"packagetype": "bdist_wheel", "url": "http://files.example/w.whl"}]},
        )

    source = HttpSource(httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(CatalogError, match="https only"):
        source.wheel(VERSION)


# ---- sigstore adapter and the optional extra -------------------------------------------------


def test_core_imports_without_sigstore_and_the_adapter_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "sigstore", None)  # as if the extra were not installed
    from beadhive import hq_release_catalog_sigstore as adapter

    assert adapter.available() is False
    with pytest.raises(CatalogError, match=r"beadhive\[release-verify\]"):
        adapter.SigstoreBundleVerifier()


def test_der_utf8_reads_fulcio_extension_values():
    from beadhive.hq_release_catalog_sigstore import _der_utf8

    assert _der_utf8(b"\x0c\x05hello") == "hello"
    long = b"x" * 200
    assert _der_utf8(b"\x0c\x81\xc8" + long) == long.decode()
    for bad in (b"", b"\x04\x01a", b"\x0c\x05hi"):
        with pytest.raises(CatalogError):
            _der_utf8(bad)


# ---- cli -------------------------------------------------------------------------------------


def test_cli_actions_drive_the_core_and_report_refusals(tmp_path, monkeypatch):
    from beadhive import hq_release_catalog_cli as cli

    wheel, store = _wheel(), MemoryStore()
    monkeypatch.setattr(cli, "_verifier", lambda: FakeVerifier(_verified(wheel)))
    monkeypatch.setattr(cli, "_store", lambda _settings: store)

    def no_network():
        raise CatalogError("no network in tests")

    monkeypatch.setattr(cli, "_remote", no_network)
    bundle, wheel_path = tmp_path / "b.json", tmp_path / "w.whl"
    bundle.write_bytes(b"{}")
    wheel_path.write_bytes(wheel)
    app = typer.Typer()
    app.command("rc")(cli.release_catalog_cmd)
    app.command("noop")(lambda: None)
    run = CliRunner().invoke
    imported = run(
        app, ["rc", "import", VERSION, "--bundle", str(bundle), "--wheel", str(wheel_path)]
    )
    assert imported.exit_code == 0, imported.output
    assert json.loads(imported.output)["result"] == "inserted"
    assert run(app, ["rc", "verify", "--all"]).exit_code == 0
    assert run(app, ["rc", "verify"]).exit_code == 1  # version or --all
    assert run(app, ["rc", "import", VERSION, "--bundle", str(bundle)]).exit_code == 1  # no network
    assert run(app, ["rc", "frobnicate", VERSION]).exit_code == 1
    revoked = run(app, ["rc", "revoke", VERSION, "--reason", "bad build"])
    assert revoked.exit_code == 0 and json.loads(revoked.output)["revoked"]
    assert run(app, ["rc", "revoke", VERSION]).exit_code == 1  # --reason required


# ---- auto-import -----------------------------------------------------------------------------


def _importer(store, listing, source, verifier):
    return auto.AutoImporter(store, source, verifier, lambda: listing, interval=3600.0)


class _Source:
    def __init__(self, wheels):
        self.wheels = wheels

    def bundle(self, version, wheel_sha256):
        return b"{}"

    def wheel(self, version):
        return self.wheels[version]


def test_auto_import_is_off_by_default_and_needs_dolt_server_when_on():
    assert auto.build_loop({}, hq_mode="dolt-server") is None
    assert (
        auto.build_loop({"hq": {"release_catalog": {"auto_import": False}}}, hq_mode="git") is None
    )
    with pytest.raises(auto.AutoImportRefused, match="dolt-server"):
        auto.build_loop({"hq": {"release_catalog": {"auto_import": True}}}, hq_mode="git")


def test_auto_import_does_nothing_until_the_first_manual_import(tmp_path):
    importer = _importer(
        MemoryStore(), ["0.28.5"], _Source({}), FakeVerifier(error=CatalogError("x"))
    )
    assert importer.tick() == []


def test_auto_import_takes_only_newer_final_releases_in_order(tmp_path):
    old, new, newer = _wheel("0.28.5"), _wheel("0.28.6", extra=b"6"), _wheel("0.28.7", extra=b"7")
    store = MemoryStore()
    _import(tmp_path, old, FakeVerifier(_verified(old)), store)

    def result(bundle):
        raise AssertionError

    verifier = FakeVerifier()
    wheels = {"0.28.6": new, "0.28.7": newer}
    verifier.result = None

    class ByVersion(FakeVerifier):
        def verify(self, bundle, *, identity, issuer, trusted_root):
            version = identity.rsplit("@refs/tags/v", 1)[1]
            return _verified(wheels[version], version)

    listing = ["0.28.7", "0.28.6", "0.28.5", "0.28.4", "0.29.0rc1", "0.29.0.dev1", "not-a-version"]
    importer = _importer(store, listing, _Source(wheels), ByVersion())
    assert importer.tick() == ["0.28.6", "0.28.7"]
    assert importer.tick() == []  # already catalogued


def test_auto_import_stops_at_a_refused_version_and_gives_up_after_the_cap(tmp_path):
    old, new = _wheel("0.28.5"), _wheel("0.28.6", extra=b"6")
    store = MemoryStore()
    _import(tmp_path, old, FakeVerifier(_verified(old)), store)
    importer = _importer(
        store,
        ["0.28.6", "0.28.7"],
        _Source({"0.28.6": new}),
        FakeVerifier(error=CatalogError("bad")),
    )
    for _ in range(auto.MAX_ATTEMPTS):
        assert importer.tick() == []
    # 0.28.7 is never attempted past a refused 0.28.6 while 0.28.6 is still being retried.
    assert importer.attempts == {"0.28.6": auto.MAX_ATTEMPTS}
    assert importer.pending() == ["0.28.7"]  # only once 0.28.6 is given up until restart


def test_pypi_listing_skips_yanked_only_releases():
    payload = {
        "releases": {
            "0.28.5": [{"yanked": False}],
            "0.28.6": [{"yanked": True}],
            "0.28.7": [],
        }
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    )
    assert auto.pypi_versions(client) == ["0.28.5"]


# ---- the config key ----------------------------------------------------------------------------


def test_auto_import_is_a_host_local_opt_in_defaulting_to_false():
    from beadhive.config_partition import HOST, partition_of
    from beadhive.modules.config.contracts import HqConfig

    config = HqConfig()
    assert config.release_catalog.auto_import is False
    assert partition_of("hq.release_catalog.auto_import") == HOST
    with pytest.raises(ValueError, match="absolute"):
        HqConfig.model_validate({"release_catalog": {"operator_settings": "relative.yaml"}})
