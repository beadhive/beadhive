"""The version-bump content judge and its receipt carry (bh-d21ng).

Every test pins a SAFETY property: the carry only fires for a diff that is a version bump and
nothing else, against a single parent whose keys are green. Hermetic: git in a tmp repo, no
network, no clock.
"""

from __future__ import annotations

import subprocess

import pytest

from beadhive import version_bump_judge as judge
from beadhive.modules.config.contracts import (
    AttestConfig,
    AttestKeyConfig,
    AttestVersionBumpConfig,
)
from beadhive.modules.work.domain.impact import ImpactReceipt, native_full_receipt
from beadhive.selective_validation import version_bump_carry

PYPROJECT = '[project]\nname = "bh"\nversion = "{v}"\ndependencies = ["{dep}"]\n'
LOCK = (
    'version = 1\n[[package]]\nname = "bh"\nversion = "{v}"\nsource = {{ editable = "." }}\n'
    '[[package]]\nname = "{dep}"\nversion = "1.0"\nsource = {{ registry = "https://pypi.org" }}\n'
)
SERVER = '{{"name": "x", "version": "{v}", "packages": [{{"identifier": "bh", "version": "{v}"}}]}}'


def files(v, dep="dep", server=SERVER):
    return {
        "pyproject.toml": PYPROJECT.format(v=v, dep=dep),
        "uv.lock": LOCK.format(v=v, dep=dep),
        "server.json": server.format(v=v),
        "CHANGELOG.md": f"# {v}\n",
    }


def diff(old, new):
    return {p: (old.get(p), new.get(p)) for p in set(old) | set(new) if old.get(p) != new.get(p)}


def test_version_only_is_yes():
    verdict, _, bumped = judge.judge_version_bump(diff(files("1.0.0"), files("1.0.1")))
    assert (verdict, bumped) == (judge.YES, True)


def test_dependency_bump_is_no():
    verdict, reason, _ = judge.judge_version_bump(diff(files("1.0.0"), files("1.0.1", dep="other")))
    assert verdict == judge.NO, reason


def test_lock_dependency_change_is_no():
    old, new = files("1.0.0"), files("1.0.1")
    new["uv.lock"] = new["uv.lock"].replace('version = "1.0"', 'version = "2.0"')
    assert judge.judge_version_bump(diff(old, new))[0] == judge.NO


def test_registry_package_sharing_the_name_is_not_a_root():
    old, new = files("1.0.0"), files("1.0.1")
    for f in (old, new):
        f["uv.lock"] = f["uv.lock"].replace('{ editable = "." }', '{ registry = "x" }')
    assert judge.judge_version_bump(diff(old, new))[0] == judge.UNKNOWN


def test_server_json_extra_field_is_no():
    old, new = files("1.0.0"), files("1.0.1")
    new["server.json"] = new["server.json"].replace('"name": "x"', '"name": "y"')
    assert judge.judge_version_bump(diff(old, new))[0] == judge.NO


def test_extra_file_and_edited_test_are_no():
    new = {**files("1.0.1"), "src/mod.py": "x = 1\n"}
    assert judge.judge_version_bump(diff(files("1.0.0"), new))[0] == judge.NO
    new = {**files("1.0.1"), "tests/test_a.py": "x = 1\n"}
    assert judge.judge_version_bump(diff(files("1.0.0"), new))[0] == judge.NO


def test_deleted_pyproject_and_garbage_are_unknown():
    new = files("1.0.1")
    del new["pyproject.toml"]
    assert judge.judge_version_bump(diff(files("1.0.0"), new))[0] == judge.UNKNOWN
    new = files("1.0.1")
    new["pyproject.toml"] = "not = [toml"
    assert judge.judge_version_bump(diff(files("1.0.0"), new))[0] == judge.UNKNOWN


def test_changelog_only_and_empty_are_unknown():
    assert judge.judge_version_bump({"CHANGELOG.md": ("a", "b")})[0] == judge.UNKNOWN
    assert judge.judge_version_bump({})[0] == judge.UNKNOWN


# ---- decide: two judges, parent shape ----------------------------------------


def policy(**kw):
    kw.setdefault("enabled", True)
    kw.setdefault("keys", ["integration"])
    return AttestVersionBumpConfig(**kw)


def read_ok(paths):
    return diff(files("1.0.0"), files("1.0.1"))


def decide(pol, **kw):
    args = dict(parents=["p"], base_tree="T", parent_tree="T")
    args.update(kw)
    return judge.decide(pol, list(read_ok(None)), read_ok, **args)


def test_decide_yes_records_parent_tree():
    ok, record = decide(policy())
    assert ok and record["applied"] and record["parent_tree"] == "T"


@pytest.mark.parametrize(
    "kw",
    [
        {"parents": ["a", "b"]},
        {"parents": []},
        {"parent_tree": "other"},
    ],
)
def test_decide_refuses_non_single_parent_or_wrong_base(kw):
    assert decide(policy(), **kw)[0] is False


def test_decide_off_or_no_keys_or_path_veto():
    assert decide(policy(enabled=False))[0] is False
    assert decide(policy(keys=[]))[0] is False
    assert decide(policy(paths=["CHANGELOG.md"]))[0] is False


def test_decide_content_veto_and_exception_fail_closed():
    ok, record = judge.decide(
        policy(),
        ["pyproject.toml"],
        lambda p: {
            "pyproject.toml": (PYPROJECT.format(v="1", dep="a"), PYPROJECT.format(v="2", dep="b"))
        },
        parents=["p"],
        base_tree="T",
        parent_tree="T",
    )
    assert not ok and record["content_verdict"] == judge.NO

    def boom(_):
        raise RuntimeError("x")

    ok, record = judge.decide(
        policy(), ["pyproject.toml"], boom, parents=["p"], base_tree="T", parent_tree="T"
    )
    assert not ok and record["content_verdict"] == judge.UNKNOWN


# ---- wiring over a real tmp git repo ------------------------------------------


def git(repo, *args):
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": __import__("os").environ["PATH"],
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def commit(repo, content, msg):
    for path, text in content.items():
        (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD")


KEYS = [
    AttestKeyConfig(name=n, cmd=f"just attest-{n}") for n in ("unit", "integration", "stateful")
]


def attest(**kw):
    return AttestConfig(keys=KEYS, version_bump=policy(keys=["integration", "stateful"], **kw))


def receipt_for(repo, parent, head):
    from beadhive.modules.work.domain.impact import ChangedPath

    paths = git(repo, "diff", "--name-only", parent, head).splitlines()
    return native_full_receipt(
        base_tree=git(repo, "rev-parse", f"{parent}^{{tree}}"),
        head_tree=git(repo, "rev-parse", f"{head}^{{tree}}"),
        changed=[ChangedPath(p) for p in paths],
        keys=[type("K", (), {"name": k.name})() for k in KEYS],
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def test_wiring_carries_named_green_keys_and_records_judge(repo):
    parent = commit(repo, files("1.0.0"), "init")
    head = commit(repo, files("1.0.1"), "bump")
    receipt = receipt_for(repo, parent, head)
    keys = [type("K", (), {"name": k.name})() for k in KEYS]
    out, record = version_bump_carry(
        attest(), str(repo), head, receipt, keys, parent_green=lambda key: True
    )
    assert record["applied"]
    assert set(out.unaffected_keys) == {"integration", "stateful"}
    assert out.invalidated_keys == ("unit",)
    assert out.judge["parent_tree"] == receipt.base_tree
    assert out.backend == "version-bump" and not out.is_fallback
    # the judge record is inside the digest and survives a round trip
    assert ImpactReceipt.from_dict(out.to_dict()).judge == out.judge
    assert out.digest != receipt.digest


def test_wiring_parent_not_green_or_off_or_dependency_keeps_normal_route(repo):
    parent = commit(repo, files("1.0.0"), "init")
    head = commit(repo, files("1.0.1"), "bump")
    receipt = receipt_for(repo, parent, head)
    keys = [type("K", (), {"name": k.name})() for k in KEYS]
    out, record = version_bump_carry(
        attest(), str(repo), head, receipt, keys, parent_green=lambda key: False
    )
    assert out is receipt and not record["applied"]
    off = AttestConfig(keys=KEYS)
    assert version_bump_carry(off, str(repo), head, receipt, keys)[0] is receipt

    dep = commit(repo, files("1.0.2", dep="other"), "dep")
    receipt = receipt_for(repo, head, dep)
    out, record = version_bump_carry(
        attest(), str(repo), dep, receipt, keys, parent_green=lambda k: True
    )
    assert out is receipt and record["content_verdict"] == judge.NO


def test_wiring_base_not_the_parent_is_refused(repo):
    root = commit(repo, files("1.0.0"), "init")
    commit(repo, files("1.0.1"), "bump1")
    head = commit(repo, files("1.0.2"), "bump2")
    receipt = receipt_for(repo, root, head)  # two commits back, not the parent
    keys = [type("K", (), {"name": k.name})() for k in KEYS]
    out, record = version_bump_carry(attest(), str(repo), head, receipt, keys)
    assert out is receipt and not record["applied"]


# ---- the carry is recorded as carried, with the judge record -------------------


def test_carried_ledger_record_names_parent_tree_and_judge(tmp_path, monkeypatch):
    from beadhive import host, validation_ledger
    from beadhive.modules.work.domain.impact import AttestKey, KeyEvidence

    host.mint_if_needed()
    repo = tmp_path / "ws" / "github" / "org" / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
    entry = {"provider": "github", "org": "org", "repo": "repo"}
    key = AttestKey("integration", "just attest-integration")
    validation_ledger.record(entry, "parent", key.cmd, 0)
    judged = ImpactReceipt(
        backend="version-bump",
        backend_version="1",
        base_tree="parent",
        head_tree="head",
        changed_paths=("pyproject.toml",),
        unowned_paths=(),
        global_inputs_hit=(),
        invalidated_keys=(),
        unaffected_keys=("integration",),
        evidence={"integration": KeyEvidence("version-bump")},
        judge={"kind": "version-bump", "parent_tree": "parent", "applied": True},
    )
    assert validation_ledger.carry_key_verdict(entry, key, judged)
    found = validation_ledger.key_verdict(entry, "head", key)
    assert found.state == validation_ledger.KeyVerdictState.CARRIED
    assert found.record["source_tree"] == "parent"
    assert found.record["receipt"]["judge"]["kind"] == "version-bump"
