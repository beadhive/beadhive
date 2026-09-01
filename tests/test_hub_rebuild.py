"""THE REBUILD TEST (bh-89wxf.1) — `rm -rf` the hub, hydrate again, get the identical aggregate.

This is the only test that proves nothing AUTHORITATIVE is hiding in the hub. Every other
property of the contract (no remote, never pushed, issues no ids) is a rule someone can forget;
this one is a measurement. If a bead can survive `rm -rf ~/.beadhive/hub` only because it was
stored there, the aggregate was never derived and the whole split is a lie.

Real bd, embedded, entirely inside `tmp_path` — zero production writes, and NOT marked
`integration` so it runs on the fast gate (same posture as `test_bd_repo_sync_additive.py`,
which self-skips when bd is absent).

It also pins the two halves of the prefix contract that only a real bd can answer:
  * `bd init --prefix _HUB_ISSUES_NO_IDS` is ACCEPTED (a punctuation sentinel like `!hub` is
    not — bd rejects it outright, which is why the prefix shouts inside bd's alphabet instead);
  * no bead reachable in the hub carries that prefix — every one keeps its SOURCE hive's.

The second test is bh-89wxf.2's other half: HQ's Dolt database carries only hq-prefixed beads,
asserted rather than eyeballed, through the same function the migration verb uses.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest

from beadhive import hub
from beadhive.run import run
from harness.beads import skip_if_no_bd

pytestmark = [skip_if_no_bd, pytest.mark.dolt_server]


def _bd_env() -> dict:
    return {**os.environ, "BD_NON_INTERACTIVE": "1"}


def _bd_init(path, prefix: str):
    """``bd init`` via cwd (not -C): init IS what creates the .beads dir."""
    path.mkdir(parents=True, exist_ok=True)
    return run(
        [
            "bd",
            "init",
            "--prefix",
            prefix,
            "--shared-server",
            "--skip-agents",
            "--skip-hooks",
            "--quiet",
        ],
        cwd=str(path),
        check=False,
        capture=True,
        env=_bd_env(),
    )


def _bd(path, *args):
    return run(
        ["bd", "-C", str(path), *[str(a) for a in args]], check=False, capture=True, env=_bd_env()
    )


def _make_hive(root, prefix: str, titles):
    """A real bd hive with `titles` as beads, exported to `.beads/issues.jsonl` — the one file
    the hub hydrates from (`hub._sync_hive`)."""
    hive = root / prefix
    assert _bd_init(hive, prefix).returncode == 0
    for title in titles:
        assert _bd(hive, "q", title).returncode == 0
    assert _bd(hive, "export", "-o", str(hive / ".beads" / "issues.jsonl")).returncode == 0
    return hive


def _hydrate(hub_dir, hives):
    """Stand the hub up and hydrate it with the same shared-server mode that
    `hub.ensure_store` uses, followed by the `repo add` + `repo sync` pair `hub.sync()` runs."""
    assert _bd_init(hub_dir, hub.HUB_PREFIX).returncode == 0, (
        f"bd refused the hub prefix {hub.HUB_PREFIX!r} — the sentinel must stay inside bd's "
        "own database-name alphabet"
    )
    for hive in hives:
        assert _bd(hub_dir, "repo", "add", str(hive)).returncode == 0
    assert _bd(hub_dir, "repo", "sync").returncode == 0


def _aggregate(hub_dir):
    """The comparable content of the aggregate: every bead's id/title/status, sorted.

    Deliberately NOT the whole record — `updated_at` and friends move on every import by
    construction (it is the field bd touches on every mutation, and the reason a per-host
    rebuild inside a replicated database is the maximal-conflict shape this molecule exists to
    undo). What must be identical is WHICH beads are there and what they say."""
    res = _bd(hub_dir, "list", "--all", "--json")
    assert res.returncode == 0, res.stderr
    beads = json.loads(res.stdout or "[]")
    beads = beads if isinstance(beads, list) else beads.get("issues", [])
    return sorted((b["id"], b.get("title", ""), b.get("status", "")) for b in beads)


def test_rm_rf_the_hub_then_rehydrate_yields_the_identical_aggregate(tmp_path):
    """Delete the hub outright and rebuild it from the hives alone — same aggregate, and no
    bead anywhere carrying the hub's own prefix."""
    hives = [
        _make_hive(tmp_path / "hives", "srcone", ["one alpha", "one beta"]),
        _make_hive(tmp_path / "hives", "srctwo", ["two gamma"]),
    ]
    hub_dir = tmp_path / "hub"

    _hydrate(hub_dir, hives)
    before = _aggregate(hub_dir)
    assert len(before) == 3, before

    shutil.rmtree(hub_dir)  # the whole point: the hub is disposable
    _hydrate(hub_dir, hives)

    assert _aggregate(hub_dir) == before

    # ISSUES NO IDS: every bead carries its SOURCE hive's prefix, never the hub's.
    assert all(bead_id.split("-")[0] in {"srcone", "srctwo"} for bead_id, _, _ in before), before
    assert not any(bead_id.startswith(hub.HUB_PREFIX) for bead_id, _, _ in before), before


# ---------------------------------------------------------------------------
# THE OTHER HALF (bh-89wxf.2): HQ's Dolt database carries only hq-prefixed beads. "Assert this,
# do not eyeball it" — so the check that gates a push is the same function the migration uses,
# run against a real bd store.
# ---------------------------------------------------------------------------


def test_hq_publishes_no_hive_derived_beads_and_prune_makes_it_so(tmp_path, monkeypatch, capsys):
    """A pre-split HQ carries other hives' beads; `bh hq prune-aggregate --confirm` removes
    exactly those and leaves HQ's own untouched, so what a later `bh hq push` publishes is
    HQ and only HQ."""
    from beadhive import hq as hq_mod

    hq_dir = tmp_path / "hq"
    assert _bd_init(hq_dir, "hq").returncode == 0
    assert _bd(hq_dir, "q", "an escalation").returncode == 0

    # Hydrate a hive's beads into HQ — exactly what `_aggregation_target()`'s HQ branch did.
    hive = _make_hive(tmp_path / "hives", "srcone", ["derived one", "derived two"])
    assert _bd(hq_dir, "repo", "add", str(hive)).returncode == 0
    assert _bd(hq_dir, "repo", "sync").returncode == 0

    derived = hq_mod.hive_derived_ids(hq_dir)
    assert len(derived) == 2 and all(i.startswith("srcone-") for i in derived), derived

    monkeypatch.setattr(hq_mod, "_hq_dir_or_exit", lambda: hq_dir)

    hq_mod.prune_aggregate(dry_run=True)
    assert hq_mod.hive_derived_ids(hq_dir) == derived  # dry run mutates nothing

    hq_mod.prune_aggregate(confirm=True)

    assert hq_mod.hive_derived_ids(hq_dir) == []
    # HQ's OWN bead survived — the prune is scoped, not a wipe.
    res = _bd(hq_dir, "list", "--all", "--json")
    beads = json.loads(res.stdout or "[]")
    beads = beads if isinstance(beads, list) else beads.get("issues", [])
    assert [b["id"] for b in beads if b["id"].startswith("hq-")], beads

    # …and re-running is a clean no-op, not an error.
    hq_mod.prune_aggregate(confirm=True)
    assert "already clean" in capsys.readouterr().out
