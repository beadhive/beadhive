"""Where a hive's Dolt git-transport bare repo lives — :mod:`beadhive.transport_locator`.

Moved from ``tests/test_host_fence.py`` with the module (bh-vwbxy); assertions unchanged.
"""

from __future__ import annotations

import subprocess

from beadhive import transport_locator


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


# ---- where refs/dolt/data actually lives ---------------------------------------------


def test_transport_repos_finds_dolts_hidden_staging_repo(tmp_path):
    """The measured topology (bh-ytbb.7): the hive checkout has NO local refs/dolt/data —
    bd/Dolt stages the push through a hidden bare repo under .beads/embeddeddolt/…, and the
    ADR's push formulation has to run from there."""
    hive = tmp_path / "hive"
    staged = hive / ".beads/embeddeddolt/bh/.dolt/git-remote-cache/deadbeef/repo.git"
    staged.mkdir(parents=True)
    legacy = hive / ".beads/embeddeddolt/beads/.dolt/git-remote-cache/cafe/repo.git"
    legacy.mkdir(parents=True)
    assert transport_locator.transport_repos(hive) == sorted([legacy, staged])


def test_transport_repos_is_empty_for_a_hive_with_no_embedded_dolt(tmp_path):
    hive = tmp_path / "nodb-hive"
    (hive / ".beads").mkdir(parents=True)
    assert transport_locator.transport_repos(hive) == []
    assert transport_locator.transport_repos(tmp_path / "not-a-hive") == []


# ---- the SAME transport, under bd's shared server (bh-areg.6) --------------------------


def _server_hive(tmp_path, monkeypatch, *, database="bh", make_repo=True):
    """A server-mode hive: `.beads/metadata.json` says server, and the database — with its
    transport repo — lives under the SERVER's data dir, outside the hive entirely. The layout
    below `<db>/` is byte-identical to embedded's; only the parent moves (bh-ukit.2)."""
    server = tmp_path / "shared-server"
    monkeypatch.setenv("BEADS_SHARED_SERVER_DIR", str(server))
    hive = tmp_path / "server-hive"
    (hive / ".beads").mkdir(parents=True)
    (hive / ".beads" / "metadata.json").write_text(
        f'{{"dolt_mode": "server", "dolt_database": "{database}"}}'
    )
    transport = server / "dolt" / database / ".dolt" / "git-remote-cache" / "abc123" / "repo.git"
    if make_repo:
        _git(["init", "--bare", "-q", str(transport)], tmp_path)
    return hive, transport


def test_transport_lookup_follows_the_database_out_to_the_server(tmp_path, monkeypatch):
    """The repo is not missing under a server — it moved. `transport_repos` globbing under
    `hive/.beads` is exactly why it read as absent (bh-u562.1 item 1)."""
    hive, transport = _server_hive(tmp_path, monkeypatch)

    lookup = transport_locator.transport_lookup(hive)

    assert lookup.state == transport_locator.FOUND
    assert lookup.repos == [transport]
    assert lookup.ok


def test_a_server_lookup_never_returns_another_hives_transport(tmp_path, monkeypatch):
    """The server's data dir is shared by EVERY hive on the host, unlike embedded's private
    `.beads/`. An unscoped glob there would hand back a neighbour's transport repo — and
    `prepush` would bake THIS hive's id into that hive's hook."""
    hive, mine = _server_hive(tmp_path, monkeypatch, database="mine")
    neighbour = tmp_path / "shared-server/dolt/theirs/.dolt/git-remote-cache/def456/repo.git"
    _git(["init", "--bare", "-q", str(neighbour)], tmp_path)

    lookup = transport_locator.transport_lookup(hive)

    assert lookup.repos == [mine]
    assert neighbour not in lookup.repos


# ---- "no transport" vs "not found yet" vs "on another machine" -------------------------


def test_a_hive_with_no_dolt_at_all_reports_none(tmp_path):
    hive = tmp_path / "nodb-hive"
    (hive / ".beads").mkdir(parents=True)
    assert transport_locator.transport_lookup(hive).state == transport_locator.NONE
    assert (
        transport_locator.transport_lookup(tmp_path / "not-a-hive").state == transport_locator.NONE
    )


def test_a_dolt_hive_that_has_never_pushed_reports_not_found(tmp_path, monkeypatch):
    """bd creates the transport repo lazily on the first `bd dolt push`. Benign — but it is a
    DIFFERENT answer from "this hive has no transport", and a caller must be able to say which."""
    hive, _ = _server_hive(tmp_path, monkeypatch, make_repo=False)
    lookup = transport_locator.transport_lookup(hive)
    assert lookup.state == transport_locator.NOT_FOUND
    assert lookup.ok  # benign: nothing to fence YET
    assert "lazily" in lookup.detail

    embedded = tmp_path / "embedded-hive"
    (embedded / ".beads" / "embeddeddolt" / "bh").mkdir(parents=True)
    assert transport_locator.transport_lookup(embedded).state == transport_locator.NOT_FOUND


def test_a_non_local_server_reports_unreachable_not_absent(tmp_path, monkeypatch):
    """(c)-remote: the transport repo exists, on the server's disk, on another machine. The one
    empty-lookup state that is NOT benign — reporting it as "not found yet" would read as a
    fence that simply hasn't been staged, rather than one this host can never install."""
    hive, _ = _server_hive(tmp_path, monkeypatch, make_repo=False)
    monkeypatch.setenv("BEADS_DOLT_SERVER_HOST", "dolt.example.invalid")

    lookup = transport_locator.transport_lookup(hive)

    assert lookup.state == transport_locator.UNREACHABLE
    assert not lookup.ok
    assert "dolt.example.invalid" in lookup.detail
    assert (
        transport_locator.transport_repos(hive) == []
    )  # and the list-only form still says nothing
