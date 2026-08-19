"""`bh hive repair --prefix <p>` — reconcile registry prefix vs beads-DB issue_prefix.

Real git hive under $GIT_WORKSPACE (identity/registry resolve for real); `bd config get
issue_prefix` and `bd rename-prefix` faked by monkeypatching the bd.json/bd.run seam directly
(no swarm/gate state to track here, so no need for test_plan_repair.py's stateful FakeBd).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import typer

from beadhive import bd as bd_mod
from beadhive import config, hive_repair, registry
from beadhive.run import run as real_run

_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

CONFIG_YAML = """\
providers: [github]
managed_repos:
  - {provider: github, org: myorg, repo: myrepo, prefix: mr, kind: personal}
  - {provider: github, org: myorg, repo: other, prefix: ot, kind: personal}
"""


def _git(*args, cwd):
    return real_run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


@pytest.fixture
def hive(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    main = ws_root / "github" / "myorg" / "myrepo"
    main.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=main)
    _git("config", "user.email", "human@example.com", cwd=main)
    _git("config", "user.name", "human", cwd=main)
    _git("commit", "--allow-empty", "-m", "init", cwd=main)
    (main / ".beads").mkdir()

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.delenv("WS_CREW", raising=False)
    monkeypatch.delenv("BH_DEV", raising=False)
    monkeypatch.chdir(main)
    return SimpleNamespace(main=main, tmp=tmp_path)


class FakeBd:
    """Stateful fake for the two bd calls repair makes: `config get issue_prefix` (read) and
    `rename-prefix <p>-` (write, mutates the served db_prefix so a second detect() converges)."""

    def __init__(self, db_prefix):
        self.db_prefix = db_prefix
        self.rename_calls = []

    def fake_json(self, args, cwd):
        assert args == ["config", "get", "issue_prefix"]
        return {"key": "issue_prefix", "schema_version": 1, "value": self.db_prefix}

    def fake_run(self, args, cwd, actor="", capture=False, text_input=None):
        assert args[0] == "rename-prefix"
        self.rename_calls.append(args[1])
        self.db_prefix = args[1].rstrip("-")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _patch_bd(monkeypatch, db_prefix):
    fake = FakeBd(db_prefix)
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    return fake


# ---- normalize_prefix -------------------------------------------------------


def test_normalize_prefix_strips_trailing_hyphen():
    assert hive_repair.normalize_prefix("mr-") == "mr"


def test_normalize_prefix_rejects_empty():
    with pytest.raises(hive_repair.RepairError, match="cannot be empty"):
        hive_repair.normalize_prefix("")


def test_normalize_prefix_rejects_bad_chars():
    with pytest.raises(hive_repair.RepairError, match="invalid prefix"):
        hive_repair.normalize_prefix("1abc")


def test_normalize_prefix_rejects_too_long():
    with pytest.raises(hive_repair.RepairError, match="max is 8"):
        hive_repair.normalize_prefix("abcdefgh")


# ---- detect / repair flow ---------------------------------------------------


def test_repair_detects_mismatch_and_dry_run_makes_no_changes(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    hive_repair.repair(hive="", prefix="newpre", yes=False, dry_run=True)
    out = capsys.readouterr().out
    assert "Registry prefix: mr -> newpre" in out
    assert "dry-run" in out
    assert fake.rename_calls == []
    cfg = config.load()
    entry = registry.find_entry(cfg, "github", "myorg", "myrepo")
    assert entry is not None
    assert entry["prefix"] == "mr"


def test_repair_refuses_without_yes(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="", prefix="newpre", yes=False, dry_run=False)
    assert exc.value.exit_code == 1
    assert "refusing" in capsys.readouterr().err
    assert fake.rename_calls == []


def test_repair_applies_with_yes(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "Database migrated" in out
    assert "Registry updated" in out
    assert "Prefixes consistent" in out
    assert fake.rename_calls == ["newpre-"]
    cfg = config.load()
    entry = registry.find_entry(cfg, "github", "myorg", "myrepo")
    assert entry is not None
    assert entry["prefix"] == "newpre"


def test_repair_second_run_is_idempotent_noop(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "mr")
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    fake.rename_calls.clear()
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "nothing to repair" in out
    assert fake.rename_calls == []


def test_repair_only_updates_registry_when_db_already_matches_target(hive, monkeypatch, capsys):
    fake = _patch_bd(monkeypatch, "newpre")
    hive_repair.repair(hive="", prefix="newpre", yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "Registry updated" in out
    assert "Database migrated" not in out
    assert fake.rename_calls == []


def test_repair_refuses_unregistered_hive(hive, monkeypatch):
    _patch_bd(monkeypatch, "mr")
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="github/nope/nope", prefix="newpre", yes=True, dry_run=False)
    assert exc.value.exit_code == 1


def test_repair_refuses_missing_beads_dir(hive, monkeypatch):
    _patch_bd(monkeypatch, "ot")
    other = hive.tmp / "ws" / "github" / "myorg" / "other"
    other.mkdir(parents=True)  # no .beads/ under it
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="github/myorg/other", prefix="newpre", yes=True, dry_run=False)
    assert exc.value.exit_code == 1


def test_repair_refuses_prefix_collision_with_another_hive(hive, monkeypatch):
    _patch_bd(monkeypatch, "mr")
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="", prefix="ot", yes=True, dry_run=False)
    assert exc.value.exit_code == 1


def test_repair_requires_exactly_one_mode(hive):
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="", yes=True, dry_run=False)
    assert exc.value.exit_code == 1
    with pytest.raises(typer.Exit) as exc:
        hive_repair.repair(hive="", prefix="newpre", node_id=True, yes=True, dry_run=False)
    assert exc.value.exit_code == 1


# ---- expected_role (bh-f3blt) -----------------------------------------------


def test_expected_role_maps_org_native_and_hq_to_maintainer():
    assert hive_repair.expected_role("org-native") == "maintainer"
    assert hive_repair.expected_role("hq") == "maintainer"


def test_expected_role_maps_everything_else_to_contributor():
    for kind in ("fork", "external", "personal", "prototype", ""):
        assert hive_repair.expected_role(kind) == "contributor"


# ---- --node-id mode (bh-y85rj) -----------------------------------------------


class FakeConfigKV:
    """Fakes `bd config get/set <key>` for a single in-memory key, shared by the node_id and
    beads.role repair tests — both read/write through the same two-verb shape."""

    def __init__(self, key, initial=""):
        self.key = key
        self.value = initial

    def fake_json(self, args, cwd, **kw):
        assert args == ["config", "get", self.key]
        return {"key": self.key, "schema_version": 1, "value": self.value}

    def fake_run(self, args, cwd, actor="", capture=False, text_input=None, **kw):
        assert args[:2] == ["config", "set"] and args[2] == self.key
        self.value = args[3]
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def _patch_kv(monkeypatch, key, initial=""):
    fake = FakeConfigKV(key, initial)
    monkeypatch.setattr(bd_mod, "json", fake.fake_json)
    monkeypatch.setattr(bd_mod, "run", fake.fake_run)
    return fake


def test_repair_node_id_sets_from_host_id(hive, monkeypatch, capsys):
    fake = _patch_kv(monkeypatch, "node_id", "")
    monkeypatch.setattr(hive_repair.host, "host_id", lambda: "host-abc")
    hive_repair.repair(hive="", node_id=True, yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "node_id set" in out
    assert fake.value == "host-abc"


def test_repair_node_id_dry_run_makes_no_changes(hive, monkeypatch, capsys):
    fake = _patch_kv(monkeypatch, "node_id", "")
    monkeypatch.setattr(hive_repair.host, "host_id", lambda: "host-abc")
    hive_repair.repair(hive="", node_id=True, yes=False, dry_run=True)
    assert "dry-run" in capsys.readouterr().out
    assert fake.value == ""


def test_repair_node_id_refuses_without_yes(hive, monkeypatch):
    _patch_kv(monkeypatch, "node_id", "")
    monkeypatch.setattr(hive_repair.host, "host_id", lambda: "host-abc")
    with pytest.raises(typer.Exit):
        hive_repair.repair(hive="", node_id=True, yes=False, dry_run=False)


def test_repair_node_id_already_set_is_noop(hive, monkeypatch, capsys):
    fake = _patch_kv(monkeypatch, "node_id", "host-abc")
    monkeypatch.setattr(hive_repair.host, "host_id", lambda: "host-abc")
    hive_repair.repair(hive="", node_id=True, yes=True, dry_run=False)
    assert "nothing to repair" in capsys.readouterr().out
    assert fake.value == "host-abc"


# ---- --role mode (bh-f3blt) --------------------------------------------------


def test_repair_role_sets_from_kind(hive, monkeypatch, capsys):
    fake = _patch_kv(monkeypatch, "beads.role", "")
    hive_repair.repair(hive="", role=True, yes=True, dry_run=False)
    out = capsys.readouterr().out
    assert "beads.role set" in out
    # myrepo's registered kind is "personal" -> contributor (not org-native/hq)
    assert fake.value == "contributor"


def test_repair_role_can_overwrite_an_explicit_mismatch_with_yes(hive, monkeypatch, capsys):
    fake = _patch_kv(monkeypatch, "beads.role", "maintainer")
    hive_repair.repair(hive="", role=True, yes=True, dry_run=False)
    assert fake.value == "contributor"


def test_repair_role_already_correct_is_noop(hive, monkeypatch, capsys):
    fake = _patch_kv(monkeypatch, "beads.role", "contributor")
    hive_repair.repair(hive="", role=True, yes=True, dry_run=False)
    assert "already correct" in capsys.readouterr().out
    assert fake.value == "contributor"


# ---- --role targets the HIVE, not the runner's cwd (bh-s08me) ---------------------------
#
# The regression that hid the CWD-vs-target bug at n=1: exercising `--role` against exactly
# one hive from that hive's OWN directory, where cwd and target coincide. These fake the real
# `bd` binary's actual quirk — discovered against the live fleet, see bh-s08me's brief —
# rather than faking bd.json/bd.run's return value directly: `-C <path>` scopes which beads DB
# bd opens, but a git-config-backed key (`beads.role`) resolves off the CHILD PROCESS's real
# cwd, independent of `-C`. `RealCwdGitConfig` models exactly that: one value per directory the
# subprocess actually ran in (`kwargs["cwd"]` when a caller pins it, else `os.getcwd()`), so a
# regression that drops `pin_process_cwd=True` at a call site makes this fail the same way the
# real fleet did (bh-s08me evidence #1-#4), not just a mocked-away green.


class RealCwdGitConfig:
    """Fakes `bd_mod._run` (the real subprocess seam) well enough to reproduce bd's own
    `-C` vs. process-cwd split for a git-config-backed key. One in-memory value per resolved
    directory."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def __call__(self, cmd, **kw):
        resolved = str(kw.get("cwd") or os.getcwd())
        if cmd[0] == "git":  # the READ path is a direct `git config --get` since bh-i6e5g
            value = self.store.get(resolved, "")
            # git exits 1 (not 0-with-empty) for a key that is not set — the signal
            # `bd.beads_role` distinguishes "unset" from "unreadable" by.
            return SimpleNamespace(returncode=0 if value else 1, stdout=value, stderr="")
        if "get" in cmd:
            import json as _json

            value = self.store.get(resolved, "")
            payload = _json.dumps({"key": "beads.role", "value": value})
            return SimpleNamespace(returncode=0, stdout=payload, stderr="")
        if "set" in cmd:
            self.store[resolved] = cmd[-1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected bd invocation: {cmd}")


CONFIG_YAML_TWO_KINDS = """\
providers: [github]
managed_repos:
  - {provider: github, org: myorg, repo: personalrepo, prefix: pr, kind: personal}
  - {provider: github, org: myorg, repo: orgrepo, prefix: og, kind: org-native}
"""


@pytest.fixture
def two_hives(tmp_path, monkeypatch):
    """Two registered hives with DIFFERENT expected roles (personal -> contributor,
    org-native -> maintainer), plus a THIRD directory that is neither — the shape the
    acceptance criteria require and the one the merged code was never exercised against."""
    ws_root = tmp_path / "ws"
    personal = ws_root / "github" / "myorg" / "personalrepo"
    org = ws_root / "github" / "myorg" / "orgrepo"
    runner = tmp_path / "runner"
    for d in (personal, org, runner):
        (d / ".beads").mkdir(parents=True)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(CONFIG_YAML_TWO_KINDS)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_CONFIG", str(cfg_path))
    monkeypatch.setenv("BH_HOME", str(tmp_path / "bhhome"))
    monkeypatch.delenv("WS_CREW", raising=False)
    monkeypatch.delenv("BH_DEV", raising=False)
    monkeypatch.chdir(runner)  # neither hive's own directory
    return SimpleNamespace(personal=personal, org=org, runner=runner)


def test_repair_role_n_gt_1_targets_each_hive_not_the_runner_cwd(two_hives, monkeypatch, capsys):
    """Repair BOTH hives, in one run, from a THIRD directory. Each must converge to its OWN
    expected role, and neither the runner's cwd nor the other hive's directory may pick up a
    stray value — the exact inversion bh-s08me's dogfood measured (10 mismatches became 5
    DIFFERENT ones after "repairing" them)."""
    fake = RealCwdGitConfig()
    monkeypatch.setattr(bd_mod, "_run", fake)

    hive_repair.repair(hive="github/myorg/personalrepo", role=True, yes=True, dry_run=False)
    capsys.readouterr()
    hive_repair.repair(hive="github/myorg/orgrepo", role=True, yes=True, dry_run=False)
    capsys.readouterr()

    assert fake.store.get(str(two_hives.personal)) == "contributor"
    assert fake.store.get(str(two_hives.org)) == "maintainer"
    # Neither repair wrote to the directory the process was actually sitting in.
    assert str(two_hives.runner) not in fake.store


def test_doctor_beads_role_reads_each_hive_not_the_runner_cwd(two_hives, monkeypatch):
    """`bh doctor`'s beads.role section must report each hive's OWN value, not the runner's
    cwd's — checked from the same third directory, on the SAME two-hive fixture as the repair
    test above (the n>1 shape)."""
    from beadhive import doctor

    fake = RealCwdGitConfig()
    # The runner's own directory answers 'maintainer' for anything unpinned — if either read
    # below leaks through unscoped, it would silently pass this value off as the hive's own.
    fake.store[str(two_hives.runner)] = "maintainer"
    monkeypatch.setattr(bd_mod, "_run", fake)

    findings = doctor._data_beads_role(config.load())

    by_hive = {f["hive"]: f for f in findings}
    # personal -> contributor expected; unset ('') != expected -> flagged, and NOT 'maintainer'
    # (which is what the runner cwd would have silently answered before this fix).
    assert by_hive["github/myorg/personalrepo"]["actual"] == ""
    assert by_hive["github/myorg/personalrepo"]["expected"] == "contributor"
    # org-native -> maintainer expected; unset ('') != expected -> also flagged.
    assert by_hive["github/myorg/orgrepo"]["actual"] == ""
    assert by_hive["github/myorg/orgrepo"]["expected"] == "maintainer"


def test_beads_role_real_git_reads_the_TARGET_repo_not_the_process_cwd(tmp_path, monkeypatch):
    """bh-i6e5g swapped the `bd config get beads.role` spawn for a direct `git config --get`.
    That read must keep bh-s08me's scoping, so this checks it against REAL git rather than the
    fake above: two real repos with DIFFERENT beads.role, plus a third the process actually
    sits in that answers something else entirely. A read that leaks to the process cwd — the
    original bug — returns 'runner-value' for both."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))  # no host global leak
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    dirs = {}
    for name, role in (("a", "contributor"), ("b", "maintainer"), ("runner", "runner-value")):
        d = tmp_path / name
        d.mkdir()
        _git("init", "-q", cwd=d)
        _git("config", "beads.role", role, cwd=d)
        dirs[name] = d
    monkeypatch.chdir(dirs["runner"])

    assert bd_mod.beads_role(dirs["a"]) == "contributor"
    assert bd_mod.beads_role(dirs["b"]) == "maintainer"

    unset = tmp_path / "unset"
    unset.mkdir()
    _git("init", "-q", cwd=unset)
    assert bd_mod.beads_role(unset) == ""  # set-to-nothing, reportable as drift

    # Outside a repo git still exits 1 ("not set" in the scopes it can see), which is the same
    # answer as an unset key — the None case is a git that could not run at all (exit 127).
    plain = tmp_path / "notarepo"
    plain.mkdir()
    assert bd_mod.beads_role(plain) == ""
    monkeypatch.setattr(
        bd_mod, "_run", lambda *a, **k: SimpleNamespace(returncode=127, stdout="", stderr="")
    )
    assert bd_mod.beads_role(dirs["a"]) is None  # no git, no answer — caller skips, never flags


def test_sync_remote_reads_config_yaml_without_a_subprocess(tmp_path, monkeypatch):
    """bh-i6e5g: `sync.remote` comes off `.beads/config.yaml` now. Both spellings bd's dotted
    key accepts resolve, a store without the key is "", and no subprocess is spawned at all."""
    monkeypatch.setattr(
        bd_mod, "_run", lambda *a, **k: pytest.fail("sync_remote must not spawn a process")
    )
    flat = tmp_path / "flat"
    (flat / ".beads").mkdir(parents=True)
    (flat / ".beads" / "config.yaml").write_text('sync.remote: "git+ssh://git@example.com/x.git"\n')
    assert bd_mod.sync_remote(flat) == "git+ssh://git@example.com/x.git"

    nested = tmp_path / "nested"
    (nested / ".beads").mkdir(parents=True)
    (nested / ".beads" / "config.yaml").write_text("sync:\n  remote: ssh://n\n")
    assert bd_mod.sync_remote(nested) == "ssh://n"

    bare = tmp_path / "bare"
    (bare / ".beads").mkdir(parents=True)
    (bare / ".beads" / "config.yaml").write_text("# nothing wired\n")
    assert bd_mod.sync_remote(bare) == ""
    assert bd_mod.sync_remote(tmp_path / "nosuchhive") == ""  # absent file, not a crash
