"""Standalone receive guard for explicitly provisioned operator-owned Git HQ."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path

HEAD = "refs/heads/bh-authority"
WITNESS = "refs/bh/authority-witness/"
ZERO = "0" * 40
POLICY = "bh-authority-policy.json"
DOMAIN = "beadhive/git-authority/v1"
EXECUTABLES = {"git": "git", "ssh_keygen": "ssh-keygen"}


def git(*args):
    result = subprocess.run([EXECUTABLES["git"], *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "authority Git operation failed")
    return result.stdout.strip()


def fingerprint(public):
    with tempfile.NamedTemporaryFile(mode="w") as key:
        key.write(public.strip() + "\n")
        key.flush()
        result = subprocess.run(
            [EXECUTABLES["ssh_keygen"], "-lf", key.name], capture_output=True, text=True
        )
    if result.returncode:
        raise ValueError("invalid SSH public key")
    return result.stdout.split()[1]


def binding(record):
    a = record["authority"]
    return tuple(
        a[k]
        for k in (
            "frame_id",
            "holder_identity",
            "instance_ref",
            "key_fingerprint",
            "epoch",
            "audience",
        )
    )


def validate_record(identity, record, issued):
    if set(record) != {
        "authority",
        "public_key",
        "state",
        "desired",
        "receipt",
        "cordoned",
        "drain_deadline",
    }:
        raise ValueError("invalid registry record fields")
    a = record["authority"]
    fields = {
        "frame_id",
        "holder_identity",
        "instance_ref",
        "key_fingerprint",
        "epoch",
        "audience",
        "config_revision",
        "candidate_expires_at",
    }
    if set(a) != fields or a["frame_id"] != identity:
        raise ValueError("invalid authority binding")
    if any(
        not isinstance(a[k], str) or not a[k] for k in fields - {"epoch", "candidate_expires_at"}
    ):
        raise ValueError("empty authority identity")
    if type(a["epoch"]) is not int or a["epoch"] < 0:
        raise ValueError("invalid incarnation epoch")
    expiry = a["candidate_expires_at"]
    if expiry is not None and (type(expiry) not in (int, float) or not math.isfinite(expiry)):
        raise ValueError("invalid candidate expiry")
    if record["state"] not in {
        "pending",
        "active",
        "draining",
        "drained",
        "parked",
        "quarantined",
        "retired",
    }:
        raise ValueError("invalid lifecycle state")
    if type(record["cordoned"]) is not bool:
        raise ValueError("invalid cordon status")
    deadline = record["drain_deadline"]
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError("invalid drain deadline")
    if fingerprint(record["public_key"]) != a["key_fingerprint"]:
        raise ValueError("public key fingerprint mismatch")
    desired = record["desired"]
    if (
        set(desired) != {"declared", "release", "caps", "profile"}
        or type(desired["declared"]) is not bool
        or not isinstance(desired["profile"], str)
        or not desired["profile"]
    ):
        raise ValueError("invalid declared frame policy")
    release = desired["release"]
    if (
        set(release) != {"id", "digest"}
        or not release["id"]
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", release["digest"])
    ):
        raise ValueError("invalid approved release")
    r = record["receipt"]
    if set(r) != {"sequence", "sha", "first_seen", "consecutive", "lease", "registration"}:
        raise ValueError("invalid observer receipt")
    if (
        type(r["sequence"]) is not int
        or r["sequence"] < 0
        or type(r["consecutive"]) is not int
        or r["consecutive"] < 0
    ):
        raise ValueError("invalid observer sequence")
    if r["sequence"] == 0:
        if r != {
            "sequence": 0,
            "sha": "",
            "first_seen": None,
            "consecutive": 0,
            "lease": None,
            "registration": None,
        }:
            raise ValueError("invalid empty receipt")
    elif (
        not re.fullmatch(r"[0-9a-f]{40}", r["sha"])
        or type(r["first_seen"]) not in (int, float)
        or not math.isfinite(r["first_seen"])
        or r["first_seen"] > issued
        or not isinstance(r["lease"], dict)
    ):
        raise ValueError("invalid durable receipt")


def records(state):
    for identity, entry in state["frames"].items():
        for record in (entry["active"], entry["candidate"], *entry["retired"]):
            if record is not None:
                yield identity, record


def validate_state(state):
    if (
        set(state) != {"domain", "generation", "revision", "issued_at", "expires_at", "frames"}
        or state["domain"] != DOMAIN
        or not isinstance(state["generation"], str)
    ):
        raise ValueError("invalid authority envelope")
    if type(state["revision"]) is not int or state["revision"] < 1:
        raise ValueError("invalid authority revision")
    for k in ("issued_at", "expires_at"):
        if type(state[k]) not in (int, float) or not math.isfinite(state[k]):
            raise ValueError("invalid authority timestamp")
    if state["expires_at"] <= state["issued_at"] or not isinstance(state["frames"], dict):
        raise ValueError("invalid authority validity/registry")
    keys, holders = set(), set()
    for identity, entry in state["frames"].items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,127}", identity) or set(entry) != {
            "active",
            "candidate",
            "retired",
            "epoch_floor",
        }:
            raise ValueError("invalid frame registry entry")
        if (
            type(entry["epoch_floor"]) is not int
            or entry["epoch_floor"] < 0
            or not isinstance(entry["retired"], list)
        ):
            raise ValueError("invalid frame epoch floor/history")
        if entry["candidate"] is not None and entry["candidate"]["state"] not in {
            "pending",
            "quarantined",
        }:
            raise ValueError("candidate cannot claim active state")
        if entry["active"] is not None and entry["active"]["state"] in {"pending", "retired"}:
            raise ValueError("invalid active slot lifecycle")
        if any(r["state"] != "retired" for r in entry["retired"]):
            raise ValueError("retired history must remain retired")
    for identity, record in records(state):
        validate_record(identity, record, state["issued_at"])
        fp = record["authority"]["key_fingerprint"]
        if fp in keys or record["authority"]["epoch"] > state["frames"][identity]["epoch_floor"]:
            raise ValueError("signer reuse or ungranted epoch")
        holder = record["authority"]["holder_identity"]
        if holder in holders:
            raise ValueError("holder identity names multiple incarnations")
        holders.add(holder)
        keys.add(fp)


def read_state(sha):
    if (
        int(git("cat-file", "-s", f"{sha}:authority.json")) > 4 * 1024 * 1024
        or git("ls-tree", "--name-only", sha) != "authority.json"
    ):
        raise ValueError("invalid authority carrier")
    state = json.loads(git("show", f"{sha}:authority.json"))
    validate_state(state)
    return state


def continuous(before, after):
    if binding(before) != binding(after):
        raise ValueError("incarnation changed within slot")
    r1, r2 = before["receipt"], after["receipt"]
    if r2["sequence"] < r1["sequence"] or (r2["sequence"] == r1["sequence"] and r2 != r1):
        raise ValueError("accepted receipt rollback/refresh")


def signed_by(sha, record):
    with tempfile.NamedTemporaryFile(mode="w") as trusted:
        trusted.write("runtime " + record["public_key"] + "\n")
        trusted.flush()
        git("-c", f"gpg.ssh.allowedSignersFile={trusted.name}", "verify-commit", sha)


def live_records(state):
    now = time.time()
    if state["expires_at"] <= now:
        raise ValueError("authority expired for runtime publication")
    for frame, entry in state["frames"].items():
        for slot in ("active", "candidate"):
            record = entry[slot]
            if record is None:
                continue
            expiry = record["authority"]["candidate_expires_at"]
            if expiry is None or now < expiry:
                yield frame, slot, record


def parentless_payload(sha, filename):
    if git("show", "-s", "--format=%P", sha):
        raise ValueError("runtime carrier must be parentless")
    if (
        git("ls-tree", "--name-only", sha) != filename
        or int(git("cat-file", "-s", f"{sha}:{filename}")) > 4 * 1024 * 1024
    ):
        raise ValueError("invalid runtime carrier")
    return json.loads(git("show", f"{sha}:{filename}"))


def enforce_runtime(updates, policy):
    from manifest_guard import parse_manifest, validate

    sha = git("rev-parse", HEAD)
    state = read_state(sha)
    if state["generation"] != policy["generation"]:
        raise ValueError("runtime authority generation mismatch")
    available = list(live_records(state))
    schema = json.loads(Path("bh-guard-libs/host-manifest.schema.json").read_text())
    for old, new, reference in updates:
        if new == ZERO:
            raise ValueError("frame cannot delete refs")
        if reference == "refs/heads/main":
            if old == ZERO or git("show", "-s", "--format=%P", new).split() != [old]:
                raise ValueError("frame main publication must advance exact parent")
            paths = git("diff-tree", "--no-commit-id", "--name-only", "-r", old, new).splitlines()
            matched = False
            for frame, _slot, record in available:
                a = record["authority"]
                path = f"hosts/{a['holder_identity']}.yaml"
                if paths != [path]:
                    continue
                try:
                    signed_by(new, record)
                except ValueError:
                    continue
                if (
                    git("ls-tree", new, path).split()[0] != "100644"
                    or int(git("cat-file", "-s", f"{new}:{path}")) > 4 * 1024 * 1024
                ):
                    raise ValueError("invalid own manifest carrier")
                manifest = parse_manifest(git("show", f"{new}:{path}"), schema)
                if (
                    manifest["host_id"] != a["holder_identity"]
                    or manifest.get("frame_id") != frame
                    or manifest.get("instance_ref") != a["instance_ref"]
                    or manifest.get("release") != record["desired"]["release"]
                    or manifest.get("capabilities") != record["desired"]["caps"]
                ):
                    raise ValueError("manifest differs from granted identity/release/capabilities")
                matched = True
                break
            if not matched:
                raise ValueError("frame main publication requires signed own granted manifest")
            continue
        matched = False
        for frame, slot, record in available:
            a = record["authority"]
            beat_ref = (
                f"refs/bh/{'heartbeat' if slot == 'active' else 'candidate-heartbeat'}/{frame}"
            )
            binding_json = json.dumps(
                (a["holder_identity"], a["instance_ref"], a["key_fingerprint"]),
                separators=(",", ":"),
            )
            registration = (
                f"refs/bh/registration/{frame}/"
                + hashlib.sha256(binding_json.encode()).hexdigest()[:24]
            )
            if reference not in {beat_ref, registration}:
                continue
            signed_by(new, record)
            if reference == registration:
                manifest = parentless_payload(new, "registration.json")
                validate(manifest, schema)
                if (
                    manifest["host_id"] != a["holder_identity"]
                    or manifest.get("frame_id") != frame
                    or manifest.get("instance_ref") != a["instance_ref"]
                ):
                    raise ValueError("registration identity differs from grant")
            else:
                lease = parentless_payload(new, "heartbeat.json")
                fields = {
                    "frame_id": frame,
                    "holderIdentity": a["holder_identity"],
                    "instance_ref": a["instance_ref"],
                    "key_id": a["key_fingerprint"],
                    "epoch": a["epoch"],
                    "audience": a["audience"],
                    "config_revision": a["config_revision"],
                }
                if (
                    any(lease.get(k) != v for k, v in fields.items())
                    or type(lease.get("seq")) is not int
                    or lease["seq"] <= record["receipt"]["sequence"]
                ):
                    raise ValueError("heartbeat binding/sequence differs from grant")
                if old != ZERO:
                    try:
                        signed_by(old, record)
                        previous = parentless_payload(old, "heartbeat.json")
                    except ValueError:
                        previous = None
                    if (
                        previous is not None
                        and all(previous.get(k) == v for k, v in fields.items())
                        and previous["seq"] >= lease["seq"]
                    ):
                        raise ValueError("heartbeat sequence did not advance current carrier")
            matched = True
            break
        if not matched:
            raise ValueError("frame cannot publish ungranted or unknown ref")


def load_runtime(policy):
    sys.dont_write_bytecode = True
    libraries = Path("bh-guard-libs")
    files = {str(p.relative_to(libraries)): p for p in libraries.rglob("*") if p.is_file()}
    if set(files) != set(policy["runtime_files"]):
        raise ValueError("guard runtime file set changed")
    for relative, path in files.items():
        if (
            path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != policy["runtime_files"][relative]
        ):
            raise ValueError("guard runtime bytes changed")
    stdlib = sysconfig.get_path("stdlib")
    sys.path[:] = [str(libraries.resolve()), stdlib, str(Path(stdlib) / "lib-dynload")]


def enforce(updates, policy):
    protected = [
        (old, new, ref) for old, new, ref in updates if ref == HEAD or ref.startswith(WITNESS)
    ]
    principal = os.environ.get("BH_HQ_BROKER_PRINCIPAL")
    if principal not in {"operator", "frame"}:
        raise ValueError("protected broker principal required")
    if principal == "frame":
        if protected:
            raise ValueError("frame cannot write authority or witness refs")
        enforce_runtime(updates, policy)
        return
    if not protected:
        return
    heads = [(old, new) for old, new, ref in protected if ref == HEAD]
    if len(heads) != 1:
        raise ValueError("head and witness must advance atomically")
    old, new = heads[0]
    if new == ZERO:
        raise ValueError("authority deletion forbidden")
    git("-c", f"gpg.ssh.allowedSignersFile={policy['operator_signers']}", "verify-commit", new)
    state = read_state(new)
    if (
        state["generation"] != policy["generation"]
        or state["issued_at"] > time.time() + 30
        or state["expires_at"] <= time.time()
    ):
        raise ValueError("authority generation or validity mismatch")
    parents = git("show", "-s", "--format=%P", new).split()
    witnesses = git("for-each-ref", "--format=%(refname)", WITNESS).splitlines()
    if old == ZERO:
        if parents or witnesses or state["revision"] != 1:
            raise ValueError("bootstrap requires empty history")
    else:
        previous = read_state(old)
        if parents != [old] or state["revision"] != previous["revision"] + 1:
            raise ValueError("authority must advance exact parent/revision")
        high = max(witnesses, default="")
        if high != f"{WITNESS}{previous['revision']:020d}" or git("rev-parse", high) != old:
            raise ValueError("authority history rollback detected")
        for identity, before in previous["frames"].items():
            after = state["frames"].get(identity)
            if (
                after is None
                or after["epoch_floor"] < before["epoch_floor"]
                or after["retired"][: len(before["retired"])] != before["retired"]
            ):
                raise ValueError("registry floor/history rollback")
            for slot in ("active", "candidate"):
                prior, replacement = before[slot], after[slot]
                if prior is None:
                    continue
                if replacement is not None and binding(prior) == binding(replacement):
                    continuous(prior, replacement)
                    continue
                promoted = (
                    slot == "candidate"
                    and after["active"] is not None
                    and binding(after["active"]) == binding(prior)
                )
                # Receive lock is held: reject cleanup prepared before a first carrier.
                namespace = "heartbeat" if slot == "active" else "candidate-heartbeat"
                beat = f"refs/bh/{namespace}/{identity}"
                a = prior["authority"]
                suffix = hashlib.sha256(
                    json.dumps(
                        (a["holder_identity"], a["instance_ref"], a["key_fingerprint"]),
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()[:24]
                references = [beat]
                if not promoted:
                    references.append(f"refs/bh/registration/{identity}/{suffix}")
                for reference in references:
                    current = git("for-each-ref", "--format=%(objectname)", reference)
                    if not current:
                        continue
                    target = ZERO
                    if reference == beat and slot == "active" and after["active"] is not None:
                        target = after["active"]["receipt"]["sha"]
                    if (current, target, reference) not in updates:
                        raise ValueError("stale retirement/promotion carrier cleanup; retry")
                if promoted:
                    continuous(prior, after["active"])
                elif not any(
                    binding(r) == binding(prior) and r["receipt"] == prior["receipt"]
                    for r in after["retired"]
                ):
                    raise ValueError("incarnation removed without retirement")
            if (
                before["candidate"] is not None
                and after["candidate"] is not None
                and binding(before["candidate"]) != binding(after["candidate"])
            ):
                raise ValueError("pending candidate must be retired before replacement")
    expected = [(old, new, HEAD), (ZERO, new, f"{WITNESS}{state['revision']:020d}")]
    if sorted(protected) != sorted(expected):
        raise ValueError("immutable witness must accompany authority transaction")
    if any(
        record["authority"]["key_fingerprint"] in policy["operator_fingerprints"]
        for _, record in records(state)
    ):
        raise ValueError("operator key cannot be a runtime key")


def main():
    try:
        policy = json.loads(Path(POLICY).read_text())
        EXECUTABLES.update({k: v["path"] for k, v in policy["executables"].items()})
        load_runtime(policy)
        enforce([tuple(line.split()) for line in sys.stdin if line.strip()], policy)
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"HQ authority refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
