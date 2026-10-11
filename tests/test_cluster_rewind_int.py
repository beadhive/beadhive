"""The rewindable writer-fencing cluster fixture against real bd and Dolt (bh-i970d).

One composed world -- SqlHQ, bd-embedded ``a``, bd-server ``b``, Dolt CLI ``c``, fence and guard
installed -- is built once on this worker by ``rewound_world`` and rewound from its stopped-cluster
template between uses (spike bh-jk4mn, Mechanism C). The leak guard is on for every test here
(``cluster_guard``): each restore is fingerprinted against the bootstrap.

* a fencing test's whole footprint, a SIGKILLed server frame included, rewinds to an EMPTY diff;
* the guard catches each inventoried state class when it is deliberately leaked, and a leak the
  template cannot undo (a stray process) fails loudly and makes the pool rebuild;
* a port taken while the world was parked is rebound on restore;
* a plain :class:`Cluster` (``rewound_cluster``) is shared and restored the same way.

Measured rewind times are attached to the junit report (``record_property``) and printed.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys

import pytest

from harness import composed_fence as cf
from harness import write_guard as wg
from harness.cluster_rewind import (
    RewindLeak,
    cluster_key,
    diff,
    fingerprint,
    rewind_enabled,
    world_key,
)
from harness.writer_fencing import BD_EMBEDDED, BD_SERVER, CAS, DOLT_CLI

pytestmark = [
    pytest.mark.integration,
    pytest.mark.dolt_server,
    pytest.mark.cluster_guard,
    pytest.mark.skipif(
        shutil.which("bd") is None or shutil.which("dolt") is None, reason="bd/dolt not installed"
    ),
    pytest.mark.skipif(not rewind_enabled(), reason="BH_CLUSTER_REWIND=0: nothing to rewind"),
]

FRAMES = [("a", BD_EMBEDDED), ("b", BD_SERVER), ("c", DOLT_CLI)]
SMALL = [("a", BD_EMBEDDED), ("b", BD_SERVER)]


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _dirty(world: cf.World, *, kill: bool) -> None:
    """A representative fencing test's footprint (bh-jk4mn ``dirty``)."""
    cl, hq = world.cluster, world.hq
    a, b, c = cl["a"], cl["b"], cl["c"]
    cf.write(a, "rw-a").check()
    a.push().check()
    epoch = hq.place("b").epoch
    assert cf.adopt(cl, b, epoch).landed
    cf.write(b, "rw-b").check()
    b.push().check()
    c.sql("call dolt_branch('scratch')").check()
    c.sql("call dolt_tag('t-scratch')").check()
    c.sql("create table scratch_untracked (id int primary key)").check()
    wg.provision(c, role="branch")
    b.sql("create user 'u0'@'%' identified by 'x'").check()
    b.sql("set persist max_connections = 77").check()
    b.sql("set global net_read_timeout = 77").check()
    head = cl.remote.head()
    _git("--git-dir", str(cl.remote.path), "update-ref", "refs/bh/epoch", head)
    _git("--git-dir", str(cl.remote.path), "update-ref", "refs/bh/backup/fx-0/c", head)
    _git("-C", str(a.hive), "update-ref", "refs/bh/lease/fx", "HEAD")
    hq.renew("a")
    hq.publish_evidence("b")
    c.partition()
    hq.partition("c")
    a.hold(CAS)
    if kill:
        b.kill()


def _report(record_property, label: str, timings: dict) -> None:
    record_property(label, timings)
    print(f"{label}: {timings}")


def test_a_fencing_footprint_rewinds_to_the_bootstrap_fingerprint(
    rewound_world, cluster_pool, record_property
):
    key = world_key(FRAMES)
    world = rewound_world(FRAMES)
    boot = fingerprint(world)
    for kill in (False, True):
        _dirty(world, kill=kill)
        assert diff(boot, fingerprint(world)), "the footprint must be visible to the guard"
        cluster_pool.release(key)
        assert cluster_pool.acquire(key, guard=True) is world  # restored, not rebuilt
        assert diff(boot, fingerprint(world)) == []
        _report(record_property, f"rewind_kill_{kill}", cluster_pool.restores(key)[-1])
    # the restored world is a working world: the fence still lets the placed writer publish
    assert cf.write(world["a"], "after-rewind").ok
    assert world["a"].push().ok
    _report(record_property, "build_seconds", {"builds": cluster_pool.build_seconds})


#: One deliberate leak per inventoried state class (bh-jk4mn), and the fingerprint path the
#: guard must report it under.
LEAKS = {
    "remote refs": "/remote_refs/refs/bh/epoch",
    "ignored table rows": "/frames/c/local_tables/bh_local_ident",
    "untracked table": "/frames/c/local_tables/scratch_untracked",
    "branches": "/frames/c/branches",
    "tags": "/frames/c/tags",
    "server users": "/frames/b/users",
    "HQ users": "/hq/users",
    "globals": "/frames/b/globals",
    "persisted globals": "/frames/b/persisted",
    "HQ placement": "/hq/placement",
    "HQ liveness": "/hq/liveness/a",
    "plain bd files": "/frames/a/beads_files",
    "local git refs": "/frames/a/git_refs",
    "fixture controls": "/frames/c/partitioned",
    "armed holds": "/frames/a/holds",
    "HQ reachability": "/hq/cut",
    "processes": "/processes",
}


def test_the_guard_catches_every_leaked_state_class_and_rebuilds_on_a_real_leak(
    rewound_world, cluster_pool
):
    key = world_key(FRAMES)
    world = rewound_world(FRAMES)
    cl, hq = world.cluster, world.hq
    a, b, c = cl["a"], cl["b"], cl["c"]
    boot = fingerprint(world)

    _git("--git-dir", str(cl.remote.path), "update-ref", "refs/bh/epoch", cl.remote.head())
    wg.provision(c, role="branch")
    c.sql("create table scratch_untracked (id int primary key)").check()
    c.sql("call dolt_branch('scratch')").check()
    c.sql("call dolt_tag('t-scratch')").check()
    b.sql("create user 'leak'@'%' identified by 'x'").check()
    hq._exec(("root", ""), "CREATE USER 'leak'@'%' IDENTIFIED BY 'x'")
    b.sql("set global net_read_timeout = 77").check()
    b.sql("set persist max_connections = 77").check()
    hq.place("b")
    hq.renew("a")
    (a.hive / ".beads" / "leak.txt").write_text("leaked\n")
    _git("-C", str(a.hive), "update-ref", "refs/bh/lease/fx", "HEAD")
    c.partition()
    a.hold(CAS)
    hq.partition("c")
    stray = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"], cwd=a.dir / "home"
    )
    try:
        leftovers = diff(boot, fingerprint(world))
        missed = {
            cls: path
            for cls, path in LEAKS.items()
            if not any(line.startswith(path) for line in leftovers)
        }
        assert not missed, f"guard missed {missed}; it reported {leftovers}"

        # The template undoes everything on disk; a process outside its reach is a real leak:
        # the guard fails loudly and the pool throws the world away instead of reusing it.
        builds = cluster_pool.builds
        cluster_pool.release(key)
        with pytest.raises(RewindLeak) as caught:
            cluster_pool.acquire(key, guard=True)
        assert [line.split(":")[0] for line in caught.value.leftovers] == ["/processes"]
    finally:
        stray.kill()
        stray.wait(10)
    rebuilt = cluster_pool.acquire(key, guard=True)
    assert rebuilt is not world and cluster_pool.builds == builds + 1


def test_a_port_taken_while_parked_is_rebound_on_restore(rewound_world, cluster_pool):
    key = world_key(FRAMES)
    world = rewound_world(FRAMES)
    old = {"b": world["b"].port, "hq": world.hq.port}
    cluster_pool.release(key)
    squatters = []
    try:
        for port in old.values():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(1)
            squatters.append(sock)
        assert cluster_pool.acquire(key, guard=True) is world
        assert world["b"].port != old["b"] and world.hq.port != old["hq"]
        assert world["b"].bd("list", "--json").ok
        assert world.hq.placement().writer == "a"
    finally:
        for sock in squatters:
            sock.close()


def test_a_plain_cluster_is_shared_and_restored(rewound_cluster, cluster_pool, record_property):
    key = cluster_key(SMALL)
    cluster = rewound_cluster(SMALL)
    boot = fingerprint(cluster)
    a, b = cluster["a"], cluster["b"]
    a.create_issue("plain-a").check()
    a.push().check()
    b.pull().check()
    b.sql("create user 'u1'@'%' identified by 'x'").check()
    cluster.hq.place("b")
    b.kill()
    cluster_pool.release(key)
    assert cluster_pool.acquire(key, guard=True) is cluster
    assert diff(boot, fingerprint(cluster)) == []
    assert "plain-a" not in b.issue_titles()
    _report(record_property, "cluster_rewind", cluster_pool.restores(key)[-1])
