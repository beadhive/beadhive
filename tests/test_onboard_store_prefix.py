"""onboard adopts an existing bead store's own prefix instead of the repo name (bh-ezrq9).

`bh hive onboard github/briancripe/nvidia-hackathon` registered prefix `nvidia-hackathon` while
the store that came down with the clone held ~40 beads, every one of them `nvhack-`. The
registration named a hive whose own beads could not be reached by the registered name — and
re-onboarding on a second machine produced a different answer than the first, which makes
cloning on a new host a migration rather than the sync the dolt-ref story promises.
"""

from __future__ import annotations

import pytest

from beadhive import bd as bd_mod
from beadhive import onboard, registry


def _ctx(tmp_path, **kw) -> onboard.Ctx:
    target = tmp_path / "github" / "briancripe" / "nvidia-hackathon"
    target.mkdir(parents=True, exist_ok=True)
    return onboard.Ctx(
        hive="github/briancripe/nvidia-hackathon",
        target=str(target),
        provider="github",
        org="briancripe",
        repo="nvidia-hackathon",
        cfg={},
        **kw,
    )


def _store(monkeypatch, prefix: str) -> list:
    """Stub the store probe; returns the list of cwds it was asked about."""
    asked: list = []

    def _probe(cwd):
        asked.append(str(cwd))
        return prefix

    monkeypatch.setattr(onboard.bd_mod, "store_prefix", _probe)
    return asked


# ---- the adoption itself ----------------------------------------------------


def test_a_store_prefix_wins_over_one_derived_from_the_repo_name(tmp_path, monkeypatch, capsys):
    asked = _store(monkeypatch, "nvhack")
    ctx = _ctx(tmp_path)

    assert onboard._adopt_store_prefix(ctx, "nvidia-hackathon") == "nvhack"
    assert asked == [ctx.target], "the probe reads the clone on disk, not the remote"


def test_the_disagreement_is_REPORTED_not_silently_resolved(tmp_path, monkeypatch, capsys):
    """The bead's third acceptance criterion. Adopting is the right call and is still a
    surprise: the operator asked for a repo by name and got a hive registered under another."""
    _store(monkeypatch, "nvhack")

    onboard._adopt_store_prefix(_ctx(tmp_path), "nvidia-hackathon")

    err = capsys.readouterr().err
    assert "nvhack" in err and "nvidia-hackathon" in err, "name BOTH, or the note explains nothing"
    assert "--prefix" in err, "and say how to override it"


def test_no_store_leaves_the_derived_prefix_untouched_and_silent(tmp_path, monkeypatch, capsys):
    """The ordinary case — a repo with no beads yet — must be byte-for-byte what it was."""
    _store(monkeypatch, "")

    assert onboard._adopt_store_prefix(_ctx(tmp_path), "nvidia-hackathon") == "nvidia-hackathon"
    assert capsys.readouterr().err == ""


def test_an_agreeing_store_says_nothing(tmp_path, monkeypatch, capsys):
    _store(monkeypatch, "nvhack")

    assert onboard._adopt_store_prefix(_ctx(tmp_path), "nvhack") == "nvhack"
    assert capsys.readouterr().err == "", "there is no disagreement to report"


def test_an_absent_target_says_the_store_could_not_be_consulted(tmp_path, monkeypatch, capsys):
    """`--dry-run` against a repo that is not on disk yet skips the clone, so there is no store
    to ask. Presenting the derived prefix as settled would be the same silent guess again."""
    _store(monkeypatch, "nvhack")
    ctx = _ctx(tmp_path, clone_url="https://example.invalid/r.git")
    ctx.target = str(tmp_path / "not" / "cloned" / "yet")

    assert onboard._adopt_store_prefix(ctx, "nvidia-hackathon") == "nvidia-hackathon"
    assert "could not be consulted" in capsys.readouterr().err


# ---- precedence: where adoption sits among the other prefix sources ---------


def test_an_explicit_prefix_flag_still_wins_over_the_store(tmp_path, monkeypatch):
    """`--prefix` is the documented override, and the note tells operators to reach for it."""
    _store(monkeypatch, "nvhack")
    ctx = _ctx(tmp_path, prefix="chosen")

    onboard._resolve_kind_prefix_upstream(ctx, {}, "github", "briancripe", "nvidia-hackathon", None)

    assert ctx.prefix == "chosen"


def test_a_registered_prefix_still_wins_over_the_store(tmp_path, monkeypatch):
    """Unchanged, and deliberately: re-registering under a different prefix would orphan every
    existing bead id. `_note_prefix_drift` owns telling the operator about that case."""
    asked = _store(monkeypatch, "nvhack")
    ctx = _ctx(tmp_path)

    onboard._resolve_kind_prefix_upstream(
        ctx, {}, "github", "briancripe", "nvidia-hackathon", {"prefix": "registered", "kind": "x"}
    )

    assert ctx.prefix == "registered"
    assert asked == [], "no store probe on the preserve path — the registry already answered"


def test_the_fresh_path_consults_the_store_after_deriving(tmp_path, monkeypatch):
    """The end-to-end wiring, not just the helper: a fresh hive derives, then adopts."""
    _store(monkeypatch, "nvhack")
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "org-native")
    monkeypatch.setattr(registry, "derive_prefix", lambda *a, **k: ("nvidia-hackathon", []))
    ctx = _ctx(tmp_path)

    onboard._resolve_kind_prefix_upstream(ctx, {}, "github", "briancripe", "nvidia-hackathon", None)

    assert ctx.prefix == "nvhack"


# ---- the probe ---------------------------------------------------------------


def test_store_prefix_reads_the_underscore_key_bd_actually_serves(monkeypatch):
    """`bd config get issue-prefix` (hyphen) answers "(not set)" even on a store whose prefix IS
    set; the live key is `issue_prefix`. Reading the JSON map sidesteps both traps."""
    seen = {}

    def _json(args, cwd):
        seen["args"] = list(args)
        return {"issue_prefix": "nvhack", "schema_version": 1}

    monkeypatch.setattr(bd_mod, "json", _json)

    assert bd_mod.store_prefix("/somewhere") == "nvhack"
    assert seen["args"] == ["config", "list"]


@pytest.mark.parametrize("reply", [None, {}, {"issue_prefix": ""}, {"issue_prefix": "   "}, []])
def test_store_prefix_returns_empty_for_every_cannot_answer_case(monkeypatch, reply):
    """No store, an unreachable one, an unparseable reply — all mean "fall back to deriving".
    It must never raise: this runs inside onboard's preflight, where a throwing probe would turn
    "this repo has no beads yet", the ordinary case, into a failed onboard."""
    monkeypatch.setattr(bd_mod, "json", lambda *_a, **_k: reply)
    assert bd_mod.store_prefix("/somewhere") == ""
