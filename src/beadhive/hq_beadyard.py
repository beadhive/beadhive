"""Explicit HQ instance identity inspection and one-time legacy adoption.

Reads never mint an ID. Git adoption writes the portable main-tree document
after checking the caller's observed main HEAD. SQL adoption publishes the same
raw document under the original committed revision CAS, without creating a Git
or Beads HQ store on a config-only host.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import config, hq_authority_enforce
from .beadyard_identity import DOCUMENT_PATH, BeadyardIdentityError, parse_document, parse_id
from .beadyard_identity_file import create_identity, read_identity
from .modules.config.domain.ports import FleetConfigDocument


class BeadyardOperationError(ValueError):
    """Identity inspection or explicit adoption lacked a stable trusted original."""


@dataclass(frozen=True)
class BeadyardStatus:
    backend: str
    state: str
    beadyard_id: str | None
    revision: str | None


def _git_adoption_paths(root: Path) -> tuple[Path, Path]:
    tag = hashlib.sha256(str(root.absolute()).encode()).hexdigest()[:20]
    return (
        root.parent / f".beadyard-adopt-{tag}.lock",
        root.parent / f".beadyard-adopt-{tag}.pending",
    )


def _git_adoption_receipt(root: Path) -> Path:
    return _git_adoption_paths(root)[1].with_suffix(".done")


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _adoption_record(marker: Path) -> dict[str, object] | None:
    try:
        fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise BeadyardOperationError("HQ adoption intent is unreadable") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 1024
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise BeadyardOperationError("HQ adoption intent is invalid")
        content = os.read(fd, 1025)
    finally:
        os.close(fd)
    try:
        record = json.loads(content)
    except (UnicodeError, ValueError):
        raise BeadyardOperationError("HQ adoption intent is invalid") from None
    if not isinstance(record, dict) or set(record) != {
        "expected_revision",
        "config_revision",
        "config_sequence",
        "documents_digest",
        "key_digest",
    }:
        raise BeadyardOperationError("HQ adoption intent is invalid")
    expected = record["expected_revision"]
    if (
        not isinstance(expected, str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected)
        or not isinstance(record["config_revision"], str)
        or record["config_revision"]
        and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["config_revision"])
        or type(record["config_sequence"]) is not int
        or record["config_sequence"] < 0
        or any(
            not isinstance(record[key], str) or not re.fullmatch(r"[0-9a-f]{64}", record[key])
            for key in ("documents_digest", "key_digest")
        )
    ):
        raise BeadyardOperationError("HQ adoption intent is invalid")
    return record


def _pending_original(root: Path) -> dict[str, object] | None:
    return _adoption_record(_git_adoption_paths(root)[1])


def _completed_original(root: Path) -> dict[str, object] | None:
    return _adoption_record(_git_adoption_receipt(root))


@contextmanager
def _git_adoption_lock(root: Path):
    lock, _marker = _git_adoption_paths(root)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _docs_digest(documents: object) -> str:
    from . import gitref

    return hashlib.sha256(gitref.encode(documents).encode()).hexdigest()


def _operator_public(operator_key: Path) -> bytes:
    try:
        return Path(f"{operator_key}.pub").read_bytes()
    except OSError:
        raise BeadyardOperationError("operator public signing key unavailable") from None


def _start_git_adoption(root: Path, expected_revision: str, operator_key: Path) -> None:
    _lock, marker = _git_adoption_paths(root)
    existing = _pending_original(root)
    if existing is not None:
        raise BeadyardOperationError("HQ adoption intent already exists")
    if _completed_original(root) is not None:
        raise BeadyardOperationError("HQ identity adoption already completed")
    protected = _protected_git_adoption_state(root, read_identity(root))
    if protected is not None:
        from .hq_control_plane import fingerprint

        policy = protected[0]._policy()
        if (
            fingerprint(_operator_public(operator_key).decode())
            not in policy["operator_fingerprints"]
        ):
            raise BeadyardOperationError("operator signing key is not protected HQ custody")
    config_revision = protected[1] if protected else ""
    config_state = protected[2] if protected else {}
    record = {
        "expected_revision": expected_revision,
        "config_revision": config_revision,
        "config_sequence": config_state.get("revision", 0),
        "documents_digest": _docs_digest(config_state.get("documents", [])),
        "key_digest": hashlib.sha256(_operator_public(operator_key)).hexdigest(),
    }
    fd = os.open(
        marker,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(fd, json.dumps(record, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(root.parent)


def _finish_git_adoption(root: Path) -> None:
    _lock, marker = _git_adoption_paths(root)
    completed = _git_adoption_receipt(root)
    if _completed_original(root) is not None:
        raise BeadyardOperationError("HQ identity adoption receipt already exists")
    os.replace(marker, completed)
    _sync_directory(root.parent)


def _selected_store(hq_dir: Path):
    from .hq_control_plane import SqlControlPlane, control_plane

    plane = control_plane(hq_dir)
    if isinstance(plane, SqlControlPlane):
        return "dolt-server", plane.config_store()
    return "git", plane.config_store()


def inspect(hq_dir: Path | None = None) -> BeadyardStatus:
    """Distinguish unavailable authority, absent setup, legacy and bound HQs."""
    root = Path(hq_dir or config.hq_dir())
    backend, store = _selected_store(root)
    if backend == "dolt-server":
        snapshot = store.load_snapshot()
        owner = snapshot.beadyard_id
        return BeadyardStatus(
            backend, "bound" if owner else "legacy", owner, snapshot.commit_revision
        )
    if not (root / ".git").exists():
        return BeadyardStatus(backend, "absent", None, None)
    from .hq import _git

    current = _git(["rev-parse", "main"], root)
    if current.returncode:
        raise BeadyardOperationError("HQ main revision unavailable")
    tree = _git(["show", f"main:{DOCUMENT_PATH}"], root)
    committed = None
    if tree.returncode == 0:
        try:
            committed = parse_document(tree.stdout)
        except BeadyardIdentityError:
            raise BeadyardOperationError("HQ main identity document invalid") from None
    owner = read_identity(root)
    pending = _pending_original(root)
    if committed is not None and committed != owner:
        raise BeadyardOperationError("HQ main identity differs from local HQ document")
    if pending is not None:
        return BeadyardStatus(backend, "pending", owner, pending["expected_revision"])
    state = "bound" if committed else "local" if owner else "legacy"
    if state == "bound":
        anchor = config.load_host().get("hq", {}).get("authority_anchor")
        if anchor:
            from .hq_control_plane import GitControlPlane

            try:
                revision, signed, _policy = (
                    GitControlPlane(root, authority_anchor=anchor)
                    .config_store()
                    ._read(allow_expired=True)
                )
            except Exception:
                raise BeadyardOperationError(
                    "protected Git config could not qualify HQ identity"
                ) from None
            if revision and signed["domain"].endswith("/v1"):
                state = "incomplete"
    return BeadyardStatus(backend, state, owner, current.stdout.strip())


def _protected_git_adoption_state(root: Path, owner: str | None):
    anchor = config.load_host().get("hq", {}).get("authority_anchor")
    if not anchor:
        return None
    from .hq_control_plane import GitControlPlane

    plane = GitControlPlane(root, authority_anchor=anchor)
    try:
        _revision, authority, _policy = plane._operator_read()
        config_revision, config_state, _policy = plane.config_store()._read(
            allow_expired=True, allow_legacy_bound=True
        )
    except Exception:
        raise BeadyardOperationError("protected Git authority could not qualify adoption") from None
    # Existing v1 grants remain recorded through adoption. Bound intake denies
    # their unbound authority until bootstrap publishes fresh bound evidence;
    # in-flight claims retain their preexisting lease/drain policy.
    if config_state.get("domain", "").endswith("/v2"):
        from .beadyard_identity import identity_in_documents

        existing = identity_in_documents(
            (FleetConfigDocument(**doc) for doc in config_state["documents"]),
            required=True,
        )
        if existing != owner:
            raise BeadyardOperationError("protected Git config belongs to another beadyard")
    return plane, config_revision, config_state


def _legacy_git_is_unbound(root: Path, expected_main: str) -> None:
    from .hq import _git

    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_main):
        raise BeadyardOperationError("exact observed HQ main revision required")
    current = _git(["rev-parse", "main"], root)
    if current.returncode or current.stdout.strip() != expected_main:
        raise BeadyardOperationError("HQ main changed since legacy identity inspection")
    branch = _git(["symbolic-ref", "-q", "HEAD"], root)
    if branch.returncode or branch.stdout.strip() != "refs/heads/main":
        raise BeadyardOperationError("HQ main must be checked out for identity adoption")
    tracked = _git(["status", "--porcelain", "--untracked-files=no"], root)
    if tracked.returncode or tracked.stdout.strip():
        raise BeadyardOperationError("HQ tracked working tree must be clean for adoption")
    paths = _git(["ls-tree", "--name-only", expected_main, DOCUMENT_PATH], root)
    if paths.returncode or paths.stdout.strip():
        raise BeadyardOperationError("HQ main already has identity; restore its exact document")
    remote = _git(["remote", "get-url", "origin"], root)
    if remote.returncode == 0:
        observed = _git(["ls-remote", "origin", "refs/heads/main"], root)
        if observed.returncode or observed.stdout.split("\t", 1)[0] != expected_main:
            raise BeadyardOperationError("remote HQ main differs from observed local original")
    _protected_git_adoption_state(root, read_identity(root))


def _ensure_identity_index(root: Path, commit: str) -> None:
    from .hq_control_plane import _git

    blob = _git(root, "rev-parse", f"{commit}:{DOCUMENT_PATH}")
    # Touch only this path; a concurrent ordinary writer's other staged paths
    # must never be discarded by an adoption recovery.
    _git(root, "update-index", "--add", "--cacheinfo", "100644", blob, DOCUMENT_PATH)


def _commit_git_adoption(root: Path, expected_main: str, operator_key: Path) -> str:
    """Publish the already durable document as one signed, original-parent main CAS."""
    from .hq_control_plane import ControlPlaneError, _git

    try:
        blob = _git(root, "hash-object", "-w", DOCUMENT_PATH)
        old_tree = _git(root, "ls-tree", expected_main)
        tree_rows = (old_tree + "\n" if old_tree else "") + (
            f"100644 blob {blob}\t{DOCUMENT_PATH}\n"
        )
        tree = _git(
            root,
            "mktree",
            data=tree_rows,
        )
        commit = _git(
            root,
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={operator_key}",
            "commit-tree",
            "-S",
            tree,
            "-p",
            expected_main,
            data="Adopt canonical beadyard identity\n",
        )
        _git(root, "update-ref", "refs/heads/main", commit, expected_main)
        _ensure_identity_index(root, commit)
    except ControlPlaneError:
        raise BeadyardOperationError(
            "HQ main identity adoption failed; inspect pending identity and retry original HEAD"
        ) from None
    return commit


def _pending_git_commit(
    root: Path, intent: dict[str, object], owner: str, operator_key: Path
) -> str | None:
    """Recognize only the signed first child of the durable adoption intent."""
    from .hq import _git
    from .hq_control_plane import ControlPlaneError
    from .hq_control_plane import _git as protected_git

    expected_main = intent["expected_revision"]
    public = _operator_public(operator_key)
    if hashlib.sha256(public).hexdigest() != intent["key_digest"]:
        raise BeadyardOperationError("operator signing key differs from adoption intent")
    protected = _protected_git_adoption_state(root, owner)
    if protected is not None:
        from .hq_control_plane import fingerprint

        if fingerprint(public.decode()) not in protected[0]._policy()["operator_fingerprints"]:
            raise BeadyardOperationError("operator signing key is not protected HQ custody")

    current = _git(["rev-parse", "main"], root)
    if current.returncode:
        raise BeadyardOperationError("HQ main revision unavailable during adoption retry")
    if current.stdout.strip() == expected_main:
        return None
    chain = _git(["rev-list", "--first-parent", "--reverse", f"{expected_main}..main"], root)
    if chain.returncode or not chain.stdout.strip():
        raise BeadyardOperationError("HQ main is unrelated to adoption original revision")
    first = chain.stdout.splitlines()[0]
    parent = _git(["show", "-s", "--format=%P", first], root)
    title = _git(["show", "-s", "--format=%s", first], root)
    content = _git(["show", f"{first}:{DOCUMENT_PATH}"], root)
    changed = _git(
        ["diff-tree", "--no-commit-id", "--name-status", "-r", expected_main, first], root
    )
    if (
        parent.returncode
        or parent.stdout.strip() != expected_main
        or title.returncode
        or title.stdout.strip() != "Adopt canonical beadyard identity"
        or content.returncode
        or parse_document(content.stdout) != owner
        or changed.returncode
        or changed.stdout.strip() != f"A\t{DOCUMENT_PATH}"
    ):
        raise BeadyardOperationError("HQ main is not the signed adoption descendant")
    email = _git(["show", "-s", "--format=%ae", first], root)
    if email.returncode or not re.fullmatch(r"[^\s]+@[^\s]+", email.stdout.strip()):
        raise BeadyardOperationError("HQ adoption signer identity invalid")
    with tempfile.NamedTemporaryFile(mode="wb", dir=root.parent) as signers:
        signers.write(email.stdout.strip().encode() + b" " + public.strip() + b"\n")
        signers.flush()
        try:
            protected_git(
                root,
                "-c",
                f"gpg.ssh.allowedSignersFile={signers.name}",
                "verify-commit",
                first,
            )
        except ControlPlaneError:
            raise BeadyardOperationError("HQ adoption commit signer is not trusted") from None
    previous = first
    for descendant in chain.stdout.splitlines()[1:]:
        identity_delta = _git(
            ["diff", "--name-only", previous, descendant, "--", DOCUMENT_PATH], root
        )
        if identity_delta.returncode or identity_delta.stdout.strip():
            raise BeadyardOperationError("HQ main identity changed after adoption commit")
        previous = descendant
    final = _git(["show", f"main:{DOCUMENT_PATH}"], root)
    if final.returncode or parse_document(final.stdout) != owner:
        raise BeadyardOperationError("HQ main identity changed after adoption commit")
    return first


def _confirmed_git_config_adoption(plane, current_sha: str, owner: str, intent) -> bool:
    """Require the first signed/witnessed descendant of the pinned original."""
    from . import hq_authority_guard as guard
    from .hq_control_plane import ControlPlaneError, _git

    original = intent["config_revision"]
    if not original or current_sha == original:
        return False
    try:
        chain = _git(
            plane.hq_dir,
            "rev-list",
            "--first-parent",
            "--reverse",
            f"{original}..{current_sha}",
        )
        first = chain.splitlines()[0]
        if _git(plane.hq_dir, "show", "-s", "--format=%P", first) != original:
            return False
        policy = plane._policy()
        # Trusted mode (bh-l4q0s): the config carrier may be an unsigned key-less publication.
        if hq_authority_enforce.enforced():
            _git(
                plane.hq_dir,
                "-c",
                f"gpg.ssh.allowedSignersFile={policy['operator_signers']}",
                "-c",
                f"gpg.ssh.program={policy['executables']['ssh_keygen']['path']}",
                "verify-commit",
                first,
            )
        first_state = json.loads(_git(plane.hq_dir, "show", f"{first}:config.json"))
        old_state = json.loads(_git(plane.hq_dir, "show", f"{original}:config.json"))
        if (
            _docs_digest(old_state["documents"]) != intent["documents_digest"]
            or old_state["revision"] != intent["config_sequence"]
            or first_state["domain"] != guard.CONFIG_DOMAIN_V2
            or first_state["revision"] != old_state["revision"] + 1
            or first_state["documents"][:-1] != old_state["documents"]
            or first_state["documents"][-1]["path"] != DOCUMENT_PATH
            or parse_document(first_state["documents"][-1]["content"]) != owner
        ):
            return False
        witness = _git(
            plane.hq_dir,
            "-c",
            "protocol.ext.allow=always",
            "ls-remote",
            plane._remote(policy),
            f"{guard.CONFIG_WITNESS}{first_state['revision']:020d}",
        )
        return witness.split()[0] == first
    except (ControlPlaneError, ValueError, KeyError, IndexError):
        return False


def _publish_protected_git_identity(
    root: Path, owner: str, operator_key: Path, intent: dict[str, object]
) -> None:
    protected = _protected_git_adoption_state(root, owner)
    if protected is None:
        if intent["config_revision"]:
            raise BeadyardOperationError("protected Git config disappeared during adoption")
        return
    plane, current_revision, current_state = protected
    original_revision = intent["config_revision"]
    if not original_revision:
        if current_revision:
            raise BeadyardOperationError("protected Git config changed after adoption intent")
        return
    if current_revision != original_revision:
        if _confirmed_git_config_adoption(plane, current_revision, owner, intent):
            return
        raise BeadyardOperationError("protected Git config changed after adoption intent")
    if (
        current_state["revision"] != intent["config_sequence"]
        or _docs_digest(current_state["documents"]) != intent["documents_digest"]
        or current_state["domain"].endswith("/v2")
    ):
        raise BeadyardOperationError("protected Git config differs from adoption original")
    from .hq import _git

    committed = _git(["show", f"main:{DOCUMENT_PATH}"], root)
    if committed.returncode or parse_document(committed.stdout) != owner:
        raise BeadyardOperationError("HQ main identity unavailable for protected publication")
    documents = tuple(FleetConfigDocument(**item) for item in current_state["documents"]) + (
        FleetConfigDocument(DOCUMENT_PATH, committed.stdout),
    )
    try:
        plane.config_store(operator_key=str(operator_key)).publish_snapshot(
            documents, expected_revision=original_revision, explicit_adoption=True
        )
    except Exception:
        try:
            observed = _protected_git_adoption_state(root, owner)
        except BeadyardOperationError:
            observed = None
        if observed is None or not _confirmed_git_config_adoption(
            plane, observed[1], owner, intent
        ):
            raise BeadyardOperationError(
                "protected Git identity publication unresolved; retry original adoption"
            ) from None
    observed = _protected_git_adoption_state(root, owner)
    if observed is None or not _confirmed_git_config_adoption(plane, observed[1], owner, intent):
        raise BeadyardOperationError("protected Git identity publication readback differs")


def _completed_git_adoption(
    root: Path, expected_revision: str, operator_key: Path, intent: dict[str, object]
) -> BeadyardStatus:
    """Qualify one exact completed original, without publishing or recapturing CAS."""
    if intent["expected_revision"] != expected_revision:
        raise BeadyardOperationError("HQ adoption receipt original revision differs")
    owner = read_identity(root)
    if owner is None:
        raise BeadyardOperationError("HQ adoption receipt has no local identity")
    if _pending_git_commit(root, intent, owner, operator_key) is None:
        raise BeadyardOperationError("HQ adoption receipt has no signed main descendant")
    protected = _protected_git_adoption_state(root, owner)
    if intent["config_revision"]:
        if protected is None or not _confirmed_git_config_adoption(
            protected[0], protected[1], owner, intent
        ):
            raise BeadyardOperationError("HQ adoption receipt lacks protected config witness")
    status = inspect(root)
    if status.state != "bound" or status.beadyard_id != owner:
        raise BeadyardOperationError("HQ adoption receipt bound readback differs")
    return status


def _pending_sql_directory(snapshot, *, expected_revision: str | None = None) -> Path:
    # A retry of one original SQL adoption must reuse its UUID. This candidate
    # location is derived from protected backend/generation/revision, never from
    # human HQ names or database names, and is not an authority read source.
    key = "\0".join(
        (
            snapshot.backend_identity,
            snapshot.generation,
            expected_revision or snapshot.commit_revision,
        )
    ).encode()
    return config.home() / "hq-beadyard-adoptions" / hashlib.sha256(key).hexdigest()


def _private_sql_artifact(path: Path, *, directory: bool) -> None:
    try:
        info = path.lstat()
    except OSError:
        raise BeadyardOperationError("SQL adoption journal custody unavailable") from None
    correct_kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if info.st_uid != os.geteuid() or info.st_mode & 0o077 or not correct_kind:
        raise BeadyardOperationError("SQL adoption journal custody changed")


def _prepare_sql_pending(pending: Path) -> None:
    base = pending.parent
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_sql_artifact(base, directory=True)
    pending.mkdir(mode=0o700, exist_ok=True)
    _private_sql_artifact(pending, directory=True)
    for name in (".lock", DOCUMENT_PATH, "adoption.json"):
        artifact = pending / name
        if artifact.exists() or artifact.is_symlink():
            _private_sql_artifact(artifact, directory=False)


@contextmanager
def _sql_adoption_lock(pending: Path):
    fd = os.open(pending / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise BeadyardOperationError("SQL adoption lock custody changed")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _sql_intent(pending: Path) -> dict[str, str] | None:
    path = pending / "adoption.json"
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise BeadyardOperationError("SQL adoption intent unavailable") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 512
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise BeadyardOperationError("SQL adoption intent invalid")
        content = os.read(fd, 513)
    finally:
        os.close(fd)
    try:
        record = json.loads(content)
        publication_id = uuid.UUID(record["publication_id"])
    except (ValueError, TypeError, KeyError):
        raise BeadyardOperationError("SQL adoption intent invalid") from None
    if (
        not isinstance(record, dict)
        or set(record) != {"expected_revision", "publication_id", "documents_digest", "beadyard_id"}
        or publication_id.version != 4
        or str(publication_id) != record["publication_id"]
        or not isinstance(record["expected_revision"], str)
        or not isinstance(record["documents_digest"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["documents_digest"])
    ):
        raise BeadyardOperationError("SQL adoption intent invalid")
    try:
        parse_id(record["beadyard_id"])
    except BeadyardIdentityError:
        raise BeadyardOperationError("SQL adoption intent invalid") from None
    return record


def _start_sql_adoption(pending: Path, *, expected_revision: str, documents) -> dict[str, str]:
    from .hq_sql_config import _digest

    existing = _sql_intent(pending)
    record = {
        "expected_revision": expected_revision,
        "publication_id": str(uuid.uuid4()),
        "documents_digest": _digest(documents),
        "beadyard_id": read_identity(pending),
    }
    if existing is not None:
        if any(
            existing[key] != record[key]
            for key in ("expected_revision", "documents_digest", "beadyard_id")
        ):
            raise BeadyardOperationError("SQL adoption intent differs from original")
        return existing
    fd = os.open(
        pending / "adoption.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(fd, json.dumps(record, sort_keys=True).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(pending)
    return record


def _confirmed_sql_adoption(store, intent, snapshot) -> bool:
    try:
        witness = store.recover_publication(
            intent["publication_id"], expected_revision=intent["expected_revision"]
        )
    except Exception:
        raise BeadyardOperationError("SQL adoption publication witness unavailable") from None
    return (
        witness is not None
        and witness.documents_sha256 == intent["documents_digest"]
        and snapshot.beadyard_id == intent["beadyard_id"]
    )


def adopt_legacy(
    *,
    expected_revision: str,
    hq_dir: Path | None = None,
    operator_key: Path | None = None,
) -> BeadyardStatus:
    """One-time explicit adoption against an observed Git main or SQL config HEAD.

    Caller must separately confirm intent. This function does not push Git main,
    switch HOST mode, initialize Beads, or mutate any remote configuration.
    """
    root = Path(hq_dir or config.hq_dir())
    backend, store = _selected_store(root)
    if backend == "git":
        if operator_key is None or not Path(operator_key).is_file():
            raise BeadyardOperationError("Git identity adoption requires an operator signing key")
        with _git_adoption_lock(root):
            completed = _completed_original(root)
            if completed is not None:
                if _pending_original(root) is not None:
                    raise BeadyardOperationError("HQ adoption intent and receipt conflict")
                return _completed_git_adoption(
                    root, expected_revision, Path(operator_key), completed
                )
            status = inspect(root)
            if status.state not in {"legacy", "pending"}:
                raise BeadyardOperationError("legacy HQ identity required for one-time adoption")
            if expected_revision != status.revision:
                raise BeadyardOperationError("HQ identity adoption original revision changed")
            if status.state == "legacy":
                _legacy_git_is_unbound(root, expected_revision)
                _start_git_adoption(root, expected_revision, Path(operator_key))
            intent = _pending_original(root)
            if intent is None or intent["expected_revision"] != expected_revision:
                raise BeadyardOperationError("HQ adoption intent original revision changed")
            owner = create_identity(root)
            protected = _protected_git_adoption_state(root, owner)
            if protected is None:
                if intent["config_revision"]:
                    raise BeadyardOperationError("protected Git config disappeared after intent")
            elif protected[1] != intent["config_revision"] and not _confirmed_git_config_adoption(
                protected[0], protected[1], owner, intent
            ):
                raise BeadyardOperationError("protected Git config changed after adoption intent")
            adopted_commit = _pending_git_commit(root, intent, owner, Path(operator_key))
            if adopted_commit is None:
                _legacy_git_is_unbound(root, expected_revision)
                adopted_commit = _commit_git_adoption(root, expected_revision, Path(operator_key))
                _pending_git_commit(root, intent, owner, Path(operator_key))
            else:
                _ensure_identity_index(root, adopted_commit)
            _publish_protected_git_identity(root, owner, Path(operator_key), intent)
            _finish_git_adoption(root)
            current = inspect(root)
            if current.state != "bound" or current.beadyard_id != owner:
                raise BeadyardOperationError("HQ identity adoption readback differs")
            return current

    snapshot = store.load_snapshot()
    pending = _pending_sql_directory(snapshot, expected_revision=expected_revision)
    if snapshot.beadyard_id is not None and not pending.exists():
        raise BeadyardOperationError("SQL HQ identity is already bound or foreign")
    if snapshot.beadyard_id is None and snapshot.commit_revision != expected_revision:
        raise BeadyardOperationError("HQ identity adoption original revision changed")
    _prepare_sql_pending(pending)
    with _sql_adoption_lock(pending):
        _prepare_sql_pending(pending)
        snapshot = store.load_snapshot()
        intent = _sql_intent(pending)
        if intent is not None and intent["expected_revision"] != expected_revision:
            raise BeadyardOperationError("SQL adoption intent original revision changed")
        if snapshot.beadyard_id is not None:
            if intent is None or not _confirmed_sql_adoption(store, intent, snapshot):
                raise BeadyardOperationError("SQL HQ identity is already bound or foreign")
            content = (pending / DOCUMENT_PATH).read_text()
            if not any(
                doc.path == DOCUMENT_PATH and doc.content == content for doc in snapshot.documents
            ):
                raise BeadyardOperationError("SQL adoption document changed after publication")
            return BeadyardStatus(backend, "bound", snapshot.beadyard_id, snapshot.commit_revision)
        if snapshot.commit_revision != expected_revision:
            raise BeadyardOperationError("HQ identity adoption original revision changed")
        owner = create_identity(pending)
        content = (pending / DOCUMENT_PATH).read_text()
        documents = snapshot.documents + (FleetConfigDocument(DOCUMENT_PATH, content),)
        intent = _start_sql_adoption(
            pending, expected_revision=expected_revision, documents=documents
        )
        if intent["beadyard_id"] != owner:
            raise BeadyardOperationError("SQL adoption candidate identity changed")
        try:
            store.publish_snapshot(
                documents,
                expected_revision=expected_revision,
                explicit_adoption=True,
                publication_id=intent["publication_id"],
            )
        except Exception:
            # A committed publication can lose its reply; the durable publication
            # ID and original-parent row establish whether it is exactly ours.
            pass
        try:
            committed = store.load_snapshot()
        except Exception:
            raise BeadyardOperationError(
                "SQL HQ adoption outcome unavailable; retry original revision"
            ) from None
        if not _confirmed_sql_adoption(store, intent, committed) or not any(
            doc.path == DOCUMENT_PATH and doc.content == content for doc in committed.documents
        ):
            raise BeadyardOperationError("SQL HQ identity adoption conflicted")
        return BeadyardStatus("dolt-server", "bound", owner, committed.commit_revision)
