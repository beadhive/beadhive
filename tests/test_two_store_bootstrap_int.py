"""Integration: a fresh second host bootstraps a managed private hive from its internal remote,
while an ordinary clone still bootstraps the public corpus from origin (bh-lniwf.4).

Real `bd`, real Git, a `file://` Dolt remote standing in for the HQ-declared object store — no
network, no live store, no credentials. The code origin's `refs/dolt/data` carries a PUBLIC
corpus; the internal remote carries a PRIVATE one. Managed bootstrap (an HQ entry declaring
`stores.internal.remote`) must land the private corpus without ever probing origin; an
unmanaged bootstrap of the same clone must see only the public corpus.

Marked `integration` + `dolt_server` (the managed clone lands on an isolated shared server);
self-skips without `bd` on PATH.
"""

from __future__ import annotations

import json

import pytest

from beadhive import onboard, registry
from harness.beads import (
    add_file_remote,
    bd,
    bd_json,
    create,
    init_embedded,
    push,
    skip_if_no_bd,
)
from harness.world import free_port, git, reap_dolt_server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server, skip_if_no_bd]

_TIMEOUT = 120


@pytest.fixture
def isolated_shared_server(tmp_path, monkeypatch):
    server_dir = tmp_path / "shared-server"
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server_dir))
    monkeypatch.setenv("BEADS_DOLT_SERVER_PORT", str(free_port()))
    monkeypatch.delenv("BEADS_DIR", raising=False)
    # bh-dhl6's push-access probe would ask GitHub; this test never touches the network.
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: False)
    yield
    reap_dolt_server(server_dir)


def _repo(path, *, remote=None):
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git("config", "user.email", "t@t.dev", cwd=path)
    git("config", "user.name", "T", cwd=path)
    if remote is not None:
        git("remote", "add", "origin", str(remote), cwd=path)
    (path / "README.md").write_text("hi\n")
    git("add", ".", cwd=path)
    git("commit", "-q", "-m", "init", cwd=path)
    if remote is not None:
        git("push", "-q", "-u", "origin", "main", cwd=path)
    return path


def _ctx(target, cfg=None) -> onboard.Ctx:
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(target),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(target),
        prefix="widget",
        furnish=False,
        cfg=cfg,
    )
    ctx._derived = True
    return ctx


def _titles(cwd) -> set[str]:
    rows = bd_json("list", "--all", cwd=cwd) or []
    return {row["title"] for row in rows}


def test_managed_bootstrap_selects_private_data_and_never_reads_origin(
    tmp_path, isolated_shared_server, monkeypatch
):
    # The code origin, whose refs/dolt/data carries the PUBLIC corpus (embedded, pushed).
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=origin)
    publisher = _repo(tmp_path / "publisher", remote=origin)
    init_embedded(publisher, "widget")
    create(publisher, "public issue")
    bd("dolt", "push", cwd=publisher, capture=True, timeout=_TIMEOUT)
    assert git("ls-remote", "origin", "refs/dolt/data", cwd=publisher).stdout.strip()

    # The HQ-declared internal remote, carrying the PRIVATE corpus.
    private_url = f"file://{tmp_path / 'private-remote'}"
    (tmp_path / "private-remote").mkdir()
    author = _repo(tmp_path / "author")
    init_embedded(author, "widget")
    create(author, "private plan")
    add_file_remote(author, tmp_path / "private-remote", name="private")
    push(author, "private")

    # Managed second host: the HQ entry declares the internal store's remote descriptor.
    workspace = tmp_path / "ws"
    managed_host = workspace / "github" / "acme" / "widget"
    managed_host.parent.mkdir(parents=True)
    git("clone", "-q", str(origin), str(managed_host))
    monkeypatch.setenv("GIT_WORKSPACE", str(workspace))
    entry = {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "widget",
        "dolt_remote": {"name": "widget-private", "url": private_url},
        "stores": {"internal": {"remote": "widget-private"}},
    }

    def _no_origin_probe(_ctx):
        raise AssertionError("managed bootstrap probed the code origin's refs/dolt/data")

    original_probe = onboard._origin_has_dolt_data
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", _no_origin_probe)
    onboard._act_bd_init(_ctx(managed_host, cfg={"managed_repos": [entry]}))

    assert _titles(managed_host) == {"private plan"}
    metadata = json.loads((managed_host / ".beads" / "metadata.json").read_text())
    assert metadata.get("dolt_mode") == "server"

    monkeypatch.setattr(onboard, "_origin_has_dolt_data", original_probe)
    # Ordinary (unmanaged) host, same origin: today's auto-bootstrap sees the public corpus only
    # (after the managed clone above started the isolated shared server it reuses).
    public_host = tmp_path / "public-host"
    git("clone", "-q", str(origin), str(public_host))
    onboard._act_bd_init(_ctx(public_host))
    assert _titles(public_host) == {"public issue"}
