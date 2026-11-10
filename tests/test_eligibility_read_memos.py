"""The pure memos under a frame-eligibility read keep successes only (bh-25fvb).

One intake host check validated the same committed config carrier 14 times and fingerprinted the
same few frame keys about 200 times. Both are pure functions of their input, so they are
remembered; what is pinned here is that only a SUCCESS is: an invalid carrier or key is refused
afresh on every call, never answered from memory. The checks themselves are stubs that count
their calls, so no YAML is parsed and no ``ssh-keygen`` runs.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from beadhive import hq_authority_guard, hq_sql_config
from beadhive.modules.config.domain.ports import FleetConfigDocument

# -- the committed config carrier ----------------------------------------------------------------


class Checks:
    def __init__(self) -> None:
        self.calls = 0
        self.refuse = False

    def __call__(self, documents, *, version=None) -> None:
        self.calls += 1
        if self.refuse:
            raise hq_sql_config.SqlConfigError("carrier invalid")


@pytest.fixture
def checks(monkeypatch) -> Checks:
    fake = Checks()
    monkeypatch.setattr(hq_sql_config, "_check_carrier", fake)
    monkeypatch.setattr(hq_sql_config, "_VALID_CARRIERS", {})
    return fake


def _docs(content: str = "providers: [github]\n") -> tuple[FleetConfigDocument, ...]:
    return (FleetConfigDocument("config.yaml", content),)


def test_a_valid_carrier_is_checked_once_per_version_and_content(checks) -> None:
    for _ in range(3):
        hq_sql_config._validate_carrier(_docs(), version=4)
    assert checks.calls == 1
    hq_sql_config._validate_carrier(_docs(), version=5)  # another storage version
    hq_sql_config._validate_carrier(_docs("providers: []\n"), version=4)  # other content
    assert checks.calls == 3


def test_an_invalid_carrier_is_refused_on_every_call(checks) -> None:
    checks.refuse = True
    for _ in range(3):
        with pytest.raises(hq_sql_config.SqlConfigError):
            hq_sql_config._validate_carrier(_docs(), version=4)
    assert checks.calls == 3 and hq_sql_config._VALID_CARRIERS == {}


@pytest.mark.parametrize(
    "documents",
    [(), [FleetConfigDocument("config.yaml", "x")], (SimpleNamespace(path="a", content="b"),)],
    ids=["empty", "not-a-tuple", "not-a-document"],
)
def test_an_unkeyable_carrier_is_never_remembered(checks, documents) -> None:
    for _ in range(2):
        hq_sql_config._validate_carrier(documents, version=4)
    assert checks.calls == 2


def test_the_memo_is_bounded_oldest_first(checks) -> None:
    for n in range(hq_sql_config._VALID_CARRIERS_MAX + 1):
        hq_sql_config._validate_carrier(_docs(f"n: {n}\n"), version=1)
    assert len(hq_sql_config._VALID_CARRIERS) == hq_sql_config._VALID_CARRIERS_MAX
    hq_sql_config._validate_carrier(_docs("n: 0\n"), version=1)  # the oldest was dropped
    assert checks.calls == hq_sql_config._VALID_CARRIERS_MAX + 2


# -- SSH key fingerprints ------------------------------------------------------------------------

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample frame@host"


class Keygen:
    def __init__(self) -> None:
        self.calls = 0
        self.returncode = 0

    def __call__(self, argv, **_kwargs):
        self.calls += 1
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout="256 SHA256:abc123 frame@host (ED25519)\n", stderr=""
        )


@pytest.fixture
def keygen(monkeypatch) -> Keygen:
    fake = Keygen()
    monkeypatch.setattr(hq_authority_guard.subprocess, "run", fake)
    monkeypatch.setattr(hq_authority_guard, "_FINGERPRINTS", {})
    return fake


def test_a_key_is_fingerprinted_once(keygen) -> None:
    assert hq_authority_guard.fingerprint(KEY) == "SHA256:abc123"
    assert hq_authority_guard.fingerprint(f"  {KEY}\n") == "SHA256:abc123"  # the same key text
    assert keygen.calls == 1


def test_another_key_is_fingerprinted_on_its_own(keygen) -> None:
    hq_authority_guard.fingerprint(KEY)
    hq_authority_guard.fingerprint(KEY.replace("Example", "Another"))
    assert keygen.calls == 2


def test_an_invalid_key_is_refused_on_every_call(keygen) -> None:
    keygen.returncode = 1
    for _ in range(3):
        with pytest.raises(ValueError, match="invalid SSH public key"):
            hq_authority_guard.fingerprint(KEY)
    assert keygen.calls == 3 and hq_authority_guard._FINGERPRINTS == {}
    keygen.returncode = 0
    assert hq_authority_guard.fingerprint(KEY) == "SHA256:abc123"
