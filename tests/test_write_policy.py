"""The off-primary write policy: fenced, forward and open (trust-profiles ADR section 4, bh-sxhnq).

Fakes only: no real hive, no Dolt server, no network. The clock is injected wherever the
readiness window is read.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import typer

from beadhive import (
    bd,
    engine,
    fence_data,
    frame_eligibility,
    guard,
    hive_forward,
    hive_forward_cli,
    hq_why,
    hq_why_layer_write_policy,
    registry,
    trust_profiles,
    write_policy,
    write_policy_publish,
)
from beadhive.modules.config import contracts
from beadhive.writer_adopt import WriterRow

PREFIX = "wp"
ME = "frame-me"
WRITER = "frame-w"
DAY = 86400.0


def profile(name: str):
    return trust_profiles.resolve(name)


def policy(name: str, *, profile_name: str = "open", resolved: str | None = None):
    return write_policy.EffectivePolicy(
        name=name, profile=profile_name, resolved=resolved or name, hive=PREFIX
    )


@pytest.fixture(autouse=True)
def _fresh_process():
    write_policy.reset_warned()
    fence_data.reset_probe_cache()
    yield
    write_policy.reset_warned()
    fence_data.reset_probe_cache()


# ---- resolution ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [("open", "open"), ("trusted", "forward"), ("verified", "forward"), ("attested", "forward")],
)
def test_the_profile_supplies_the_default(name, expected):
    resolved = write_policy.resolve(profile(name), hive=PREFIX)
    assert (resolved.name, resolved.source) == (expected, "profile")


@pytest.mark.parametrize("name", ["trusted", "verified", "attested"])
def test_forward_without_a_configured_forwarder_is_fenced(name):
    resolved = write_policy.resolve(profile(name), forwarding_configured=False)
    assert (resolved.name, resolved.resolved) == ("fenced", "forward")
    assert "no forwarder configured" in resolved.describe()


def test_open_ignores_a_configured_forwarder():
    resolved = write_policy.resolve(profile("open"), forwarding_configured=True)
    assert resolved.open and not resolved.forwards


def test_the_fleet_default_tightens_and_only_trusted_authority_loosens():
    assert write_policy.resolve(profile("open"), fleet_default="fenced").name == "fenced"
    assert write_policy.resolve(profile("verified"), fleet_default="fenced").name == "fenced"
    refused = write_policy.resolve(profile("verified"), fleet_default="open")
    assert refused.name == "forward"
    assert refused.refusals and "verified trust profile" in refused.refusals[0].message
    honoured = write_policy.resolve(profile("trusted"), fleet_default="open")
    assert honoured.name == "open"
    assert honoured.source == "hive.write_policy (fleet.yaml)"


def test_the_per_hive_override_replaces_the_fleet_default():
    resolved = write_policy.resolve(
        profile("open"), fleet_default="fenced", hive_override="open", hive=PREFIX
    )
    assert resolved.name == "open"  # the hive's own value, not a loosening of the fleet's
    assert write_policy.resolve(profile("open"), hive_override="forward").name == "forward"


def test_the_host_override_may_only_tighten_the_fleet_layer():
    tightened = write_policy.resolve(profile("open"), host_override="fenced")
    assert (tightened.name, tightened.source) == ("fenced", "hive.write_policy (host.yaml)")
    refused = write_policy.resolve(
        profile("trusted"), fleet_default="forward", host_override="open"
    )
    assert refused.name == "forward" and refused.refusals


def test_an_invalid_layer_is_refused_not_applied():
    resolved = write_policy.resolve(profile("open"), fleet_default="sideways")
    assert resolved.name == "open" and "must be one of" in resolved.refusals[0].message


def test_the_keys_validate_at_fleet_default_and_per_hive_override():
    assert contracts.HiveConfig(write_policy="open").write_policy == "open"
    with pytest.raises(ValueError):
        contracts.HiveConfig(write_policy="sideways")
    entry = contracts.ManagedRepoEntry(hive={"write_policy": "fenced", "readiness_warning": "off"})
    assert entry.hive.write_policy == "fenced" and entry.hive.readiness_warning is False
    with pytest.raises(ValueError):
        contracts.ManagedRepoEntry(hive={"write_policy": "sideways"})
    assert contracts.HiveConfig().readiness_warning is True
    assert contracts.HiveConfig().readiness_window == "P7D"
    with pytest.raises(ValueError):
        contracts.HiveConfig(readiness_window="a week")


def test_layers_read_the_fleet_document_only_when_the_host_sets_the_key(monkeypatch):
    reads = []
    monkeypatch.setattr(
        write_policy,
        "_fleet_settings",
        lambda: reads.append(1) or {"hive": {"write_policy": "fenced"}},
    )
    entry = {"prefix": PREFIX, "hive": {"write_policy": "open"}}
    merged = {"hive": {"write_policy": "forward"}}
    assert write_policy.layers(entry, merged, host={}) == {
        "fleet_default": "forward",
        "hive_override": "open",
        "host_override": None,
    }
    assert reads == []
    host = {"hive": {"write_policy": "forward"}}
    assert write_policy.layers(entry, merged, host=host)["fleet_default"] == "fenced"
    assert reads == [1]


# ---- open never forwards --------------------------------------------------------------------


def _hive(tmp_path) -> Path:
    hive = tmp_path / "hive"
    hive.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(hive)], check=True)
    return hive


def _point(hive: Path) -> None:
    endpoint = {"host": "p.example", "port": 3307, "user": "fwd-me"}
    hive_forward.point(
        hive,
        hive_forward.ForwardTarget("p", 3, endpoint),
        prefix=PREFIX,
        self_frame=ME,
        endpoints={"p": endpoint},
    )


@pytest.mark.parametrize(
    ("name", "forwards"), [("open", False), ("fenced", False), ("forward", True)]
)
def test_the_forward_marker_decides_only_under_forward(tmp_path, monkeypatch, name, forwards):
    hive = _hive(tmp_path)
    _point(hive)
    monkeypatch.setattr(hive_forward, "ensure_current", hive_forward.read_marker)
    monkeypatch.setattr(hive_forward, "bd_env", lambda _d, base=None: {"FORWARDED": "1"})
    monkeypatch.setattr(write_policy, "for_dir", lambda *_a, **_k: policy(name))
    marker = guard.forward_marker(cfg={}, hive_dir=hive)
    assert (marker is not None) is forwards
    env, refusal, forwarded = engine._forwarded(hive, {"X": "1"})
    assert refusal is None
    assert forwarded is forwards
    if not forwards:
        assert env == {"X": "1"}  # bd runs against this replica's own data


def test_open_ignores_host_forward_endpoints_too():
    cfg = {"host": {"forward": {"enabled": True, "endpoints": {"p": {"host": "p"}}}}}
    assert write_policy.forwarding_configured(cfg)
    resolved = write_policy.resolve(profile("open"), forwarding_configured=True)
    assert resolved.name == "open"


def test_forward_point_refuses_under_open_naming_the_profile(tmp_path, monkeypatch, capsys):
    hive = _hive(tmp_path)
    monkeypatch.setattr(write_policy, "trust_profile", lambda *_a, **_k: profile("open"))
    monkeypatch.setattr(write_policy, "_host_settings", dict)
    cfg = {"host": {"forward": {"enabled": True, "endpoints": {}}}}
    with pytest.raises(typer.Exit):
        hive_forward_cli.run("point", cfg=cfg, entry={"prefix": PREFIX}, hive_dir=hive)
    err = capsys.readouterr().err
    assert "open trust profile" in err and "open never forwards" in err
    assert "hive forward point" in err
    assert hive_forward.read_marker(hive) is None


def test_forward_point_refusal_by_policy():
    assert write_policy.forward_point_refusal(policy("forward", profile_name="trusted")) == ""
    unconfigured = policy("fenced", profile_name="trusted", resolved="forward")
    assert write_policy.forward_point_refusal(unconfigured) == ""  # pointing configures it
    assert "only the writer frame" in write_policy.forward_point_refusal(
        policy("fenced", profile_name="trusted")
    )


# ---- the allowed operations -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "kind"),
    [
        (["comments", "add", "wp-1", "hi"], "comment"),
        (["comment", "wp-1", "hi"], "comment"),
        (["label", "add", "wp-1", "x"], "label"),
        (["note", "wp-1", "n"], "note"),
        (["update", "wp-1", "--status", "blocked"], "status"),
        (["update", "wp-1", "--append-notes", "n"], "note"),
        (["--actor", "dev/x", "update", "wp-1", "--claim", "--json"], "claim"),
        (["dep", "add", "wp-1", "wp-2"], "dependency"),
        (["close", "wp-1", "--reason", "done"], "close"),
        (["reopen", "wp-1"], "status"),
        (["create", "t"], "create"),
        (["q", "t"], "create"),
        (["create", "t", "--parent", "wp-1"], "child-create"),
        (["create", "t", "--parent=wp-1"], "child-create"),
        (["create", "t", "--id", "wp-1.3"], "child-create"),
        (["update", "wp-1", "--title", "x"], "other"),
        (["delete", "wp-1"], "other"),
    ],
)
def test_classify(args, kind):
    assert write_policy.classify(args).kind == kind


WHERE = write_policy.OffPrimary(PREFIX, WRITER, ME, True)


def test_open_refuses_child_create_citing_4796():
    refusal = write_policy.open_refusal(["create", "t", "--parent", "wp-1"], WHERE)
    assert "gastownhall/beads#4796" in refusal and "child create" in refusal
    assert f"hq why {PREFIX}" in refusal


def test_open_refuses_top_level_create_pointing_at_the_decision():
    refusal = write_policy.open_refusal(["create", "t"], WHERE)
    assert "bh-xwy5x" in refusal and "top-level create" in refusal


def test_open_refuses_operations_outside_the_list():
    refusal = write_policy.open_refusal(["update", "wp-1", "--title", "x"], WHERE)
    assert "bd update --title" in refusal and "close" in refusal
    assert write_policy.open_refusal(["update", "wp-1", "--claim"], WHERE) == ""


# ---- an open write on a non-primary replica -------------------------------------------------


@pytest.fixture
def replica(tmp_path, monkeypatch):
    """A non-primary replica under open: bd records its calls and never runs."""
    hive = _hive(tmp_path)
    calls, ensured = [], []
    monkeypatch.setattr(
        bd,
        "_run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    monkeypatch.setattr(
        fence_data,
        "ensure_replica_ident",
        lambda d, **k: ensured.append((d, k.get("role"))) or None,
    )
    open_write = write_policy.OpenWrite(policy("open"), WHERE, {"prefix": PREFIX})
    monkeypatch.setattr(write_policy, "open_write", lambda *_a, **_k: open_write)
    monkeypatch.setattr(write_policy, "_safe_cfg", dict)
    return hive, calls, ensured


def test_an_allowed_write_lands_locally_with_the_branch_role(replica, capsys):
    hive, calls, ensured = replica
    eng = engine.BdEngine()
    assert eng.invoke(["comments", "add", "wp-1", "hi"], cwd=hive).returncode == 0
    assert eng.invoke(["update", "wp-1", "--claim"], cwd=hive).returncode == 0
    assert ensured == [(hive, "branch"), (hive, "branch")]
    assert len(calls) == 2 and all("-C" in cmd for cmd in calls)
    err = capsys.readouterr().err
    assert err.count("⚠ wp: write policy open") == 1  # exactly one warning per command


@pytest.mark.parametrize(
    ("args", "cited"),
    [(["create", "t", "--parent", "wp-1"], "#4796"), (["create", "t"], "bh-xwy5x")],
)
def test_create_is_refused_before_bd_runs(replica, args, cited):
    hive, calls, _ = replica
    result = engine.BdEngine().invoke(args, cwd=hive, capture=True)
    assert result.returncode == 1 and cited in result.stderr
    assert calls == []


def test_publishing_goes_only_to_the_frame_ref(replica, monkeypatch):
    hive, calls, _ = replica
    published = []
    monkeypatch.setattr(
        write_policy_publish,
        "publish",
        lambda d, frame, **_k: published.append((d, frame)) or f"refs/dolt/frame/{frame}",
    )
    result = engine.BdEngine().push_state(hive, message="bh: sync")
    assert result.returncode == 0
    assert "refs/dolt/frame/frame-me" in result.stdout and "never main" in result.stdout
    assert published == [(hive, ME)]
    assert [cmd[cmd.index("dolt") :][:2] for cmd in calls] == [["dolt", "commit"]]
    assert not any("push" in cmd for cmd in calls)


class FakeEngine:
    """A store engine: its remotes, the statements it ran and the remote it pushed to."""

    remote = "origin"

    def __init__(self, remotes):
        self.remotes = remotes
        self.executed: list[str] = []
        self.pushed: list[str] = []

    def query(self, sql):
        assert sql.startswith("SELECT name, url, params FROM dolt_remotes")
        return self.remotes

    def execute(self, statements):
        self.executed += list(statements)

    def push(self):
        self.pushed.append(self.remote)
        return self._landed

    _landed = True


class FakeNode:
    def __init__(self, engine_):
        self.engine = engine_


def test_publish_configures_a_private_ref_remote_and_never_pushes_origin(tmp_path):
    fake = FakeEngine([{"name": "origin", "url": "git+file:///srv/hive.git", "params": "{}"}])
    ref = write_policy_publish.publish(tmp_path, ME, node=FakeNode(fake))
    assert ref == "refs/dolt/frame/frame-me"
    assert fake.executed == [
        "CALL DOLT_REMOTE('add', '--ref', 'refs/dolt/frame/frame-me', 'frame-frame-me', "
        "'git+file:///srv/hive.git/')"
    ]
    assert fake.pushed == ["frame-frame-me"]  # pushed through the private remote only
    assert fake.remote == "origin"  # the hive remote itself is never repointed


def test_publish_reuses_a_current_remote_and_reports_a_non_fast_forward(tmp_path):
    params = json.dumps({"git_ref": "refs/dolt/frame/frame-me"})
    fake = FakeEngine(
        [
            {"name": "origin", "url": "file:///h", "params": "{}"},
            {"name": "frame-frame-me", "url": "file:///h/", "params": params},
        ]
    )
    FakeEngine._landed = False
    try:
        with pytest.raises(write_policy_publish.PublishError, match="not a fast-forward"):
            write_policy_publish.publish(tmp_path, ME, node=FakeNode(fake))
    finally:
        FakeEngine._landed = True
    assert fake.executed == []


def test_publish_refuses_a_frame_id_that_cannot_name_a_ref(tmp_path):
    with pytest.raises(write_policy_publish.PublishError):
        write_policy_publish.publish(tmp_path, "../main", node=FakeNode(FakeEngine([])))


# ---- the guard ------------------------------------------------------------------------------


@pytest.fixture
def cut_over(monkeypatch):
    monkeypatch.setattr(guard, "forward_marker", lambda *_a, **_k: None)
    monkeypatch.setattr(guard, "writer_state", lambda *_a, **_k: (PREFIX, ME, WriterRow(WRITER, 4)))
    monkeypatch.setattr(frame_eligibility, "require_intake", lambda *_a, **_k: None)
    monkeypatch.setattr(guard, "_entry_or_none", lambda *_a, **_k: {"prefix": PREFIX})
    monkeypatch.setattr(write_policy, "_safe_cfg", dict)

    def set_policy(name):
        monkeypatch.setattr(write_policy, "for_hive", lambda *_a, **_k: policy(name))

    return set_policy


@pytest.mark.parametrize("capability", ["author", "claim"])
def test_open_lets_a_non_writer_author_and_claim_with_one_warning(cut_over, capsys, capability):
    cut_over("open")
    guard.guard_capability(capability, PREFIX, cfg={})
    guard.guard_capability(capability, PREFIX, cfg={})
    err = capsys.readouterr().err
    assert err.count("⚠ wp: write policy open") == 1
    assert f"writer frame {WRITER}" in err and "trusted trust profile and above" in err
    assert f"forwarded to {WRITER}" in err


@pytest.mark.parametrize("capability", ["publish", "execute"])
def test_open_keeps_publish_and_execute_with_the_writer(cut_over, capability):
    cut_over("open")
    with pytest.raises(typer.Exit):
        guard.guard_capability(capability, PREFIX, cfg={})


@pytest.mark.parametrize("name", ["fenced", "forward"])
def test_fenced_and_forward_refuse_a_non_writer_as_today(cut_over, name):
    cut_over(name)
    with pytest.raises(typer.Exit):
        guard.guard_capability("claim", PREFIX, cfg={})


def test_the_passthrough_lets_open_writes_reach_the_engine(monkeypatch):
    monkeypatch.setattr(guard, "forward_marker", lambda *_a, **_k: None)
    monkeypatch.setattr(guard, "writer_state", lambda *_a, **_k: (PREFIX, ME, WriterRow(WRITER, 4)))
    monkeypatch.setattr(write_policy, "for_dir", lambda *_a, **_k: policy("open"))
    cfg = {"managed_repos": []}
    assert guard.bd_write_refusal(["update", "wp-1", "--claim"], "/x", cfg=cfg) == ""
    assert guard.bd_write_refusal(["dolt", "push"], "/x", cfg=cfg)  # main stays the writer's
    monkeypatch.setattr(write_policy, "for_dir", lambda *_a, **_k: policy("fenced"))
    assert guard.bd_write_refusal(["update", "wp-1", "--claim"], "/x", cfg=cfg)


# ---- the readiness warning ------------------------------------------------------------------


def _write(entry=None):
    return write_policy.OpenWrite(policy("open"), WHERE, entry or {"prefix": PREFIX})


def _events(tmp_path=None) -> list[dict]:
    path = write_policy._events_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_readiness_warns_and_counts_once_per_command():
    printed = []
    assert write_policy.readiness(_write(), cfg={}, now=lambda: 10.0, echo=printed.append)
    assert not write_policy.readiness(_write(), cfg={}, now=lambda: 11.0, echo=printed.append)
    assert len(printed) == 1 and PREFIX in printed[0] and WRITER in printed[0]
    assert [(e["hive"], e["writer"], e["frame"], e["warned"]) for e in _events()] == [
        (PREFIX, WRITER, ME, True)
    ]


@pytest.mark.parametrize(
    ("cfg", "entry"),
    [
        ({"hive": {"readiness_warning": False}}, None),
        ({}, {"prefix": PREFIX, "hive": {"readiness_warning": "off"}}),
    ],
)
def test_the_warning_is_suppressible_but_still_counted(cfg, entry):
    printed = []
    write_policy.readiness(_write(entry), cfg=cfg, now=lambda: 1.0, echo=printed.append)
    assert printed == []
    assert [e["warned"] for e in _events()] == [False]


def test_counts_cover_the_configured_window_with_a_mocked_clock(tmp_path):
    path = tmp_path / "events.jsonl"
    for at, hive in ((0.0, "a"), (6 * DAY, "a"), (8 * DAY, "a"), (8 * DAY, "b")):
        write_policy.record_event({"at": at, "hive": hive}, path=path)
    now = lambda: 8 * DAY + 1  # noqa: E731
    assert write_policy.readiness_counts(7 * 86400, now=now, path=path) == {"a": 2, "b": 1}
    assert write_policy.readiness_counts(86400, now=now, path=path) == {"a": 1, "b": 1}
    assert write_policy.readiness_window_s({}) == 7 * 86400
    assert write_policy.readiness_window_s({"hive": {"readiness_window": "PT12H"}}) == 43200


def test_an_oversized_event_log_drops_events_outside_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(write_policy, "EVENTS_COMPACT_BYTES", 50)
    path = tmp_path / "events.jsonl"
    for at in (0.0, 1.0, 2.0, 10 * DAY):
        write_policy.record_event({"at": at, "hive": "a"}, window_s=86400, path=path)
    assert [e["at"] for e in write_policy._read_events(path)] == [10 * DAY]


# ---- bh doctor ------------------------------------------------------------------------------


def _doctor(cfg, name, tmp_path, *, now=lambda: 8 * DAY):
    return write_policy.doctor_data(
        cfg,
        profile=profile(name),
        host={},
        now=now,
        events=tmp_path / "events.jsonl",
        hive_dir=lambda _e: None,
    )


def test_doctor_reports_the_policy_and_readiness_count_per_hive(tmp_path):
    path = tmp_path / "events.jsonl"
    for at in (0.0, 7.5 * DAY, 7.9 * DAY):
        write_policy.record_event({"at": at, "hive": "aa"}, path=path)
    cfg = {
        "hive": {"readiness_window": "P1D"},
        "managed_repos": [
            {"prefix": "aa"},
            {"prefix": "bb", "hive": {"write_policy": "fenced"}},
            {"prefix": "hq", "kind": "hq"},
        ],
    }
    data = _doctor(cfg, "open", tmp_path)
    assert data["window"] == "1d" and data["level"] == "ok"
    rows = {item["hive"]: item for item in data["hives"]}
    assert set(rows) == {"aa", "bb"}
    assert (rows["aa"]["policy"], rows["aa"]["readiness_warnings"]) == ("open", 2)
    assert rows["bb"]["policy"] == "fenced"
    lines = write_policy.doctor_lines(data)
    assert any("aa: open" in line and "2 readiness warning(s) in 1d" in line for line in lines)


def test_doctor_flags_a_forwarder_login_that_is_not_per_frame_and_pinned(tmp_path):
    cfg = {
        "managed_repos": [{"prefix": "aa"}],
        "host": {
            "forward": {
                "enabled": True,
                "endpoints": {
                    "p1": {"host": "p1", "user": "root", "tls_mode": "required"},
                    "p2": {"host": "p2", "user": "fwd-me", "tls_mode": "disabled"},
                    "p3": {
                        "host": "p3",
                        "user": "fwd-me",
                        "tls_mode": "required",
                        "ca_file": "/ca.pem",
                        "server_name": "p3",
                    },
                },
            }
        },
    }
    data = _doctor(cfg, "trusted", tmp_path)
    assert data["level"] == "warn"
    problems = data["forwarder_logins"]
    assert any("endpoints.p1" in p and "shared, root or operator" in p for p in problems)
    assert any("endpoints.p2" in p and "TLS is not required" in p for p in problems)
    assert not any("endpoints.p3" in p for p in problems)
    assert _doctor(cfg, "open", tmp_path)["forwarder_logins"] == []  # open never forwards


def test_doctor_payload_carries_the_section(monkeypatch):
    from beadhive import doctor

    monkeypatch.setattr(write_policy, "doctor_data", lambda cfg: {"level": "ok", "hives": []})
    assert doctor._data_write_policy({})["level"] == "ok"
    monkeypatch.setattr(
        write_policy, "doctor_data", lambda cfg: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert "boom" in doctor._data_write_policy({})["error"]


# ---- bh hq why ------------------------------------------------------------------------------


def _ctx(capability="author"):
    ctx = hq_why.WhyContext(PREFIX, capability, cfg={})
    ctx.memo.update(entry={"prefix": PREFIX}, hive_dir=Path("/nowhere"))
    return ctx


def test_why_decides_an_open_replica_write(monkeypatch):
    monkeypatch.setattr(write_policy, "for_entry", lambda *_a, **_k: policy("open"))
    monkeypatch.setattr(write_policy, "off_primary", lambda **_k: WHERE)
    verdict = hq_why_layer_write_policy.check_write_policy(_ctx())
    assert (verdict.status, verdict.code) == (hq_why.DECIDED, "open_replica_write")
    assert "refs/dolt/frame/frame-me" in verdict.summary and "#4796" in verdict.summary
    publish = hq_why_layer_write_policy.check_write_policy(_ctx("publish"))
    assert publish.status == hq_why.OK


def test_why_refuses_a_fenced_override_naming_the_fix(monkeypatch):
    monkeypatch.setattr(
        write_policy, "for_entry", lambda *_a, **_k: policy("fenced", profile_name="trusted")
    )
    monkeypatch.setattr(write_policy, "off_primary", lambda **_k: WHERE)
    verdict = hq_why_layer_write_policy.check_write_policy(_ctx())
    assert (verdict.status, verdict.code) == (hq_why.REFUSED, "write_policy_fenced")
    assert verdict.command.endswith("config unset hive.write_policy")


def test_why_skips_under_forward(monkeypatch):
    monkeypatch.setattr(
        write_policy, "for_entry", lambda *_a, **_k: policy("forward", profile_name="verified")
    )
    assert hq_why_layer_write_policy.check_write_policy(_ctx()).status == hq_why.SKIPPED


# ---- the replica's guard role ---------------------------------------------------------------


class IdentNode:
    def __init__(self, ident, writer=WRITER):
        self._ident = ident
        self._writer = WriterRow(writer, 4)
        self.provisioned = []
        self.engine = type("E", (), {"store": Path("/store")})()

    def ident(self):
        return self._ident

    def writer(self):
        return self._writer

    def provision_ident(self, frame, role="replica"):
        self.provisioned.append((frame, role))
        self._ident = (frame, role)


@pytest.fixture
def ident_node(monkeypatch):
    def install(node):
        monkeypatch.setattr(fence_data, "resolve_cut_over", lambda *_a, **_k: node)
        monkeypatch.setattr(fence_data, "_this_frame", lambda: ME)
        return node

    return install


def test_open_switches_the_replica_to_the_branch_role_and_back(tmp_path, ident_node):
    node = ident_node(IdentNode((ME, "replica")))
    assert fence_data.ensure_replica_ident(tmp_path, role="branch") is None
    assert fence_data.ensure_replica_ident(tmp_path, role="replica") is None
    assert node.provisioned == [(ME, "branch"), (ME, "replica")]


def test_the_writers_own_identity_is_never_switched(tmp_path, ident_node):
    node = ident_node(IdentNode((ME, "replica"), writer=ME))
    assert fence_data.ensure_replica_ident(tmp_path, role="branch") is None
    assert node.provisioned == []


def test_a_missing_identity_is_provisioned_with_the_asked_role(tmp_path, ident_node):
    node = ident_node(IdentNode(None))
    assert fence_data.ensure_replica_ident(tmp_path, role="branch") is None
    assert node.provisioned == [(ME, "branch")]


def test_registry_entry_resolution_feeds_for_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _d: {"prefix": PREFIX, "hive": {}})
    monkeypatch.setattr(write_policy, "trust_profile", lambda *_a, **_k: profile("open"))
    monkeypatch.setattr(write_policy, "_host_settings", dict)
    resolved = write_policy.for_dir(tmp_path, cfg={})
    assert (resolved.name, resolved.hive) == ("open", PREFIX)
    assert write_policy.ignores_forwarding(tmp_path, cfg={})


def test_under_forward_author_and_claim_route_through_the_forwarder(tmp_path, monkeypatch):
    """A forwarded checkout is decided by its marker (the primary's guard decides the write);
    the local writer fence, which names another frame, is never asked."""
    hive = _hive(tmp_path)
    _point(hive)
    monkeypatch.setattr(hive_forward, "ensure_current", hive_forward.read_marker)
    monkeypatch.setattr(registry, "hive_dir_for", lambda *_a, **_k: hive)
    monkeypatch.setattr(write_policy, "for_dir", lambda *_a, **_k: policy("forward"))
    monkeypatch.setattr(
        guard, "writer_state", lambda *_a, **_k: pytest.fail("the marker decides, not the fence")
    )
    for capability in ("author", "claim"):
        guard.guard_capability(capability, PREFIX, cfg={"managed_repos": []})
