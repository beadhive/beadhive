"""High-water carry roots across successive transitions (attest-keys ADR, section 3.5; bh-xdmga).

These drive the real evaluator (`selective_validation.run`) over a real git repository and the
real validation ledger. Impact comes from the real fail-closed resolver over a small fake backend,
so the fail-closed rules (unowned paths, global inputs, backend errors) are the production ones.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import host, selective_validation, validation_ledger
from beadhive.adapters.impact_git import GitTreeDiff
from beadhive.modules.work.application.impact import FailClosedResolver
from beadhive.modules.work.domain.impact import (
    AttestKey,
    BackendImpact,
    ImpactReceipt,
    KeyEvidence,
)

K = {"name": "k", "cmd": "just attest-k", "selectors": {"fake": "attest:k"}}
J = {"name": "j", "cmd": "just attest-j", "selectors": {"fake": "attest:j"}}
K_KEY = AttestKey("k", "just attest-k", selectors={"fake": "attest:k"})
OWNED = ("k", "j", "docs")


@pytest.fixture(autouse=True)
def _host():
    host.mint_if_needed()


class _Backend:
    """Owns `k/`, `j/` and `docs/`; anything else is unowned (rule 1). Optionally refuses to
    answer about a span starting at one tree, or lists global-input patterns (rule 2)."""

    name = "fake"
    version = "1"

    def __init__(self, *, refuse_base: str = "", global_inputs: tuple[str, ...] = ()):
        self.refuse_base = refuse_base
        self.global_inputs = global_inputs

    def analyze(self, request):
        if self.refuse_base and request.base_tree == self.refuse_base:
            raise RuntimeError("graph unavailable for this span")
        owners = {
            change.path: (change.path.split("/")[0],)
            for change in request.changed
            if change.path.split("/")[0] in OWNED
        }
        return BackendImpact(
            backend_version="1",
            owners=owners,
            key_units={"k": ("k",), "j": ("j",)},
            proven_keys=frozenset({"k", "j"}),
            global_inputs=self.global_inputs,
        )


class _Spy:
    """Records every (base tree, head tree) span the evaluator asked about."""

    def __init__(self, inner):
        self.inner = inner
        self.spans: list[tuple[str, str]] = []

    def resolve(self, repo, base_rev, head_rev, keys):
        receipt = self.inner.resolve(repo, base_rev, head_rev, keys)
        self.spans.append((receipt.base_tree, receipt.head_tree))
        return receipt


class Hive:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.repo = tmp_path / "ws" / "github" / "org" / "repo"
        self.repo.mkdir(parents=True)
        monkeypatch.setenv("GIT_WORKSPACE", str(tmp_path / "ws"))
        self.monkeypatch = monkeypatch
        self.entry = {"provider": "github", "org": "org", "repo": "repo"}
        self.backend = _Backend()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.c0 = self.commit({"k/a.txt": "k", "j/a.txt": "j", "docs/r.md": "0"}, "c0")
        self.runs: list[tuple[str, str]] = []

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, files: dict[str, str], message: str) -> str:
        for path, text in files.items():
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def tree(self, rev: str) -> str:
        return self.git("rev-parse", f"{rev}^{{tree}}")

    def cfg(self, **carry) -> dict:
        gate = {"carry": {"high_water": True, **carry}}
        return {"work": {"attest": {"keys": [K, J]}, "gate": gate}}

    def land(self, base: str, head: str, cfg: dict) -> tuple[int, list[str], str, _Spy]:
        """One transition: the evaluator at `head` against `base`; the runner records at head."""
        spy = _Spy(FailClosedResolver(self.backend, GitTreeDiff()))
        self.monkeypatch.setattr(selective_validation, "impact_resolver", lambda *_a, **_k: spy)
        ran: list[str] = []

        def runner(cmd: str) -> int:
            ran.append(cmd)
            self.runs.append((head, cmd))
            validation_ledger.record(self.entry, head, cmd, 0, cfg=cfg)
            return 0

        out = _Capture()
        self.monkeypatch.setattr(selective_validation.typer, "echo", out)
        rc = selective_validation.run(
            self.entry,
            cfg,
            base_rev=base,
            head_rev=head,
            runner=runner,
            repo_path=str(self.repo),
        )
        return rc, ran, out.text, spy

    def carried(self, rev: str) -> dict:
        found = validation_ledger.key_verdict(self.entry, rev, K_KEY)
        assert found.state == validation_ledger.KeyVerdictState.CARRIED, found
        return found.record or {}

    def current(self, rev: str) -> dict:
        found = validation_ledger.verdict(self.entry, rev, K_KEY.cmd)
        assert found is not None
        return found


class _Capture:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, message="", **_kwargs):
        self.lines.append(str(message))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def hive(tmp_path, monkeypatch):
    return Hive(tmp_path, monkeypatch)


def _three_lands(hive: Hive, cfg: dict):
    """Bead 1 and 2 touch docs only; bead 3 touches docs and `j/`. Returns each land's result."""
    c1 = hive.commit({"docs/r.md": "1"}, "bead 1")
    first = hive.land(hive.c0, c1, cfg)
    c2 = hive.commit({"docs/r.md": "2"}, "bead 2")
    second = hive.land(c1, c2, cfg)
    c3 = hive.commit({"docs/r.md": "3", "j/a.txt": "j3"}, "bead 3")
    third = hive.land(c2, c3, cfg)
    return (c1, c2, c3), (first, second, third)


def test_unaffected_key_runs_once_then_carries_from_its_root_across_two_lands(hive) -> None:
    cfg = hive.cfg()
    (c1, c2, c3), (first, second, third) = _three_lands(hive, cfg)

    # Land 1 bootstraps both keys (no source anywhere); lands 2 and 3 never run K again.
    assert [rc for rc, *_ in (first, second, third)] == [0, 0, 0]
    assert sorted(first[1]) == ["just attest-j", "just attest-k"]
    assert second[1] == []
    assert third[1] == ["just attest-j"]  # the affected key re-runs
    assert [cmd for _rev, cmd in hive.runs].count("just attest-k") == 1

    root = hive.current(c1)
    for rev in (c2, c3):
        record = hive.carried(rev)
        assert record["source_tree"] == hive.tree(c1)
        assert record["source_run_id"] == root["run_id"]
        assert record["at"] == root["at"]  # the root's time: age and TTL are never refreshed

    # Land 3 reached the root through the carried base and diffed root -> head directly.
    third_spy = third[3]
    assert (hive.tree(c1), hive.tree(c3)) in third_spy.spans
    assert hive.carried(c3)["receipt"]["base_tree"] == hive.tree(c1)
    assert f"k: carried from root {hive.tree(c1)[:12]}" in third[2]
    assert f"via receipt {hive.carried(c3)['receipt_digest'][:12]}" in third[2]
    # The single-hop land 2 printed its source tree and receipt digest too.
    assert f"k: carried from {hive.tree(c1)[:12]} via receipt" in second[2]


def test_high_water_off_restores_single_hop_sourcing(hive) -> None:
    cfg = hive.cfg(high_water=False)
    (_c1, _c2, c3), (_first, second, third) = _three_lands(hive, cfg)

    assert second[1] == []  # single-hop from a real run on the base still carries
    assert sorted(third[1]) == ["just attest-j", "just attest-k"]
    assert len(third[3].spans) == 1
    assert validation_ledger.key_verdict(hive.entry, c3, K_KEY).state == "current"


def test_unset_high_water_follows_the_legacy_gate_model(hive) -> None:
    cfg = {"work": {"attest": {"keys": [K, J]}}}
    (_c1, _c2, _c3), (_first, _second, third) = _three_lands(hive, cfg)

    assert sorted(third[1]) == ["just attest-j", "just attest-k"]


def test_expired_root_forces_a_run(hive, monkeypatch) -> None:
    cfg = hive.cfg()
    c1 = hive.commit({"docs/r.md": "1"}, "bead 1")
    hive.land(hive.c0, c1, cfg)
    c2 = hive.commit({"docs/r.md": "2"}, "bead 2")
    hive.land(c1, c2, cfg)
    later = time.time() + 2 * 86400  # past the default P1D TTL of the root run
    monkeypatch.setattr(validation_ledger, "time", SimpleNamespace(time=lambda: later))
    c3 = hive.commit({"docs/r.md": "3"}, "bead 3")

    rc, ran, _out, spy = hive.land(c2, c3, cfg)

    assert rc == 0
    assert "just attest-k" in ran
    assert all(base != hive.tree(c1) for base, _head in spy.spans)


def test_root_whose_commit_is_not_resolvable_is_skipped(hive) -> None:
    cfg = hive.cfg()
    side = hive.commit({"docs/r.md": "side"}, "side branch, later discarded")
    validation_ledger.record(hive.entry, side, K_KEY.cmd, 0, cfg=cfg)
    side_tree = hive.tree(side)
    hive.git("reset", "-q", "--hard", hive.c0)
    hive.git("update-ref", "-d", "ORIG_HEAD")
    hive.git("reflog", "expire", "--expire=now", "--all")
    hive.git("gc", "-q", "--prune=now")
    gone = subprocess.run(["git", "-C", str(hive.repo), "cat-file", "-e", side], check=False)
    assert gone.returncode != 0  # the root's commit no longer resolves in this clone
    assert validation_ledger.verdict(hive.entry, side_tree, K_KEY.cmd) is not None
    head = hive.commit({"docs/r.md": "head"}, "bead")

    rc, ran, _out, spy = hive.land(hive.c0, head, cfg)

    assert rc == 0
    assert "just attest-k" in ran
    assert all(base != side_tree for base, _head in spy.spans)


@pytest.mark.parametrize(
    "span_change,backend",
    [
        ({"other/x.txt": "x"}, _Backend()),  # unowned path in root -> base
        ({"docs/g.cfg": "g"}, _Backend(global_inputs=("docs/*.cfg",))),  # global input
        ({"docs/r.md": "2"}, None),  # backend error over the root span -> fallback receipt
    ],
    ids=["unowned", "global-input", "fallback"],
)
def test_doubt_anywhere_in_the_root_to_head_span_invalidates_the_key(
    hive, span_change, backend
) -> None:
    """The base's carry was earned when this span looked clean (an older backend answer); the
    current resolver's whole-span receipt is what decides the onward carry."""
    cfg = hive.cfg()
    c1 = hive.commit({"docs/r.md": "1"}, "bead 1")
    hive.land(hive.c0, c1, cfg)
    c2 = hive.commit(span_change, "bead 2")
    earlier = ImpactReceipt(
        backend="fake",
        backend_version="1",
        base_tree=hive.tree(c1),
        head_tree=hive.tree(c2),
        changed_paths=tuple(span_change),
        unowned_paths=(),
        global_inputs_hit=(),
        invalidated_keys=(),
        unaffected_keys=("k",),
        evidence={"k": KeyEvidence("unaffected", ("k",))},
    )
    assert validation_ledger.carry_key_verdict(hive.entry, K_KEY, earlier, cfg=cfg)
    hive.backend = backend or _Backend(refuse_base=hive.tree(c1))
    c3 = hive.commit({"docs/r.md": "3"}, "bead 3")

    rc, ran, out, spy = hive.land(c2, c3, cfg)

    assert rc == 0
    assert (hive.tree(c1), hive.tree(c3)) in spy.spans  # the root span was asked about
    assert "just attest-k" in ran
    assert f"high-water root {hive.tree(c1)[:12]} skipped" in out
    assert validation_ledger.key_verdict(hive.entry, c3, K_KEY).state == "current"


def test_newest_ledger_root_carries_when_the_base_has_no_proof(hive) -> None:
    cfg = hive.cfg()
    side = hive.commit({"docs/r.md": "side"}, "side branch")
    validation_ledger.record(hive.entry, side, K_KEY.cmd, 0, cfg=cfg)
    hive.git("reset", "-q", "--hard", hive.c0)
    head = hive.commit({"docs/r.md": "head"}, "bead")

    rc, ran, out, spy = hive.land(hive.c0, head, cfg)

    assert rc == 0
    assert ran == ["just attest-j"]
    assert (hive.tree(side), hive.tree(head)) in spy.spans
    assert hive.carried(head)["source_tree"] == hive.tree(side)
    assert f"k: carried from root {hive.tree(side)[:12]}" in out


def test_max_roots_zero_disables_the_ledger_scan(hive) -> None:
    cfg = hive.cfg(max_roots=0)
    side = hive.commit({"docs/r.md": "side"}, "side branch")
    validation_ledger.record(hive.entry, side, K_KEY.cmd, 0, cfg=cfg)
    hive.git("reset", "-q", "--hard", hive.c0)
    head = hive.commit({"docs/r.md": "head"}, "bead")

    _rc, ran, _out, spy = hive.land(hive.c0, head, cfg)

    assert sorted(ran) == ["just attest-j", "just attest-k"]
    assert len(spy.spans) == 1


def test_a_red_base_is_never_routed_around(hive) -> None:
    cfg = hive.cfg()
    side = hive.commit({"docs/r.md": "side"}, "side branch")
    validation_ledger.record(hive.entry, side, K_KEY.cmd, 0, cfg=cfg)
    hive.git("reset", "-q", "--hard", hive.c0)
    validation_ledger.record(hive.entry, hive.c0, K_KEY.cmd, 1, cfg=cfg)
    head = hive.commit({"docs/r.md": "head"}, "bead")

    assert (
        validation_ledger.carry_root_candidates(
            hive.entry, hive.tree(hive.c0), K_KEY, max_roots=3, cfg=cfg
        )
        == ()
    )
    _rc, ran, _out, _spy = hive.land(hive.c0, head, cfg)
    assert "just attest-k" in ran


@pytest.mark.parametrize(
    "work,expected",
    [
        ({}, (False, 3)),
        ({"gate": {"model": "keys"}}, (True, 3)),
        ({"gate": {"model": "legacy", "carry": {"high_water": True}}}, (True, 3)),
        ({"gate": {"model": "keys", "carry": {"high_water": False}}}, (False, 3)),
        ({"gate": {"carry": {"high_water": "yes", "max_roots": 5}}}, (False, 5)),
        ({"gate": {"carry": {"max_roots": -1}}}, (False, 0)),
        ({"gate": {"carry": {"max_roots": "3"}}}, (False, 0)),
    ],
)
def test_carry_settings_degrade_toward_fewer_candidates(work, expected) -> None:
    from beadhive import config_work_settings as settings

    cfg = {"work": work}
    assert (settings.carry_high_water(cfg, {}), settings.carry_max_roots(cfg, {})) == expected


# ---- bh-usmrs: post-land and union run the evaluator under work.gate.model: keys ----------


def _keys_cfg(hive: Hive, model: str = "keys") -> dict:
    cfg = hive.cfg()
    cfg["work"]["gate"]["model"] = model
    hive.monkeypatch.setattr(selective_validation, "catalog_ok", lambda *_a, **_k: True)
    cfg["work"]["validate"] = {"postland": "just legacy-postland", "union": "just legacy-union"}
    return cfg


def _land_api(hive: Hive, cfg: dict, *, red: frozenset = frozenset()):
    """A minimal work_merge api over the REAL evaluator, ledger and config; checkouts are recorded
    (and ledger-recorded at the tip, as the production runner does) instead of shelling out."""
    ran: list[str] = []
    events: list[str] = []
    monkeypatch = hive.monkeypatch
    spy = _Spy(FailClosedResolver(hive.backend, GitTreeDiff()))
    monkeypatch.setattr(selective_validation, "impact_resolver", lambda *_a, **_k: spy)
    monkeypatch.setattr(selective_validation.typer, "echo", lambda *_a, **_k: None)

    def clean_checkout(entry, rev, cmd, **_kw):
        ran.append(cmd)
        rc = 1 if cmd in red else 0
        validation_ledger.record(entry, hive.git("rev-parse", rev), cmd, rc, cfg=cfg)
        return rc

    api = SimpleNamespace(
        config=selective_validation.config,
        selective_validation=selective_validation,
        worktree=SimpleNamespace(
            clean_checkout=clean_checkout, clone_for_branch=lambda *_a: hive.repo
        ),
        otel=SimpleNamespace(count_validation=lambda *_a, **_k: None),
        typer=SimpleNamespace(
            echo=lambda msg, **_k: events.append(f"echo:{msg}"),
            Exit=type("Exit", (Exception,), {}),
        ),
        _rollback_or_keep=lambda *_a: events.append("rollback") or True,
    )
    return api, ran, events


def _same_tree_commit(hive: Hive, rev: str, message: str) -> str:
    return hive.git("commit-tree", f"{rev}^{{tree}}", "-p", "HEAD", "-m", message)


def _postland(hive, api, cfg, mol, tip):
    hive.git("update-ref", "refs/heads/tip", tip)
    from beadhive import work_merge

    work_merge.impl__postland_revalidate_molecule(
        api, cfg, hive.entry, str(hive.repo), "tip", mol, "conservative", False, "e-1", mol, {}
    )


def test_keys_postland_onto_unmoved_main_reuses_every_key_and_runs_nothing(hive) -> None:
    cfg = _keys_cfg(hive)
    mol = hive.commit({"docs/r.md": "1", "j/a.txt": "j1"}, "molecule")
    assert hive.land(hive.c0, mol, cfg)[0] == 0  # the pre-land molecule gate ran every key
    tip = _same_tree_commit(hive, mol, "chore(merge): molecule e-1")  # byte-identical tree
    api, ran, events = _land_api(hive, cfg)

    _postland(hive, api, cfg, mol, tip)

    assert ran == [] and events == []


def test_keys_postland_onto_moved_main_runs_only_the_delta_affected_keys(hive) -> None:
    cfg = _keys_cfg(hive)
    mol = hive.commit({"docs/r.md": "1"}, "molecule")
    assert hive.land(hive.c0, mol, cfg)[0] == 0
    moved = hive.commit({"docs/r.md": "2", "j/a.txt": "moved"}, "main moved underneath")
    api, ran, events = _land_api(hive, cfg)

    _postland(hive, api, cfg, mol, moved)

    assert ran == ["just attest-j"]  # k carried from the validated molecule tree
    assert "rollback" not in events


def test_keys_postland_red_key_rolls_back_with_the_existing_message(hive) -> None:
    cfg = _keys_cfg(hive)
    mol = hive.commit({"docs/r.md": "1"}, "molecule")
    assert hive.land(hive.c0, mol, cfg)[0] == 0
    moved = hive.commit({"j/a.txt": "red"}, "main moved underneath")
    api, ran, events = _land_api(hive, cfg, red=frozenset({"just attest-j"}))

    with pytest.raises(api.typer.Exit):
        _postland(hive, api, cfg, mol, moved)

    assert events[0] == "rollback"
    assert "post-land validation failed (exit 1)" in events[1]
    assert "Rolled tip back to" in events[1]


def test_legacy_postland_runs_the_command_string_unchanged(hive) -> None:
    cfg = _keys_cfg(hive, model="legacy")
    mol = hive.commit({"docs/r.md": "1"}, "molecule")
    api, ran, events = _land_api(hive, cfg)

    _postland(hive, api, cfg, mol, _same_tree_commit(hive, mol, "land"))

    assert ran == ["just legacy-postland"]


def test_keys_union_validator_runs_only_affected_keys_and_legacy_has_none(hive) -> None:
    from beadhive import work_merge

    cfg = _keys_cfg(hive)
    pre = hive.commit({"docs/r.md": "1"}, "main tip")
    assert hive.land(hive.c0, pre, cfg)[0] == 0
    result = hive.commit({"j/a.txt": "union"}, "union result")
    api, ran, _events = _land_api(hive, cfg)

    validate = work_merge._union_key_validator(api, cfg, hive.entry)
    assert validate(pre, result, "main") == 0
    assert ran == ["just attest-j"]

    legacy = _keys_cfg(hive, model="legacy")
    assert work_merge._union_key_validator(api, legacy, hive.entry) is None
