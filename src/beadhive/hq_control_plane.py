"""HQ port with protected Git authority, independent observer, and admission."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from . import config, gitref, hq_authority_ceiling, hq_git_broker, hq_manifest_guard
from . import hq_authority_guard as guard
from .hq_authority_enforce import enforced as authority_enforced
from .run import run

if TYPE_CHECKING:
    from .host_heartbeat_core import AuthoritySnapshot
    from .modules.config.domain.ports import FleetConfigSnapshot


def _check_duration(duration, ceiling):
    try:
        hq_authority_ceiling.check_duration(duration, ceiling)
    except ValueError as exc:
        raise ControlPlaneError(str(exc)) from None


class ControlPlaneError(ValueError):
    """Unavailable protection, rejected authority, or unsupported binding."""


class CommittedManifestAbsent(FileNotFoundError):
    """A verified selected snapshot contains no document for this host."""


class HqLeaseUnknown(ControlPlaneError):
    """An exact frame proposal may have been accepted; do not refresh its CAS."""

    def __init__(self, request_id, request_sha256, expected_revision):
        self.request_id = request_id
        self.request_sha256 = request_sha256
        self.expected_revision = expected_revision
        super().__init__(
            f"HQ hive lease acknowledgment unknown for request {request_id} "
            f"against original revision {expected_revision}"
        )


@dataclass(frozen=True)
class HqConsistencyToken:
    """Provider-issued identity of one qualified config/authority read boundary."""

    backend_identity: str
    generation: str
    revision: str

    def __post_init__(self):
        if any(
            not isinstance(value, str) or not value
            for value in (self.backend_identity, self.generation, self.revision)
        ):
            raise ControlPlaneError("HQ consistency token requires backend/generation/revision")


@dataclass(frozen=True)
class ConfigAuthoritySnapshot:
    """Both immutable views from one provider-qualified authoritative transaction.

    Only the backend issues this result; joining independent reads does not qualify.
    The token revision identifies the combined read, while config.commit_revision and
    authority.authority.config_revision retain their distinct document/policy meaning.
    SQL must reject mixed, revoked or expired facts before returning this result.
    """

    token: HqConsistencyToken
    config: FleetConfigSnapshot
    authority: AuthoritySnapshot

    def __post_init__(self):
        from .host_heartbeat_core import AuthoritySnapshot
        from .modules.config.domain.ports import FleetConfigSnapshot

        if (
            not isinstance(self.token, HqConsistencyToken)
            or not isinstance(self.config, FleetConfigSnapshot)
            or not isinstance(self.authority, AuthoritySnapshot)
            or self.token.backend_identity != self.config.backend_identity
            or self.token.generation != self.config.generation
            or self.authority.authority.beadyard_id != self.config.beadyard_id
            or max(self.config.fetched_at, self.authority.checked_at)
            >= min(self.config.valid_until, self.authority.valid_until)
        ):
            raise ControlPlaneError("inconsistent HQ config/authority provenance")


class HqControlPlane(Protocol):
    def load_host_manifest(self, host_id: str): ...
    def fetch_config(self, frame: str, *, holder_identity: str | None = None) -> dict: ...
    def publish_registration(self, manifest: str, *, attempts: int = 3) -> bool: ...
    def heartbeat(self, lease, *, signing_key: str) -> str: ...
    def watch_state(self, frame: str): ...
    def authority_status(self) -> dict: ...
    def eligibility_authority_status(self) -> dict: ...
    def observe(self, manifest, *, now=None, observer_dir=None): ...
    def read_eligibility(self, manifest, *, now=None): ...
    def read_hive_lease(self, prefix, *, holder_identity=None): ...
    def read_hive_lease_record(self, prefix, *, holder_identity=None): ...
    def publish_hive_lease(self, prefix, lease, *, expected, operation, force=False): ...
    def config_store(self, *, operator_key=None, duration=3600, ceiling=None): ...
    def load_config_authority_snapshot(
        self, frame: str, *, revision: str | None = None
    ) -> ConfigAuthoritySnapshot: ...
    def heartbeat_reference(self, lease): ...
    def publish_registration_evidence(self, manifest, *, signing_key): ...
    def grant(self, authority, public_key, desired, *, expected, operator_key): ...
    def accept_observation(self, frame, *, expected, operator_key, holder_identity=""): ...
    def renew(self, *, expected, operator_key, duration=3600, ceiling=None): ...
    def lifecycle(
        self,
        verb,
        frame,
        action="plan",
        *,
        expected="",
        expected_host_id="",
        expected_release="",
        operator_key="",
        confirm=False,
        supersede=False,
        deadline=None,
        emergency_prefix="",
        emergency_reason="",
        emergency_duration=600,
        execution_digest="",
    ): ...


def _git(directory, *args, data=None):
    forbidden = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    }
    if any(name in os.environ for name in forbidden):
        raise ControlPlaneError("Git environment injection is unsupported for authority operations")
    result = run(
        ["git", *args],
        cwd=str(directory),
        capture=True,
        check=False,
        text_input=data,
        timeout=gitref.GIT_TIMEOUT,
    )
    if result.returncode:
        raise ControlPlaneError(gitref.message(result))
    return (result.stdout or "").strip()


def fingerprint(public_key):
    return guard.fingerprint(public_key)


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _hook_text(remote, policy):
    return (
        f"#!{policy['interpreter']}\nimport runpy\n"
        f"runpy.run_path({str(remote / 'bh-authority-guard.py')!r}, run_name='__main__')\n"
    )


def install_guard(
    hq_dir,
    operator_public_key,
    generation,
    *,
    interpreter=None,
    confirm_server_custody=False,
    hive_policies=None,
):
    if not confirm_server_custody or not generation:
        raise ControlPlaneError("explicit operator server custody and recovery generation required")
    guard.validate_hive_policies(hive_policies or {})
    url = _git(hq_dir, "remote", "get-url", "origin")
    if url.startswith("file://"):
        url = url[7:]
    if ":" in url:
        raise ControlPlaneError("provisioning requires a controlled local bare HQ")
    remote = (Path(hq_dir) / url).resolve()
    if _git(remote, "rev-parse", "--is-bare-repository") != "true":
        raise ControlPlaneError("controlled bare HQ required")
    policy_path, hook = remote / guard.POLICY, remote / "hooks/pre-receive"
    if (
        hook.exists()
        or policy_path.exists()
        or _git(
            remote,
            "for-each-ref",
            "--format=%(refname)",
            guard.HEAD,
            guard.WITNESS,
            guard.CONFIG_HEAD,
            guard.CONFIG_WITNESS,
        )
    ):
        raise ControlPlaneError("existing authority protection requires out-of-band recovery")
    interpreter = str(Path(interpreter or sys.executable).resolve())
    if not os.access(interpreter, os.X_OK):
        raise ControlPlaneError("explicit executable server interpreter required")
    anchor = remote / "bh-authority-operators"
    anchor.write_text("operator " + operator_public_key.strip() + "\n")
    server_guard, server_broker = remote / "bh-authority-guard.py", remote / "bh-git-broker.py"
    server_guard.write_bytes(Path(guard.__file__).read_bytes())
    server_broker.write_bytes(Path(hq_git_broker.__file__).read_bytes())
    from ruamel import yaml

    from .hosts import HostManifest

    libraries = remote / "bh-guard-libs"
    yaml_source = Path(yaml.__file__).parent
    for source in yaml_source.rglob("*.py"):
        if "__pycache__" in source.parts:
            continue
        target = libraries / "ruamel/yaml" / source.relative_to(yaml_source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    (libraries / "manifest_guard.py").write_bytes(Path(hq_manifest_guard.__file__).read_bytes())
    # The receive hook imports this file as a standalone module from its sealed runtime.
    # Copy the pure source, not the public compatibility surface with package imports.
    from .modules.config.domain import beadyard_identity as identity_contract

    (libraries / "beadyard_identity.py").write_bytes(Path(identity_contract.__file__).read_bytes())
    (libraries / "host-manifest.schema.json").write_text(
        gitref.encode(HostManifest.model_json_schema())
    )
    runtime_files = {
        str(p.relative_to(libraries)): _digest(p) for p in libraries.rglob("*") if p.is_file()
    }
    executables = {}
    for name, program in (("git", "git"), ("ssh_keygen", "ssh-keygen")):
        executable = shutil.which(program)
        if not executable:
            raise ControlPlaneError(f"server executable missing: {program}")
        executable = str(Path(executable).resolve())
        executables[name] = {"path": executable, "digest": _digest(executable)}
    policy = {
        "hive_policies": hive_policies or {},
        "generation": generation,
        "server_root": str(remote),
        "operator_signers": str(anchor),
        "operator_signers_digest": _digest(anchor),
        "operator_fingerprints": [fingerprint(operator_public_key)],
        "guard_digest": _digest(server_guard),
        "broker_digest": _digest(server_broker),
        "runtime_files": runtime_files,
        "interpreter": interpreter,
        "interpreter_digest": _digest(interpreter),
        "executables": executables,
        "server_path": os.pathsep.join(
            sorted({str(Path(v["path"]).parent) for v in executables.values()})
        ),
        "custody": "operator-only-filesystem-writes",
    }
    policy_path.write_text(gitref.encode(policy))
    hook.write_text(_hook_text(remote, policy))
    hook.chmod(0o755)
    for path in (remote, *remote.rglob("*")):
        if path.is_symlink():
            raise ControlPlaneError("server authority tree must not contain symlinks")
        path.chmod(path.stat().st_mode & ~0o022)
    return _digest(policy_path)


def _broker_url(endpoint, interpreter):
    source = Path(hq_git_broker.__file__).resolve()
    paths = str(endpoint) + interpreter + str(source)
    if any(c.isspace() for c in paths) or "%" in paths:
        raise ControlPlaneError("broker paths cannot contain whitespace or percent escapes")
    return f"ext::{interpreter} {source} client {endpoint} %S"


def bind_broker(
    hq_dir, server_root, endpoint, policy_digest, *, client_interpreter=None, role="frame"
):
    """Operator provisions a protected per-client anchor, outside client Git config."""
    remote, endpoint = Path(server_root).resolve(), Path(endpoint).resolve()
    if role not in {"operator", "frame"} or not os.access(remote, os.W_OK):
        raise ControlPlaneError("anchor provisioning requires operator filesystem custody")
    policy = json.loads((remote / guard.POLICY).read_text())
    if _digest(remote / guard.POLICY) != policy_digest or policy["server_root"] != str(remote):
        raise ControlPlaneError("provided protected server policy digest mismatch")
    interpreter = str(Path(client_interpreter or sys.executable).resolve())
    if not os.access(interpreter, os.X_OK):
        raise ControlPlaneError("executable client interpreter required")
    identity = hashlib.sha256(str(Path(hq_dir).resolve()).encode()).hexdigest()[:24]
    protected = remote / f"bh-client-anchor-{identity}.json"
    record = {
        "server_root": str(remote),
        "socket": str(endpoint),
        "policy_digest": policy_digest,
        "generation": policy["generation"],
        "role": role,
        "client_interpreter": interpreter,
        "client_interpreter_digest": _digest(interpreter),
    }
    protected.write_text(gitref.encode(record))
    protected.chmod(0o644)
    return protected


def candidate_ref(frame):
    from .host_heartbeat_core import ref_name

    return ref_name(frame).replace("/heartbeat/", "/candidate-heartbeat/")


def registration_ref(authority):
    from .host_heartbeat_core import ref_name

    binding = (
        authority["holder_identity"],
        authority["instance_ref"],
        authority["key_fingerprint"],
    )
    suffix = hashlib.sha256(json.dumps(binding, separators=(",", ":")).encode()).hexdigest()[:24]
    return ref_name(authority["frame_id"]).replace("/heartbeat/", "/registration/") + "/" + suffix


def verified_anchor_role(hq_dir, anchor):
    """Classify only an anchor with verified custody, digest and recovery generation."""
    return GitControlPlane(hq_dir, authority_anchor=anchor)._policy()["client"]["role"]


class GitControlPlane:
    def __init__(self, hq_dir, *, authority_anchor=None, clock=time.time, policy_digest=""):
        self.hq_dir, self.clock = Path(hq_dir), clock
        self.authority_anchor = Path(authority_anchor) if authority_anchor else None
        # Legacy callers cannot turn a mutable Git-config digest into authority.
        if policy_digest:
            raise ControlPlaneError("use an operator-provisioned protected authority anchor")

    def load_host_manifest(self, host_id: str):
        from . import hosts

        return hosts.load(self.hq_dir, host_id)

    def config_store(self, *, operator_key=None, duration=3600, ceiling=None):
        from .hq_fleet_config import GitFleetConfigRevisionStore

        return GitFleetConfigRevisionStore(
            self, _git, operator_key=operator_key, duration=duration, ceiling=ceiling
        )

    def load_config_authority_snapshot(self, frame, *, revision=None):
        raise ControlPlaneError("atomic config/authority snapshot unsupported by Git binding")

    def authority_status(self):
        sha, state, _ = self._operator_read()
        return {
            "revision": sha,
            "state": state,
            "authority_ready": bool(sha) and self.clock() < state["expires_at"],
        }

    def eligibility_authority_status(self):
        revision, state, _ = self._read()
        return {"revision": revision, "state": state, "authority_ready": True}

    def read_eligibility(self, manifest, *, now=None):
        """Qualify constituent reads against one protected monotonic authority revision.

        This is a Git read bracket, not an atomic SQL composite snapshot promise.
        Every read verifies protected policy/witnesses and current validity.
        """
        revision, _, _ = self._read()
        desired = self.fetch_config(manifest.frame_id, holder_identity=manifest.host_id)
        from .beadyard_identity_file import read_identity

        owner = read_identity(self.hq_dir)
        if manifest.beadyard_id != owner or desired["authority"].get("beadyard_id") != owner:
            raise ControlPlaneError("frame belongs to a different beadyard")
        observation = self.observe(manifest, now=now)
        current, _, _ = self._read()
        if current != revision:
            raise ControlPlaneError("authority changed during eligibility read")
        return revision, desired, observation

    def read_hive_lease_record(self, prefix, *, holder_identity=None):
        from .host_lease_contracts import HostLease, lease_ref
        from .hq_frame_lease import read

        return read(
            self,
            prefix,
            holder_identity=holder_identity,
            git=_git,
            error=ControlPlaneError,
            decode=HostLease.from_record,
            lease_ref=lease_ref,
            json_decode=gitref.decode,
        )

    def read_hive_lease(self, prefix, *, holder_identity=None):
        return self.read_hive_lease_record(prefix, holder_identity=holder_identity)[1]

    def publish_hive_lease(self, prefix, lease, *, expected, operation, force=False):
        from . import host, hosts
        from .host_lease_contracts import HostLease, lease_ref
        from .hq_frame_lease import publish

        return publish(
            self,
            prefix,
            lease,
            expected=expected,
            operation=operation,
            force=force,
            git=_git,
            error=ControlPlaneError,
            decode=HostLease.from_record,
            lease_ref=lease_ref,
            manifest=hosts.load(self.hq_dir, host.host_id()),
            signing_key=host.signing_key(),
            json_decode=gitref.decode,
            json_encode=gitref.encode,
        )

    def observe(self, manifest, *, now=None, observer_dir=None):
        from . import host_heartbeat_core as hb

        try:
            policy = self._policy()
        except ControlPlaneError:
            # Git-only compatibility diagnostics may verify an existing signed
            # carrier, but cannot derive freshness without protected authority.
            return hb.observe(
                self.hq_dir,
                manifest,
                now=now,
                observer_dir=observer_dir,
                trusted_authority_lookup=lambda *_: None,
            )

        def trusted_authority(_directory, frame):
            try:
                return self.watch_state(frame)
            except ControlPlaneError:
                return None

        return hb.observe(
            self.hq_dir,
            manifest,
            now=now,
            observer_dir=observer_dir,
            remote=self._remote(policy),
            transport_options=["-c", "protocol.ext.allow=always"],
            trusted_authority_lookup=trusted_authority,
        )

    def _policy(self):
        if self.authority_anchor is None:
            raise ControlPlaneError("protected authority anchor has not been provisioned")
        try:
            client = json.loads(self.authority_anchor.read_text())
            remote = Path(client["server_root"])
            paths = (
                self.authority_anchor,
                remote,
                remote / "hooks",
                remote / "hooks/pre-receive",
                remote / guard.POLICY,
                remote / "bh-authority-guard.py",
                remote / "bh-git-broker.py",
            )
            if any(
                p.is_symlink()
                or p.stat().st_uid != remote.stat().st_uid
                or p.stat().st_mode & 0o022
                for p in paths
            ):
                raise ControlPlaneError("protected authority custody changed")
            writable = os.access(remote, os.W_OK)
            if (
                client["role"] == "frame"
                and remote.stat().st_uid == os.geteuid()
                and not os.statvfs(remote).f_flag & os.ST_RDONLY
            ):
                raise ControlPlaneError(
                    "frame-owned server requires enforced read-only mount custody"
                )
            if client["role"] == "frame" and writable:
                raise ControlPlaneError(
                    "frame process must not have server filesystem write access"
                )
            if client["role"] == "operator" and not writable:
                raise ControlPlaneError("operator anchor cannot be used from a frame sandbox")
            encoded = (remote / guard.POLICY).read_bytes()
            if hashlib.sha256(encoded).hexdigest() != client["policy_digest"]:
                raise ControlPlaneError(
                    "authority policy trust anchor changed; out-of-band recovery required"
                )
            policy = json.loads(encoded)
            if (
                policy["server_root"] != str(remote)
                or policy["generation"] != client["generation"]
                or policy["custody"] != "operator-only-filesystem-writes"
            ):
                raise ControlPlaneError("protected server/generation binding mismatch")
            if (remote / "hooks/pre-receive").read_text() != _hook_text(
                remote, policy
            ) or not os.access(remote / "hooks/pre-receive", os.X_OK):
                raise ControlPlaneError("authority receive guard absent or changed")
            for path, digest in (
                (remote / "bh-authority-guard.py", policy["guard_digest"]),
                (remote / "bh-git-broker.py", policy["broker_digest"]),
                (Path(policy["operator_signers"]), policy["operator_signers_digest"]),
                (Path(policy["interpreter"]), policy["interpreter_digest"]),
                (Path(client["client_interpreter"]), client["client_interpreter_digest"]),
            ):
                if _digest(path) != digest:
                    raise ControlPlaneError("provisioned authority executable/trust bytes changed")
            libraries = remote / "bh-guard-libs"
            files = {str(p.relative_to(libraries)): p for p in libraries.rglob("*") if p.is_file()}
            if set(files) != set(policy["runtime_files"]):
                raise ControlPlaneError("provisioned guard runtime file set changed")
            for relative, path in files.items():
                if (
                    path.is_symlink()
                    or path.stat().st_uid != remote.stat().st_uid
                    or path.stat().st_mode & 0o022
                    or _digest(path) != policy["runtime_files"][relative]
                ):
                    raise ControlPlaneError("provisioned guard runtime bytes/custody changed")
            for executable in policy["executables"].values():
                if _digest(executable["path"]) != executable["digest"]:
                    raise ControlPlaneError("provisioned server executable changed")
            endpoint = Path(client["socket"])
            if (
                endpoint.is_symlink()
                or endpoint.stat().st_uid != remote.stat().st_uid
                or endpoint.parent.stat().st_mode & 0o022
            ):
                raise ControlPlaneError("broker endpoint custody changed")
            if client["role"] == "frame" and os.access(endpoint.parent, os.W_OK):
                raise ControlPlaneError("frame can rewrite broker endpoint")
            rewritten = (
                run(
                    [
                        policy["executables"]["git"]["path"],
                        "config",
                        "--get-regexp",
                        r"^url\..*\.(insteadof|pushinsteadof)$",
                    ],
                    cwd=str(self.hq_dir),
                    capture=True,
                    check=False,
                    timeout=gitref.GIT_TIMEOUT,
                )
                if self.hq_dir.exists()
                else None
            )
            if rewritten is not None and rewritten.returncode != 1:
                raise ControlPlaneError(
                    "Git URL rewrite configuration cannot select authority transport"
                )
            return {**policy, "client": client}
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ControlPlaneError("protected authority backend unavailable") from exc

    def _remote(self, policy):
        client = policy["client"]
        return _broker_url(Path(client["socket"]), client["client_interpreter"])

    def _read(self, *, allow_expired=False):
        policy = self._policy()
        rows = _git(
            self.hq_dir,
            "-c",
            "protocol.ext.allow=always",
            "ls-remote",
            self._remote(policy),
            guard.HEAD,
            guard.WITNESS + "*",
        ).splitlines()
        refs = dict(row.split()[::-1] for row in rows)
        sha = refs.get(guard.HEAD, "")
        witnesses = sorted(ref for ref in refs if ref.startswith(guard.WITNESS))
        if not sha:
            if witnesses:
                raise ControlPlaneError("missing authority head with retained witnesses")
            return "", {}, policy
        _git(
            self.hq_dir,
            "-c",
            "protocol.ext.allow=always",
            "fetch",
            "--no-tags",
            self._remote(policy),
            sha,
        )
        _git(
            self.hq_dir,
            "-c",
            f"gpg.ssh.allowedSignersFile={policy['operator_signers']}",
            "-c",
            f"gpg.ssh.program={policy['executables']['ssh_keygen']['path']}",
            "verify-commit",
            sha,
        )
        if (
            int(_git(self.hq_dir, "cat-file", "-s", f"{sha}:authority.json")) > 4 * 1024 * 1024
            or _git(self.hq_dir, "ls-tree", "--name-only", sha) != "authority.json"
        ):
            raise ControlPlaneError("invalid authority carrier")
        state = json.loads(_git(self.hq_dir, "show", f"{sha}:authority.json"))
        guard.validate_state(state)
        if (
            not witnesses
            or witnesses[-1] != f"{guard.WITNESS}{state['revision']:020d}"
            or refs[witnesses[-1]] != sha
        ):
            raise ControlPlaneError("authority rollback or inconsistent witness detected")
        if state["generation"] != policy["generation"] or self.clock() < state["issued_at"] - 30:
            raise ControlPlaneError("authority recovery generation or clock mismatch")
        if not allow_expired and self.clock() >= state["expires_at"] and authority_enforced():
            # BH_HQ_AUTHORITY_ENFORCE=false (UNSUPPORTED, dev-only) skips expiry on this host.
            raise ControlPlaneError("authority validity interval expired")
        return sha, state, policy

    def _operator_read(self):
        sha, state, policy = self._read(allow_expired=True)
        if policy["client"]["role"] != "operator":
            raise ControlPlaneError(
                "operator operation requires a protected operator anchor and checkout"
            )
        return sha, state, policy

    def _write(
        self,
        state,
        expected,
        operator_key,
        *,
        duration=3600,
        updates=(),
        expires_at_cap=None,
        ceiling=None,
    ):
        current, previous, policy = self._operator_read()
        if current != expected:
            raise ControlPlaneError("expected authority revision/duration mismatch")
        _check_duration(duration, ceiling)
        bound = any(
            record["authority"].get("beadyard_id") is not None for _, record in guard.records(state)
        )
        issued_at = self.clock()
        expires_at = issued_at + duration
        if expires_at_cap is not None:
            expires_at = min(expires_at, expires_at_cap)
        state.update(
            domain=guard.DOMAIN_V2 if bound else guard.DOMAIN,
            generation=policy["generation"],
            revision=previous.get("revision", 0) + 1,
            issued_at=issued_at,
            expires_at=expires_at,
        )
        guard.validate_state(state)
        blob = _git(self.hq_dir, "hash-object", "-w", "--stdin", data=gitref.encode(state))
        tree = _git(self.hq_dir, "mktree", data=f"100644 blob {blob}\tauthority.json\n")
        args = [
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={operator_key}",
            "commit-tree",
            "-S",
            tree,
        ]
        if expected:
            args += ["-p", expected]
        sha = _git(self.hq_dir, *args, data=f"HQ authority revision {state['revision']}\n")
        _git(
            self.hq_dir,
            "-c",
            f"gpg.ssh.allowedSignersFile={policy['operator_signers']}",
            "-c",
            f"gpg.ssh.program={policy['executables']['ssh_keygen']['path']}",
            "verify-commit",
            sha,
        )
        witness = f"{guard.WITNESS}{state['revision']:020d}"
        push = [
            "-c",
            "protocol.ext.allow=always",
            "push",
            "--atomic",
            f"--force-with-lease={guard.HEAD}:{expected}",
            f"--force-with-lease={witness}:",
        ]
        for reference, old, _new in updates:
            push += [f"--force-with-lease={reference}:{old}"]
        push += [self._remote(policy), f"{sha}:{guard.HEAD}", f"{sha}:{witness}"]
        push += [f"{new}:{reference}" for reference, old, new in updates]
        _git(self.hq_dir, *push)
        if self._read()[0] != sha:
            raise ControlPlaneError("authority changed before readback")
        return sha

    def renew(self, *, expected, operator_key, duration=3600, ceiling=None):
        sha, state, _ = self._operator_read()
        if not sha:
            raise ControlPlaneError("cannot renew absent authority")
        return self._write(state, expected, operator_key, duration=duration, ceiling=ceiling)

    def _trust(self, state):
        path = Path(_git(self.hq_dir, "rev-parse", "--git-path", "bh-authority-signers"))
        if not path.is_absolute():
            path = self.hq_dir / path
        path.write_text(
            "".join(
                "frame " + r["public_key"].strip() + "\n"
                for _, r in guard.records(state)
                if r["state"] != "retired"
            )
        )
        return path

    def _snapshot(self, frame, record, state, *, candidate=False):
        from .host_heartbeat_core import AuthoritySnapshot, ObservationAuthority, ref_name

        if record is None or record["state"] == "retired":
            return None
        auth = ObservationAuthority(**record["authority"])
        expiry = min(state["expires_at"], auth.candidate_expires_at or state["expires_at"])
        if self.clock() >= expiry:
            return None
        receipt = record["receipt"]
        return AuthoritySnapshot(
            auth,
            self.clock(),
            expiry,
            receipt["sequence"],
            receipt["sha"],
            receipt["first_seen"],
            record["state"],
            record["state"] == "active" and not record["cordoned"],
            candidate_ref(frame) if candidate else ref_name(frame),
        )

    def watch_state(self, frame):
        from dataclasses import replace

        _, state, _ = self._read()
        entry = state.get("frames", {}).get(frame)
        if entry is None:
            return None
        self._trust(state)
        active = self._snapshot(frame, entry["active"], state)
        pending = self._snapshot(frame, entry["candidate"], state, candidate=True)
        return replace(active, alternatives=(pending,)) if active and pending else active or pending

    def _select(self, lease):
        from .host_heartbeat_core import _authority_matches

        snapshot = self.watch_state(lease.frame_id)
        for selected in () if snapshot is None else (snapshot, *snapshot.alternatives):
            if _authority_matches(lease, selected.authority):
                return selected
        raise ControlPlaneError("heartbeat requires a current operator-granted incarnation")

    def heartbeat_reference(self, lease):
        return self._select(lease).carrier_ref

    def heartbeat(self, lease, *, signing_key):
        from . import host_heartbeat_core as hb

        policy = self._policy()
        if policy["client"]["role"] != "frame":
            raise ControlPlaneError(
                "runtime publication requires a separate protected frame anchor"
            )
        snapshot = self._select(lease)
        if lease.seq <= snapshot.sequence:
            raise ControlPlaneError("heartbeat replays durable accepted sequence floor")
        result = hb.publish(
            self.hq_dir,
            lease,
            signing_key=signing_key,
            remote=self._remote(policy),
            reference=snapshot.carrier_ref,
            transport_options=["-c", "protocol.ext.allow=always"],
            trusted_authority_lookup=lambda *_: self._select(lease),
        )
        self._select(lease)  # reject a revocation race on readback
        return result

    def fetch_config(self, frame, *, holder_identity=None):
        _, state, _ = self._read()
        entry = state.get("frames", {}).get(frame)
        if entry is None:
            raise ControlPlaneError("unknown declared frame")
        # BH_HQ_AUTHORITY_ENFORCE=false (UNSUPPORTED, bh-6pqul): an expired grant still names
        # this frame's desired state; only a retired incarnation is unavailable.
        enforce = authority_enforced()
        available = [
            record
            for slot in ("active", "candidate")
            if (record := entry[slot]) is not None
            and (
                self._snapshot(frame, record, state, candidate=slot == "candidate") is not None
                if enforce
                else record["state"] != "retired"
            )
            and (
                holder_identity is None or record["authority"]["holder_identity"] == holder_identity
            )
        ]
        if not available:
            raise ControlPlaneError("unknown, expired or retired frame incarnation")
        if len(available) != 1:
            raise ControlPlaneError(
                "config requires exact holder identity for coexisting incarnations"
            )
        record = available[0]
        return {
            **record["desired"],
            **({"emergency": record["emergency"]} if "emergency" in record else {}),
            "state": record["state"],
            "cordoned": record["cordoned"],
            "authority": record["authority"],
        }

    def publish_registration(self, manifest, *, attempts=3):
        from . import host, hosts, hq_manifest_publication

        record = hosts.load(self.hq_dir, manifest)
        remote, signing_key, ssh_keygen = None, None, None
        if record.frame_id:
            policy = self._policy()
            remote = self._remote(policy)
            signing_key = host.signing_key()
            if not signing_key:
                raise hq_manifest_publication.HostPublicationError(
                    "frame registration requires its own runtime signing key"
                )
            ssh_keygen = policy["executables"]["ssh_keygen"]["path"]
        changed = hq_manifest_publication._publish_host_manifest_git(
            self.hq_dir,
            manifest,
            attempts=attempts,
            remote_override=remote,
            signing_key=signing_key,
            ssh_keygen=ssh_keygen,
        )
        if record.frame_id:
            self.publish_registration_evidence(record, signing_key=signing_key)
        return changed

    def publish_registration_evidence(self, manifest, *, signing_key):
        from .beadyard_identity_file import read_identity

        if manifest.beadyard_id != read_identity(self.hq_dir):
            raise ControlPlaneError("registration belongs to another beadyard")
        policy = self._policy()
        # Pending registration is authenticated evidence, never an authority grant.
        public_path = Path(signing_key + ".pub")
        if not public_path.exists():
            public_path = Path(signing_key)
        fp = fingerprint(public_path.read_text())
        reference = registration_ref(
            {
                "frame_id": manifest.frame_id,
                "holder_identity": manifest.host_id,
                "instance_ref": manifest.instance_ref,
                "key_fingerprint": fp,
            }
        )
        remote = self._remote(policy)
        expected = gitref.remote_sha(
            remote, reference, cwd=self.hq_dir, git_options=["-c", "protocol.ext.allow=always"]
        )
        blob = _git(
            self.hq_dir,
            "hash-object",
            "-w",
            "--stdin",
            data=gitref.encode(manifest.model_dump(mode="json", exclude_none=True)),
        )
        tree = _git(self.hq_dir, "mktree", data=f"100644 blob {blob}\tregistration.json\n")
        sha = _git(
            self.hq_dir,
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={signing_key}",
            "commit-tree",
            "-S",
            tree,
            data="Frame candidate registration\n",
        )
        _git(
            self.hq_dir,
            "-c",
            "protocol.ext.allow=always",
            "push",
            f"--force-with-lease={reference}:{expected}",
            remote,
            f"{sha}:{reference}",
        )
        return sha

    def grant(self, authority, public_key, desired, *, expected, operator_key):
        from .beadyard_identity_file import read_identity
        from .hq_authority_payload import authority_payload

        if authority.beadyard_id != read_identity(self.hq_dir):
            raise ControlPlaneError("candidate belongs to a different beadyard")
        sha, state, policy = self._operator_read()
        if (
            sha != expected
            or authority.candidate_expires_at is None
            or authority.candidate_expires_at <= self.clock()
        ):
            raise ControlPlaneError(
                "candidate grant requires exact revision and bounded future expiry"
            )
        fp = fingerprint(public_key)
        if (
            fp != authority.key_fingerprint
            or fp in policy["operator_fingerprints"]
            or any(
                r["authority"]["key_fingerprint"] == fp
                for _, r in guard.records(state or {"frames": {}})
            )
        ):
            raise ControlPlaneError(
                "candidate key mismatch, operator key reuse, or incarnation signer reuse"
            )
        if any(
            r["authority"]["holder_identity"] == authority.holder_identity
            for _, r in guard.records(state or {"frames": {}})
        ):
            raise ControlPlaneError("candidate holder identity already names an incarnation")
        frames = state.setdefault("frames", {})
        entry = frames.setdefault(
            authority.frame_id,
            {"active": None, "candidate": None, "retired": [], "epoch_floor": -1},
        )
        if entry["candidate"] is not None or authority.epoch <= entry["epoch_floor"]:
            raise ControlPlaneError(
                "candidate exists or epoch does not advance operator-granted floor"
            )
        entry["candidate"] = {
            "authority": authority_payload(authority),
            "public_key": public_key.strip(),
            "state": "pending",
            "desired": desired,
            "cordoned": False,
            "drain_deadline": None,
            "receipt": {
                "sequence": 0,
                "sha": "",
                "first_seen": None,
                "consecutive": 0,
                "lease": None,
                "registration": None,
            },
        }
        entry["epoch_floor"] = authority.epoch
        return self._write(state, expected, operator_key)

    def bind_beadyard(self, *, expected, operator_key):
        """Atomically bind every live legacy frame without replacing its incarnation.

        The old signed runtime/registration carriers remain recorded as history,
        but do not qualify new intake until the same signer publishes fresh v2
        evidence and the ordinary observation path accepts it.
        """
        from .beadyard_identity import parse_document
        from .beadyard_identity_file import read_identity

        owner = read_identity(self.hq_dir)
        main_document = _git(self.hq_dir, "show", "main:beadyard.json")
        if owner is None or parse_document(main_document) != owner:
            raise ControlPlaneError("canonical Git HQ identity unavailable")
        snapshot = self.config_store().load_snapshot()
        if snapshot.beadyard_id != owner:
            raise ControlPlaneError("committed fleet config belongs to another beadyard")
        sha, state, _policy = self._operator_read()
        if sha != expected or state.get("domain") != guard.DOMAIN:
            raise ControlPlaneError("exact legacy authority revision required for binding")
        now = self.clock()
        remaining = int(state["expires_at"] - now)
        if remaining < 1:
            raise ControlPlaneError("expired authority cannot be revived by identity binding")
        live = [
            record
            for entry in state["frames"].values()
            for record in (entry["active"], entry["candidate"])
            if record is not None
        ]
        if not live:
            raise ControlPlaneError("no live legacy grants require identity binding")
        for record in live:
            authority = record["authority"]
            expiry = authority["candidate_expires_at"]
            if (
                "beadyard_id" in authority
                or (
                    expiry is None
                    and record["state"] not in {"active", "draining", "drained", "parked"}
                )
                or (expiry is not None and expiry <= now)
            ):
                raise ControlPlaneError("expired or already bound grant cannot be rebound")
            authority["beadyard_id"] = owner
        return self._write(
            state,
            expected,
            operator_key,
            duration=remaining,
            expires_at_cap=state["expires_at"],
        )

    def accept_observation(self, frame, *, expected, operator_key, holder_identity=""):
        from . import host_heartbeat_core as hb
        from . import hosts

        sha, state, policy = self._operator_read()
        if sha != expected:
            raise ControlPlaneError("expected authority revision changed")
        entry = state["frames"].get(frame)
        record = entry["active"] or entry["candidate"] if entry else None
        candidate = bool(entry and record is entry["candidate"])
        if holder_identity and entry:
            for slot in ("active", "candidate"):
                selected = entry[slot]
                if selected and selected["authority"]["holder_identity"] == holder_identity:
                    record, candidate = selected, slot == "candidate"
                    break
            else:
                raise ControlPlaneError("unknown granted holder")
        if record is None:
            raise ControlPlaneError("unknown or retired candidate")
        authority = hb.ObservationAuthority(**record["authority"])
        now = self.clock()
        if authority.candidate_expires_at is not None and now >= authority.candidate_expires_at:
            raise ControlPlaneError("candidate grant expired")
        reference = candidate_ref(frame) if candidate else hb.ref_name(frame)
        remote = self._remote(policy)
        beat = gitref.remote_sha(
            remote, reference, cwd=self.hq_dir, git_options=["-c", "protocol.ext.allow=always"]
        )
        if not beat:
            raise ControlPlaneError("heartbeat absent")
        _git(self.hq_dir, "-c", "protocol.ext.allow=always", "fetch", "--no-tags", remote, beat)
        envelope = hb.framelease_envelope(self.hq_dir, beat, self._trust(state))
        payload = {k: v for k, v in envelope["spec"].items() if k != "signature"}
        lease = hb.HeartbeatLease.model_validate(payload)
        if not hb._authority_matches(lease, authority):
            raise ControlPlaneError("ungranted heartbeat incarnation")
        receipt = record["receipt"]
        if lease.seq == receipt["sequence"] and beat == receipt["sha"]:
            return sha
        if lease.seq <= receipt["sequence"]:
            raise ControlPlaneError("heartbeat replays accepted sequence floor")
        age = now - lease.observed_at
        if age < -30 or age >= lease.leaseDurationSeconds:
            raise ControlPlaneError("first observation is expired or future-skewed")
        same_identity = receipt["lease"] is None or receipt["lease"].get(
            "beadyard_id"
        ) == payload.get("beadyard_id")
        streak = (
            receipt["consecutive"] + 1
            if same_identity and lease.seq == receipt["sequence"] + 1
            else 1
        )
        if (
            receipt["lease"]
            and now - receipt["first_seen"] >= receipt["lease"]["leaseDurationSeconds"]
        ):
            streak = 1
        registration_sha = gitref.remote_sha(
            remote,
            registration_ref(record["authority"]),
            cwd=self.hq_dir,
            git_options=["-c", "protocol.ext.allow=always"],
        )
        registration = None
        if registration_sha:
            _git(
                self.hq_dir,
                "-c",
                "protocol.ext.allow=always",
                "fetch",
                "--no-tags",
                remote,
                registration_sha,
            )
            if (
                hb._fingerprint(self.hq_dir, registration_sha, self._trust(state))
                != authority.key_fingerprint
            ):
                raise ControlPlaneError("registration signer mismatch")
            if (
                _git(self.hq_dir, "show", "-s", "--format=%P", registration_sha)
                or _git(self.hq_dir, "ls-tree", "--name-only", registration_sha)
                != "registration.json"
                or int(_git(self.hq_dir, "cat-file", "-s", f"{registration_sha}:registration.json"))
                > hb.MAX_BYTES
            ):
                raise ControlPlaneError("invalid registration carrier")
            manifest = hosts.HostManifest.model_validate_json(
                _git(self.hq_dir, "show", f"{registration_sha}:registration.json")
            )
            if (
                manifest.frame_id,
                manifest.host_id,
                manifest.instance_ref,
                manifest.beadyard_id,
            ) != (
                frame,
                authority.holder_identity,
                authority.instance_ref,
                authority.beadyard_id,
            ):
                raise ControlPlaneError("registration incarnation mismatch")
            registration = manifest.model_dump(mode="json", exclude_none=True)
        record["receipt"] = {
            "sequence": lease.seq,
            "sha": beat,
            "first_seen": now,
            "consecutive": streak,
            "lease": payload,
            "registration": registration,
        }
        if record["state"] == "draining" and lease.state_seen == "drained":
            record["state"] = "drained"
        return self._write(state, expected, operator_key)

    def lifecycle(
        self,
        verb,
        frame,
        action="plan",
        *,
        expected="",
        expected_host_id="",
        expected_release="",
        operator_key="",
        confirm=False,
        supersede=False,
        deadline=None,
        emergency_prefix="",
        emergency_reason="",
        emergency_duration=600,
        execution_digest="",
    ):
        from .host_heartbeat_core import HeartbeatLease, ref_name

        sha, state, policy = self._operator_read()
        entry = state.get("frames", {}).get(frame)
        if entry is None:
            raise ControlPlaneError("frame has no operator grant")
        record = (
            entry["candidate"]
            if verb == "admit" and entry["candidate"]
            else entry["active"] or entry["candidate"]
        )
        if expected_host_id:
            matching = [
                r
                for r in (entry["active"], entry["candidate"], *reversed(entry["retired"]))
                if r and r["authority"]["holder_identity"] == expected_host_id
            ]
            if matching:
                record = matching[0]
        if record is None:
            record = entry["retired"][-1] if entry["retired"] else None
        if record is None:
            raise ControlPlaneError("unknown incarnation")
        from . import frame_emergency

        a, r = record["authority"], record["receipt"]
        lease, current = r["lease"], record["state"]
        release = lease["release"]["digest"] if lease else ""
        result = {
            "frame_id": frame,
            "host_id": a["holder_identity"],
            "instance_ref": a["instance_ref"],
            "key_fingerprint": a["key_fingerprint"],
            "epoch": a["epoch"],
            "revision": sha,
            "state": current,
            "cordoned": record["cordoned"],
            "release": release,
            "toplevel": lease.get("toplevel") if lease else None,
            "image": lease.get("image") if lease else None,
            "capabilities": (r["registration"] or {}).get("capabilities"),
            "attestor_evidence_kind": "ssh-runtime-signature",
            "consecutive_verified_beats": r["consecutive"],
            "conformance": lease["conformance"] if lease else None,
            "emergency": frame_emergency.status(record, self.clock()),
            "prior_active": entry["active"]["authority"]
            if record is entry["candidate"] and entry["active"]
            else None,
        }
        if action in {"plan", "check"}:
            return result
        if action != "apply" or not confirm or not operator_key:
            raise ControlPlaneError("apply requires explicit --confirm and separate operator key")
        if (
            expected != sha
            or expected_host_id != a["holder_identity"]
            or expected_release != release
        ):
            raise ControlPlaneError("expected authoritative revision/host/release mismatch")
        updates = []
        remote = self._remote(policy)
        if verb in {"emergency-admit", "emergency-revoke"}:
            now = self.clock()
            if state["expires_at"] <= now:
                raise ControlPlaneError("emergency mutation requires current authority")
            if verb == "emergency-admit":
                from .host_manifest_contracts import HostManifest
                from .hq_authority_guard import requirements

                hive = policy.get("hive_policies", {}).get(emergency_prefix)
                if hive is None or now >= hive["valid_until"]:
                    raise ControlPlaneError("emergency requires explicit current hive scope")
                requirements(hive["requires"], record["desired"]["caps"])
                if hive["config_revision"] != a["config_revision"]:
                    raise ControlPlaneError("emergency hive config revision mismatch")
                if record is entry["candidate"] and entry["active"]:
                    raise ControlPlaneError("emergency cannot supersede active incarnation")
                registration = (
                    HostManifest.model_validate(r["registration"]) if r["registration"] else None
                )
                parsed = HeartbeatLease.model_validate(lease) if lease else None
                frame_emergency.authorize(
                    record,
                    prefix=emergency_prefix,
                    reason=emergency_reason,
                    duration=emergency_duration,
                    revision=sha,
                    now=now,
                    registration=registration,
                    beat=parsed,
                    execution_digest=execution_digest,
                )
                if record is entry["candidate"]:
                    entry["active"], entry["candidate"] = record, None
                    reference = ref_name(frame)
                    updates.append(
                        (
                            reference,
                            gitref.remote_sha(
                                remote,
                                reference,
                                cwd=self.hq_dir,
                                git_options=["-c", "protocol.ext.allow=always"],
                            ),
                            r["sha"],
                        )
                    )
                    updates.append((candidate_ref(frame), r["sha"], ""))
            else:
                frame_emergency.revoke(record, now)
        elif verb == "admit":
            reviewing_emergency = current == "active" and record.get("emergency", {}).get(
                "review_required"
            )
            if current == "active" and not record["cordoned"] and not reviewing_emergency:
                return result
            if not reviewing_emergency and (
                record is not entry["candidate"] or current != "pending"
            ):
                raise ControlPlaneError("admit requires pending candidate")
            desired, registration = record["desired"], r["registration"]
            if not desired["declared"] or not registration:
                raise ControlPlaneError(
                    "admit requires declared frame and authenticated registration"
                )
            if not lease or r["consecutive"] < 3:
                raise ControlPlaneError("admit requires three consecutive verified beats")
            parsed = HeartbeatLease.model_validate(lease)
            now = self.clock()
            if (
                (a["candidate_expires_at"] is not None and now >= a["candidate_expires_at"])
                or now - r["first_seen"] >= parsed.leaseDurationSeconds
                or now - parsed.observed_at >= parsed.leaseDurationSeconds
                or now >= state["expires_at"]
            ):
                raise ControlPlaneError("admit requires fresh trusted observation")
            if (
                lease["release"] != desired["release"]
                or registration.get("release") != desired["release"]
            ):
                raise ControlPlaneError("admit release mismatch")
            if registration.get("capabilities") != desired["caps"]:
                raise ControlPlaneError("admit capability mismatch")
            conformance = lease["conformance"]
            if (
                conformance["profile"] != desired["profile"]
                or conformance["status"] != "conformant"
                or not conformance["checks"]
                or any(c["status"] == "fail" for c in conformance["checks"])
            ):
                raise ControlPlaneError("admit requires conformant evidence")
            if reviewing_emergency:
                record["emergency"].update(review_required=False, revoked_at=now)
                record["cordoned"] = False
                self._write(state, sha, operator_key)
                frame_emergency.audit("reviewed-admission", record, revision=sha)
                return self.lifecycle(verb, frame, "check", expected_host_id=expected_host_id)
            if entry["active"] and not supersede:
                raise ControlPlaneError("another active incarnation requires explicit --supersede")
            if entry["active"]:
                old = entry["active"]
                old.update(state="retired", cordoned=True)
                entry["retired"].append(old)
                old_registration = registration_ref(old["authority"])
                old_sha = gitref.remote_sha(
                    remote,
                    old_registration,
                    cwd=self.hq_dir,
                    git_options=["-c", "protocol.ext.allow=always"],
                )
                if old_sha:
                    updates.append((old_registration, old_sha, ""))
            entry["active"], entry["candidate"] = record, None
            record.update(state="active", cordoned=False)
            a["candidate_expires_at"] = None
            reference = ref_name(frame)
            updates.append(
                (
                    reference,
                    gitref.remote_sha(
                        remote,
                        reference,
                        cwd=self.hq_dir,
                        git_options=["-c", "protocol.ext.allow=always"],
                    ),
                    r["sha"],
                )
            )
            updates.append((candidate_ref(frame), r["sha"], ""))
        elif verb == "retire":
            if current == "retired":
                return result
            slot = "candidate" if record is entry["candidate"] else "active"
            record.update(state="retired", cordoned=True)
            entry[slot] = None
            entry["retired"].append(record)
            for reference in (
                candidate_ref(frame) if slot == "candidate" else ref_name(frame),
                registration_ref(a),
            ):
                old = gitref.remote_sha(
                    remote,
                    reference,
                    cwd=self.hq_dir,
                    git_options=["-c", "protocol.ext.allow=always"],
                )
                if old:
                    updates.append((reference, old, ""))
        elif verb == "cordon":
            if current != "active":
                raise ControlPlaneError("cordon requires active frame")
            if record["cordoned"]:
                return result
            record["cordoned"] = True
        else:
            transitions = {
                "drain": ({"active", "draining"}, "draining"),
                "park": ({"drained", "parked"}, "parked"),
                "resume": ({"parked", "active"}, "active"),
                "quarantine": (
                    {"pending", "active", "draining", "drained", "parked", "quarantined"},
                    "quarantined",
                ),
            }
            if verb not in transitions or current not in transitions[verb][0]:
                raise ControlPlaneError("illegal lifecycle transition")
            target = transitions[verb][1]
            if current == target and not (verb == "resume" and record["cordoned"]):
                return result
            if verb == "drain":
                if deadline is None or deadline <= self.clock():
                    raise ControlPlaneError("drain requires future deadline")
                record["drain_deadline"] = deadline
            record.update(state=target, cordoned=verb != "resume")
        self._write(state, expected, operator_key, updates=updates)
        if verb in {"emergency-admit", "emergency-revoke"}:
            frame_emergency.audit(verb, record, revision=sha)
        # Return the exact selected incarnation's authoritative state after readback.
        return self.lifecycle(verb, frame, "check", expected_host_id=expected_host_id)


def _validated_sql_binding(settings):
    """Do not render Pydantic's raw rejected HOST values, which may be secrets."""
    from pydantic import ValidationError

    from .modules.config.contracts import HqSqlConfig

    try:
        return HqSqlConfig.model_validate(settings)
    except (ValidationError, TypeError, ValueError):
        raise ControlPlaneError("invalid SQL HOST binding") from None


class SqlControlPlane:
    """SQL config capability; runtime authority needs an explicit separate binding."""

    def release_upgrade(self, frame, action="plan", **kwargs):
        """Rotate a pending release through an exact reviewed, operator-signed SQL CAS."""
        from .frame_release_upgrade import release_upgrade

        return release_upgrade(self, frame, action, **kwargs)

    config_backend = "sql"

    @staticmethod
    def verified_manifest_absence(error: BaseException) -> bool:
        return isinstance(error, CommittedManifestAbsent)

    def __init__(self, settings, *, broker=None, clock=time.time):
        from .hq_sql_runtime import liveness_mode

        self.settings = _validated_sql_binding(settings).model_dump()
        self.broker, self.clock = broker, clock
        try:
            # Fail at selection, not mid-read, on a malformed BH_HQ_SQL_LIVENESS.
            liveness_mode(self.settings)
        except ValueError as exc:
            raise ControlPlaneError(str(exc)) from None

    def load_host_manifest(self, host_id: str):
        from ruamel.yaml import YAML

        from . import hosts

        snapshot = self.config_store().load_snapshot()
        documents = [item for item in snapshot.documents if item.path == f"hosts/{host_id}.yaml"]
        if not documents:
            raise CommittedManifestAbsent(f"committed host manifest unavailable for {host_id}")
        if len(documents) != 1:
            raise ControlPlaneError("committed host manifest selection invalid")
        try:
            raw = YAML(typ="safe").load(documents[0].content)
            if isinstance(raw, dict) and "state" not in raw:
                raw = {**raw, "state": "active"}
            return hosts.HostManifest.model_validate(raw)
        except Exception:  # noqa: BLE001 - malformed committed input must not expose values
            raise ControlPlaneError("committed host manifest invalid") from None

    def config_store(self, *, operator_key=None, duration=3600, ceiling=None):
        from .hq_sql_config import SqlFleetConfigRevisionStore

        return SqlFleetConfigRevisionStore(self.settings, broker=self.broker, clock=self.clock)

    def _runtime_authority(self):
        from .hq_sql_runtime import SqlRuntimeAuthority

        if self.settings.get("runtime") is None:
            raise ControlPlaneError("AUTHORITY_NOT_READY: SQL runtime binding unavailable")
        return SqlRuntimeAuthority(self.settings, broker=self.broker, clock=self.clock)

    def authority_status(self):
        if self.settings.get("runtime") is None:
            return {"revision": "", "state": "AUTHORITY_NOT_READY", "authority_ready": False}
        enforce = authority_enforced()
        try:
            # Disabled enforcement (UNSUPPORTED, bh-6pqul) still reports an expired authority,
            # honestly marked not ready, instead of failing the status read.
            revision, state, _, _ = self._runtime_authority().load_state(allow_expired=not enforce)
        except ValueError as exc:
            raise ControlPlaneError(str(exc)) from None
        ready = enforce or self.clock() < state["expires_at"]
        return {"revision": revision, "state": state, "authority_ready": ready}

    def eligibility_authority_status(self):
        if self.settings.get("runtime") is None:
            return self.authority_status()
        try:
            head, state, *_ = self._runtime_authority().read_frame_composite()
            return {"revision": head, "state": state, "authority_ready": True}
        except ValueError:
            raise ControlPlaneError("qualified SQL authority unavailable") from None

    def fetch_config(self, frame, *, holder_identity=None):
        try:
            (_head, _state, route, _slot, record, _snapshot, _policies, _observation, _lease) = (
                self._runtime_authority().read_frame_composite()
            )
            if frame != route.frame_id or (
                holder_identity is not None and holder_identity != route.holder_identity
            ):
                raise ControlPlaneError("frame config identity differs from authenticated grant")
            return {
                **record["desired"],
                **({"emergency": record["emergency"]} if "emergency" in record else {}),
                "state": record["state"],
                "cordoned": record["cordoned"],
                "authority": record["authority"],
            }
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("qualified SQL frame config unavailable") from None

    def observe(self, manifest, *, now=None, observer_dir=None):
        if observer_dir is not None:
            raise ControlPlaneError("SQL observer storage is protected server-local custody")
        return self.read_eligibility(manifest, now=now)[2]

    def watch_state(self, frame):
        try:
            (head, state, route, slot, record, _snapshot, _policies, row, _lease) = (
                self._runtime_authority().read_frame_composite()
            )
            if frame != route.frame_id:
                return None
            return self._authority_snapshot(state, route, slot, record, row)
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("qualified SQL frame state unavailable") from None

    def _authority_snapshot(self, state, route, slot, record, row):
        from .host_heartbeat_core import AuthoritySnapshot, ObservationAuthority

        authority = ObservationAuthority(**record["authority"])
        checked = self.clock()
        validity = min(
            state["expires_at"],
            authority.candidate_expires_at or state["expires_at"],
        )
        if row is None:
            sequence, digest, first_seen = 0, "", None
        else:
            observation = self._public_observation(
                row,
                route,
                slot,
                granted_public_key=record["public_key"],
                now=checked,
                signed=self.signed_liveness,
            )
            sequence, digest = row[0], row[1]
            first_seen = row[2] - observation.lease.leaseDurationSeconds
        return AuthoritySnapshot(
            authority,
            checked,
            validity,
            sequence,
            digest,
            first_seen,
            record["state"],
            record["state"] == "active" and not record["cordoned"],
            f"sql:observation/{route.principal}/{route.epoch}",
        )

    def load_config_authority_snapshot(self, frame, *, revision=None):
        try:
            (head, state, route, slot, record, snapshot, _policies, row, _lease) = (
                self._runtime_authority().read_frame_composite()
            )
            if (
                frame != route.frame_id
                or revision is not None
                and revision != snapshot.commit_revision
            ):
                raise ControlPlaneError(
                    "SQL config/authority snapshot identity or revision mismatch"
                )
            authority = self._authority_snapshot(state, route, slot, record, row)
            return ConfigAuthoritySnapshot(
                HqConsistencyToken(snapshot.backend_identity, snapshot.generation, head),
                snapshot,
                authority,
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("qualified SQL config/authority snapshot unavailable") from None

    def heartbeat_reference(self, lease):
        from .hq_framelease_contracts import HeartbeatLease

        lease = HeartbeatLease.model_validate(lease)
        (_head, _state, route, _slot, record, _snapshot, _policies, _row, _lease) = (
            self._runtime_authority().read_frame_composite()
        )
        authority = record["authority"]
        if (
            lease.frame_id != route.frame_id
            or lease.holderIdentity != route.holder_identity
            or lease.instance_ref != route.instance_ref
            or lease.epoch != route.epoch
            or lease.key_id != route.signer_fingerprint
            or lease.audience != authority["audience"]
            or lease.config_revision != authority["config_revision"]
            or lease.beadyard_id != authority.get("beadyard_id")
        ):
            raise ControlPlaneError("heartbeat reference differs from current grant")
        return f"sql:inbox/{route.principal}/{route.epoch}"

    def publish_registration_evidence(self, manifest, *, signing_key, deadline=None):
        import uuid

        from . import hosts
        from .hq_sql_signatures import sign_registration

        manifest = hosts.HostManifest.model_validate(manifest)
        runtime = self._runtime_authority()
        if deadline is None:
            deadline = time.monotonic() + self.settings["runtime"]["operation_timeout"]
        try:
            (head, _state, route, _slot, record, _snapshot, _policies, _observation, _lease) = (
                runtime.read_frame_composite(deadline=deadline)
            )
            authority = record["authority"]
            if (
                manifest.frame_id != route.frame_id
                or manifest.host_id != route.holder_identity
                or manifest.instance_ref != route.instance_ref
                or authority["key_fingerprint"] != route.signer_fingerprint
                or manifest.beadyard_id != _snapshot.beadyard_id
                or manifest.beadyard_id != authority.get("beadyard_id")
            ):
                raise ControlPlaneError("registration manifest differs from current grant")
            request_id = str(uuid.uuid4())
            request = {
                "domain": (
                    "beadhive/sql-registration/v2"
                    if manifest.beadyard_id is not None
                    else "beadhive/sql-registration/v1"
                ),
                "request_id": request_id,
                "principal": route.principal,
                "frame_id": route.frame_id,
                "holder_identity": route.holder_identity,
                "instance_ref": route.instance_ref,
                "epoch": route.epoch,
                "key_fingerprint": route.signer_fingerprint,
                "audience": authority["audience"],
                "authority_revision": head,
                "manifest": manifest.model_dump(mode="json", exclude_none=True),
            }
            if manifest.beadyard_id is not None:
                request["beadyard_id"] = manifest.beadyard_id
            _, digest = runtime.publish_inbox(
                "registration",
                sign_registration(request, signing_key=signing_key),
                binding=route,
                expected_head=head,
                request_id=request_id,
                deadline=deadline,
            )
            return digest
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("SQL registration evidence unavailable") from None

    def publish_registration(self, manifest, *, attempts=3):
        from ruamel.yaml import YAML

        from . import host, hosts

        if type(attempts) is not int or attempts < 1:
            raise ControlPlaneError("registration attempt bound invalid")
        if self.settings.get("runtime") is None:
            raise ControlPlaneError("AUTHORITY_NOT_READY: SQL runtime binding unavailable")
        deadline = time.monotonic() + self.settings["runtime"]["operation_timeout"]
        if isinstance(manifest, hosts.HostManifest):
            record = manifest
        elif isinstance(manifest, str):
            (*_other, snapshot, _policies, _observation, _lease) = (
                self._runtime_authority().read_frame_composite(deadline=deadline)
            )
            documents = [
                document
                for document in snapshot.documents
                if document.path == f"hosts/{manifest}.yaml"
            ]
            if len(documents) != 1:
                raise ControlPlaneError("committed host manifest unavailable")
            record = hosts.HostManifest.model_validate(YAML(typ="safe").load(documents[0].content))
        else:
            raise ControlPlaneError("validated registration manifest required")
        key = host.signing_key()
        if not key:
            raise ControlPlaneError("frame registration requires its own runtime signing key")
        self.publish_registration_evidence(record, signing_key=key, deadline=deadline)
        return True

    def _operator(self):
        from .hq_sql_operator import SqlRuntimeOperator

        return SqlRuntimeOperator(self.settings, broker=self.broker, clock=self.clock)

    def _operator_deadline(self):
        binding = self.settings.get("authority_writer")
        if binding is None:
            raise ControlPlaneError("separate authority writer capability unavailable")
        return time.monotonic() + binding["operation_timeout"]

    def grant(self, authority, public_key, desired, *, expected, operator_key):
        from .host_heartbeat_core import ObservationAuthority
        from .hq_authority_payload import authority_payload
        from .hq_sql_operator import SqlRuntimeOperator
        from .hq_sql_signatures import fingerprint

        if not isinstance(authority, ObservationAuthority):
            raise ControlPlaneError("validated candidate authority required")
        operator = self._operator()
        budget = self._operator_deadline()
        try:
            head, original, _crossref, _policies = operator.load(deadline=budget)
            if (
                head != expected
                or authority.candidate_expires_at is None
                or authority.candidate_expires_at <= self.clock()
                or fingerprint(public_key) != authority.key_fingerprint
                or authority.key_fingerprint
                == fingerprint(self.settings["runtime_operator_public_key"])
                or any(
                    record["authority"]["key_fingerprint"] == authority.key_fingerprint
                    or record["authority"]["holder_identity"] == authority.holder_identity
                    for _, record in guard.records(original)
                )
            ):
                raise ControlPlaneError("candidate grant identity or original CAS invalid")
            state = json.loads(json.dumps(original))
            entry = state["frames"].setdefault(
                authority.frame_id,
                {"active": None, "candidate": None, "retired": [], "epoch_floor": -1},
            )
            if entry["candidate"] is not None or authority.epoch <= entry["epoch_floor"]:
                raise ControlPlaneError("candidate exists or epoch does not advance")
            entry["candidate"] = {
                "authority": authority_payload(authority),
                "public_key": public_key.strip(),
                "state": "pending",
                "desired": desired,
                "cordoned": False,
                "drain_deadline": None,
                "receipt": {
                    "sequence": 0,
                    "sha": "",
                    "first_seen": None,
                    "consecutive": 0,
                    "lease": None,
                    "registration": None,
                },
            }
            entry["epoch_floor"] = authority.epoch
            state["revision"] += 1
            state["issued_at"] = self.clock()
            state["expires_at"] = state["issued_at"] + 3600
            state["domain"] = (
                guard.DOMAIN_V2
                if any(
                    row["authority"].get("beadyard_id") is not None
                    for _, row in guard.records(state)
                )
                else guard.DOMAIN
            )
            guard.validate_state(state)
            principal = SqlRuntimeOperator.principal_for(authority)
            return operator.publish(
                state,
                expected_revision=expected,
                operator_key=operator_key,
                provisioned_route=(
                    principal,
                    authority.frame_id,
                    authority.holder_identity,
                    authority.instance_ref,
                    authority.epoch,
                    authority.key_fingerprint,
                ),
                deadline=budget,
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("SQL candidate grant unavailable") from None

    def prune_inbox(self, frame, *, retention_s, dry_run=False):
        """Operator-side bounded retention of signed-mode heartbeat inbox rows (bh-ce886)."""
        operator = self._operator()
        budget = self._operator_deadline()
        try:
            return operator.prune_inbox(
                frame, retention_s=retention_s, dry_run=dry_run, deadline=budget
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            # SqlOperatorError messages are fixed text, never row or credential content.
            raise ControlPlaneError(f"SQL inbox prune unavailable: {exc}") from None

    def bind_beadyard(self, *, expected, operator_key):
        """Bind the existing SQL authority ledger to its current committed HQ ID."""
        operator = self._operator()
        budget = self._operator_deadline()
        try:
            snapshot = self.config_store().load_snapshot()
            owner = snapshot.beadyard_id
            local_pin = config.load_host().get("hq", {}).get("beadyard_id")
            if owner is None or local_pin != owner:
                raise ControlPlaneError("SQL HQ identity and local pin must agree")
            head, original, _crossref, _policies = operator.load(deadline=budget)
            if head != expected or original["domain"] != guard.DOMAIN:
                raise ControlPlaneError("exact legacy authority revision required for binding")
            now = self.clock()
            remaining = int(original["expires_at"] - now)
            if remaining < 1:
                raise ControlPlaneError("expired authority cannot be revived by identity binding")
            state = json.loads(json.dumps(original))
            live = [
                record
                for entry in state["frames"].values()
                for record in (entry["active"], entry["candidate"])
                if record is not None
            ]
            if not live:
                raise ControlPlaneError("no live legacy grants require identity binding")
            for record in live:
                authority = record["authority"]
                expiry = authority["candidate_expires_at"]
                if (
                    "beadyard_id" in authority
                    or (
                        expiry is None
                        and record["state"] not in {"active", "draining", "drained", "parked"}
                    )
                    or (expiry is not None and expiry <= now)
                ):
                    raise ControlPlaneError("expired or already bound grant cannot be rebound")
                authority["beadyard_id"] = owner
            state.update(
                domain=guard.DOMAIN_V2,
                revision=original["revision"] + 1,
                issued_at=now,
                expires_at=now + remaining,
            )
            guard.validate_state(state)
            guard.validate_legacy_binding_transition(original, state, trusted_now=now)
            return operator.publish(
                state, expected_revision=expected, operator_key=operator_key, deadline=budget
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("SQL authority identity binding unavailable") from None

    def renew(self, *, expected, operator_key, duration=3600, ceiling=None):
        _check_duration(duration, ceiling)
        operator = self._operator()
        budget = self._operator_deadline()
        try:
            head, current, _crossref, _policies = operator.load(deadline=budget)
            if head != expected:
                raise ControlPlaneError("original authority revision changed")
            state = json.loads(json.dumps(current))
            state["revision"] += 1
            state["issued_at"] = self.clock()
            state["expires_at"] = self.clock() + duration
            return operator.publish(
                state,
                expected_revision=expected,
                operator_key=operator_key,
                deadline=budget,
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("SQL authority renewal unavailable") from None

    def accept_observation(self, frame, *, expected, operator_key, holder_identity=""):
        operator = self._operator()
        budget = self._operator_deadline()
        try:
            head, original, _crossref, _policies = operator.load(deadline=budget)
            if head != expected:
                raise ControlPlaneError("original authority revision changed")
            entry = original.get("frames", {}).get(frame)
            if entry is None:
                raise ControlPlaneError("unknown frame observation")
            selected = [
                record
                for record in (entry["active"], entry["candidate"])
                if record is not None
                and (
                    not holder_identity or record["authority"]["holder_identity"] == holder_identity
                )
            ]
            if len(selected) != 1:
                raise ControlPlaneError("observation requires exact granted holder")
            holder = selected[0]["authority"]["holder_identity"]
            record, _registration, receipts = operator.evidence(
                frame, holder, expected_revision=head, deadline=budget
            )
            if not receipts:
                raise ControlPlaneError("accepted protected heartbeat absent")
            latest = receipts[0][3]
            if self.clock() - receipts[0][2] >= latest.leaseDurationSeconds:
                raise ControlPlaneError("accepted protected heartbeat expired")
            if record["state"] != "draining" or latest.state_seen != "drained":
                return head
            state = json.loads(json.dumps(original))
            target = state["frames"][frame]
            for slot in ("active", "candidate"):
                candidate = target[slot]
                if candidate and candidate["authority"]["holder_identity"] == holder:
                    candidate["state"] = "drained"
                    break
            state["revision"] += 1
            state["issued_at"] = self.clock()
            state["expires_at"] = state["issued_at"] + 3600
            return operator.publish(
                state,
                expected_revision=head,
                operator_key=operator_key,
                deadline=budget,
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("SQL protected observation unavailable") from None

    def lifecycle(
        self,
        verb,
        frame,
        action="plan",
        *,
        expected="",
        expected_host_id="",
        expected_release="",
        operator_key="",
        confirm=False,
        supersede=False,
        deadline=None,
        emergency_prefix="",
        emergency_reason="",
        emergency_duration=600,
        execution_digest="",
        _sql_deadline=None,
    ):
        operator = self._operator()
        budget = _sql_deadline or self._operator_deadline()
        try:
            head, original, _crossref, _policies = operator.load(deadline=budget)
            entry = original.get("frames", {}).get(frame)
            if entry is None:
                raise ControlPlaneError("frame has no operator grant")
            selected = [
                (slot, record)
                for slot, record in (("active", entry["active"]), ("candidate", entry["candidate"]))
                if record is not None
                and (
                    not expected_host_id
                    or record["authority"]["holder_identity"] == expected_host_id
                )
            ]
            if not selected and expected_host_id:
                selected = [
                    ("retired", record)
                    for record in reversed(entry["retired"])
                    if record["authority"]["holder_identity"] == expected_host_id
                ][:1]
            if not selected:
                raise ControlPlaneError("unknown protected incarnation")
            if len(selected) != 1:
                raise ControlPlaneError("lifecycle requires exact holder identity")
            slot, record = selected[0]
            authority = record["authority"]
            registration = None
            receipts = []
            if slot != "retired":
                _evidence_record, registration, receipts = operator.evidence(
                    frame,
                    authority["holder_identity"],
                    expected_revision=head,
                    deadline=budget,
                )
            latest = receipts[0][3] if receipts else None
            streak = 0
            for index, (sequence, _digest, first_seen, beat) in enumerate(receipts):
                if index and receipts[index - 1][0] != sequence + 1:
                    break
                if index and receipts[index - 1][2] - first_seen >= beat.leaseDurationSeconds:
                    break
                streak += 1
            from . import frame_emergency

            release = latest.release.digest if latest else ""
            result = {
                "frame_id": frame,
                "host_id": authority["holder_identity"],
                "instance_ref": authority["instance_ref"],
                "key_fingerprint": authority["key_fingerprint"],
                "epoch": authority["epoch"],
                "revision": head,
                "state": record["state"],
                "cordoned": record["cordoned"],
                "release": release,
                "toplevel": latest.toplevel if latest else None,
                "image": latest.image if latest else None,
                "capabilities": (registration.capabilities.model_dump() if registration else None),
                "attestor_evidence_kind": "ed25519-runtime-signature",
                "consecutive_verified_beats": streak,
                "conformance": (latest.conformance.model_dump() if latest else None),
                "emergency": frame_emergency.status(record, self.clock()),
                "prior_active": entry["active"]["authority"]
                if slot == "candidate" and entry["active"]
                else None,
            }
            if action in {"plan", "check"}:
                return result
            if action != "apply" or not confirm or not operator_key:
                raise ControlPlaneError("apply requires explicit --confirm and operator key")
            if (
                expected != head
                or expected_host_id != authority["holder_identity"]
                or expected_release != release
            ):
                raise ControlPlaneError("original authority/holder/release CAS mismatch")
            state = json.loads(json.dumps(original))
            target = state["frames"][frame]
            record = target[slot] if slot != "retired" else None
            now = self.clock()
            if slot == "retired" and verb != "retire":
                raise ControlPlaneError("retired incarnation cannot transition")
            if verb in {"emergency-admit", "emergency-revoke"}:
                from . import frame_emergency

                if original["expires_at"] <= now:
                    raise ControlPlaneError("emergency mutation requires current authority")
                if verb == "emergency-admit":
                    if slot == "candidate" and target["active"]:
                        raise ControlPlaneError(
                            "emergency admission cannot supersede active incarnation"
                        )
                    policy = _policies.get(emergency_prefix)
                    if policy is None or now >= policy["valid_until"]:
                        raise ControlPlaneError("emergency requires explicit current hive scope")
                    from .hq_authority_guard import requirements

                    requirements(policy["requires"], record["desired"]["caps"])
                    if policy["config_revision"] != authority["config_revision"]:
                        raise ControlPlaneError("emergency hive config revision mismatch")
                    frame_emergency.authorize(
                        record,
                        prefix=emergency_prefix,
                        reason=emergency_reason,
                        duration=emergency_duration,
                        revision=head,
                        now=now,
                        registration=registration,
                        beat=latest,
                        execution_digest=execution_digest,
                    )
                    if slot == "candidate":
                        target["active"], target["candidate"] = record, None
                else:
                    frame_emergency.revoke(record, now)
            elif verb == "admit":
                reviewing_emergency = slot == "active" and record.get("emergency", {}).get(
                    "review_required"
                )
                if not reviewing_emergency and (
                    slot != "candidate" or record["state"] != "pending"
                ):
                    raise ControlPlaneError("admit requires pending candidate")
                if (
                    not record["desired"]["declared"]
                    or registration is None
                    or latest is None
                    or streak < 3
                    or (
                        authority["candidate_expires_at"] is not None
                        and now >= authority["candidate_expires_at"]
                    )
                    or now - receipts[0][2] >= latest.leaseDurationSeconds
                    or latest.release.model_dump() != record["desired"]["release"]
                    or registration.release.model_dump() != record["desired"]["release"]
                    or registration.capabilities.model_dump() != record["desired"]["caps"]
                    or latest.conformance.status != "conformant"
                    or latest.conformance.profile != record["desired"]["profile"]
                    or not latest.conformance.checks
                    or any(check.status == "fail" for check in latest.conformance.checks)
                ):
                    raise ControlPlaneError("admit requires current complete trusted evidence")
                if reviewing_emergency:
                    record["emergency"].update(review_required=False, revoked_at=now)
                if not reviewing_emergency and target["active"] and not supersede:
                    raise ControlPlaneError("another active incarnation requires supersede")
                if not reviewing_emergency and target["active"]:
                    old = target["active"]
                    old.update(state="retired", cordoned=True)
                    target["retired"].append(old)
                if not reviewing_emergency:
                    target["active"], target["candidate"] = record, None
                record.update(state="active", cordoned=False)
                record["authority"]["candidate_expires_at"] = None
            elif verb == "retire":
                if slot == "retired":
                    return result
                record.update(state="retired", cordoned=True)
                target[slot] = None
                target["retired"].append(record)
            elif verb == "cordon":
                if record["state"] != "active":
                    raise ControlPlaneError("cordon requires active frame")
                if record["cordoned"]:
                    return result
                record["cordoned"] = True
            else:
                transitions = {
                    "drain": ({"active", "draining"}, "draining"),
                    "park": ({"drained", "parked"}, "parked"),
                    "resume": ({"parked", "active"}, "active"),
                    "quarantine": (
                        {"pending", "active", "draining", "drained", "parked", "quarantined"},
                        "quarantined",
                    ),
                }
                if verb not in transitions or record["state"] not in transitions[verb][0]:
                    raise ControlPlaneError("illegal lifecycle transition")
                destination = transitions[verb][1]
                if record["state"] == destination and not (verb == "resume" and record["cordoned"]):
                    return result
                if verb == "drain":
                    if type(deadline) not in (int, float) or deadline <= now:
                        raise ControlPlaneError("drain requires future deadline")
                    record["drain_deadline"] = deadline
                record.update(state=destination, cordoned=verb != "resume")
            state["revision"] += 1
            state["issued_at"] = now
            state["expires_at"] = now + 3600
            operator.publish(
                state,
                expected_revision=head,
                operator_key=operator_key,
                deadline=budget,
            )
            if verb in {"emergency-admit", "emergency-revoke"}:
                frame_emergency.audit(verb, record, revision=head)
            return self.lifecycle(
                verb,
                frame,
                "check",
                expected_host_id=expected_host_id,
                _sql_deadline=budget,
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("SQL operator lifecycle unavailable") from None

    @property
    def signed_liveness(self) -> bool:
        """``hq.sql.liveness: signed`` — read-time signed heartbeats, advisory lease expiry."""
        from .hq_sql_runtime import signed_liveness

        return signed_liveness(self.settings)

    @staticmethod
    def _public_observation(row, route, slot, *, granted_public_key, now, signed=False):
        """Verify one observation row; ``signed`` rows come from the frame's own inbox.

        A receiver row's freshness is the receiver's first-seen clock. A ``signed`` row
        (:func:`beadhive.hq_sql_runtime.newest_signed_heartbeat`) carries
        ``accepted_until = renewTime + leaseDurationSeconds``, so its age is the reader's
        clock minus the signed ``renewTime``; up to 30 s of future skew clamps to age 0.
        """
        from .host_heartbeat_core import VerifiedObservation
        from .hq_sql_runtime import SIGNED_LIVENESS_SKEW_SECONDS
        from .hq_sql_signatures import canonical, verify_heartbeat

        if row is None:
            return VerifiedObservation("missing", candidate=slot == "candidate")
        sequence, digest, accepted_until, body, envelope_body, signer = row
        try:
            if isinstance(body, memoryview):
                body = body.tobytes()
            if isinstance(envelope_body, memoryview):
                envelope_body = envelope_body.tobytes()
            if isinstance(body, str):
                body = body.encode()
            if isinstance(envelope_body, str):
                envelope_body = envelope_body.encode()
            envelope = json.loads(envelope_body)
            if envelope_body != canonical(envelope):
                raise ValueError()
            lease, verified_digest = verify_heartbeat(
                envelope, granted_public_key=granted_public_key
            )
            if (
                type(sequence) is not int
                or sequence < 1
                or lease.seq != sequence
                or lease.frame_id != route.frame_id
                or lease.holderIdentity != route.holder_identity
                or lease.instance_ref != route.instance_ref
                or lease.epoch != route.epoch
                or lease.key_id != signer
                or signer != route.signer_fingerprint
                or digest != verified_digest
                or body != canonical(lease.model_dump(mode="json", exclude_none=True))
            ):
                raise ValueError()
            first_seen = accepted_until - lease.leaseDurationSeconds
            age = now - first_seen
            if signed:
                if first_seen != lease.observed_at or age < -SIGNED_LIVENESS_SKEW_SECONDS:
                    raise ValueError()
                age = max(age, 0)
            elif age < 0:
                raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise ControlPlaneError("protected public observation invalid") from None
        return VerifiedObservation(
            "fresh" if now < accepted_until else "stale",
            verified=True,
            fresh=now < accepted_until,
            age_seconds=age,
            lease=lease,
            sha=digest,
            candidate=slot == "candidate",
            age_basis="signed-envelope-reader-clock" if signed else "protected-receiver-first-seen",
        )

    def read_eligibility(self, manifest, *, now=None):
        """Return one qualified SQL config/authority/observation read boundary."""
        import math

        at = self.clock() if now is None else now
        if type(at) not in (int, float) or not math.isfinite(at):
            raise ControlPlaneError("eligibility clock invalid")
        # BH_HQ_AUTHORITY_ENFORCE=false (UNSUPPORTED, dev-only): expired authority and an
        # unbound config head are tolerated; the frame-identity comparisons below still hold.
        enforce = authority_enforced()
        try:
            head, _state, route, slot, record, _snapshot, policies, row, _ = (
                self._runtime_authority().read_frame_composite(enforce=enforce)
            )
            if (
                manifest.frame_id != route.frame_id
                or manifest.host_id != route.holder_identity
                or manifest.instance_ref != route.instance_ref
                or getattr(manifest, "beadyard_id", None) != _snapshot.beadyard_id
                or getattr(manifest, "beadyard_id", None) != record["authority"].get("beadyard_id")
                or enforce
                and not any(
                    policy["config_revision"] == record["authority"]["config_revision"]
                    for policy in policies.values()
                )
            ):
                raise ControlPlaneError("manifest or desired policy differs from current grant")
            desired = {
                **record["desired"],
                **({"emergency": record["emergency"]} if "emergency" in record else {}),
                "state": record["state"],
                "cordoned": record["cordoned"],
                "authority": record["authority"],
            }
            return (
                head,
                desired,
                self._public_observation(
                    row,
                    route,
                    slot,
                    granted_public_key=record["public_key"],
                    now=at,
                    signed=self.signed_liveness,
                ),
            )
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("qualified SQL eligibility unavailable") from None

    def read_hive_lease_record(self, prefix, *, holder_identity=None, incumbent_identity=None):
        from .host_lease_contracts import HostLease, _parse_stamp
        from .hq_sql_signatures import canonical

        if holder_identity is not None and incumbent_identity is not None:
            raise ControlPlaneError("hive lease identity qualifier ambiguous")
        signed = self.signed_liveness
        # BH_HQ_AUTHORITY_ENFORCE=false (UNSUPPORTED, dev-only): skip authority expiry, config
        # binding, cordon/admission state and emergency review; lease ownership still holds.
        enforce = authority_enforced()

        try:
            (
                _head,
                _state,
                route,
                slot,
                record,
                _snapshot,
                policies,
                observation_row,
                lease_row,
            ) = self._runtime_authority().read_frame_composite(prefix=prefix, enforce=enforce)
            if lease_row is None:
                return "", None
            revision, body, _request_id, _request_sha = lease_row
            if isinstance(body, memoryview):
                body = body.tobytes()
            if isinstance(body, str):
                body = body.encode()
            envelope = json.loads(body)
            if body != canonical(envelope) or set(envelope) != {"authority", "lease"}:
                raise ControlPlaneError("protected hive lease carrier invalid")
            raw = envelope["lease"]
            if (
                not isinstance(raw, dict)
                or set(raw) != {"host_id", "label", "epoch", "adopted_at", "expires_at"}
                or any(
                    type(raw[key]) is not str
                    for key in ("host_id", "label", "adopted_at", "expires_at")
                )
                or type(raw["epoch"]) is not int
                or raw["epoch"] < 1
                or _parse_stamp(raw["adopted_at"]) <= 0
                or _parse_stamp(raw["expires_at"]) <= 0
            ):
                raise ControlPlaneError("protected hive lease record invalid")
            # Signed liveness: expiry is an advisory failover hint (not part of the record);
            # tombstones and foreign holders are still refused below and by every caller.
            lease = HostLease(**raw, advisory_expiry=signed)
            from .frame_emergency import locally_active
            from .hq_authority_guard import emergency_review_required

            emergency = locally_active(record, prefix, self.clock())
            # The lease names this exact grant, or an archived active predecessor
            # from a reviewed release rotation of the same identity lineage.
            same_incarnation = envelope["authority"] == {
                "frame_id": route.frame_id,
                **record["authority"],
            } or guard.same_incumbent_after_rotation(envelope["authority"], route.frame_id, record)
            if (
                enforce
                and (holder_identity is not None or incumbent_identity is not None)
                and emergency_review_required(record)
                and not emergency
            ):
                return revision, None
            if incumbent_identity is not None:
                if (
                    incumbent_identity != route.holder_identity
                    or not same_incarnation
                    or lease.host_id != incumbent_identity
                ):
                    return revision, None
            if holder_identity is not None:
                policy = policies.get(prefix)
                observation = self._public_observation(
                    observation_row,
                    route,
                    slot,
                    granted_public_key=record["public_key"],
                    now=self.clock(),
                    signed=signed,
                )
                authority_ok = not enforce or not (
                    record["state"] != "active"
                    or record["cordoned"]
                    or policy is None
                    or self.clock() >= policy["valid_until"]
                    or policy["config_revision"] != record["authority"]["config_revision"]
                )
                if (
                    holder_identity != route.holder_identity
                    or not same_incarnation
                    or lease.host_id != holder_identity
                    or (not signed and lease.is_expired(self.clock()))
                    or slot != "active"
                    or not authority_ok
                    or not observation.verified
                    or (not observation.fresh and not emergency)
                ):
                    return revision, None
            return revision, lease
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError("qualified SQL hive lease unavailable") from None

    def read_hive_lease(self, prefix, *, holder_identity=None):
        return self.read_hive_lease_record(prefix, holder_identity=holder_identity)[1]

    def heartbeat(self, lease, *, signing_key):
        from .hq_framelease_contracts import HeartbeatLease
        from .hq_sql_runtime import InboxUnknown
        from .hq_sql_signatures import sign_heartbeat

        if self.settings.get("runtime") is None:
            raise ControlPlaneError("AUTHORITY_NOT_READY: SQL runtime binding unavailable")
        deadline = time.monotonic() + self.settings["runtime"]["operation_timeout"]
        lease = HeartbeatLease.model_validate(lease)
        runtime = self._runtime_authority()
        enforce = authority_enforced()
        try:
            head, state, _, _ = runtime.load_state(deadline=deadline, allow_expired=not enforce)
            binding = runtime.load_frame_binding(expected_head=head, deadline=deadline)
            entry = state.get("frames", {}).get(lease.frame_id)
            selected = [
                record
                for record in ((entry or {}).get("active"), (entry or {}).get("candidate"))
                if record is not None
                and record["authority"]["holder_identity"] == binding.holder_identity
                and record["authority"]["instance_ref"] == binding.instance_ref
                and record["authority"]["epoch"] == binding.epoch
                and record["authority"]["key_fingerprint"] == binding.signer_fingerprint
            ]
            if len(selected) != 1 or binding.frame_id != lease.frame_id:
                raise ControlPlaneError("frame principal has no exact operator-granted incarnation")
            authority = selected[0]["authority"]
            if (
                selected[0]["state"] == "retired"
                or enforce
                and authority["candidate_expires_at"] is not None
                and self.clock() >= authority["candidate_expires_at"]
                or (
                    lease.frame_id,
                    lease.holderIdentity,
                    lease.instance_ref,
                    lease.key_id,
                    lease.epoch,
                    lease.audience,
                    lease.config_revision,
                    lease.beadyard_id,
                )
                != (
                    authority["frame_id"],
                    authority["holder_identity"],
                    authority["instance_ref"],
                    authority["key_fingerprint"],
                    authority["epoch"],
                    authority["audience"],
                    authority["config_revision"],
                    authority.get("beadyard_id"),
                )
            ):
                raise ControlPlaneError("heartbeat does not match current granted incarnation")
            _, digest = runtime.publish_inbox(
                "heartbeat",
                sign_heartbeat(lease, signing_key=signing_key),
                binding=binding,
                expected_head=head,
                deadline=deadline,
            )
            return digest
        except InboxUnknown:
            raise
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError(str(exc)) from None

    def propose_hive_lease(
        self, prefix, lease, *, expected, operation, force=False, signing_key, deadline=None
    ):
        """Submit a signed frame proposal; only the separate receiver can mutate the lease."""
        import uuid

        from .host_lease_contracts import HostLease, lease_ref
        from .hq_sql_signatures import sign_hive_request

        lease_ref(prefix)
        if (
            self.settings.get("runtime") is None
            or force
            or operation not in {"adopt", "renew", "release"}
            or not isinstance(lease, HostLease)
            or not isinstance(expected, str)
        ):
            raise ControlPlaneError("frame hive lease proposal requires bound unforced operation")
        runtime = self._runtime_authority()
        try:
            head, state, _, _ = runtime.load_state(deadline=deadline)
            binding = runtime.load_frame_binding(expected_head=head, deadline=deadline)
            entry = state.get("frames", {}).get(binding.frame_id)
            record = entry.get("active") if entry else None
            if (
                record is None
                or record["authority"]["holder_identity"] != binding.holder_identity
                or record["authority"]["instance_ref"] != binding.instance_ref
                or record["authority"]["epoch"] != binding.epoch
                or record["authority"]["key_fingerprint"] != binding.signer_fingerprint
                or operation in {"adopt", "renew"}
                and (record["state"] != "active" or record["cordoned"])
                or operation != "release"
                and lease.host_id != binding.holder_identity
                or operation == "release"
                and lease.host_id != ""
            ):
                raise ControlPlaneError("hive lease proposal requires exact active incarnation")
            if operation != "release":
                from .frame_emergency import cap_lease

                lease = cap_lease(record, prefix, lease, self.clock())
            authority = record["authority"]
            request_id = str(uuid.uuid4())
            request = {
                "domain": (
                    "beadhive/sql-hive-lease/v2"
                    if authority.get("beadyard_id") is not None
                    else "beadhive/sql-hive-lease/v1"
                ),
                "request_id": request_id,
                "principal": binding.principal,
                "frame_id": binding.frame_id,
                "holder_identity": binding.holder_identity,
                "instance_ref": binding.instance_ref,
                "epoch": binding.epoch,
                "key_fingerprint": binding.signer_fingerprint,
                "audience": authority["audience"],
                "config_revision": authority["config_revision"],
                "authority_revision": head,
                "prefix": prefix,
                "expected_revision": expected,
                "operation": operation,
                "force": False,
                "lease": lease.to_record(),
            }
            if authority.get("beadyard_id") is not None:
                request["beadyard_id"] = authority["beadyard_id"]
            _, digest = runtime.publish_inbox(
                "hive_lease",
                sign_hive_request(request, signing_key=signing_key),
                binding=binding,
                expected_head=head,
                request_id=request_id,
                deadline=deadline,
            )
            return request_id, digest.removeprefix("sha256:"), binding, authority["audience"]
        except ValueError as exc:
            if isinstance(exc, ControlPlaneError):
                raise
            raise ControlPlaneError(str(exc)) from None

    def publish_hive_lease(self, prefix, lease, *, expected, operation, force=False):
        from . import host
        from .hq_sql_runtime import InboxUnknown

        if self.settings.get("runtime") is None:
            raise ControlPlaneError("AUTHORITY_NOT_READY: SQL runtime binding unavailable")
        deadline = time.monotonic() + self.settings["runtime"]["operation_timeout"]
        try:
            signing_key = host.signing_key()
            if time.monotonic() >= deadline:
                raise ControlPlaneError("hive lease signing deadline exceeded")
            request_id, digest, binding, audience = self.propose_hive_lease(
                prefix,
                lease,
                expected=expected,
                operation=operation,
                force=force,
                signing_key=signing_key,
                deadline=deadline,
            )
        except InboxUnknown as exc:
            raise HqLeaseUnknown(exc.request_id, exc.payload_sha256, expected) from None
        if time.monotonic() >= deadline:
            raise HqLeaseUnknown(request_id, digest, expected)
        runtime = self._runtime_authority()
        while time.monotonic() < deadline:
            result = runtime.read_public_result(
                request_id,
                request_sha256=digest,
                principal=binding,
                audience=audience,
                expected_revision=expected,
                deadline=deadline,
            )
            if time.monotonic() >= deadline:
                raise HqLeaseUnknown(request_id, digest, expected)
            if result is not None:
                status, revision = result
                if status != "accepted" or not revision:
                    raise ControlPlaneError("trusted receiver rejected hive lease proposal")
                return revision
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise HqLeaseUnknown(request_id, digest, expected)

    def __getattr__(self, name):
        if name in {
            "fetch_config",
            "publish_registration",
            "heartbeat",
            "watch_state",
            "observe",
            "read_eligibility",
            "read_hive_lease",
            "read_hive_lease_record",
            "publish_hive_lease",
            "load_config_authority_snapshot",
            "heartbeat_reference",
            "publish_registration_evidence",
            "grant",
            "accept_observation",
            "renew",
            "lifecycle",
        }:

            def unavailable(*_args, **_kwargs):
                raise ControlPlaneError("AUTHORITY_NOT_READY: SQL runtime binding unavailable")

            return unavailable
        raise AttributeError(name)


def control_plane(hq_dir=None):
    # Backend connection/trust must be available before fleet/effective resolution.
    cfg = config.load_host().get("hq", {})
    sql = _validated_sql_binding(cfg.get("sql", {}))
    if sql.enabled:
        return SqlControlPlane(sql.model_dump())
    if cfg.get("mode", "git") != "git":
        raise ControlPlaneError(f"unsupported HQ control-plane mode: {cfg.get('mode')}")
    return GitControlPlane(hq_dir or config.hq_dir(), authority_anchor=cfg.get("authority_anchor"))


def attach_fleet_config(hq_dir=None, *, bootstrap=None, operator_key=None):
    """Read back central config using only host-injected connection/trust inputs.

    Returning a binding and committed snapshot performs no checkout/Beads hydration,
    source migration, host identity change or implicit backend switch.
    """
    settings = bootstrap if bootstrap is not None else config.load_host().get("hq", {})
    mode = settings.get("mode", "git")
    sql = _validated_sql_binding(settings.get("sql", {}))
    if sql.enabled:
        store = SqlControlPlane(sql.model_dump()).config_store()
        return store, store.load_snapshot()
    if mode != "git":
        raise ControlPlaneError(f"unsupported HQ configuration bootstrap mode: {mode}")
    plane = GitControlPlane(
        hq_dir or config.hq_dir(), authority_anchor=settings.get("authority_anchor")
    )
    store = plane.config_store(operator_key=operator_key)
    return store, store.load_snapshot()
