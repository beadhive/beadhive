"""The signed release catalog: import, verify and revoke (bh-vfrem.4).

``docs/design/frame-release-range-adr.md`` sections 2 and 3. The catalog binds each admitted
``bh`` version to the measured digest the release workflow signed for it (trust anchor (a)):
``bh hq release-catalog import <version>`` fetches the Sigstore bundle and the wheel, verifies the
bundle against the identity policy below, **recomputes** the measured digest from the wheel, and
appends one row to ``hq_release_catalog`` through the separately credentialed
``hq.sql.release_catalog`` binding (INSERT on that table only).

This module is the core half and imports without the optional ``beadhive[release-verify]``
extra: the rules (:func:`check_attestation`, :func:`plan_append`), the wheel measurement, the
storage (:class:`ReleaseCatalogStore`) and the verbs (:func:`import_release`, :func:`verify_rows`,
:func:`revoke_release`) take a :class:`BundleVerifier`. The Sigstore implementation lives in
:mod:`beadhive.hq_release_catalog_sigstore` and is imported lazily by the CLI.

Identity policy, exactly as the ADR states it:

* issuer ``https://token.actions.githubusercontent.com``;
* SAN ``https://github.com/beadhive/beadhive/.github/workflows/release.yml@refs/tags/v<version>``;
* a ``refs/tags/v*`` ref only, so a ``workflow_dispatch`` run on a branch is refused;
* the certificate's source-repository digest equal to the predicate's ``commit``;
* the repository ID checked as well as its name.

Frames never verify anything and never import this module's Sigstore half.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Protocol

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .release_measurement import fold_digest

__all__ = [
    "ALGORITHM",
    "CATALOG",
    "GITHUB_ISSUER",
    "PREDICATE_TYPE",
    "BundleVerifier",
    "CatalogConflict",
    "CatalogEntry",
    "CatalogError",
    "CatalogRow",
    "ComposedSource",
    "FileSource",
    "HttpSource",
    "IdentityPolicy",
    "ReleaseCatalogStore",
    "ReleaseSource",
    "VerifiedAttestation",
    "WheelMeasurement",
    "check_attestation",
    "effective_active",
    "import_release",
    "measure_wheel",
    "normalise_version",
    "plan_append",
    "revoke_release",
    "verify_rows",
]

CATALOG = "beadhive"
ALGORITHM = "beadhive-tree-sha256-v1"
PREDICATE_TYPE = "https://beadhive.ai/attestation/release-manifest/v1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
_PREDICATE_KEYS = frozenset(
    {"version", "measured_digest", "algorithm", "file_count", "wheel_sha256", "commit"}
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_WHEEL_BYTES = 256 * 1024 * 1024
_MAX_BUNDLE_BYTES = 4 * 1024 * 1024
_MAX_MEMBERS = 20_000
_MAX_REASON = 480


class CatalogError(ValueError):
    """An import, verify or revoke was refused; nothing was written."""


class CatalogConflict(CatalogError):
    """The append would break a catalog rule (a second active digest, a revoked digest)."""


# =============================================================================================
# Identity policy and the verifier boundary
# =============================================================================================


@dataclass(frozen=True)
class IdentityPolicy:
    """Who may sign a catalog entry. Defaults are the ADR's; a fork or private build pins its
    own repository and ID explicitly."""

    repository: str = "beadhive/beadhive"
    #: GitHub's numeric repository ID: a rename or a transfer to an attacker-held name changes
    #: the name's meaning but never the ID.
    repository_id: str = "1297186619"
    issuer: str = GITHUB_ISSUER
    workflow: str = ".github/workflows/release.yml"

    def tag_ref(self, version: str) -> str:
        return f"refs/tags/v{version}"

    def san(self, version: str) -> str:
        return f"https://github.com/{self.repository}/{self.workflow}@{self.tag_ref(version)}"

    def repository_uri(self) -> str:
        return f"https://github.com/{self.repository}"


@dataclass(frozen=True)
class VerifiedAttestation:
    """What a :class:`BundleVerifier` proved: the bundle's signature chain verified and the
    certificate matched the expected identity. ``statement`` is the signed in-toto statement;
    the other fields are read from the verified Fulcio certificate."""

    statement: Mapping[str, Any]
    issuer: str
    san: str
    repository_uri: str
    repository_id: str
    ref: str
    source_digest: str


class BundleVerifier(Protocol):
    """The Sigstore boundary. ``identity`` and ``issuer`` are the exact values the certificate
    must carry; ``trusted_root`` (a file) selects offline verification against a pinned root,
    ``None`` refreshes the TUF root online. Raises :class:`CatalogError` on any failure."""

    def verify(
        self, bundle: bytes, *, identity: str, issuer: str, trusted_root: Path | None
    ) -> VerifiedAttestation: ...


# =============================================================================================
# Wheel measurement (the shared fold in release_measurement)
# =============================================================================================


@dataclass(frozen=True)
class WheelMeasurement:
    digest: str
    file_count: int
    name: str
    version: str


def measure_wheel(wheel: bytes) -> WheelMeasurement:
    """Recompute the measured digest from the wheel itself, and read its metadata."""
    if len(wheel) > _MAX_WHEEL_BYTES:
        raise CatalogError("wheel is larger than the import limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(wheel))
    except zipfile.BadZipFile:
        raise CatalogError("wheel is not a valid zip archive") from None
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        if len(infos) > _MAX_MEMBERS or len(set(names)) != len(names):
            raise CatalogError("wheel has too many or duplicate members")
        if sum(info.file_size for info in infos) > 4 * _MAX_WHEEL_BYTES:
            raise CatalogError("wheel expands beyond the import limit")
        entries = [(info.filename, archive.read(info)) for info in infos]
        metadata = [n for n in names if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
        if len(metadata) != 1:
            raise CatalogError("wheel has no single dist-info METADATA")
        headers = BytesParser().parsebytes(archive.read(metadata[0]), headersonly=True)
    digest, count = fold_digest(entries)
    if not count:
        raise CatalogError("wheel holds no beadhive/ package files")
    return WheelMeasurement(
        digest, count, canonicalize_name(headers.get("Name", "")), headers.get("Version", "")
    )


def normalise_version(version: str) -> str:
    """The canonical PEP 440 form; an alias spelling is refused, never silently rewritten."""
    try:
        canonical = str(Version(version))
    except (InvalidVersion, TypeError):
        raise CatalogError("version is not a PEP 440 version") from None
    if canonical != version or not re.fullmatch(r"[0-9A-Za-z.+!-]{1,64}", version):
        raise CatalogError(f"version must be written in canonical form ({canonical})")
    return canonical


# =============================================================================================
# The rules (pure)
# =============================================================================================


@dataclass(frozen=True)
class CatalogEntry:
    version: str
    measured_digest: str
    algorithm: str
    wheel_sha256: str
    commit: str
    bundle: bytes
    catalog: str = CATALOG


@dataclass(frozen=True)
class CatalogRow:
    catalog: str
    version: str
    measured_digest: str
    status: str
    algorithm: str
    wheel_sha256: str
    commit: str
    bundle: bytes
    reason: str
    recorded_at: float


def check_attestation(
    verified: VerifiedAttestation,
    *,
    version: str,
    wheel: bytes,
    bundle: bytes,
    policy: IdentityPolicy,
) -> CatalogEntry:
    """Every check section 3 requires after the signature chain verified, in order.

    Refuses a bad issuer or SAN, a non-tag ref, a repository name or ID mismatch, a statement
    whose subject is not this wheel, a certificate source digest that differs from the predicate's
    commit, and a predicate digest that differs from the digest recomputed from the wheel."""
    version = normalise_version(version)
    if verified.issuer != policy.issuer:
        raise CatalogError("certificate issuer is not the expected OIDC issuer")
    if verified.san != policy.san(version):
        raise CatalogError("certificate identity is not the release workflow for this version")
    if not verified.ref.startswith("refs/tags/v"):
        raise CatalogError("certificate ref is not a refs/tags/v* tag (branch runs are refused)")
    if verified.ref != policy.tag_ref(version):
        raise CatalogError("certificate tag does not match the requested version")
    if verified.repository_uri != policy.repository_uri():
        raise CatalogError("certificate repository is not the expected repository")
    if verified.repository_id != policy.repository_id:
        raise CatalogError("certificate repository ID is not the expected repository ID")
    statement = verified.statement
    if statement.get("_type") != STATEMENT_TYPE or statement.get("predicateType") != PREDICATE_TYPE:
        raise CatalogError("statement is not a beadhive release-manifest attestation")
    predicate = statement.get("predicate")
    if not isinstance(predicate, Mapping) or set(predicate) != _PREDICATE_KEYS:
        raise CatalogError("release-manifest predicate has unexpected fields")
    wheel_sha = hashlib.sha256(wheel).hexdigest()
    subjects = statement.get("subject")
    if (
        not isinstance(subjects, list)
        or len(subjects) != 1
        or not isinstance(subjects[0], Mapping)
        or (subjects[0].get("digest") or {}).get("sha256") != wheel_sha
    ):
        raise CatalogError("statement subject is not this wheel (sha256 mismatch)")
    commit = predicate["commit"]
    if (
        predicate["version"] != version
        or predicate["wheel_sha256"] != wheel_sha
        or predicate["algorithm"] != ALGORITHM
        or not isinstance(commit, str)
        or not _COMMIT.fullmatch(commit)
        or not isinstance(predicate["measured_digest"], str)
        or not _DIGEST.fullmatch(predicate["measured_digest"])
    ):
        raise CatalogError("predicate does not describe this version and wheel")
    if verified.source_digest != commit:
        raise CatalogError("certificate source digest differs from the predicate commit")
    measured = measure_wheel(wheel)
    if measured.name != "beadhive" or measured.version != version:
        raise CatalogError("wheel metadata is not beadhive at the requested version")
    if (
        measured.digest != predicate["measured_digest"]
        or measured.file_count != predicate["file_count"]
    ):
        raise CatalogError("digest recomputed from the wheel differs from the signed predicate")
    return CatalogEntry(version, measured.digest, ALGORITHM, wheel_sha, commit, bundle)


def effective_active(rows: Sequence[CatalogRow], catalog: str, version: str) -> set[str]:
    """The digests of ``version`` with an ``active`` row and no ``revoked`` tombstone."""
    mine = [row for row in rows if (row.catalog, row.version) == (catalog, version)]
    revoked = {row.measured_digest for row in mine if row.status == "revoked"}
    return {r.measured_digest for r in mine if r.status == "active"} - revoked


def plan_append(rows: Sequence[CatalogRow], entry: CatalogEntry) -> str:
    """``"insert"`` or ``"noop"`` for ``entry`` against the rows of its version; a revoked
    digest or a second, different active digest raises :class:`CatalogConflict`."""
    mine = [row for row in rows if (row.catalog, row.version) == (entry.catalog, entry.version)]
    if any(r.status == "revoked" and r.measured_digest == entry.measured_digest for r in mine):
        raise CatalogConflict("this digest was revoked for the version; it cannot be re-activated")
    active = effective_active(rows, entry.catalog, entry.version)
    if entry.measured_digest in active:
        return "noop"
    if active:
        raise CatalogConflict(
            f"version {entry.version} already has a different active digest; revoke it first"
        )
    return "insert"


# =============================================================================================
# Sources: where the bundle and the wheel come from
# =============================================================================================


class ReleaseSource(Protocol):
    def bundle(self, version: str, wheel_sha256: str) -> bytes: ...

    def wheel(self, version: str) -> bytes: ...


@dataclass(frozen=True)
class FileSource:
    """A bundle and a wheel already on disk (offline import)."""

    bundle_path: Path
    wheel_path: Path

    def bundle(self, version: str, wheel_sha256: str) -> bytes:
        return _read(self.bundle_path, _MAX_BUNDLE_BYTES)

    def wheel(self, version: str) -> bytes:
        return _read(self.wheel_path, _MAX_WHEEL_BYTES)


def _read(path: Path, limit: int) -> bytes:
    try:
        if path.stat().st_size > limit:
            raise CatalogError(f"{path.name} is larger than the import limit")
        return path.read_bytes()
    except OSError as exc:
        raise CatalogError(f"cannot read {path.name}: {exc.strerror or exc}") from None


@dataclass
class HttpSource:
    """The release's bundle from GitHub's attestation API and its wheel from PyPI, https only.

    ``client`` is an ``httpx.Client`` (tests inject a ``MockTransport``); the wheel's integrity
    needs no trust in PyPI's JSON because the statement's subject must equal its sha256."""

    client: Any
    repository: str = "beadhive/beadhive"
    pypi_url: str = "https://pypi.org/pypi/beadhive"
    api_url: str = "https://api.github.com"
    _wheels: dict[str, bytes] = field(default_factory=dict)

    def _get(self, url: str, limit: int) -> bytes:
        if not url.startswith("https://"):
            raise CatalogError("release assets are fetched over https only")
        try:
            response = self.client.get(url, follow_redirects=True)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - never echo URLs with credentials or bodies
            raise CatalogError(f"fetch failed ({type(exc).__name__})") from None
        if len(response.content) > limit:
            raise CatalogError("fetched asset is larger than the import limit")
        return response.content

    def bundle(self, version: str, wheel_sha256: str) -> bytes:
        """The release-manifest bundle from GitHub's attestation service, keyed by the wheel's
        sha256 (public repository, no credential). There is no release asset (operator decision
        2026-10-22). Only the bundle whose predicate type is the release manifest is taken."""
        if not _HEX64.fullmatch(wheel_sha256):
            raise CatalogError("wheel sha256 is malformed")
        url = f"{self.api_url}/repos/{self.repository}/attestations/sha256:{wheel_sha256}"
        body = self._get(url, 16 * 1024 * 1024)
        try:
            listing = json.loads(body)
            candidates = listing["attestations"]
        except (ValueError, KeyError, TypeError):
            raise CatalogError("attestation service response is malformed") from None
        for item in candidates:
            try:
                payload = base64.b64decode(item["bundle"]["dsseEnvelope"]["payload"])
                if json.loads(payload).get("predicateType") == PREDICATE_TYPE:
                    return json.dumps(item["bundle"]).encode()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        raise CatalogError("no release-manifest attestation is recorded for this wheel")

    def wheel(self, version: str) -> bytes:
        meta = json.loads(self._get(f"{self.pypi_url}/{version}/json", 4 * 1024 * 1024))
        wheels = [item for item in meta.get("urls", []) if item.get("packagetype") == "bdist_wheel"]
        if len(wheels) != 1:
            raise CatalogError("PyPI does not list exactly one wheel for this version")
        return self._get(str(wheels[0]["url"]), _MAX_WHEEL_BYTES)


@dataclass
class ComposedSource:
    """A local ``--bundle`` and/or ``--wheel`` (air-gapped), else the remote for that half."""

    remote: Callable[[], ReleaseSource]
    bundle_path: Path | None = None
    wheel_path: Path | None = None

    def bundle(self, version: str, wheel_sha256: str) -> bytes:
        if self.bundle_path is not None:
            return _read(self.bundle_path, _MAX_BUNDLE_BYTES)
        return self.remote().bundle(version, wheel_sha256)

    def wheel(self, version: str) -> bytes:
        if self.wheel_path is not None:
            return _read(self.wheel_path, _MAX_WHEEL_BYTES)
        return self.remote().wheel(version)


# =============================================================================================
# Storage: the catalog binding (INSERT on hq_release_catalog only)
# =============================================================================================


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes | bytearray | memoryview) else str(value)


def _row(values: Sequence[Any]) -> CatalogRow:
    (catalog, version, digest, status, algorithm, wheel, commit, bundle, reason, at) = values
    return CatalogRow(
        _text(catalog),
        _text(version),
        _text(digest),
        _text(status),
        _text(algorithm),
        _text(wheel),
        _text(commit),
        bytes(bundle),
        _text(reason),
        float(at),
    )


class ReleaseCatalogStore:
    """The catalog over one ``hq.sql.release_catalog`` binding (an ``HqSqlConnection`` dict).

    Every operation owns one connection. A write checks the connection is the binding's user on
    its database's ``main`` branch and that the working tree is clean, so the commit it makes
    holds exactly the appended rows (the restore floor accepts nothing else)."""

    def __init__(
        self,
        binding: Mapping[str, Any],
        *,
        broker: Any = None,
        clock: Callable[[], float] = time.time,
        connect: Callable[..., Any] | None = None,
    ):
        self._binding = dict(binding)
        self._broker = broker
        self._clock = clock
        self._connect = connect

    def _open(self):
        from .hq_sql_transport import FnoxBroker, SqlTransportError
        from .hq_sql_transport import connect as transport_connect

        connect = self._connect or transport_connect
        deadline = time.monotonic() + float(self._binding.get("operation_timeout", 15))
        try:
            return connect(self._binding, self._broker or FnoxBroker(), deadline=deadline)
        except SqlTransportError:
            raise CatalogError("release catalog connection unavailable") from None

    def _checked(self, connection):
        from .hq_storage_composition import runtime_statements

        sql = runtime_statements(connection.cursor())
        user, database, branch, _version = sql.identity()
        if (
            not str(user).startswith(self._binding["user"] + "@")
            or database != self._binding["database"]
            or branch != "main"
        ):
            raise CatalogError("release catalog principal or endpoint mismatch")
        return sql

    def rows(self, version: str | None = None) -> list[CatalogRow]:
        connection = self._open()
        try:
            sql = self._checked(connection)
            result = sql.catalog_rows(CATALOG, version)
            connection.rollback()
            return [_row(values) for values in result]
        except CatalogError:
            raise
        except Exception:
            raise CatalogError("release catalog read unavailable") from None
        finally:
            connection.close()

    def _append(self, version: str, *, message: str, check):
        """Insert what ``check(rows)`` returns for the version's rows and commit it alone
        (``[]`` is a no-op)."""
        connection = self._open()
        try:
            sql = self._checked(connection)
            sql.begin()
            if sql.status_tables():
                raise CatalogError("committed working tree is not clean; refusing to append")
            todo = check([_row(values) for values in sql.catalog_rows(CATALOG, version)])
            if not todo:
                connection.rollback()
                return []
            for row in todo:
                sql.catalog_insert(
                    (
                        row.catalog,
                        row.version,
                        row.measured_digest,
                        row.status,
                        row.algorithm,
                        row.wheel_sha256,
                        row.commit,
                        row.bundle,
                        row.reason,
                        row.recorded_at,
                    )
                )
            connection.commit()
            sql.commit_table("hq_release_catalog", message)
            connection.commit()
            return todo
        except CatalogError:
            connection.rollback()
            raise
        except Exception:
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise CatalogError(
                "release catalog append failed or is unconfirmed; inspect the catalog before "
                "retrying (a dirty working tree blocks the next append)"
            ) from None
        finally:
            connection.close()

    def add(self, entry: CatalogEntry) -> str:
        """Append ``entry`` unless it is already active: ``"inserted"`` or ``"unchanged"``."""
        row = CatalogRow(
            entry.catalog,
            entry.version,
            entry.measured_digest,
            "active",
            entry.algorithm,
            entry.wheel_sha256,
            entry.commit,
            entry.bundle,
            "",
            self._clock(),
        )

        def check(rows):
            return [row] if plan_append(rows, entry) == "insert" else []

        done = self._append(
            entry.version, message=f"release catalog: import {entry.version}", check=check
        )
        return "inserted" if done else "unchanged"

    def revoke(self, version: str, reason: str) -> list[str]:
        """Append a tombstone for every active digest of ``version``; returns those digests."""
        if not reason or len(reason) > _MAX_REASON or reason != reason.strip():
            raise CatalogError(f"a revocation reason of 1 to {_MAX_REASON} characters is required")

        def check(rows):
            active = effective_active(rows, CATALOG, version)
            if not active:
                raise CatalogError(f"version {version} has no active catalog entry")
            now = self._clock()
            tombstones = []
            for digest in sorted(active):
                source = next(
                    r
                    for r in rows
                    if r.status == "active" and r.measured_digest == digest and r.version == version
                )
                tombstones.append(
                    CatalogRow(
                        CATALOG,
                        version,
                        digest,
                        "revoked",
                        source.algorithm,
                        source.wheel_sha256,
                        source.commit,
                        b"",
                        reason,
                        now,
                    )
                )
            return tombstones

        done = self._append(version, message=f"release catalog: revoke {version}", check=check)
        return [row.measured_digest for row in done]


# =============================================================================================
# The verbs
# =============================================================================================


def import_release(
    version: str,
    *,
    source: ReleaseSource,
    verifier: BundleVerifier,
    store: ReleaseCatalogStore,
    policy: IdentityPolicy | None = None,
    trusted_root: Path | None = None,
) -> dict[str, str]:
    """Fetch, verify, recompute and append one release. Nothing is written on any refusal."""
    policy = policy or IdentityPolicy()
    version = normalise_version(version)
    wheel = source.wheel(version)
    bundle = source.bundle(version, hashlib.sha256(wheel).hexdigest())
    verified = verifier.verify(
        bundle, identity=policy.san(version), issuer=policy.issuer, trusted_root=trusted_root
    )
    entry = check_attestation(verified, version=version, wheel=wheel, bundle=bundle, policy=policy)
    outcome = store.add(entry)
    return {
        "version": version,
        "measured_digest": entry.measured_digest,
        "commit": entry.commit,
        "result": outcome,
    }


def revoke_release(version: str, reason: str, *, store: ReleaseCatalogStore) -> dict[str, Any]:
    """Append a tombstone. The binding may revoke because revoking only fails safe; a PyPI yank
    never revokes, that stays an explicit operator decision."""
    version = normalise_version(version)
    digests = store.revoke(version, reason)
    return {"version": version, "revoked": digests, "reason": reason}


def verify_rows(
    *,
    store: ReleaseCatalogStore,
    verifier: BundleVerifier,
    version: str | None = None,
    policy: IdentityPolicy | None = None,
    trusted_root: Path | None = None,
) -> dict[str, Any]:
    """Re-verify the stored bundles of every effectively active row, with no fetch.

    Checks the identity policy and that the signed statement still says what the row says
    (version, digest, algorithm, wheel hash, commit). It cannot recompute the digest, because
    the catalog stores the bundle, not the wheel; import did that once."""
    policy = policy or IdentityPolicy()
    rows = store.rows(normalise_version(version) if version else None)
    results, failed = [], 0
    for key in sorted({(r.version, r.measured_digest) for r in rows if r.status == "active"}):
        entry_version, digest = key
        if digest not in effective_active(rows, CATALOG, entry_version):
            results.append({"version": entry_version, "status": "revoked", "ok": True})
            continue
        row = next(
            r for r in rows if r.status == "active" and (r.version, r.measured_digest) == key
        )
        problem = _reverify(row, verifier, policy, trusted_root)
        failed += bool(problem)
        results.append(
            {"version": entry_version, "status": "active", "ok": problem is None, "error": problem}
        )
    return {"verified": len(results) - failed, "failed": failed, "entries": results}


def _reverify(row: CatalogRow, verifier, policy: IdentityPolicy, trusted_root) -> str | None:
    try:
        verified = verifier.verify(
            row.bundle,
            identity=policy.san(row.version),
            issuer=policy.issuer,
            trusted_root=trusted_root,
        )
        if (
            verified.issuer != policy.issuer
            or verified.san != policy.san(row.version)
            or verified.ref != policy.tag_ref(row.version)
            or verified.repository_uri != policy.repository_uri()
            or verified.repository_id != policy.repository_id
        ):
            return "certificate identity differs from the policy"
        statement = verified.statement
        predicate = statement.get("predicate") or {}
        subjects = statement.get("subject") or []
        if (
            statement.get("predicateType") != PREDICATE_TYPE
            or len(subjects) != 1
            or (subjects[0].get("digest") or {}).get("sha256") != row.wheel_sha256
            or predicate.get("version") != row.version
            or predicate.get("measured_digest") != row.measured_digest
            or predicate.get("algorithm") != row.algorithm
            or predicate.get("commit") != row.commit
            or verified.source_digest != row.commit
        ):
            return "stored bundle no longer matches the catalog row"
    except CatalogError as exc:
        return str(exc)
    return None
