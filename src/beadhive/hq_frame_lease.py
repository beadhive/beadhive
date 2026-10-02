"""Protected Git frame leases; legacy non-frame blob leases remain unchanged."""

from __future__ import annotations

import json
import tempfile

from . import gitref, host, host_lease, hosts
from .hq_control_plane import ControlPlaneError, _git

DOMAIN = "beadhive-frame-hive-lease-v1"


def _endpoint(plane):
    revision, state, policy = plane._read()
    remote = plane._remote(policy)
    origin = _git(plane.hq_dir, "remote", "get-url", "origin").strip()
    if origin not in {remote, str(policy["client"]["server_root"])}:
        raise ControlPlaneError("HQ origin differs from protected authority endpoint")
    return revision, state, policy, remote


def read(plane, prefix, *, holder_identity=None):
    from . import hq_authority_guard as guard

    revision, state, policy, remote = _endpoint(plane)
    ref = host_lease.lease_ref(prefix)
    rows = _git(plane.hq_dir, "-c", "protocol.ext.allow=always", "ls-remote", remote, ref)
    if not rows.strip():
        return "", None
    sha = rows.split()[0]
    _git(plane.hq_dir, "-c", "protocol.ext.allow=always", "fetch", "--no-tags", remote, sha)
    # Frames cannot trust unsigned legacy blobs as an intake authorization.
    if _git(plane.hq_dir, "cat-file", "-t", sha) != "commit":
        if holder_identity is not None:
            return sha, None
        record = gitref.decode(_git(plane.hq_dir, "cat-file", "-p", sha))
        if plane._read()[0] != revision:
            raise ControlPlaneError("authority changed during legacy incumbent read")
        return sha, host_lease.HostLease.from_record(record)
    if (
        _git(plane.hq_dir, "show", "-s", "--format=%P", sha)
        or _git(plane.hq_dir, "ls-tree", "--name-only", sha) != "hive-lease.json"
    ):
        raise ControlPlaneError("invalid frame hive lease carrier")
    envelope = json.loads(_git(plane.hq_dir, "show", f"{sha}:hive-lease.json"))
    if envelope.get("domain") != DOMAIN or envelope.get("prefix") != prefix:
        raise ControlPlaneError("frame hive lease domain mismatch")
    matches = [
        (f, r)
        for f, r in guard.records(state)
        if envelope.get("authority") == {"frame_id": f, **r["authority"]}
    ]
    if len(matches) != 1:
        raise ControlPlaneError("frame hive lease incarnation unavailable")
    _, record = matches[0]
    with tempfile.NamedTemporaryFile(mode="w") as signers:
        signers.write("frame " + record["public_key"] + "\n")
        signers.flush()
        _git(plane.hq_dir, "-c", f"gpg.ssh.allowedSignersFile={signers.name}", "verify-commit", sha)
    lease = host_lease.HostLease.from_record(envelope["lease"])
    if lease.host_id and lease.host_id != record["authority"]["holder_identity"]:
        raise ControlPlaneError("frame hive lease holder mismatch")
    if plane._read()[0] != revision:
        raise ControlPlaneError("authority changed during hive lease read")
    if holder_identity is not None:
        hive = policy.get("hive_policies", {}).get(prefix)
        if (
            record["state"] != "active"
            or record["cordoned"]
            or lease.host_id != holder_identity
            or hive is None
            or plane.clock() >= hive["valid_until"]
            or record["authority"]["config_revision"] != hive["config_revision"]
        ):
            return sha, None
        # Existing config adapter verifies signatures, generation and rollback witnesses.
        config_head, _, _ = plane.config_store()._read()
        if config_head != hive["config_head"]:
            return sha, None
        guard.requirements(hive["requires"], record["desired"]["caps"])
        expiry = record["authority"]["candidate_expires_at"]
        if expiry is not None and plane.clock() >= expiry:
            return sha, None
    if plane._read()[0] != revision:
        raise ControlPlaneError("authority changed during hive lease read")
    return sha, lease


def publish(plane, prefix, lease, *, expected, operation, force=False):
    revision, state, policy, remote = _endpoint(plane)
    if force and policy["client"]["role"] != "operator":
        raise ControlPlaneError("frame cannot assert operator forced takeover")
    manifest = hosts.load(plane.hq_dir, host.host_id())
    entry = state.get("frames", {}).get(manifest.frame_id, {})
    record = entry.get("active")
    if (
        record is None
        or record["authority"]["holder_identity"] != manifest.host_id
        or record["authority"]["instance_ref"] != manifest.instance_ref
    ):
        raise ControlPlaneError("frame hive lease requires exact active incarnation")
    data = dict(
        domain=DOMAIN,
        authority_revision=revision,
        expected_lease_sha=expected,
        operation=operation,
        prefix=prefix,
        authority={"frame_id": manifest.frame_id, **record["authority"]},
        lease=lease.to_record(),
    )
    blob = _git(plane.hq_dir, "hash-object", "-w", "--stdin", data=gitref.encode(data))
    tree = _git(plane.hq_dir, "mktree", data=f"100644 blob {blob}\thive-lease.json\n")
    sha = _git(
        plane.hq_dir,
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={host.signing_key()}",
        "commit-tree",
        "-S",
        tree,
        data="Frame hive lease\n",
    )
    _git(
        plane.hq_dir,
        "-c",
        "protocol.ext.allow=always",
        "push",
        f"--force-with-lease={host_lease.lease_ref(prefix)}:{expected}",
        remote,
        f"{sha}:{host_lease.lease_ref(prefix)}",
    )
    if read(plane, prefix)[0] != sha:
        raise ControlPlaneError("frame hive lease publication readback mismatch")
    return sha
