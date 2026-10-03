"""Standalone receive guard for explicitly provisioned operator-owned Git HQ."""

from __future__ import annotations

import datetime
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
CONFIG_HEAD = "refs/heads/bh-config"
CONFIG_WITNESS = "refs/bh/config-witness/"
CONFIG_DOMAIN = "beadhive/fleet-config/v1"
CONFIG_DOMAIN_V2 = "beadhive/fleet-config/v2"
ZERO = "0" * 40
POLICY = "bh-authority-policy.json"
DOMAIN = "beadhive/git-authority/v1"
DOMAIN_V2 = "beadhive/git-authority/v2"
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
    values = tuple(
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
    return values + ((a["beadyard_id"],) if "beadyard_id" in a else ())


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
    if set(a) not in (fields, fields | {"beadyard_id"}) or a["frame_id"] != identity:
        raise ValueError("invalid authority binding")
    if "beadyard_id" in a:
        _beadyard_parser_id()(a["beadyard_id"])
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
        or state["domain"] not in (DOMAIN, DOMAIN_V2)
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
    bound_ids = {record["authority"].get("beadyard_id") for _, record in records(state)} - {None}
    if state["domain"] == DOMAIN:
        if bound_ids:
            raise ValueError("legacy authority carrier cannot contain beadyard identity")
    elif len(bound_ids) != 1 or any(
        record is not None and "beadyard_id" not in record["authority"]
        for entry in state["frames"].values()
        for record in (entry["active"], entry["candidate"])
    ):
        raise ValueError("bound authority carrier requires one beadyard identity")


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


def legacy_binding_upgrade(before, after, issued, trusted_now):
    """Permit only a missing→bound ID on one unchanged live incarnation."""
    if "beadyard_id" in before["authority"] or after is None:
        return False
    owner = after["authority"].get("beadyard_id")
    expiry = before["authority"]["candidate_expires_at"]
    return (
        owner is not None
        and (
            expiry is None
            and before["state"] in {"active", "draining", "drained", "parked"}
            or expiry is not None
            and expiry > max(issued, trusted_now)
        )
        and after["authority"] == {**before["authority"], "beadyard_id": owner}
        and {key: value for key, value in after.items() if key != "authority"}
        == {key: value for key, value in before.items() if key != "authority"}
    )


def validate_legacy_binding_transition(previous, state, *, trusted_now):
    """An atomic v1→v2 bridge may add only one ID to every live record."""
    if (
        previous["domain"] != DOMAIN
        or state["domain"] != DOMAIN_V2
        or state["issued_at"] < previous["issued_at"]
        or previous["expires_at"] <= state["issued_at"]
        or state["expires_at"] > previous["expires_at"]
        or set(state["frames"]) != set(previous["frames"])
    ):
        raise ValueError("legacy identity binding original/expiry changed")
    found = False
    for frame, before in previous["frames"].items():
        after = state["frames"][frame]
        if after["epoch_floor"] != before["epoch_floor"] or after["retired"] != before["retired"]:
            raise ValueError("identity binding changed frame floor or retired history")
        for slot in ("active", "candidate"):
            old, new = before[slot], after[slot]
            if old is None:
                if new is not None:
                    raise ValueError("identity binding created an incarnation")
                continue
            found = True
            if not legacy_binding_upgrade(old, new, state["issued_at"], trusted_now):
                raise ValueError("identity binding changed incarnation beyond UUID")
    if not found:
        raise ValueError("identity binding requires a live legacy incarnation")


def same_incumbent_after_binding(previous, current):
    """An old lease can renew/release under the same newly bound incarnation."""
    return previous == current or (
        isinstance(previous, dict)
        and isinstance(current, dict)
        and "beadyard_id" not in previous
        and current.get("beadyard_id") is not None
        and previous == {key: value for key, value in current.items() if key != "beadyard_id"}
    )


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


def validate_hive_policies(policies):
    if not isinstance(policies, dict):
        raise ValueError("invalid canonical hive policy projection")
    for prefix, item in policies.items():
        if (
            not isinstance(prefix, str)
            or not re.fullmatch(r"[a-z][a-z0-9-]*", prefix)
            or not isinstance(item, dict)
            or set(item)
            != {"config_revision", "config_head", "valid_until", "requires", "evict_after_s"}
            or not isinstance(item["config_revision"], str)
            or not item["config_revision"]
            or not isinstance(item["config_head"], str)
            or (item["config_head"] and not re.fullmatch(r"[0-9a-f]{40}", item["config_head"]))
            or type(item["valid_until"]) not in (int, float)
            or not math.isfinite(item["valid_until"])
            or type(item["evict_after_s"]) not in (int, float)
            or not math.isfinite(item["evict_after_s"])
            or item["evict_after_s"] <= 0
            or not isinstance(item["requires"], dict)
        ):
            raise ValueError("invalid canonical hive policy projection")
        requirements(item["requires"], None)


def requirements(requires, caps):
    for key, value in requires.items():
        if key == "max_sessions":
            valid = type(value) is int and value > 0
            satisfied = caps is None or (type(caps.get(key)) is int and caps[key] >= value)
        elif key in {"isolation", "trust_zone", "arch", "harness"}:
            valid = isinstance(value, str) and bool(value)
            satisfied = caps is None or (
                value in caps.get("harnesses", []) if key == "harness" else caps.get(key) == value
            )
        elif key == "harnesses":
            valid = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
            satisfied = caps is None or (valid and all(v in caps.get(key, []) for v in value))
        else:
            valid = satisfied = False
        if not valid or not satisfied:
            raise ValueError("hive capability requirements unsatisfied")


def enforce_hive_lease(old, new, reference, state, policy, head):
    envelope = parentless_payload(new, "hive-lease.json")
    if set(envelope) != {
        "domain",
        "authority_revision",
        "expected_lease_sha",
        "operation",
        "prefix",
        "authority",
        "lease",
    }:
        raise ValueError("invalid frame hive lease envelope")
    prefix = reference.removeprefix("refs/bh/lease/")
    hive = policy.get("hive_policies", {}).get(prefix)
    if not isinstance(envelope.get("authority"), dict):
        raise ValueError("invalid hive lease authority binding")
    authority_id = envelope["authority"].get("beadyard_id")
    expected_domain = (
        "beadhive-frame-hive-lease-v2"
        if authority_id is not None
        else "beadhive-frame-hive-lease-v1"
    )
    if authority_id is not None:
        _beadyard_parser_id()(authority_id)
    if (
        envelope["domain"] != expected_domain
        or envelope["prefix"] != prefix
        or envelope["authority_revision"] != head
        or envelope["expected_lease_sha"] != ("" if old == ZERO else old)
    ):
        raise ValueError("hive lease policy/revision/CAS unavailable")
    matches = [
        (f, r)
        for f, r in records(state)
        if envelope["authority"] == {"frame_id": f, **r["authority"]}
    ]
    if len(matches) != 1:
        raise ValueError("hive lease incarnation unavailable")
    frame, record = matches[0]
    signed_by(new, record)
    authority, lease = record["authority"], envelope["lease"]
    if authority_id is not None:
        config_head = git("for-each-ref", "--format=%(objectname)", CONFIG_HEAD)
        if not config_head or config_beadyard_id(read_config_state(config_head)) != authority_id:
            raise ValueError("hive lease belongs to another beadyard than current config")
    fields = {"host_id", "label", "epoch", "adopted_at", "expires_at"}
    if (
        not isinstance(lease, dict)
        or set(lease) != fields
        or type(lease["epoch"]) is not int
        or lease["epoch"] < 1
        or any(not isinstance(lease[k], str) for k in fields - {"epoch"})
    ):
        raise ValueError("invalid frame hive lease record")

    def timestamp(value):
        return (
            datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=datetime.UTC)
            .timestamp()
        )

    expiry = timestamp(lease["expires_at"])
    now = time.time()
    previous = None
    if old != ZERO:
        if git("cat-file", "-t", old) == "commit":
            previous = parentless_payload(old, "hive-lease.json")
        else:
            previous = {"lease": json.loads(git("cat-file", "-p", old)), "authority": None}
    prior = previous["lease"] if previous else None
    operation = envelope["operation"]
    if operation == "release":
        if (
            prior is None
            or prior["host_id"] != authority["holder_identity"]
            or not same_incumbent_after_binding(previous["authority"], envelope["authority"])
            or lease["host_id"]
            or lease["epoch"] != prior["epoch"]
            or lease["adopted_at"] != prior["adopted_at"]
            or expiry > now
        ):
            raise ValueError("release requires exact incumbent incarnation/epoch")
        return
    if (
        hive is None
        or hive["config_head"] != git("for-each-ref", "--format=%(objectname)", CONFIG_HEAD)
        or now >= hive["valid_until"]
    ):
        raise ValueError("hive lease policy/revision/CAS unavailable")
    if (
        operation not in {"adopt", "renew"}
        or record["state"] != "active"
        or record["cordoned"]
        or lease["host_id"] != authority["holder_identity"]
        or authority["config_revision"] != hive["config_revision"]
        or (
            authority["candidate_expires_at"] is not None
            and now >= authority["candidate_expires_at"]
        )
        or expiry <= now
        or expiry > now + 86400
    ):
        raise ValueError("frame not authorized for hive lease")
    receipt = record["receipt"]
    beat = receipt["lease"]
    if git("rev-parse", f"refs/bh/heartbeat/{frame}") != receipt["sha"]:
        raise ValueError("hive lease requires accepted current heartbeat")
    if (
        not beat
        or now < receipt["first_seen"]
        or now - receipt["first_seen"] >= beat["leaseDurationSeconds"]
        or beat["release"] != record["desired"]["release"]
        or beat["conformance"]["status"] != "conformant"
        or beat["conformance"]["profile"] != record["desired"]["profile"]
        or any(c["status"] == "fail" for c in beat["conformance"]["checks"])
    ):
        raise ValueError("hive lease requires fresh conformant protected receipt")
    if authority_id is not None:
        beat_id = beat.get("beadyard_id")
        if operation == "adopt" and (
            beat_id != authority_id
            or (receipt["registration"] or {}).get("beadyard_id") != authority_id
            or (receipt["registration"] or {}).get("release") != record["desired"]["release"]
            or (receipt["registration"] or {}).get("capabilities") != record["desired"]["caps"]
        ):
            raise ValueError("bound hive adoption requires fresh matching identity evidence")
        if operation == "renew" and beat_id != authority_id:
            # Only the first exact-incumbent renewal of an existing v1 lease may
            # carry the still-fresh unbound receipt. Its remaining host/epoch,
            # lease CAS, expiry and same-incarnation checks run below. Once the
            # lease itself is v2, later renewals need a bound accepted beat.
            first_legacy_bridge = (
                beat_id is None
                and previous is not None
                and previous.get("domain") == "beadhive-frame-hive-lease-v1"
                and isinstance(previous.get("authority"), dict)
                and "beadyard_id" not in previous["authority"]
                and previous["authority"]
                == {
                    key: value
                    for key, value in envelope["authority"].items()
                    if key != "beadyard_id"
                }
            )
            if not first_legacy_bridge:
                raise ValueError("bound hive renewal requires matching identity evidence")
    caps = record["desired"]["caps"]
    if type(caps.get("max_sessions")) is not int or caps["max_sessions"] <= 0:
        raise ValueError("no frame intake capacity")
    requirements(hive["requires"], caps)
    if operation == "renew":
        if (
            prior is None
            or not same_incumbent_after_binding(previous["authority"], envelope["authority"])
            or prior["host_id"] != lease["host_id"]
            or timestamp(prior["expires_at"]) <= now
            or lease["epoch"] != prior["epoch"]
            or lease["adopted_at"] != prior["adopted_at"]
        ):
            raise ValueError("renew requires current live exact-incarnation hive holder")
    elif prior:
        if lease["epoch"] <= prior["epoch"]:
            raise ValueError("adopt hive epoch must advance")
        if (
            prior["host_id"]
            and prior["host_id"] != lease["host_id"]
            and timestamp(prior["expires_at"]) > now
        ):
            incumbents = [
                r
                for f, r in records(state)
                if previous["authority"] == {"frame_id": f, **r["authority"]}
            ]
            if len(incumbents) != 1:
                raise ValueError("incumbent incarnation unavailable")
            incumbent = incumbents[0]
            r = incumbent["receipt"]
            if incumbent["state"] not in {"retired", "quarantined"} and (
                not r["lease"]
                or now - r["first_seen"] <= hive["evict_after_s"]
                or now - r["first_seen"] < r["lease"]["leaseDurationSeconds"]
            ):
                raise ValueError("live incumbent is not evictable")


def enforce_runtime(updates, policy):
    # Resolved from the separately copied bh-guard-libs directory in the server hook.
    from manifest_guard import parse_manifest, validate  # pants: no-infer-dep

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
                    or manifest.get("beadyard_id") != a.get("beadyard_id")
                    or manifest.get("release") != record["desired"]["release"]
                    or manifest.get("capabilities") != record["desired"]["caps"]
                ):
                    raise ValueError("manifest differs from granted identity/release/capabilities")
                matched = True
                break
            if not matched:
                raise ValueError("frame main publication requires signed own granted manifest")
            continue
        if reference.startswith("refs/bh/lease/"):
            enforce_hive_lease(old, new, reference, state, policy, sha)
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
                    or manifest.get("beadyard_id") != a.get("beadyard_id")
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
                    or lease.get("beadyard_id") != a.get("beadyard_id")
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


def _beadyard_parser():
    if __package__:
        from .beadyard_identity import parse_document
    else:
        from beadyard_identity import parse_document

    return parse_document


def _beadyard_parser_id():
    if __package__:
        from .beadyard_identity import parse_id
    else:
        from beadyard_identity import parse_id

    return parse_id


def config_beadyard_id(state):
    for document in state["documents"]:
        if document["path"] == "beadyard.json":
            return _beadyard_parser()(document["content"])
    return None


def validate_config_state(state):
    """Validate the carrier, leaving config semantics to the existing config module."""
    if set(state) != {"domain", "generation", "revision", "issued_at", "expires_at", "documents"}:
        raise ValueError("invalid configuration envelope")
    if state["domain"] not in (CONFIG_DOMAIN, CONFIG_DOMAIN_V2) or not isinstance(
        state["generation"], str
    ):
        raise ValueError("invalid configuration domain/generation")
    if type(state["revision"]) is not int or state["revision"] < 1:
        raise ValueError("invalid configuration revision")
    if (
        any(
            type(state[k]) not in (int, float) or not math.isfinite(state[k])
            for k in ("issued_at", "expires_at")
        )
        or state["expires_at"] <= state["issued_at"]
    ):
        raise ValueError("invalid configuration validity")
    documents = state["documents"]
    if not isinstance(documents, list) or not documents:
        raise ValueError("configuration documents required")
    paths = set()
    for doc in documents:
        if not isinstance(doc, dict) or set(doc) != {"path", "content"}:
            raise ValueError("invalid configuration document")
        path = doc["path"]
        if (
            not isinstance(path, str)
            or not re.fullmatch(
                r"beadyard\.json|fleet\.yaml|workspace(?:-[A-Za-z0-9_-]+)?\.toml|allowed_signers|"
                r"hosts/[A-Za-z0-9_-]+\.yaml|hives/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.yaml",
                path,
            )
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise ValueError("unsupported configuration document path")
        if path in paths or not isinstance(doc["content"], str):
            raise ValueError("duplicate path or invalid configuration document content")
        if path == "beadyard.json":
            # The standalone guard imports the same parser from its pinned libs.
            _beadyard_parser()(doc["content"])
        paths.add(path)
    if "fleet.yaml" not in paths:
        raise ValueError("fleet document required")
    if (state["domain"] == CONFIG_DOMAIN) == ("beadyard.json" in paths):
        raise ValueError("configuration carrier version and beadyard identity disagree")


def read_config_state(sha):
    if (
        git("ls-tree", "--name-only", sha) != "config.json"
        or int(git("cat-file", "-s", f"{sha}:config.json")) > 4 * 1024 * 1024
    ):
        raise ValueError("invalid configuration carrier")
    state = json.loads(git("show", f"{sha}:config.json"))
    validate_config_state(state)
    return state


def enforce_config(updates, policy, principal):
    protected = [
        (old, new, ref)
        for old, new, ref in updates
        if ref == CONFIG_HEAD or ref.startswith(CONFIG_WITNESS)
    ]
    if not protected:
        return
    if principal != "operator":
        raise ValueError("frame cannot write configuration or witnesses")
    heads = [(old, new) for old, new, ref in protected if ref == CONFIG_HEAD]
    if len(heads) != 1:
        raise ValueError("configuration head/witness must advance atomically")
    old, new = heads[0]
    if new == ZERO:
        raise ValueError("configuration deletion forbidden")
    git("-c", f"gpg.ssh.allowedSignersFile={policy['operator_signers']}", "verify-commit", new)
    state = read_config_state(new)
    if (
        state["generation"] != policy["generation"]
        or state["issued_at"] > time.time() + 30
        or state["expires_at"] <= time.time()
    ):
        raise ValueError("configuration generation/validity mismatch")
    parents = git("show", "-s", "--format=%P", new).split()
    previous = read_config_state(old) if old != ZERO else None
    previous_revision = previous["revision"] if previous is not None else 0
    if parents != ([] if old == ZERO else [old]) or state["revision"] != previous_revision + 1:
        raise ValueError("configuration must advance exact parent/revision")
    if previous is not None:
        prior_id = config_beadyard_id(previous)
        next_id = config_beadyard_id(state)
        if prior_id is not None and next_id != prior_id:
            raise ValueError("beadyard identity cannot change or disappear")
    authority_head = git("for-each-ref", "--format=%(objectname)", HEAD)
    if authority_head:
        authority_state = read_state(authority_head)
        authority_ids = {
            record["authority"].get("beadyard_id")
            for _, record in records(authority_state)
            if record["authority"].get("beadyard_id") is not None
        }
        if authority_ids and authority_ids != {config_beadyard_id(state)}:
            raise ValueError("configuration belongs to another beadyard than authority")
    if sorted(protected) != sorted(
        [(old, new, CONFIG_HEAD), (ZERO, new, f"{CONFIG_WITNESS}{state['revision']:020d}")]
    ):
        raise ValueError("configuration witness mismatch")


def enforce(updates, policy):
    protected = [
        (old, new, ref) for old, new, ref in updates if ref == HEAD or ref.startswith(WITNESS)
    ]
    principal = os.environ.get("BH_HQ_BROKER_PRINCIPAL")
    if principal not in {"operator", "frame"}:
        raise ValueError("protected broker principal required")
    enforce_config(updates, policy, principal)
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
    if state["domain"] == DOMAIN_V2:
        config_head = git("for-each-ref", "--format=%(objectname)", CONFIG_HEAD)
        config_id = config_beadyard_id(read_config_state(config_head)) if config_head else None
        authority_ids = {
            record["authority"].get("beadyard_id")
            for _, record in records(state)
            if record["authority"].get("beadyard_id") is not None
        }
        if authority_ids != {config_id}:
            raise ValueError("authority belongs to another beadyard than configuration")
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
        if previous["domain"] == DOMAIN_V2 and state["domain"] != DOMAIN_V2:
            raise ValueError("authority beadyard binding cannot regress")
        upgrading = previous["domain"] == DOMAIN and state["domain"] == DOMAIN_V2
        if upgrading and any(
            entry["active"] is not None or entry["candidate"] is not None
            for entry in previous["frames"].values()
        ):
            validate_legacy_binding_transition(previous, state, trusted_now=time.time())
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
                if upgrading and legacy_binding_upgrade(
                    prior, replacement, state["issued_at"], time.time()
                ):
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
                and not (
                    upgrading
                    and legacy_binding_upgrade(
                        before["candidate"], after["candidate"], state["issued_at"], time.time()
                    )
                )
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
