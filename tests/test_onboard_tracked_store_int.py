"""Second-host onboard of a FURNISHED hive whose repository tracks `.beads/` (bh-ky918).

The checkout already has `.beads/` (config.yaml, metadata.json, .gitignore) from `git clone`,
but no database on the host's shared Dolt server. Onboarding must clone the internal store from
the HQ descriptor (moto S3 on loopback, dummy credentials), leave the tracked tree clean, and
never read the code origin's `refs/dolt/data`. Real bd and Dolt; skips without them.

Reuses the two-store world (tests/test_two_store_e2e_int.py): one bare code origin, a moto
internal remote, one isolated shared server per host. The server-mode clone waits about 70 s for
its not-yet-existing database (bh-mi40l), so this is the only clone in the file.
"""

from __future__ import annotations

import pytest

from beadhive import hive_ready, onboard, store_locator
from harness.beads import skip_if_no_bd
from harness.s3 import moto_s3  # noqa: F401 - pytest fixture
from harness.world import git
from test_two_store_e2e_int import (  # noqa: F401 - `world` is a pytest fixture
    PRIVATE_TITLE,
    _bd,
    _create,
    _ctx,
    _invoke,
    _titles,
    world,
)

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server, skip_if_no_bd]


def _furnished(host):
    ctx = _ctx(host)
    ctx.furnish = True
    return ctx


def test_second_host_clones_the_internal_store_despite_tracked_beads(world):  # noqa: F811
    # First host: seed the empty remote, then TRACK the .beads scaffolding in the code repo.
    first = world.host("first")
    world.monkeypatch.setenv(onboard.SEED_REMOTE_ENV, "widget")
    onboard._act_bd_init(_furnished(first))
    world.monkeypatch.delenv(onboard.SEED_REMOTE_ENV, raising=False)
    private = _create(first.checkout, PRIVATE_TITLE)
    beads = first.checkout / ".beads"
    tracked = [n for n in ("config.yaml", "metadata.json", ".gitignore") if (beads / n).is_file()]
    assert "metadata.json" in tracked
    config_yaml = beads / "config.yaml"  # bd leaves its one-call remote hint as a comment
    config_yaml.write_text(
        "".join(
            line for line in config_yaml.read_text().splitlines(keepends=True)
            if not line.startswith("# sync.remote")
        )
    )  # fmt: skip
    for name in tracked:
        git("add", "-f", f".beads/{name}", cwd=first.checkout)
    git("commit", "-q", "-m", "chore: track beads scaffolding", cwd=first.checkout)
    git("push", "-q", "origin", "main", cwd=first.checkout)
    code, out = _invoke("hive", "sync", "remotes", "widget", "--push")
    assert code == 0, out
    world.leave(first)

    # Second host: a plain clone has .beads/ but no database on its (fresh) shared server.
    second = world.host("second")
    git("pull", "-q", "origin", "main", cwd=second.checkout)
    assert (second.checkout / ".beads" / "metadata.json").is_file()
    assert not store_locator.has_direct_server_database(
        store_locator.server_database(second.checkout, "widget")
    )
    ready = hive_ready.scan(
        second.cfg,
        ("github", "acme", "widget"),
        {**second.entry, "kind": "personal"},
        second.checkout,
    )
    assert any(c.label == "internal store" and c.required and c.state == "missing" for c in ready)

    probed = []
    world.monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda _c: probed.append(1))
    onboard._act_bd_init(_furnished(second))

    assert probed == []
    assert store_locator.has_direct_server_database(
        store_locator.server_database(second.checkout, "widget")
    )
    assert _titles(second.checkout, None) >= {PRIVATE_TITLE}
    _bd("show", private, cwd=second.checkout)
    assert git("status", "--porcelain", cwd=second.checkout).stdout.strip() == "", git(
        "diff", cwd=second.checkout
    ).stdout
    # The code origin's Dolt ref was never fetched into the second checkout.
    refs = git("for-each-ref", "refs/dolt", cwd=second.checkout).stdout.strip()
    assert refs == ""
    ready = hive_ready.scan(
        second.cfg,
        ("github", "acme", "widget"),
        {**second.entry, "kind": "personal"},
        second.checkout,
    )
    assert not any(c.label == "internal store" for c in ready)
