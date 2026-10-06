"""Committed Git fleet documents behind the configuration revision port.

This adapter validates ordered raw documents with the pure shared contract before
publication and on immutable read. It does not load a working checkout, mutate
Beads, or infer frame admission.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict

from . import gitref, hq_authority_ceiling
from . import hq_authority_guard as guard
from .beadyard_identity import (
    BeadyardIdentityError,
    identity_in_documents,
    validate_publication_identity,
)
from .beadyard_identity_file import read_identity
from .hq_document_validation import (
    DocumentValidationError,
    validate_documents,
    validate_repair_carrier,
)
from .modules.config.domain.ports import (
    FleetConfigDocument,
    FleetConfigSnapshot,
    RawFleetConfigRevision,
)


class FleetConfigError(ValueError):
    """A committed configuration revision could not be read or published."""


class GitFleetConfigRevisionStore:
    """Operator-signed config head plus immutable anti-rollback witnesses."""

    def __init__(self, plane, git, *, operator_key=None, duration=3600, ceiling=None):
        self.plane, self.git = plane, git
        self.operator_key, self.duration, self.ceiling = operator_key, duration, ceiling

    def _read(self, *, allow_expired=False, allow_legacy_bound=False):
        plane, git = self.plane, self.git
        policy = plane._policy()
        remote = plane._remote(policy)
        options = ("-c", "protocol.ext.allow=always")
        rows = git(
            plane.hq_dir,
            *options,
            "ls-remote",
            remote,
            guard.CONFIG_HEAD,
            guard.CONFIG_WITNESS + "*",
        ).splitlines()
        refs = dict(row.split()[::-1] for row in rows)
        sha = refs.get(guard.CONFIG_HEAD, "")
        witnesses = sorted(ref for ref in refs if ref.startswith(guard.CONFIG_WITNESS))
        if not sha:
            if witnesses:
                raise FleetConfigError("configuration head missing with retained witnesses")
            return "", {}, policy
        git(plane.hq_dir, *options, "fetch", "--no-tags", remote, sha)
        git(
            plane.hq_dir,
            "-c",
            f"gpg.ssh.allowedSignersFile={policy['operator_signers']}",
            "-c",
            f"gpg.ssh.program={policy['executables']['ssh_keygen']['path']}",
            "verify-commit",
            sha,
        )
        if (
            git(plane.hq_dir, "ls-tree", "--name-only", sha) != "config.json"
            or int(git(plane.hq_dir, "cat-file", "-s", f"{sha}:config.json")) > 4 * 1024 * 1024
        ):
            raise FleetConfigError("invalid configuration carrier")
        import json

        state = json.loads(git(plane.hq_dir, "show", f"{sha}:config.json"))
        guard.validate_config_state(state)
        try:
            committed_id = identity_in_documents(
                (FleetConfigDocument(**doc) for doc in state["documents"]), required=False
            )
            local_id = read_identity(plane.hq_dir)
        except BeadyardIdentityError:
            raise FleetConfigError("committed or local beadyard identity invalid") from None
        if committed_id != local_id:
            if not (allow_legacy_bound and committed_id is None and local_id is not None):
                raise FleetConfigError("committed beadyard identity conflicts with local HQ")
        if (
            not witnesses
            or witnesses[-1] != f"{guard.CONFIG_WITNESS}{state['revision']:020d}"
            or refs[witnesses[-1]] != sha
        ):
            raise FleetConfigError("configuration rollback or inconsistent witness")
        if state["generation"] != policy["generation"] or plane.clock() < state["issued_at"] - 30:
            raise FleetConfigError("configuration generation/clock mismatch")
        if not allow_expired and plane.clock() >= state["expires_at"]:
            raise FleetConfigError("configuration validity expired")
        return sha, state, policy

    def _snapshot(self, sha, state, policy):
        documents = tuple(FleetConfigDocument(**doc) for doc in state["documents"])
        try:
            validate_documents(documents)
        except DocumentValidationError as exc:
            raise FleetConfigError(str(exc)) from None
        identity = hashlib.sha256(
            (policy["server_root"] + "\0" + policy["client"]["policy_digest"]).encode()
        ).hexdigest()
        return FleetConfigSnapshot(
            backend_identity="git:" + identity,
            commit_revision=sha,
            generation=state["generation"],
            fetched_at=self.plane.clock(),
            valid_until=state["expires_at"],
            documents=documents,
        )

    def load_snapshot(self, *, revision=None):
        sha, state, policy = self._read()
        if not sha:
            raise FleetConfigError("committed fleet configuration has not been published")
        # Config authority reads current committed head; historical export is a separate operation.
        if revision is not None and revision != sha:
            raise FleetConfigError("requested configuration revision is no longer current")
        return self._snapshot(sha, state, policy)

    def inspect_raw_for_repair(self):
        """Expose the signed original carrier only to the operator repair capability."""
        if not self.operator_key:
            raise FleetConfigError("configuration repair requires operator custody")
        sha, state, policy = self._read(allow_expired=True)
        if not sha or policy["client"]["role"] != "operator":
            raise FleetConfigError("configuration repair requires operator custody")
        documents = tuple(FleetConfigDocument(**doc) for doc in state["documents"])
        try:
            validate_repair_carrier(documents)
        except DocumentValidationError as exc:
            raise FleetConfigError(str(exc)) from None
        return RawFleetConfigRevision(sha, documents)

    def publish_snapshot(
        self, documents, *, expected_revision, explicit_adoption=False, publication_id=None
    ):
        if publication_id is not None:
            try:
                parsed_id = uuid.UUID(publication_id)
                if parsed_id.version != 4 or str(parsed_id) != publication_id:
                    raise ValueError()
            except (TypeError, ValueError, AttributeError):
                raise FleetConfigError("configuration publication ID invalid") from None
        try:
            validate_documents(documents)
        except DocumentValidationError as exc:
            raise FleetConfigError(str(exc)) from None
        if not self.operator_key:
            raise FleetConfigError(
                "configuration publication requires operator key/bounded validity"
            )
        try:
            hq_authority_ceiling.check_duration(self.duration, self.ceiling)
        except ValueError as exc:
            raise FleetConfigError(str(exc)) from None
        previous_sha, previous, policy = self._read(
            allow_expired=True, allow_legacy_bound=explicit_adoption
        )
        if policy["client"]["role"] != "operator":
            raise FleetConfigError("configuration publication requires operator custody")
        if previous_sha != expected_revision:
            raise FleetConfigError("expected configuration revision changed")
        try:
            proposed_id = validate_publication_identity(
                tuple(FleetConfigDocument(**doc) for doc in previous.get("documents", ())),
                documents,
                local_id=read_identity(self.plane.hq_dir),
                explicit_adoption=explicit_adoption,
            )
        except BeadyardIdentityError:
            raise FleetConfigError("beadyard identity publication conflict") from None
        now = self.plane.clock()
        state = {
            "domain": guard.CONFIG_DOMAIN_V2 if proposed_id is not None else guard.CONFIG_DOMAIN,
            "generation": policy["generation"],
            "revision": previous.get("revision", 0) + 1,
            "issued_at": now,
            "expires_at": now + self.duration,
            "documents": [asdict(document) for document in documents],
        }
        guard.validate_config_state(state)
        encoded = gitref.encode(state)
        if len(encoded.encode()) > 4 * 1024 * 1024:
            raise FleetConfigError("oversized configuration snapshot")
        git, directory = self.git, self.plane.hq_dir
        blob = git(directory, "hash-object", "-w", "--stdin", data=encoded)
        tree = git(directory, "mktree", data=f"100644 blob {blob}\tconfig.json\n")
        args = [
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={self.operator_key}",
            "commit-tree",
            "-S",
            tree,
        ]
        if expected_revision:
            args += ["-p", expected_revision]
        message = f"Fleet configuration revision {state['revision']}\n"
        if publication_id is not None:
            message += f"\nHQ export publication ID: {publication_id}\n"
        sha = git(directory, *args, data=message)
        witness = f"{guard.CONFIG_WITNESS}{state['revision']:020d}"
        git(
            directory,
            "-c",
            "protocol.ext.allow=always",
            "push",
            "--atomic",
            f"--force-with-lease={guard.CONFIG_HEAD}:{expected_revision}",
            f"--force-with-lease={witness}:",
            self.plane._remote(policy),
            f"{sha}:{guard.CONFIG_HEAD}",
            f"{sha}:{witness}",
        )
        # A successful transport/row update alone is not publication; verify committed head.
        return self.load_snapshot(revision=sha)
