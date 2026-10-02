"""Explicit, crash-recoverable operator refresh after a Git HQ identity adoption.

The receive broker serializes this operation with every protected Git receive.
Only the projected config HEAD in existing hive policies may change. The durable
intent records exact old/new policy and anchor bytes so an interrupted rotation
can be completed, never reinterpreted against a newer config or new anchors.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from . import hq_authority_guard as guard
from .beadyard_identity import DOCUMENT_PATH, parse_document
from .beadyard_identity_file import read_identity


class PolicyRefreshError(ValueError):
    """The explicit adoption projection could not be safely qualified."""


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_digest(path: Path, *, limit: int = 256 * 1024 * 1024) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise PolicyRefreshError("protected executable invalid")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError:
        raise PolicyRefreshError("protected executable unavailable") from None


def _read(path: Path, *, limit: int = 1024 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise PolicyRefreshError("protected policy refresh file invalid")
            return os.read(fd, limit + 1)
        finally:
            os.close(fd)
    except OSError:
        raise PolicyRefreshError("protected policy refresh file unavailable") from None


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: Path, content: bytes, *, mode: int = 0o644) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".beadyard-policy-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _receive_lock(remote: Path):
    path = remote / "bh-receive.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _git(remote: Path, *args: str) -> str:
    from .hq_control_plane import _git as call

    return call(remote, *args)


def _assert_custody(remote: Path, paths: tuple[Path, ...]) -> None:
    owner = remote.stat().st_uid
    if not os.access(remote, os.W_OK) or remote.is_symlink() or remote.stat().st_mode & 0o022:
        raise PolicyRefreshError("protected HQ server custody unavailable")
    for path in paths:
        info = path.lstat()
        if path.is_symlink() or info.st_uid != owner or info.st_mode & 0o022:
            raise PolicyRefreshError("protected policy refresh custody changed")


def _anchor_paths(remote: Path, supplied: tuple[Path, ...]) -> tuple[Path, ...]:
    selected = tuple(sorted(Path(item).absolute() for item in supplied))
    if len(selected) != len(set(selected)) or not selected:
        raise PolicyRefreshError("explicit complete protected anchor set required")
    discovered = tuple(sorted(remote.glob("bh-client-anchor-*.json")))
    if selected != discovered or any(path.parent != remote for path in selected):
        raise PolicyRefreshError("explicit protected anchor set changed")
    return selected


def _qualified_config(
    remote: Path, policy: dict, old_head: str, original_parent: str, owner: str
) -> str:
    """Prove the latest signed config commit only added this HQ's identity."""
    head = _git(remote, "for-each-ref", "--format=%(objectname)", guard.CONFIG_HEAD)
    if not head:
        raise PolicyRefreshError("signed bound config HEAD unavailable")
    parent = _git(remote, "show", "-s", "--format=%P", head)
    if not parent or parent != original_parent or old_head not in ("", parent):
        raise PolicyRefreshError("policy original config HEAD does not match adoption")
    old = json.loads(_git(remote, "show", f"{parent}:config.json"))
    new = json.loads(_git(remote, "show", f"{head}:config.json"))
    guard.validate_config_state(old)
    guard.validate_config_state(new)
    if (
        old["domain"] != guard.CONFIG_DOMAIN
        or new["domain"] != guard.CONFIG_DOMAIN_V2
        or not (old["generation"] == new["generation"] == policy["generation"])
        or new["revision"] != old["revision"] + 1
        or new["expires_at"] <= time.time()
        or new["documents"][:-1] != old["documents"]
        or new["documents"][-1]["path"] != DOCUMENT_PATH
        or parse_document(new["documents"][-1]["content"]) != owner
    ):
        raise PolicyRefreshError("config advance is not exact identity adoption")
    if not old_head and (old["revision"] != 1 or _git(remote, "show", "-s", "--format=%P", parent)):
        raise PolicyRefreshError("unprojected config had later legacy changes")
    for sha, revision in ((parent, old["revision"]), (head, new["revision"])):
        _git(
            remote, "-c", f"gpg.ssh.allowedSignersFile={policy['operator_signers']}",
            "-c", f"gpg.ssh.program={policy['executables']['ssh_keygen']['path']}",
            "verify-commit", sha,
        )
        witness = _git(
            remote, "for-each-ref", "--format=%(objectname)",
            f"{guard.CONFIG_WITNESS}{revision:020d}",
        )
        if witness != sha:
            raise PolicyRefreshError("config adoption witness differs")
    witnesses = _git(remote, "for-each-ref", "--format=%(refname)", guard.CONFIG_WITNESS)
    if max(witnesses.splitlines(), default="") != (
        f"{guard.CONFIG_WITNESS}{new['revision']:020d}"
    ):
        raise PolicyRefreshError("later config witness prevents policy refresh")
    return head


def _assert_static_policy(policy: dict, remote: Path) -> None:
    from .hq_control_plane import _hook_text

    if (
        policy.get("server_root") != str(remote)
        or policy.get("custody") != "operator-only-filesystem-writes"
    ):
        raise PolicyRefreshError("protected policy server binding changed")
    hook = remote / "hooks/pre-receive"
    protected = (
        remote / "hooks", hook, remote / guard.POLICY,
        remote / "bh-authority-guard.py", remote / "bh-git-broker.py",
        Path(policy["operator_signers"]), remote / "bh-guard-libs",
    )
    _assert_custody(remote, protected)
    if _read(hook) != _hook_text(remote, policy).encode() or not os.access(hook, os.X_OK):
        raise PolicyRefreshError("protected receive hook changed")
    checks = (
        (remote / "bh-authority-guard.py", policy["guard_digest"], 1024 * 1024),
        (remote / "bh-git-broker.py", policy["broker_digest"], 1024 * 1024),
        (Path(policy["operator_signers"]), policy["operator_signers_digest"], 8192),
        (Path(policy["interpreter"]), policy["interpreter_digest"], None),
    )
    for path, digest, limit in checks:
        observed = _digest(_read(path, limit=limit)) if limit is not None else _file_digest(path)
        if observed != digest:
            raise PolicyRefreshError("protected executable or signer bytes changed")
    for executable in policy["executables"].values():
        if _file_digest(Path(executable["path"])) != executable["digest"]:
            raise PolicyRefreshError("protected executable bytes changed")
    library = remote / "bh-guard-libs"
    files = {str(path.relative_to(library)): path for path in library.rglob("*") if path.is_file()}
    if set(files) != set(policy["runtime_files"]):
        raise PolicyRefreshError("protected runtime inventory changed")
    for relative, path in files.items():
        _assert_custody(remote, (path,))
        if path.is_symlink() or _digest(_read(path)) != policy["runtime_files"][relative]:
            raise PolicyRefreshError("protected runtime bytes changed")


def _intended_bytes(remote: Path, policy_bytes: bytes, anchors: tuple[Path, ...], head: str):
    from . import gitref

    policy = json.loads(policy_bytes)
    new_policy = json.loads(policy_bytes)
    old_heads = {item["config_head"] for item in policy["hive_policies"].values()}
    if len(old_heads) != 1:
        raise PolicyRefreshError("hive policy config heads are inconsistent")
    if any(item["valid_until"] <= time.time() for item in policy["hive_policies"].values()):
        raise PolicyRefreshError("expired hive policy cannot be revived by refresh")
    for item in new_policy["hive_policies"].values():
        item["config_head"] = head
    guard.validate_hive_policies(new_policy["hive_policies"])
    new_policy_bytes = gitref.encode(new_policy).encode()
    old_digest, new_digest = _digest(policy_bytes), _digest(new_policy_bytes)
    anchor_bytes = {}
    for path in anchors:
        original = _read(path)
        record = json.loads(original)
        if (
            set(record)
            != {"server_root", "socket", "policy_digest", "generation", "role",
                "client_interpreter", "client_interpreter_digest"}
            or record["server_root"] != str(remote)
            or record["generation"] != policy["generation"]
            or record["policy_digest"] != old_digest
            or record["role"] not in {"operator", "frame"}
            or _file_digest(Path(record["client_interpreter"]))
            != record["client_interpreter_digest"]
        ):
            raise PolicyRefreshError("protected client anchor changed")
        endpoint = Path(record["socket"])
        if (
            endpoint.is_symlink()
            or endpoint.stat().st_uid != remote.stat().st_uid
            or endpoint.parent.stat().st_mode & 0o022
        ):
            raise PolicyRefreshError("protected client endpoint custody changed")
        updated = {**record, "policy_digest": new_digest}
        anchor_bytes[str(path)] = (original, gitref.encode(updated).encode())
    return new_policy_bytes, anchor_bytes


def _pack_bytes(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _unpack_bytes(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _intent(remote: Path) -> Path:
    return remote / "bh-beadyard-policy-refresh.pending"


def _completion(remote: Path) -> Path:
    return remote / "bh-beadyard-policy-refresh.done"


def _load_intent(path: Path):
    if not path.exists():
        return None
    try:
        record = json.loads(_read(path, limit=2 * 1024 * 1024))
        if set(record) != {
            "old_digest", "old_head", "config_parent", "new_head", "policy", "anchors"
        }:
            raise ValueError
        old, new = (_unpack_bytes(item) for item in record["policy"])
        pairs = {
            key: tuple(_unpack_bytes(item) for item in values)
            for key, values in record["anchors"].items()
        }
        if len(pairs) != len(record["anchors"]) or any(len(value) != 2 for value in pairs.values()):
            raise ValueError
        if _digest(old) != record["old_digest"]:
            raise ValueError
        return record, old, new, pairs
    except (ValueError, TypeError, KeyError, IndexError):
        raise PolicyRefreshError("protected policy refresh intent invalid") from None


def _validate_intent_contents(record, old: bytes, new: bytes, pairs) -> None:
    original = json.loads(old)
    proposed = json.loads(new)
    expected = json.loads(old)
    for item in expected["hive_policies"].values():
        if item["config_head"] != record["old_head"]:
            raise PolicyRefreshError("protected policy refresh original head changed")
        item["config_head"] = record["new_head"]
    if proposed != expected:
        raise PolicyRefreshError("protected policy refresh changed unrelated fields")
    new_digest = _digest(new)
    for original_bytes, proposed_bytes in pairs.values():
        before = json.loads(original_bytes)
        after = json.loads(proposed_bytes)
        if (
            before.get("generation") != original["generation"]
            or before.get("policy_digest") != record["old_digest"]
            or after != {**before, "policy_digest": new_digest}
        ):
            raise PolicyRefreshError("protected anchor refresh changed unrelated fields")


def _write_intent(remote: Path, record: dict) -> None:
    path = _intent(remote)
    fd, temporary = tempfile.mkstemp(prefix=".beadyard-intent-", dir=remote)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(record, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        _sync_directory(remote)
    finally:
        os.unlink(temporary)


def refresh_after_adoption(
    hq_dir: Path, *, expected_policy_digest: str, expected_config_head: str,
    expected_config_parent: str, anchors: tuple[Path, ...], operator_anchor: Path,
) -> str:
    """Explicitly rotate only the server's config-head projection and its known anchors."""
    from .hq_control_plane import GitControlPlane

    root = Path(hq_dir)
    anchor = Path(operator_anchor).absolute()
    try:
        selector = json.loads(_read(anchor))
        if selector["role"] != "operator":
            raise PolicyRefreshError("operator anchor required for policy refresh")
        remote = Path(selector["server_root"])
    except (ValueError, KeyError, TypeError):
        raise PolicyRefreshError("operator anchor unavailable for policy refresh") from None
    if remote.stat().st_uid != os.geteuid():
        raise PolicyRefreshError("server-local operator custody required")
    with _receive_lock(remote):
        selected = _anchor_paths(remote, anchors)
        if anchor not in selected:
            raise PolicyRefreshError("operator anchor missing from explicit refresh set")
        protected_paths = (remote / guard.POLICY, *selected)
        for journal in (_intent(remote), _completion(remote)):
            if journal.exists() or journal.is_symlink():
                protected_paths += (journal,)
        _assert_custody(remote, protected_paths)
        owner = read_identity(root)
        if owner is None or parse_document(_git(root, "show", f"main:{DOCUMENT_PATH}")) != owner:
            raise PolicyRefreshError("local canonical HQ identity unavailable")
        pending = _load_intent(_intent(remote))
        completed = _load_intent(_completion(remote))
        if pending is not None and completed is not None:
            raise PolicyRefreshError("protected policy refresh has conflicting journals")
        if pending is None and completed is None:
            GitControlPlane(root, authority_anchor=anchor)._policy()
            old_bytes = _read(remote / guard.POLICY)
            if _digest(old_bytes) != expected_policy_digest:
                raise PolicyRefreshError("original protected policy digest changed")
            old_policy = json.loads(old_bytes)
            _assert_static_policy(old_policy, remote)
            old_heads = {item["config_head"] for item in old_policy["hive_policies"].values()}
            if old_heads != {expected_config_head}:
                raise PolicyRefreshError("original hive policy config HEAD changed")
            new_head = _qualified_config(
                remote, old_policy, expected_config_head, expected_config_parent, owner
            )
            new_bytes, anchor_bytes = _intended_bytes(remote, old_bytes, selected, new_head)
            record = {
                "old_digest": expected_policy_digest,
                "old_head": expected_config_head,
                "config_parent": expected_config_parent,
                "new_head": new_head,
                "policy": [_pack_bytes(old_bytes), _pack_bytes(new_bytes)],
                "anchors": {
                    path: [_pack_bytes(old), _pack_bytes(new)]
                    for path, (old, new) in anchor_bytes.items()
                },
            }
            _validate_intent_contents(record, old_bytes, new_bytes, anchor_bytes)
            _write_intent(remote, record)
        else:
            record, old_bytes, new_bytes, anchor_bytes = pending or completed
            if (
                record["old_digest"] != expected_policy_digest
                or record["old_head"] != expected_config_head
                or record["config_parent"] != expected_config_parent
                or set(anchor_bytes) != {str(path) for path in selected}
            ):
                raise PolicyRefreshError("protected policy refresh retry differs from intent")
            old_policy = json.loads(old_bytes)
            _assert_static_policy(old_policy, remote)
            _validate_intent_contents(record, old_bytes, new_bytes, anchor_bytes)
            _qualified_config(
                remote, old_policy, expected_config_head, expected_config_parent, owner
            )
            latest_head = _git(remote, "for-each-ref", "--format=%(objectname)", guard.CONFIG_HEAD)
            if latest_head != record["new_head"]:
                raise PolicyRefreshError("config HEAD advanced during protected policy refresh")
        if completed is not None:
            if _read(remote / guard.POLICY) != new_bytes or any(
                _read(path) != anchor_bytes[str(path)][1] for path in selected
            ):
                raise PolicyRefreshError("completed policy refresh changed after publication")
            return _digest(new_bytes)
        if _read(remote / guard.POLICY) not in (old_bytes, new_bytes):
            raise PolicyRefreshError("protected policy changed during refresh")
        for path in selected:
            if _read(path) not in anchor_bytes[str(path)]:
                raise PolicyRefreshError("protected anchor changed during refresh")
        if _read(remote / guard.POLICY) == old_bytes:
            _atomic(remote / guard.POLICY, new_bytes)
        for path in selected:
            original, updated = anchor_bytes[str(path)]
            if _read(path) == original:
                _atomic(path, updated)
        os.replace(_intent(remote), _completion(remote))
        _sync_directory(remote)
        return _digest(new_bytes)
