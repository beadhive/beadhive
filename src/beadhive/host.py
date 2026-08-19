"""``~/.beadhive/host.yaml`` — this machine's stable identity, distinct from configuration.

Minted once by ``bh config init``, never regenerated after that, and never synced or
templated — see ``docs/design/multi-host-model-adr.md`` ("host id"). Sibling to
``config.yaml`` under :func:`beadhive.config.home`; NOT inside a hive's ``.beads/``, and not
one of the files ``hq.scaffold_layout`` writes into the HQ store that gets pushed to a
remote (that store's contents are enumerated there — ``fleet.yaml`` / ``workspace.toml`` /
``hosts/README.md`` — and this file is deliberately absent from the list; see
``tests/test_host.py``'s sync-exclusion tests for the enforcement).

Shape: ``{host_id: <uuid4 str>, label: <human string, hostname-derived default>,
signing_key: <path to THIS machine's SSH public key>}``. ``host_id`` is the
fencing/identity value the rest of this molecule (``guard_primary``, the lease/epoch ref,
the per-host HQ manifest at ``hosts/<host_id>.yaml``) keys off; ``label`` is cosmetic only,
freely editable, and never consulted for identity — a machine rename never changes
``host_id``.

``signing_key`` (bh-ijd4) is the PER-HOST half of a git identity, and it lives here for
exactly the reason ``host_id`` does: it is a fact about this machine, not fleet-wide truth.
The fleet-wide half (operator name/email, and whose signatures are trusted) arrives later,
with HQ — see :mod:`beadhive.git_identity`, which marries the two. Storing it here is what
keeps the design free of any "copy the private key between machines" step: each host
references its OWN key, and only the PUBLIC half is ever read or published.
"""

from __future__ import annotations

import socket
import threading
import uuid
from pathlib import Path

from ruamel.yaml import YAML

from . import config
from .run import run

# Same round-trip settings as config.py's writer — plain 2-key file, but consistent style.
_yaml = YAML()
_yaml.indent(mapping=2, sequence=4, offset=2)

# Same fix as config.py's bh-3qo60 and hive_schema.py: `ruamel.yaml.YAML()` instances are not
# thread-safe, and every `_bd_schema_skew_warnings` pool worker reaches this module's `_yaml`
# via `hive_schema.refresh` -> `host.host_id()` -> `load()`. A process-lifetime `@cache` on
# `host_id()` was considered instead (host.yaml is minted once and never regenerated, so it's
# process-constant) but rejected: `host_id()`/`mint_if_needed()` are exercised directly by ~18
# test modules that monkeypatch `config.home()` to a fresh tmp dir per test and expect a freshly
# minted id each time, all within one pytest process — a global cache would return a stale id
# across tests. Locking (like the other two singletons) has no such cross-test hazard.
_yaml_lock = threading.Lock()

#: Default SSH public keys probed, in order, when git itself has no ``user.signingkey``.
#: PUBLIC halves only — bh never reads, generates or moves private key material (the bead's
#: "explicitly not in scope"). Hardware-backed (``_sk``) keys first: a host that has one is
#: saying that is the key it signs with.
_SSH_PUBKEY_CANDIDATES: tuple[str, ...] = (
    "id_ed25519_sk.pub",
    "id_ecdsa_sk.pub",
    "id_ed25519.pub",
    "id_ecdsa.pub",
    "id_rsa.pub",
)

GIT_TIMEOUT = 10.0  # a local `git config` read; bounded so a wedged git can't hang minting


def path() -> Path:
    """Where the host identity file lives: sibling to ``config.yaml`` under
    :func:`beadhive.config.home` — never inside a hive's ``.beads/`` and never under
    :func:`beadhive.config.hq_dir` (see module docstring)."""
    return config.home() / "host.yaml"


def _default_label() -> str:
    return socket.gethostname()


def discover_signing_key() -> str:
    """This host's SSH signing key REFERENCE — read from the machine, never invented, and
    never written by this function.

    Order, and the order is the whole contract:

    1. ``git config --global user.signingkey``. A host where the human already told git what
       they sign with (the origin Mac) reports that value verbatim, so minting can only ever
       AGREE with an existing setup — bh has nothing to overwrite because it adopts.
    2. The first existing public key in :data:`_SSH_PUBKEY_CANDIDATES` under ``~/.ssh``. This
       is the provisioned-host case: the VM's ``id_ed25519`` was created for ``gh`` and git
       simply never learned about it.
    3. ``""`` — no key found. NOT an error here: a host with no key is a real state, and the
       honest report belongs at the verifying gate, not in a silent invention.
    """
    res = run(
        ["git", "config", "--global", "--get", "user.signingkey"],
        check=False,
        capture=True,
        timeout=GIT_TIMEOUT,
    )
    if res.returncode == 0 and (existing := (res.stdout or "").strip()):
        return existing
    ssh = Path.home() / ".ssh"
    for name in _SSH_PUBKEY_CANDIDATES:
        cand = ssh / name
        if cand.is_file():
            return str(cand)
    return ""


def mint_if_needed() -> bool:
    """Mint ``host.yaml`` with a fresh UUID ``host_id`` + hostname-derived ``label`` + this
    host's ``signing_key`` reference, if and only if it doesn't already exist. Returns
    ``True`` when it minted, ``False`` when an existing file was left completely untouched —
    the one-time, never-regenerate contract ``bh config init`` relies on. Deliberately takes
    no ``force`` parameter: unlike the other templated files ``config init`` scaffolds, host
    identity is never rewritten, not even by ``--force`` — see the module docstring."""
    p = path()
    if p.exists():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "host_id": str(uuid.uuid4()),
        "label": _default_label(),
        "signing_key": discover_signing_key(),
    }
    with _yaml_lock, p.open("w") as f:
        _yaml.dump(data, f)
    return True


def ensure_signing_key() -> str:
    """The signing key this host signs with, back-filling the field into a ``host.yaml``
    minted before it existed. Returns the reference (``""`` when none could be found).

    Back-fill only — a ``signing_key`` already recorded is returned untouched, never
    re-discovered and never rewritten. That is :func:`mint_if_needed`'s never-regenerate
    contract applied to the second identity field: once a host has declared which key it
    signs with, changing it is an operator's edit, not something a later ``bh`` run does
    behind their back."""
    p = path()
    if not p.exists():
        mint_if_needed()
        return str(load().get("signing_key") or "")
    data = load()
    if recorded := str(data.get("signing_key") or "").strip():
        return recorded
    found = discover_signing_key()
    if not found:
        return ""
    data["signing_key"] = found
    with _yaml_lock, p.open("w") as f:
        _yaml.dump(data, f)
    return found


def load() -> dict:
    """The host identity exactly as written. Raises ``FileNotFoundError`` (with ``config
    init`` guidance, matching :func:`beadhive.config.load_host`'s message shape) when
    ``host.yaml`` hasn't been minted yet."""
    p = path()
    if not p.exists():
        raise FileNotFoundError(
            f"{config.BINARY_ALIAS} host identity not found at {p}\n"
            f"  scaffold it with:  {config.BINARY_ALIAS} config init"
        )
    with _yaml_lock:
        return _yaml.load(p.read_text())


def host_id() -> str:
    return str(load()["host_id"])


def label() -> str:
    return str(load()["label"])


def signing_key() -> str:
    """The recorded per-host signing key reference, or ``""`` — read-only (never mints, never
    back-fills; :func:`ensure_signing_key` is the writer)."""
    return str(load().get("signing_key") or "")
