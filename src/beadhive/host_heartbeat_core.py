"""Signed bounded observations for U2; admission owns authority, never the sender.

The independent FrameLease schema is pinned under schemas/frame. Git signatures
cover source commits; verified observations never confer admission or dispatch.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from . import config, gitref, hosts
from .hq_framelease_contracts import (
    DOMAIN_V2,
    HeartbeatError,
    HeartbeatLease,
)
from .hq_framelease_contracts import (
    ConformanceCheck as ConformanceCheck,
)
from .hq_framelease_contracts import (
    HeartbeatConformance as HeartbeatConformance,
)
from .run import run

MAX_BYTES = 65536


@dataclass(frozen=True)
class ObservationAuthority:
    frame_id: str
    holder_identity: str
    instance_ref: str
    key_fingerprint: str
    epoch: int
    audience: str
    config_revision: str
    candidate_expires_at: float | None = None
    beadyard_id: str | None = None

    def __post_init__(self):
        for value in (
            self.frame_id,
            self.holder_identity,
            self.instance_ref,
            self.key_fingerprint,
            self.audience,
            self.config_revision,
        ):
            if not isinstance(value, str) or not value:
                raise HeartbeatError("authority identity fields must be nonempty strings")
        if type(self.epoch) is not int or self.epoch < 0:
            raise HeartbeatError("authority epoch must be a nonnegative integer")
        if self.candidate_expires_at is not None and (
            type(self.candidate_expires_at) not in (int, float)
            or not math.isfinite(self.candidate_expires_at)
        ):
            raise HeartbeatError("candidate expiry must be finite")
        if self.beadyard_id is not None:
            from .beadyard_identity import parse_id

            parse_id(self.beadyard_id)


@dataclass(frozen=True)
class AuthoritySnapshot:
    """Trusted admission/observer port result; sender bytes cannot supply this.

    The provider must read the current authority source and its durable observation
    receipt atomically. A local checkout or restored observer database is insufficient.
    """

    authority: ObservationAuthority
    checked_at: float
    valid_until: float
    sequence: int
    sha: str
    first_seen: float | None
    lifecycle_state: str = "pending"
    eligible: bool = False
    carrier_ref: str = ""
    alternatives: tuple[AuthoritySnapshot, ...] = ()

    def __post_init__(self):
        if type(self.sequence) is not int or self.sequence < 0:
            raise HeartbeatError("authoritative sequence floor must be nonnegative")
        if self.sequence == 0:
            if self.sha != "" or self.first_seen is not None:
                raise HeartbeatError("new epoch without receipt requires empty SHA and first_seen")
        elif (
            not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64}", self.sha)
            or self.first_seen is None
        ):
            raise HeartbeatError(
                "authoritative receipt requires an exact carrier digest and first_seen"
            )
        timestamps = (self.checked_at, self.valid_until)
        if self.first_seen is not None:
            timestamps += (self.first_seen,)
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in timestamps):
            raise HeartbeatError("authority receipt timestamps must be finite")
        if (
            self.first_seen is not None and self.first_seen > self.checked_at
        ) or self.valid_until <= self.checked_at:
            raise HeartbeatError("authority receipt timestamps are inconsistent")


def load_trusted_authority(hq_dir: Path, identity: str) -> AuthoritySnapshot | None:
    """Internal transport requires its caller to bind protected authority explicitly."""
    raise HeartbeatError("heartbeat core requires an explicit protected authority binding")


@dataclass(frozen=True)
class VerifiedObservation:
    status: str
    verified: bool = False
    fresh: bool = False
    age_seconds: float | None = None
    lease: HeartbeatLease | None = None
    reason: str = ""
    sha: str = ""
    candidate: bool = False
    age_basis: str = ""


def ref_name(identity: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identity):
        raise HeartbeatError("invalid heartbeat identity")
    return f"refs/bh/heartbeat/{identity}"


def _git(args: list[str], cwd: Path, text_input: str | None = None):
    return run(
        ["git", *args],
        cwd=str(cwd),
        capture=True,
        check=False,
        timeout=gitref.GIT_TIMEOUT,
        text_input=text_input,
    )


def _required(args: list[str], cwd: Path, text_input: str | None = None) -> str:
    result = _git(args, cwd, text_input)
    if result.returncode:
        raise HeartbeatError(gitref.message(result))
    return (result.stdout or "").strip()


def load_authority(hq_dir: Path, identity: str) -> ObservationAuthority | None:
    """Read-only inventory seam. Admission must own both registries and their trust."""
    ref_name(identity)
    for directory in ("heartbeat-authorities", "heartbeat-candidates"):
        path = hq_dir / directory / f"{identity}.json"
        if path.exists():
            data = json.loads(path.read_text())
            authority = ObservationAuthority(**data)
            if directory == "heartbeat-candidates" and authority.candidate_expires_at is None:
                raise HeartbeatError("candidate observation authority requires expiry")
            return authority
    return None


def _trust_path(hq_dir: Path, authority: ObservationAuthority | None) -> Path:
    authoritative = Path(_required(["rev-parse", "--git-path", "bh-authority-signers"], hq_dir))
    if not authoritative.is_absolute():
        authoritative = hq_dir / authoritative
    if authoritative.exists():
        return authoritative
    candidate = authority is not None and authority.candidate_expires_at is not None
    return hq_dir / ("candidate_signers" if candidate else "allowed_signers")


def _fingerprint(hq_dir: Path, sha: str, trust: Path) -> str:
    _required(["-c", f"gpg.ssh.allowedSignersFile={trust.resolve()}", "verify-commit", sha], hq_dir)
    return _required(
        ["-c", f"gpg.ssh.allowedSignersFile={trust.resolve()}", "show", "-s", "--format=%GF", sha],
        hq_dir,
    )


def _authority_matches(lease: HeartbeatLease, authority: ObservationAuthority) -> bool:
    return (
        lease.frame_id,
        lease.holderIdentity,
        lease.instance_ref,
        lease.key_id,
        lease.epoch,
        lease.audience,
        lease.config_revision,
        lease.beadyard_id,
    ) == (
        authority.frame_id,
        authority.holder_identity,
        authority.instance_ref,
        authority.key_fingerprint,
        authority.epoch,
        authority.audience,
        authority.config_revision,
        authority.beadyard_id,
    )


def _binding(lease: HeartbeatLease) -> str:
    # Policy/config revision can change without replacing the runtime incarnation.
    values = {
        "frame": lease.frame_id,
        "host": lease.holderIdentity,
        "instance": lease.instance_ref,
        "key": lease.key_id,
        "audience": lease.audience,
    }
    if lease.beadyard_id is not None:
        values["beadyard_id"] = lease.beadyard_id
    return hashlib.sha256(gitref.encode(values).encode()).hexdigest()


def publish(
    hq_dir: Path,
    lease: HeartbeatLease,
    *,
    signing_key: str,
    remote: str = "origin",
    reference: str | None = None,
    transport_options: list[str] | None = None,
    now: float | None = None,
    trusted_authority_lookup: Callable[[Path, str], AuthoritySnapshot | None] | None = None,
    _default_authority_lookup: Callable[[Path, str], AuthoritySnapshot | None] | None = None,
) -> str:
    """CAS a signed parentless commit; never touch index, main or unrelated refs.

    Replacing evidence whose key has been revoked requires the current explicit
    operator authority and a new commit verified to its exact current fingerprint.
    """
    active_reference = ref_name(lease.frame_id or lease.holderIdentity)
    reference = reference or active_reference
    if reference not in {
        active_reference,
        active_reference.replace("/heartbeat/", "/candidate-heartbeat/"),
    }:
        raise HeartbeatError("heartbeat carrier must belong to the granted frame")
    transport = transport_options or []
    expected = (
        gitref.remote_sha(remote, reference, cwd=hq_dir, git_options=transport)
        if transport
        else gitref.remote_sha(remote, reference, cwd=hq_dir)
    )
    authority = load_authority(hq_dir, lease.frame_id or lease.holderIdentity)
    snapshot = None
    if expected or trusted_authority_lookup is not None:
        snapshot = (
            trusted_authority_lookup or _default_authority_lookup or load_trusted_authority
        )(hq_dir, lease.frame_id or lease.holderIdentity)
        at = time.time() if now is None else now
        if not math.isfinite(at):
            raise HeartbeatError("publication clock must be finite")
        if snapshot is None or at < snapshot.checked_at or at >= snapshot.valid_until:
            raise HeartbeatError("current trusted authority unavailable for replacement")
        authority = snapshot.authority
        if not _authority_matches(lease, authority):
            raise HeartbeatError("replacing a beat requires matching current authority")
        if lease.seq <= snapshot.sequence:
            raise HeartbeatError("heartbeat sequence must advance authoritative accepted floor")
    trust = _trust_path(hq_dir, authority)
    if expected:
        _required([*transport, "fetch", "--no-tags", remote, expected], hq_dir)
        try:
            previous_fingerprint = _fingerprint(hq_dir, expected, trust)
        except HeartbeatError:
            # Unknown/revoked signatures cannot establish an epoch or sequence floor.
            # Only the current trusted authority authorizes this exact replacement.
            pass
        else:
            try:
                previous = HeartbeatLease.model_validate_json(
                    _required(["show", f"{expected}:heartbeat.json"], hq_dir)
                )
            except (ValueError, HeartbeatError):
                previous = None
            # A known D16 key can sign ungranted epochs/incarnations. Such rows
            # are quarantined metadata, never a floor that advances authority.
            if previous is not None and _authority_matches(previous, authority):
                if previous.key_id != previous_fingerprint:
                    raise HeartbeatError(
                        "previous heartbeat signer does not match its key identity"
                    )
                if previous.seq >= lease.seq:
                    raise HeartbeatError("heartbeat sequence must advance within the current epoch")
                if _binding(previous) != _binding(lease):
                    raise HeartbeatError("incarnation binding changes require an epoch advance")
    payload = gitref.encode(lease.model_dump(mode="json", exclude_none=True))
    blob = _required(["hash-object", "-w", "--stdin"], hq_dir, payload)
    tree = _required(["mktree"], hq_dir, f"100644 blob {blob}\theartbeat.json\n")
    commit = _required(
        ["-c", "gpg.format=ssh", "-c", f"user.signingkey={signing_key}", "commit-tree", "-S", tree],
        hq_dir,
        f"{lease.domain}\n",
    )
    if snapshot is not None:
        at = time.time() if now is None else now
        if at < snapshot.checked_at or at >= snapshot.valid_until:
            raise HeartbeatError("current trusted authority expired before publication")
        if authority is None or not _authority_matches(lease, authority):
            raise HeartbeatError("replacing a beat requires matching current authority")
        if authority.candidate_expires_at is not None and at >= authority.candidate_expires_at:
            raise HeartbeatError("candidate replacement authority has expired")
        if _fingerprint(hq_dir, commit, trust) != authority.key_fingerprint:
            raise HeartbeatError("replacement signer must match current authority")
    result = _git(
        [
            *transport,
            "push",
            f"--force-with-lease={reference}:{expected}",
            remote,
            f"{commit}:{reference}",
        ],
        hq_dir,
    )
    if result.returncode:
        raise HeartbeatError(gitref.message(result))
    gitref.set_local(reference, commit, cwd=hq_dir)
    return commit


def framelease_envelope(hq_dir: Path, sha: str, trust: Path) -> dict:
    """Project authenticated Git bytes into FrameLease, preserving signature scope.

    This is a transport projection. The SSH signature covers the Git commit, and
    signedObject links that commit to its exact payload; no JSON signature is made.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise HeartbeatError("FrameLease Git carrier requires a SHA-1 object id")
    if int(_required(["cat-file", "-s", sha], hq_dir)) > MAX_BYTES:
        raise HeartbeatError("oversized FrameLease commit")
    if int(_required(["cat-file", "-s", f"{sha}:heartbeat.json"], hq_dir)) > MAX_BYTES:
        raise HeartbeatError("oversized FrameLease payload")
    fingerprint = _fingerprint(hq_dir, sha, trust)
    header = _required(["cat-file", "-p", sha], hq_dir).split("\n\n", 1)[0]
    if (
        "\nparent " in header
        or _required(["ls-tree", "--name-only", sha], hq_dir) != "heartbeat.json"
    ):
        raise HeartbeatError("FrameLease carrier must be a parentless heartbeat commit")
    payload = _required(["show", f"{sha}:heartbeat.json"], hq_dir)
    lease = HeartbeatLease.model_validate_json(payload)
    if lease.key_id != fingerprint:
        raise HeartbeatError("FrameLease signer does not match key_id")
    match = re.search(
        r"gpgsig -----BEGIN SSH SIGNATURE-----\n"
        r"((?: [A-Za-z0-9+/=]+\n)+) -----END SSH SIGNATURE-----",
        header,
    )
    if match is None:
        raise HeartbeatError("FrameLease requires an SSH Git signature")
    spec = lease.model_dump(mode="json", exclude_none=True)
    if json.loads(payload) != spec:
        raise HeartbeatError("FrameLease requires the complete canonical sender payload")
    spec["signature"] = {
        "keyId": fingerprint,
        "algorithm": "ssh",
        "scope": "git-commit",
        "signedObject": sha,
        "value": "".join(line.strip() for line in match[1].splitlines()),
    }
    envelope = {
        "apiVersion": "frame.beadhive.ai/v1alpha2"
        if lease.domain == DOMAIN_V2
        else "frame.beadhive.ai/v1alpha1",
        "kind": "FrameLease",
        "metadata": {"name": lease.frame_id},
        "spec": spec,
    }
    schema_version = "v1alpha2" if lease.domain == DOMAIN_V2 else "v1alpha1"
    schema_path = Path(__file__).parent / f"schemas/frame/{schema_version}/framelease.schema.json"
    schema = json.loads(schema_path.read_text())
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(envelope)
    except ValidationError as exc:
        raise HeartbeatError(f"FrameLease schema mismatch: {exc.message}") from exc
    return envelope


def verify_framelease_envelope(hq_dir: Path, envelope: dict, trust: Path) -> HeartbeatLease:
    """Reject every altered envelope field against its authenticated source bytes."""
    try:
        sha = envelope["spec"]["signature"]["signedObject"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise HeartbeatError("invalid FrameLease signed object")
        if envelope != framelease_envelope(hq_dir, sha, trust):
            raise HeartbeatError("FrameLease envelope does not match authenticated Git payload")
        payload = {k: v for k, v in envelope["spec"].items() if k != "signature"}
        return HeartbeatLease.model_validate(payload)
    except (KeyError, TypeError) as exc:
        raise HeartbeatError("invalid FrameLease envelope") from exc


def _diagnostic(
    lease: HeartbeatLease, sha: str, at: float, status: str, reason: str, candidate: bool = False
) -> VerifiedObservation:
    """Sender age can prove expiry but cannot establish trusted freshness."""
    age = max(0.0, at - lease.observed_at)
    if lease.observed_at > at + 30:
        status = "clock-skew"
    elif age >= lease.leaseDurationSeconds:
        status = "stale"
    return VerifiedObservation(
        status, True, False, age, lease, reason, sha, candidate, age_basis="sender-diagnostic"
    )


def _observation_store(hq_dir: Path, observer_dir: Path | None) -> Path:
    scope = hashlib.sha256(str(hq_dir.resolve()).encode()).hexdigest()[:24]
    root = observer_dir if observer_dir is not None else config.home() / "heartbeat-observations"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{scope}.sqlite3"


def _identity_matches(lease, fingerprint, manifest, authority):
    bindings = (
        (lease.frame_id, authority.frame_id),
        (lease.holderIdentity, authority.holder_identity),
        (lease.instance_ref, authority.instance_ref),
        (lease.key_id, authority.key_fingerprint),
        (fingerprint, authority.key_fingerprint),
        (lease.epoch, authority.epoch),
        (lease.audience, authority.audience),
        (lease.config_revision, authority.config_revision),
        (lease.beadyard_id, authority.beadyard_id),
        (manifest.beadyard_id, authority.beadyard_id),
        (lease.holderIdentity, manifest.host_id),
        (lease.frame_id, manifest.frame_id or manifest.host_id),
    )
    return all(actual == expected for actual, expected in bindings)


def assess_authenticated_observation(
    lease: HeartbeatLease,
    fingerprint: str,
    sha: str,
    manifest: hosts.HostManifest,
    snapshot: AuthoritySnapshot | None,
    *,
    at: float,
    observer_store: Path,
    authority: ObservationAuthority | None = None,
) -> VerifiedObservation:
    """Assess backend-authenticated evidence against one trusted authority/receipt.

    The carrier adapter MUST authenticate the complete envelope and signer before
    calling this policy. No sender, inventory or local cache can supply authority.
    The cache records denials/high-water only; the trusted receipt bounds freshness.
    """
    if type(at) not in (int, float) or not math.isfinite(at):
        return VerifiedObservation("invalid", reason="observer clock must be finite", sha=sha)
    identity = manifest.frame_id or manifest.host_id
    authority = snapshot.authority if snapshot else authority
    if authority is None:
        return _diagnostic(lease, sha, at, "unbound", "current authority unavailable")
    if not _identity_matches(lease, fingerprint, manifest, authority):
        return VerifiedObservation("identity-mismatch", True, lease=lease, sha=sha)
    candidate = authority.candidate_expires_at is not None
    if candidate and at >= authority.candidate_expires_at:
        return VerifiedObservation("candidate-expired", True, lease=lease, sha=sha, candidate=True)
    if lease.observed_at > at + 30:
        return _diagnostic(
            lease, sha, at, "clock-skew", "sender timestamp exceeds skew bound", candidate
        )
    if snapshot is None:
        return _diagnostic(
            lease,
            sha,
            at,
            "authority-unavailable",
            "current authority and durable observer receipt unavailable",
            candidate,
        )
    if at < snapshot.checked_at:
        return VerifiedObservation(
            "replay",
            True,
            lease=lease,
            sha=sha,
            candidate=candidate,
            reason="observer clock precedes trusted receipt",
        )
    if at >= snapshot.valid_until:
        return _diagnostic(
            lease, sha, at, "authority-unavailable", "authority snapshot expired", candidate
        )
    if snapshot.sequence == 0:
        return _diagnostic(
            lease,
            sha,
            at,
            "authority-unavailable",
            "current epoch has no trusted first-observer receipt",
            candidate,
        )
    if lease.seq < snapshot.sequence or (lease.seq == snapshot.sequence and sha != snapshot.sha):
        return VerifiedObservation("replay", True, lease=lease, sha=sha, candidate=candidate)
    if sha != snapshot.sha:
        return VerifiedObservation(
            "authority-unavailable",
            True,
            lease=lease,
            sha=sha,
            candidate=candidate,
            reason="trusted first-observer receipt unavailable",
        )
    with sqlite3.connect(observer_store, timeout=5) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS observations (identity TEXT PRIMARY KEY, "
            "epoch INTEGER, seq INTEGER, sha TEXT, first_seen REAL, "
            "last_checked REAL, binding TEXT)"
        )
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT epoch,seq,sha,first_seen,last_checked,binding "
            "FROM observations WHERE identity=?",
            (identity,),
        ).fetchone()
        if previous and (
            at < previous[4]
            or lease.epoch < previous[0]
            or (
                lease.epoch == previous[0]
                and (
                    lease.seq < previous[1]
                    or (lease.seq == previous[1] and sha != previous[2])
                    or previous[5] != _binding(lease)
                )
            )
        ):
            return VerifiedObservation("replay", True, lease=lease, sha=sha)
        first_seen = snapshot.first_seen
        if previous and sha == previous[2]:
            first_seen = min(first_seen, previous[3])
        age = max(0.0, at - min(first_seen, lease.observed_at))
        db.execute(
            "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?)",
            (identity, lease.epoch, lease.seq, sha, first_seen, at, _binding(lease)),
        )
    fresh = age < lease.leaseDurationSeconds
    return VerifiedObservation(
        "verified" if fresh else "stale",
        True,
        fresh,
        age,
        lease,
        sha=sha,
        candidate=candidate,
        age_basis="trusted-observer",
    )


def observe(
    hq_dir: Path,
    manifest: hosts.HostManifest,
    *,
    now: float | None = None,
    observer_dir: Path | None = None,
    authority_lookup: Callable[[Path, str], ObservationAuthority | None] = load_authority,
    remote: str = "origin",
    transport_options: list[str] | None = None,
    trusted_authority_lookup: Callable[[Path, str], AuthoritySnapshot | None] | None = None,
    _default_authority_lookup: Callable[[Path, str], AuthoritySnapshot | None] | None = None,
) -> VerifiedObservation:
    """Verify remote observation, retaining first-seen/high-water across process restart.

    U3 must separately require admitted inventory and matching approved properties.
    Candidate evidence here never implies permission to route work.
    """
    at = time.time() if now is None else now
    identity = manifest.frame_id or manifest.host_id
    sha = ""
    transport = transport_options or []
    remote_options = {"git_options": transport} if transport else {}
    try:
        if not math.isfinite(at):
            raise HeartbeatError("observer clock must be finite")
        reference = ref_name(identity)
        if not manifest.frame_id and not gitref.read_local(reference, cwd=hq_dir)[0]:
            configured = _git(["remote", "get-url", remote], hq_dir)
            if configured.returncode:
                return VerifiedObservation("absent")
        sha = gitref.remote_sha(remote, reference, cwd=hq_dir, **remote_options)
        if not sha:
            reference = reference.replace("/heartbeat/", "/candidate-heartbeat/")
            sha = gitref.remote_sha(remote, reference, cwd=hq_dir, **remote_options)
            if not sha:
                return VerifiedObservation("absent")
        snapshot = (
            trusted_authority_lookup or _default_authority_lookup or load_trusted_authority
        )(hq_dir, identity)
        if snapshot is not None:
            for selected in (snapshot, *snapshot.alternatives):
                authority = selected.authority
                if authority.holder_identity == manifest.host_id and (
                    manifest.instance_ref is None or authority.instance_ref == manifest.instance_ref
                ):
                    snapshot = selected
                    break
            if snapshot.carrier_ref and snapshot.carrier_ref != reference:
                reference = snapshot.carrier_ref
                sha = gitref.remote_sha(remote, reference, cwd=hq_dir, **remote_options)
                if not sha:
                    return VerifiedObservation("absent")
        if now is None:
            at = time.time()
        _required([*transport, "fetch", "--no-tags", remote, sha], hq_dir)
        if int(_required(["cat-file", "-s", sha], hq_dir)) > MAX_BYTES:
            return VerifiedObservation("invalid", reason="oversized commit", sha=sha)
        header = _required(["cat-file", "-p", sha], hq_dir)
        if not header.startswith("tree ") or "\nparent " in header.split("\n\n", 1)[0]:
            return VerifiedObservation("invalid", reason="not a parentless commit", sha=sha)
        if "\ngpgsig -----BEGIN SSH SIGNATURE-----" not in header.split("\n\n", 1)[0]:
            return VerifiedObservation("unsigned", sha=sha)
        authority = snapshot.authority if snapshot else authority_lookup(hq_dir, identity)
        trust = _trust_path(hq_dir, authority)
        if not trust.is_file():
            return VerifiedObservation("unknown-key", sha=sha)
        verdict = _git(
            ["-c", f"gpg.ssh.allowedSignersFile={trust.resolve()}", "verify-commit", sha], hq_dir
        )
        if verdict.returncode:
            return VerifiedObservation("unknown-key", reason=gitref.message(verdict), sha=sha)
        fingerprint = _required(
            [
                "-c",
                f"gpg.ssh.allowedSignersFile={trust.resolve()}",
                "show",
                "-s",
                "--format=%GF",
                sha,
            ],
            hq_dir,
        )
        entries = _required(["ls-tree", "--name-only", sha], hq_dir)
        if entries != "heartbeat.json":
            return VerifiedObservation("invalid", reason="unexpected heartbeat tree", sha=sha)
        size = int(_required(["cat-file", "-s", f"{sha}:heartbeat.json"], hq_dir))
        if size > MAX_BYTES:
            return VerifiedObservation("invalid", reason="oversized payload", sha=sha)
        lease = HeartbeatLease.model_validate_json(
            _required(["show", f"{sha}:heartbeat.json"], hq_dir)
        )
        # Preserve the Git diagnostic denial before canonical eligibility decoding.
        # A mismatched signed carrier can never reach fresh/eligible assessment.
        if authority is not None and not _identity_matches(lease, fingerprint, manifest, authority):
            return VerifiedObservation("identity-mismatch", True, lease=lease, sha=sha)
        framelease_envelope(hq_dir, sha, trust)
        return assess_authenticated_observation(
            lease,
            fingerprint,
            sha,
            manifest,
            snapshot,
            at=at,
            observer_store=_observation_store(hq_dir, observer_dir),
            authority=authority,
        )
    except (TypeError, ValueError, OverflowError, OSError, RuntimeError, sqlite3.Error) as exc:
        return VerifiedObservation("invalid" if sha else "unreachable", reason=str(exc), sha=sha)
