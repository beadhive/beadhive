"""Protected Git frame leases; legacy non-frame blob leases remain unchanged."""

from __future__ import annotations

import json
import tempfile

DOMAIN = "beadhive-frame-hive-lease-v1"


def _endpoint(plane, git, error):
    revision, state, policy = plane._read()
    remote = plane._remote(policy)
    origin = git(plane.hq_dir, "remote", "get-url", "origin").strip()
    if origin not in {remote, str(policy["client"]["server_root"])}:
        raise error("HQ origin differs from protected authority endpoint")
    return revision, state, policy, remote


def read(plane, prefix, *, git, error, decode, lease_ref, json_decode, holder_identity=None):
    from . import hq_authority_guard as guard

    revision, state, policy, remote = _endpoint(plane, git, error)
    ref = lease_ref(prefix)
    rows = git(plane.hq_dir, "-c", "protocol.ext.allow=always", "ls-remote", remote, ref)
    if not rows.strip():
        return "", None
    sha = rows.split()[0]
    git(plane.hq_dir, "-c", "protocol.ext.allow=always", "fetch", "--no-tags", remote, sha)
    # Frames cannot trust unsigned legacy blobs as an intake authorization.
    if git(plane.hq_dir, "cat-file", "-t", sha) != "commit":
        if holder_identity is not None:
            return sha, None
        record = json_decode(git(plane.hq_dir, "cat-file", "-p", sha))
        if plane._read()[0] != revision:
            raise error("authority changed during legacy incumbent read")
        return sha, decode(record)
    if (
        git(plane.hq_dir, "show", "-s", "--format=%P", sha)
        or git(plane.hq_dir, "ls-tree", "--name-only", sha) != "hive-lease.json"
    ):
        raise error("invalid frame hive lease carrier")
    envelope = json.loads(git(plane.hq_dir, "show", f"{sha}:hive-lease.json"))
    if envelope.get("domain") != DOMAIN or envelope.get("prefix") != prefix:
        raise error("frame hive lease domain mismatch")
    matches = [
        (f, r)
        for f, r in guard.records(state)
        if envelope.get("authority") == {"frame_id": f, **r["authority"]}
    ]
    if len(matches) != 1:
        raise error("frame hive lease incarnation unavailable")
    _, record = matches[0]
    with tempfile.NamedTemporaryFile(mode="w") as signers:
        signers.write("frame " + record["public_key"] + "\n")
        signers.flush()
        git(plane.hq_dir, "-c", f"gpg.ssh.allowedSignersFile={signers.name}", "verify-commit", sha)
    lease = decode(envelope["lease"])
    if lease.host_id and lease.host_id != record["authority"]["holder_identity"]:
        raise error("frame hive lease holder mismatch")
    if plane._read()[0] != revision:
        raise error("authority changed during hive lease read")
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
        raise error("authority changed during hive lease read")
    return sha, lease


def publish(
    plane,
    prefix,
    lease,
    *,
    expected,
    operation,
    git,
    error,
    decode,
    lease_ref,
    manifest,
    signing_key,
    json_decode,
    json_encode,
    force=False,
):
    revision, state, policy, remote = _endpoint(plane, git, error)
    if force and policy["client"]["role"] != "operator":
        raise error("frame cannot assert operator forced takeover")
    entry = state.get("frames", {}).get(manifest.frame_id, {})
    record = entry.get("active")
    if (
        record is None
        or record["authority"]["holder_identity"] != manifest.host_id
        or record["authority"]["instance_ref"] != manifest.instance_ref
    ):
        raise error("frame hive lease requires exact active incarnation")
    data = dict(
        domain=DOMAIN,
        authority_revision=revision,
        expected_lease_sha=expected,
        operation=operation,
        prefix=prefix,
        authority={"frame_id": manifest.frame_id, **record["authority"]},
        lease=lease.to_record(),
    )
    blob = git(plane.hq_dir, "hash-object", "-w", "--stdin", data=json_encode(data))
    tree = git(plane.hq_dir, "mktree", data=f"100644 blob {blob}\thive-lease.json\n")
    sha = git(
        plane.hq_dir,
        "-c",
        "gpg.format=ssh",
        "-c",
        f"user.signingkey={signing_key}",
        "commit-tree",
        "-S",
        tree,
        data="Frame hive lease\n",
    )
    git(
        plane.hq_dir,
        "-c",
        "protocol.ext.allow=always",
        "push",
        f"--force-with-lease={lease_ref(prefix)}:{expected}",
        remote,
        f"{sha}:{lease_ref(prefix)}",
    )
    if (
        read(
            plane,
            prefix,
            git=git,
            error=error,
            decode=decode,
            lease_ref=lease_ref,
            json_decode=json_decode,
        )[0]
        != sha
    ):
        raise error("frame hive lease publication readback mismatch")
    return sha
