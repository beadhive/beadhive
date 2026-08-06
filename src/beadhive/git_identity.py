"""``bh host identity`` — marry a host's git identity out of the two halves bh already holds.

THE MEASURED GAP (bh-ijd4, 2026-08-05). A freshly provisioned Linux host passed all ten
provisioning steps and still could not produce an attributable commit: its global git config
carried no ``user.name``, no ``user.email``, no ``user.signingkey`` and no ``gpg.format`` — while
``~/.ssh/id_ed25519`` sat right there, created for ``gh``, with git knowing nothing about it.
``work_logic._stamp`` returns early in *supervised* mode ("inherit the human's existing config —
stamp nothing"), which is correct on a laptop and inherits NOTHING on a host with no human. The
mode was never wrong; its unstated precondition was, and nothing established it.

WHY THIS IS ITS OWN STEP, and not part of ``bh config init``. The two halves of a git identity
become available at DIFFERENT provisioning steps::

    step 2  config init  ->  host.yaml         per-host: WHICH KEY this host signs with
    step 4  hq clone     ->  fleet.yaml        fleet-wide: operator name + email
                         ->  allowed_signers   fleet-wide: WHOSE signatures are trusted

``config init`` therefore *cannot* establish a full identity — on a fresh host the operator's
name and email do not exist locally until HQ is cloned, two steps later. ``config init`` owns the
host half (``host.mint_if_needed``); this module is the step that sits AFTER the clone and
consumes both. ``bh setup check`` is deliberately not the home: it is a detector, and a detector
observes the environment rather than improving it.

THREE RULES THIS MODULE HOLDS ITSELF TO:

* **It fills gaps; it never asserts.** Every write is guarded by "is this key unset?". A host
  with a working global identity (the origin Mac) comes out of this byte-identical — that is
  tested, not asserted. There is no ``--force``, on purpose: a human's git identity is not bh's
  to overwrite, and a wrong author on a signed commit is worse than no commit at all.
* **It never invents an identity.** ``name``/``email`` come from bh's own config
  (:func:`beadhive.config.work_identity`, i.e. ``work.identity.name`` / ``.email``, which
  ``fleet.yaml`` supplies fleet-wide) and from nowhere else — not ``$USER``, not ``gh``, not the
  OS. An unresolvable half is reported as unresolved, never guessed.
* **The signing key is per-host.** It is read from ``host.yaml``
  (:func:`beadhive.host.ensure_signing_key`), which references a key the machine ALREADY has.
  No private key is generated, read, or moved between machines; only public material is ever
  published (to HQ's ``allowed_signers``).

A HOST THAT NEVER PROVISIONS. Someone can install bh and work in a clone without ever running
``bh host provision``. Decided explicitly: that host FALLS BACK to whatever git already has and
is WARNED, never blocked — :func:`beadhive.host_provision.status` reports "git identity" as a
first-class check so the state is loud rather than discovered at the first refused commit. The
standalone ``bh host identity`` verb exists so such a host can marry the halves the moment it
has both, without a full provisioning run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import config, host
from .run import run

GIT_TIMEOUT = 10.0  # local `git config` reads/writes only — bounded, never a remote round trip

#: The fleet's trusted-signer file inside the HQ store. HQ is the operator's by construction and
#: is already the durable central store every host clones, which makes it the only place a
#: fleet's PUBLIC keys can live without inventing distribution machinery.
ALLOWED_SIGNERS = "allowed_signers"

#: Actions a :class:`Fill` can report. ``kept`` is the important one: it means git already had a
#: value and bh left it exactly as it was.
SET = "set"
KEPT = "kept"
UNRESOLVED = "unresolved"
WOULD = "would"


@dataclass(frozen=True)
class Fill:
    """One git-config key's outcome: what bh wrote, kept, or could not resolve."""

    key: str
    value: str
    action: str
    detail: str = ""


def _get_global(key: str) -> str:
    """The host's GLOBAL git config value for ``key`` (``""`` when unset). Read-only."""
    res = run(
        ["git", "config", "--global", "--get", key], check=False, capture=True, timeout=GIT_TIMEOUT
    )
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def _set_global(key: str, value: str) -> bool:
    res = run(
        ["git", "config", "--global", key, value], check=False, capture=True, timeout=GIT_TIMEOUT
    )
    return res.returncode == 0


def _fill(key: str, value: str, *, dry_run: bool) -> Fill:
    """Gap-fill one global git key. An existing value is ALWAYS kept — this is the single
    chokepoint that makes "never overwrite a human's config" a property of the module rather
    than a promise repeated at each call site."""
    if existing := _get_global(key):
        return Fill(key, existing, KEPT, "already set — left untouched")
    if not value:
        return Fill(key, "", UNRESOLVED, "no value available")
    if dry_run:
        return Fill(key, value, WOULD)
    if not _set_global(key, value):
        return Fill(key, value, UNRESOLVED, "git config --global write failed")
    return Fill(key, value, SET)


def operator_identity(cfg=None) -> tuple[str, str]:
    """(name, email) for this fleet's operator, from bh's config ONLY.

    Reuses :func:`beadhive.config.work_identity` (the existing resolver, layered global →
    per-hive → per-dev) with no hive entry, so what comes back is the base profile: the values
    ``fleet.yaml`` publishes fleet-wide under ``work.identity``. Deliberately mode-agnostic —
    *supervised* is precisely the mode that has no other source on a provisioned host, so
    reading the profile for its VALUES is not the same as switching the stamping mode."""
    if cfg is None:
        try:
            cfg = config.load()
        except (FileNotFoundError, config.ConfigError):
            return "", ""
    prof = config.work_identity(cfg, None)
    return str(prof.get("name") or ""), str(prof.get("email") or "")


def allowed_signers_path() -> Path:
    """Where the fleet's trusted-signer file lives — inside the cloned HQ store."""
    return config.hq_dir() / ALLOWED_SIGNERS


def public_key_material(reference: str) -> str:
    """The one-line PUBLIC key an ``allowed_signers`` entry needs, from a ``host.yaml``
    ``signing_key`` reference — which may be a PATH to a ``.pub`` file or an already-literal
    ``ssh-… AAAA…`` value. ``""`` when it is neither (e.g. a path that does not exist, or a
    path to a PRIVATE key, which is never read)."""
    ref = (reference or "").strip()
    if not ref:
        return ""
    if ref.startswith("ssh-") or ref.startswith("sk-ssh-") or ref.startswith("ecdsa-"):
        return ref
    p = Path(ref).expanduser()
    if p.suffix != ".pub" or not p.is_file():
        return ""
    return p.read_text().strip()


def enroll_signer(email: str, key_reference: str, *, dry_run: bool = False) -> Fill:
    """Publish this host's PUBLIC key into HQ's ``allowed_signers`` under the operator's email —
    append-if-absent, so a fleet's trusted-key list assembles itself as hosts join.

    This is what makes the merge gate's verification REAL rather than presence-only. Measured
    with git 2.54: with no ``allowed_signers`` file configured, git reports ``%G?`` = ``N`` for a
    perfectly-signed commit (indistinguishable from an unsigned one), and ``U`` when the file is
    configured but missing — so a gate without one could only ever refuse everything. Enrolled
    here, the same signature verifies to ``G``.

    Only public material moves (``*.pub`` content), and only into a store the operator already
    owns. Never rewrites an existing line and never removes one — a key already listed is left
    alone, so re-running is a no-op."""
    target = allowed_signers_path()
    if not target.parent.is_dir():
        return Fill(ALLOWED_SIGNERS, "", UNRESOLVED, "no local HQ store — clone HQ first")
    if not email:
        return Fill(ALLOWED_SIGNERS, "", UNRESOLVED, "no operator email in config")
    material = public_key_material(key_reference)
    if not material:
        return Fill(ALLOWED_SIGNERS, "", UNRESOLVED, "no readable public key for this host")
    existing = target.read_text() if target.is_file() else ""
    # Compare on the key BLOB (type + base64), not the whole line: the trailing comment differs
    # per machine and would make the same key look like two.
    blob = " ".join(material.split()[:2])
    if any(blob in line for line in existing.splitlines() if not line.lstrip().startswith("#")):
        return Fill(ALLOWED_SIGNERS, str(target), KEPT, "this host's key is already enrolled")
    if dry_run:
        return Fill(ALLOWED_SIGNERS, str(target), WOULD, f"would enroll {blob[:32]}… as {email}")
    header = (
        ""
        if existing
        else (
            "# Fleet-wide trusted SSH signers (bh). One `<principal> <key>` per line;\n"
            "# hosts append their own PUBLIC key here as they are provisioned.\n"
        )
    )
    body = existing if (not existing or existing.endswith("\n")) else existing + "\n"
    target.write_text(f"{header}{body}{email} {material}\n")
    return Fill(ALLOWED_SIGNERS, str(target), SET, f"enrolled this host's key as {email}")


def host_signing_key_preview() -> str:
    """The key :func:`establish` WOULD record, without writing ``host.yaml`` — the ``--dry-run``
    counterpart of :func:`beadhive.host.ensure_signing_key`."""
    try:
        if recorded := host.signing_key():
            return recorded
    except FileNotFoundError:
        pass
    return host.discover_signing_key()


def establish(cfg=None, *, dry_run: bool = False) -> list[Fill]:
    """Fill every gap in this host's GLOBAL git identity from bh's two halves, and enroll this
    host as a trusted signer. Returns one :class:`Fill` per key considered, in a fixed order.

    Idempotent and safe to re-run: a second call reports every key as ``kept``. Nothing here
    can overwrite an operator's value — see :func:`_fill`."""
    if cfg is None:
        try:
            cfg = config.load()
        except (FileNotFoundError, config.ConfigError):
            cfg = None
    name, email = operator_identity(cfg)
    key = host_signing_key_preview() if dry_run else host.ensure_signing_key()

    fills = [
        _fill("user.name", name, dry_run=dry_run),
        _fill("user.email", email, dry_run=dry_run),
        _fill("user.signingkey", key, dry_run=dry_run),
    ]
    if key:
        # Only meaningful alongside a key: `gpg.format=ssh` on a host with nothing to sign with
        # would be a claim bh cannot back. Matches the origin Mac's shape (ssh, not gpg) rather
        # than introducing a second signing scheme.
        fills.append(_fill("gpg.format", "ssh", dry_run=dry_run))
        fills.append(_fill("commit.gpgsign", "true", dry_run=dry_run))
    signers = enroll_signer(email, key, dry_run=dry_run)
    fills.append(signers)
    if signers.action in (SET, KEPT, WOULD):
        fills.append(
            _fill("gpg.ssh.allowedsignersfile", str(allowed_signers_path()), dry_run=dry_run)
        )
    return fills


def summary(cfg=None) -> tuple[bool, str]:
    """(ok, one-line detail) for the verifying gate: can this host produce an attributable,
    signed commit right now? Read-only — probes, writes nothing.

    ``ok`` requires an author identity git will actually accept (``user.name`` + ``user.email``
    resolvable globally). Signing is reported but does NOT fail the gate on its own: a host that
    can commit attributably is usable, and ``work.enforce_signing`` is the switch that makes
    signing mandatory."""
    gname, gemail = _get_global("user.name"), _get_global("user.email")
    missing = [k for k, v in (("user.name", gname), ("user.email", gemail)) if not v]
    if missing:
        cname, cemail = operator_identity(cfg)
        have = (
            f"bh config has them — run `{config.BINARY_ALIAS} host identity`"
            if (cname and cemail)
            else "and bh config carries none — set work.identity.name/.email in fleet.yaml"
        )
        return False, f"git has no {', '.join(missing)}; {have}"
    keyref = _get_global("user.signingkey")
    if not keyref:
        return True, f"{gname} <{gemail}>, UNSIGNED (no user.signingkey)"
    signers = _get_global("gpg.ssh.allowedsignersfile")
    if not signers:
        return True, f"{gname} <{gemail}>, signs with {keyref}, NO allowed_signers (verify → N)"
    if not Path(signers).expanduser().is_file():
        return True, f"{gname} <{gemail}>, signs with {keyref}, allowed_signers MISSING: {signers}"
    return True, f"{gname} <{gemail}>, signs with {keyref}, trusted via {signers}"


def signing_summary() -> tuple[bool, str]:
    """(ok, detail) for the STRICTER check that applies only when ``work.enforce_signing`` is
    on: could a commit made here actually verify as ``G``?

    Asserts the whole trust chain a `%G?` = ``G`` needs — a signing key, ``gpg.format=ssh``,
    ``commit.gpgsign``, an ``allowed_signers`` file that EXISTS, and this host's own public key
    listed inside it. That last clause is the substantive one: a key that is configured but not
    enrolled produces ``U`` (good signature, untrusted key), which the merge gate refuses, so
    checking only that a key exists would be exactly the presence-only verification bh-ijd4
    refuses to ship. Deliberately does NOT shell out to sign a probe buffer: on a headless host
    a passphrase-protected key would block on a prompt, and this establishes the same facts."""
    keyref = _get_global("user.signingkey")
    if not keyref:
        return False, "no user.signingkey — commits here would be unsigned and refused at merge"
    if _get_global("gpg.format") != "ssh":
        return False, f"gpg.format is not ssh (key {keyref} is an SSH key)"
    if _get_global("commit.gpgsign") != "true":
        return False, "commit.gpgsign is not true — commits here would be unsigned"
    signers = _get_global("gpg.ssh.allowedsignersfile")
    if not signers:
        return False, "no gpg.ssh.allowedsignersfile — git reports even signed commits as N"
    path = Path(signers).expanduser()
    if not path.is_file():
        return False, f"gpg.ssh.allowedsignersfile points at a missing file: {signers}"
    material = public_key_material(keyref)
    if not material:
        return False, f"cannot read the public half of {keyref} to check enrollment"
    blob = " ".join(material.split()[:2])
    listed = any(
        blob in line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    if not listed:
        return False, f"this host's key is not enrolled in {signers} — signatures verify as U"
    return True, f"signs with {keyref}, enrolled in {signers} — signatures verify as G"
