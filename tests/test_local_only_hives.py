"""Local-only hive registration (bh-sj2bf; docs/design/local-only-hives.md, bh-ndkos).

A hive whose bead database was never published carries ``managed_repos[].local_only``
``{owner_host, reason}``. Other frames skip it in ``hives-ready`` and stay eligible; only the
owner may lease or claim it. Hermetic: no gh, no bd, no remotes, no wall clock.
"""

from __future__ import annotations

import json

import pytest
import typer
from pydantic import ValidationError

from beadhive import config, guard, heartbeat_report, host, host_lease, registry
from beadhive import frame_eligibility as policy
from beadhive.modules.config.contracts import BeadhiveConfig, ManagedRepoEntry

OWNER = "6f1c0000-0000-4000-8000-000000000001"
OTHER = "6f1c0000-0000-4000-8000-000000000002"


def _local(prefix="tl", owner=OWNER, reason="push-denied", **extra):
    return {
        "provider": "github",
        "org": "acme",
        "repo": f"repo-{prefix}",
        "prefix": prefix,
        "kind": "prototype",
        "local_only": {"owner_host": owner, "reason": reason},
        **extra,
    }


def _plain(prefix="rp"):
    return {
        "provider": "github",
        "org": "acme",
        "repo": f"repo-{prefix}",
        "prefix": prefix,
        "kind": "prototype",
    }


# ---- R1: the field -------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["push-denied", "opt-in", "external", "migrated"])
def test_marker_validates_each_reason(reason):
    entry = ManagedRepoEntry.model_validate(_local(reason=reason))
    assert entry.local_only.owner_host == OWNER and entry.local_only.reason == reason


def test_absent_marker_is_replicated_and_unchanged():
    assert ManagedRepoEntry.model_validate(_plain()).local_only is None
    BeadhiveConfig(managed_repos=[_plain(), _local()])


@pytest.mark.parametrize(
    "marker",
    [
        {"owner_host": "", "reason": "opt-in"},
        {"owner_host": " padded ", "reason": "opt-in"},
        {"owner_host": OWNER, "reason": "replicated"},
        {"owner_host": OWNER},
        {"reason": "opt-in"},
        {"owner_host": OWNER, "reason": "opt-in", "enabled": False},
    ],
)
def test_marker_rejects_invalid_shapes(marker):
    with pytest.raises(ValidationError):
        ManagedRepoEntry.model_validate({**_plain(), "local_only": marker})


@pytest.mark.parametrize(
    "extra",
    [
        {"dolt_remote": {"url": "s3://acme-beads/beads/tl"}},
        {"stores": {"internal": {"remote": "tl-private"}}},
    ],
)
def test_marker_refused_beside_hq_remote_or_stores(extra):
    with pytest.raises(ValidationError, match="local_only cannot be combined"):
        ManagedRepoEntry.model_validate(_local(**extra))


def test_marker_is_not_register_owned():
    assert registry.LOCAL_ONLY_KEY not in registry.REGISTER_OWNED_KEYS


# ---- registry helpers ----------------------------------------------------------------------


def test_refusal_is_none_without_marker_or_for_the_owner():
    assert registry.local_only_refusal(_plain(), OTHER) is None
    assert registry.local_only_refusal(_local(), OWNER) is None


def test_refusal_names_owner_label_and_id(monkeypatch):
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio" if hid == OWNER else hid)
    text = registry.local_only_refusal(_local(), OTHER)
    assert text == (
        f"hive tl is local-only on host studio ({OWNER}); only that host may lease or claim "
        "its beads"
    )


def test_refusal_fails_closed_without_a_host_id(monkeypatch):
    monkeypatch.setattr(registry, "host_label", lambda hid: hid)
    assert registry.local_only_refusal(_local(), None)
    assert registry.local_only_refusal(_local(), "")
    assert registry.local_only_blocks(_local(), None)


def test_host_label_falls_back_to_the_id(monkeypatch):
    def boom():
        raise FileNotFoundError("no hq")

    monkeypatch.setattr(config, "hq_dir", boom)
    assert registry.host_label(OWNER) == OWNER


def test_marker_never_recorded_with_an_empty_owner():
    with pytest.raises(ValueError, match="never recorded empty"):
        registry.local_only_marker("  ", "opt-in")


FLOW = (
    "providers: [github]\n"
    "orgs:\n"
    "  acme: {code: ac, policy: personal}\n"
    "managed_repos:\n"
    '  - {"provider": "github", "org": "acme", "repo": "tool", "prefix": "tl", '
    '"kind": "prototype"}\n'
)


def _entry_json(org="acme", repo="tool"):
    entry = registry.find_entry(config.load(), "github", org, repo)
    return json.loads(json.dumps(entry))


def test_register_writes_the_marker_flow_style(world):
    registry.register(
        "github",
        "acme",
        "gadget",
        "gad",
        "prototype",
        local_only={"owner_host": OWNER, "reason": "push-denied"},
    )
    assert _entry_json(repo="gadget")["local_only"] == {
        "owner_host": OWNER,
        "reason": "push-denied",
    }
    line = next(x for x in config.config_path().read_text().splitlines() if "gadget" in x)
    assert f'"local_only": {{"owner_host": "{OWNER}", "reason": "push-denied"}}' in line


def test_force_reregister_carries_the_marker(world):
    registry.register(
        "github",
        "acme",
        "gadget",
        "gad",
        "prototype",
        local_only={"owner_host": OWNER, "reason": "opt-in"},
    )
    registry.register("github", "acme", "gadget", "gad", "personal")
    entry = _entry_json(repo="gadget")
    assert entry["kind"] == "personal"
    assert entry["local_only"] == {"owner_host": OWNER, "reason": "opt-in"}


def test_set_local_only_writes_and_deletes_preserving_other_keys(world):
    config.config_path().write_text(FLOW)
    registry.set_local_only("github", "acme", "tool", {"owner_host": OWNER, "reason": "migrated"})
    assert _entry_json()["local_only"] == {"owner_host": OWNER, "reason": "migrated"}
    registry.set_local_only("github", "acme", "tool", None)
    entry = _entry_json()
    assert "local_only" not in entry and entry["prefix"] == "tl"


def test_set_local_only_refuses_an_unregistered_hive(world):
    config.config_path().write_text(FLOW)
    with pytest.raises(LookupError):
        registry.set_local_only("github", "acme", "nope", None)


# ---- R4: hives-ready -----------------------------------------------------------------------


def _measure(monkeypatch, entries, host_id, ready_prefixes, caps=None):
    monkeypatch.setattr(heartbeat_report, "config_valid", lambda: True)
    seen = []

    def ready(entry):
        seen.append(entry["prefix"])
        return entry["prefix"] in ready_prefixes

    monkeypatch.setattr(heartbeat_report, "hive_ready", ready)
    checks = heartbeat_report.measure_conformance({"managed_repos": entries}, caps, host_id)
    return {c["id"]: c for c in checks}["hives-ready"], seen


def test_non_owner_skips_local_only_hive_with_evidence(monkeypatch):
    check, seen = _measure(monkeypatch, [_local(), _plain()], OTHER, {"rp"})
    assert check["status"] == "pass"
    assert seen == ["rp"]
    assert check["evidence"] == ("measured check passed; skipped (local-only on another host): tl")
    assert OWNER not in check["evidence"]


def test_non_owning_frame_stays_eligible_when_only_local_only_hives_exist(monkeypatch):
    check, seen = _measure(monkeypatch, [_local()], OTHER, set())
    assert check["status"] == "pass" and seen == []


def test_owner_still_requires_its_local_only_hive(monkeypatch):
    check, seen = _measure(monkeypatch, [_local()], OWNER, set())
    assert check["status"] == "fail" and seen == ["tl"]


def test_unreadable_host_id_fails_closed(monkeypatch):
    check, seen = _measure(monkeypatch, [_local()], None, set())
    assert check["status"] == "fail" and seen == ["tl"]


def test_default_host_id_is_this_hosts(monkeypatch):
    monkeypatch.setattr(host, "host_id_or_none", lambda: OTHER)
    monkeypatch.setattr(heartbeat_report, "config_valid", lambda: True)
    monkeypatch.setattr(heartbeat_report, "hive_ready", lambda e: False)
    checks = heartbeat_report.measure_conformance({"managed_repos": [_local()]}, None)
    assert {c["id"]: c for c in checks}["hives-ready"]["status"] == "pass"


def test_hive_both_local_only_and_policy_excluded_is_listed_once(monkeypatch):
    arm = {"frame_policy": {"requires": {"arch": "arm64"}}}
    entries = [_local(**arm), _plain("ab") | arm]
    caps = {"arch": "x86_64", "harnesses": [], "max_sessions": 1}
    check, _ = _measure(monkeypatch, entries, OTHER, set(), caps)
    assert check["evidence"] == (
        "measured check passed; skipped (frame_policy excludes this frame): ab; "
        "skipped (local-only on another host): tl"
    )


def test_evidence_truncates_long_prefix_lists():
    names = [f"hive{i:04d}" for i in range(300)]
    text = heartbeat_report._skip_evidence([("local-only on another host", names)])
    assert len(text) <= heartbeat_report.EVIDENCE_CAP
    assert text.endswith(f"(+{300 - text.count('hive')})")
    assert ", … (+" in text


# ---- R5: eligibility, leases and claims ----------------------------------------------------


def test_lease_adopt_refused_for_non_owner_even_forced(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "require_eligible", lambda *a, **k: None)  # legacy host
    monkeypatch.setattr(config, "load", lambda: {"managed_repos": [_local()]})
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio")
    reads = []
    monkeypatch.setattr(host_lease, "_read", lambda *a, **k: reads.append(a) or ("", None))
    for force in (False, True):
        with pytest.raises(host_lease.HostLeaseRejected, match="local-only on host studio"):
            host_lease.adopt("origin", "tl", host_id=OTHER, label="x", cwd=tmp_path, force=force)
        with pytest.raises(host_lease.HostLeaseRejected):
            host_lease.takeover("origin", "tl", host_id=OTHER, label="x", cwd=tmp_path, force=force)
    assert reads == []  # refused before HQ is read or written


def test_lease_adopt_allowed_for_the_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "require_eligible", lambda *a, **k: None)
    monkeypatch.setattr(config, "load", lambda: {"managed_repos": [_local()]})
    host_lease.refuse_local_only("tl", OWNER)  # does not raise
    host_lease.refuse_local_only("rp", OTHER)  # unknown prefix: nothing to refuse


def test_legacy_guard_refuses_a_non_owner_claim(monkeypatch):
    monkeypatch.setattr(guard, "writer_state", lambda *a, **k: None)
    monkeypatch.setattr(policy, "require_intake", lambda *a, **k: None)
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: "/nowhere")
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, d: _local())
    monkeypatch.setattr(host, "host_id_or_none", lambda: OTHER)
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio")
    with pytest.raises(typer.Exit):
        guard.guard_primary("tl", cfg={"managed_repos": [_local()]})


def test_legacy_guard_admits_the_owner(monkeypatch):
    monkeypatch.setattr(guard, "writer_state", lambda *a, **k: None)
    monkeypatch.setattr(policy, "require_intake", lambda *a, **k: None)
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: "/nowhere")
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, d: _local())
    monkeypatch.setattr(host, "host_id_or_none", lambda: OWNER)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)
    guard.guard_primary("tl", cfg={"managed_repos": [_local()]})


# ---- R3: rollout floor ---------------------------------------------------------------------


def _frames(*releases):
    frames = {}
    for i, release in enumerate(releases):
        receipt = {} if release is None else {"lease": {"release": {"id": release}}}
        frames[f"frame-{i}"] = {"active": {"receipt": receipt}}
    return {"frames": frames}, {}


def test_rollout_passes_when_every_frame_reads_local_only():
    from beadhive import local_only_rollout as rollout

    floor = rollout.MIN_LOCAL_ONLY_READER
    assert rollout.refusal(reader=lambda: _frames(floor, "9.0.0")) is None


def test_rollout_passes_on_a_legacy_fleet_with_no_frame_authority():
    from beadhive import local_only_rollout as rollout

    assert rollout.refusal(reader=lambda: None) is None


def test_rollout_refuses_an_older_reader_with_no_override():
    from beadhive import local_only_rollout as rollout

    for allow in (False, True):
        why = rollout.refusal(allow_unknown_readers=allow, reader=lambda: _frames("0.38.0"))
        assert why and "frame-0 (0.38.0)" in why and "no override" in why


def test_rollout_unknown_release_is_waivable_only_by_the_flag():
    from beadhive import local_only_rollout as rollout

    for unknown in (None, "not-a-version"):
        why = rollout.refusal(reader=lambda u=unknown: _frames(u))
        assert why and "--allow-unknown-readers" in why
        assert (
            rollout.refusal(allow_unknown_readers=True, reader=lambda u=unknown: _frames(u)) is None
        )


def test_rollout_unreadable_authority_counts_as_unknown():
    from beadhive import local_only_rollout as rollout

    def boom():
        raise RuntimeError("authority unavailable")

    assert "--allow-unknown-readers" in rollout.refusal(reader=boom)
    assert rollout.refusal(allow_unknown_readers=True, reader=boom) is None


def test_rollout_evidence_carrier_wins_over_the_receipt():
    from beadhive import local_only_rollout as rollout

    state, _ = _frames("0.38.0")
    evidence = {("frame-0", "active"): rollout.MIN_LOCAL_ONLY_READER}
    assert rollout.refusal(reader=lambda: (state, evidence)) is None


# ---- onboard (R2, R6, R7) ------------------------------------------------------------------


@pytest.fixture
def onboard_world(world, monkeypatch):
    """A world whose onboard store steps are recorded (no real bd) and whose probes are pinned."""
    from beadhive import hub, local_only_rollout, onboard

    calls: list[str] = []

    def _init(ctx):
        calls.append("bd-init")
        (ctx.base / ".beads").mkdir(exist_ok=True)

    monkeypatch.setattr(onboard, "_act_bd_init", _init)
    monkeypatch.setattr(onboard, "_act_publish", lambda ctx: calls.append("publish"))
    monkeypatch.setattr(registry, "classify", lambda *a, **k: "personal-or-prototype")
    monkeypatch.setattr(hub, "sync", lambda: None)
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: False)
    gate: list[bool] = []

    def _refusal(*, allow_unknown_readers=False, reader=None):
        gate.append(allow_unknown_readers)
        return None

    monkeypatch.setattr(local_only_rollout, "refusal", _refusal)
    world.onboard_calls = calls
    world.rollout_calls = gate
    return world


def _probe(monkeypatch, state, cause=""):
    monkeypatch.setattr(
        registry, "has_push_access", lambda *a, **k: registry.PushAccess(state, cause)
    )


def _repo(world, *, org="acme", repo="widget", with_beads=False):
    from harness.world import git

    target = world.ws_root / "github" / org / repo
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    git("config", "user.email", "t@ws.dev", cwd=target)
    git("config", "user.name", "T", cwd=target)
    (target / "README.md").write_text("hi")
    git("add", ".", cwd=target)
    git("commit", "-q", "-m", "init", cwd=target)
    if with_beads:
        (target / ".beads").mkdir()
    return target


def _onboard_ctx(target, *, org="acme", repo="widget", **kw):
    from beadhive import onboard

    ctx = onboard.Ctx(
        hive=f"github/{org}/{repo}",
        target=str(target),
        provider="github",
        org=org,
        repo=repo,
        cwd=str(target),
        cfg=config.load(),
        hub_sync=False,
        **kw,
    )
    ctx.steps = onboard.build_steps(ctx)
    return ctx


def _marker(org="acme", repo="widget"):
    entry = registry.find_entry(config.load(), "github", org, repo)
    return None if entry is None else registry.local_only_of(entry)


def _this_host():
    from beadhive import host

    return host.host_id()


def test_onboard_denied_push_registers_local_only(onboard_world, monkeypatch):
    from beadhive import onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "denied")
    plan = onboard.run_onboard(_onboard_ctx(target))

    assert plan.registered and plan.local_only == "push-denied"
    assert _marker() == {"owner_host": _this_host(), "reason": "push-denied"}
    assert any("local-only" in w and "hive replicate" in w for w in plan.warnings)
    assert onboard_world.rollout_calls == [False]


def test_onboard_denied_push_with_published_origin_stays_replicated(onboard_world, monkeypatch):
    from beadhive import onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "denied")
    monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda ctx: True)
    plan = onboard.run_onboard(_onboard_ctx(target))

    assert plan.registered and plan.local_only == "" and _marker() is None
    assert onboard_world.rollout_calls == []  # nothing to write, no floor to check


def test_onboard_confirmed_push_stays_replicated(onboard_world, monkeypatch):
    from beadhive import onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "confirmed")
    plan = onboard.run_onboard(_onboard_ctx(target))
    assert plan.registered and _marker() is None


def test_onboard_local_only_flag_opts_in_even_with_confirmed_push(onboard_world, monkeypatch):
    from beadhive import onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "confirmed")
    plan = onboard.run_onboard(_onboard_ctx(target, local_only=True, allow_unknown_readers=True))

    assert _marker() == {"owner_host": _this_host(), "reason": "opt-in"}
    assert plan.local_only == "opt-in"
    assert onboard_world.rollout_calls == [True]


def test_onboard_unknown_push_opted_in_records_opt_in(onboard_world, monkeypatch):
    from beadhive import onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "unknown", "gh-missing")
    onboard.run_onboard(_onboard_ctx(target), skip_checks=["push-access-confirmed"])
    assert _marker()["reason"] == "opt-in"


def test_onboard_external_registers_local_only_external(onboard_world, monkeypatch):
    from beadhive import onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "denied")
    onboard.run_onboard(_onboard_ctx(target, kind="external", yes=True))
    assert _marker() == {"owner_host": _this_host(), "reason": "external"}


def test_guard_unsets_beads_remote_for_local_only_even_when_confirmed(monkeypatch):
    from beadhive import onboard

    seen = []
    monkeypatch.setattr(
        onboard.bd_mod,
        "run",
        lambda args, *a, **k: seen.append(list(args)) or type("R", (), {"returncode": 0})(),
    )
    _probe(monkeypatch, "confirmed")
    ctx = onboard.Ctx(
        hive="github/a/b", target="/t", provider="github", org="a", repo="b", cwd="/t"
    )
    onboard._guard_beads_remote(ctx)
    assert seen == []
    ctx.local_only = True
    onboard._guard_beads_remote(ctx)
    assert seen == [["config", "unset", "sync.remote"]]


def test_publish_never_pushes_an_explicit_local_only_hive(monkeypatch):
    from beadhive import onboard, sync_remote

    seen = []
    monkeypatch.setattr(onboard.bd_mod, "sync_remote", lambda base: "git+ssh://x/y.git")
    monkeypatch.setattr(
        onboard.bd_mod,
        "run",
        lambda args, *a, **k: seen.append(list(args)) or type("R", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(sync_remote, "_push_dolt_state", lambda *a, **k: pytest.fail("pushed"))
    ctx = onboard.Ctx(
        hive="github/a/b",
        target="/t",
        provider="github",
        org="a",
        repo="b",
        cwd="/t",
        local_only=True,
    )
    ctx._derived = True
    onboard._act_publish(ctx)
    assert seen == [["config", "unset", "sync.remote"]]


def _register(local_only=None, **kw):
    registry.register(
        "github", "acme", "widget", "widget", "prototype", local_only=local_only, **kw
    )


def test_second_host_onboard_refuses_without_creating_a_database(onboard_world, monkeypatch):
    """R6: the non-owner refusal runs in resolve — nothing is cloned, no store is minted."""
    from beadhive import onboard

    _register({"owner_host": OWNER, "reason": "push-denied"})
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio")
    _probe(monkeypatch, "denied")
    clones = []
    monkeypatch.setattr(onboard, "_act_clone", lambda ctx: clones.append(ctx.target))
    target = onboard_world.ws_root / "github" / "acme" / "widget"  # absent: would be cloned
    ctx = _onboard_ctx(target, clone_url="https://example.invalid/acme/widget.git")

    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx)

    failed = next(c for c in ctx.plan.checks if c.id == "local-only-owner")
    assert failed.ok is False and failed.overridable is False
    assert failed.detail == (
        f"acme/widget (prefix widget) is local-only on host studio ({OWNER}): its bead database "
        "exists only there and was never published. Onboarding here would create a second, "
        "divergent database under the same prefix. Work on that host, or have its owner run "
        "`bh hive replicate widget` once it has push access."
    )
    assert clones == [] and onboard_world.onboard_calls == []
    assert not target.exists()


def test_second_host_refusal_is_not_overridable(onboard_world, monkeypatch):
    from beadhive import onboard

    _register({"owner_host": OWNER, "reason": "external"}, upstream="up/widget")
    monkeypatch.setattr(registry, "host_label", lambda hid: hid)
    target = _repo(onboard_world, with_beads=False)
    ctx = _onboard_ctx(target, force=True, yes=True)
    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx, skip_checks=["local-only-owner"])
    failed = next(c for c in ctx.plan.checks if c.id == "local-only-owner")
    assert failed.skipped is False
    assert failed.detail.endswith("External hives are never replicated; work on that host.")
    assert onboard_world.onboard_calls == [] and not (target / ".beads").exists()


def test_owner_reonboard_passes_and_keeps_the_marker(onboard_world, monkeypatch):
    from beadhive import onboard

    _register({"owner_host": _this_host(), "reason": "push-denied"})
    target = _repo(onboard_world, with_beads=True)
    _probe(monkeypatch, "denied")
    plan = onboard.run_onboard(_onboard_ctx(target, force=True))
    assert next(c for c in plan.checks if c.id == "local-only-owner").ok
    assert _marker() == {"owner_host": _this_host(), "reason": "push-denied"}
    assert onboard_world.rollout_calls == []  # carried, not newly written


def test_local_only_flag_refused_on_a_replicated_registration(onboard_world, monkeypatch):
    from beadhive import onboard

    _register()
    target = _repo(onboard_world, with_beads=True)
    _probe(monkeypatch, "confirmed")
    ctx = _onboard_ctx(target, local_only=True)
    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx)
    failed = next(c for c in ctx.plan.checks if c.id == "local-only-first-onboard")
    assert "hive repair --local-only --yes" in failed.detail
    assert _marker() is None


def test_onboard_refuses_local_only_without_a_host_id(onboard_world, monkeypatch):
    from beadhive import onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "denied")
    monkeypatch.setattr(host, "host_id_or_none", lambda: None)
    ctx = _onboard_ctx(target)
    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx)
    failed = next(c for c in ctx.plan.checks if c.id == "local-only-host-id")
    assert "config init" in failed.detail and failed.overridable is False
    assert onboard_world.onboard_calls == []
    assert registry.find_entry(config.load(), "github", "acme", "widget") is None


def test_onboard_rollout_refusal_stops_before_any_store(onboard_world, monkeypatch):
    from beadhive import local_only_rollout, onboard

    target = _repo(onboard_world)
    _probe(monkeypatch, "denied")
    monkeypatch.setattr(
        local_only_rollout, "refusal", lambda **k: "local-only refused: frame-0 (0.38.0)"
    )
    ctx = _onboard_ctx(target)
    with pytest.raises(typer.Exit):
        onboard.run_onboard(ctx, skip_checks=["local-only-readers"])
    failed = next(c for c in ctx.plan.checks if c.id == "local-only-readers")
    assert failed.skipped is False and "0.38.0" in failed.detail
    assert onboard_world.onboard_calls == []
    assert registry.find_entry(config.load(), "github", "acme", "widget") is None


# ---- bh hive replicate (R2) ----------------------------------------------------------------


@pytest.fixture
def replicate_world(world, monkeypatch):
    from beadhive import hive_local_only

    _register({"owner_host": _this_host(), "reason": "push-denied"})
    hive_dir = world.ws_root / "github" / "acme" / "widget"
    (hive_dir / ".beads").mkdir(parents=True)
    events: list[str] = []
    origin = {"data": False}

    def _origin(_dir):
        events.append(f"origin?{origin['data']}")
        return origin["data"]

    def _publish(cfg, d):
        events.append("push")
        origin["data"] = world.push_lands
        return world.push_ok, "" if world.push_ok else "remote rejected"

    world.push_ok, world.push_lands = True, True
    monkeypatch.setattr(hive_local_only, "origin_has_dolt_data", _origin)
    monkeypatch.setattr(
        hive_local_only, "restore_beads_remote", lambda cfg, d: events.append("restore")
    )
    monkeypatch.setattr(hive_local_only, "_publish", _publish)
    _probe(monkeypatch, "confirmed")
    world.events, world.origin = events, origin
    return world


def _replicate(hive="widget"):
    from beadhive import hive_local_only

    return hive_local_only.replicate(config.load(), hive)


def test_replicate_publishes_first_then_clears(replicate_world, monkeypatch):
    seen = []
    real = registry.set_local_only
    monkeypatch.setattr(
        registry,
        "set_local_only",
        lambda *a: seen.append(list(replicate_world.events)) or real(*a),
    )
    result = _replicate()
    assert result.published and result.cleared
    assert seen == [["origin?False", "restore", "push", "origin?True"]]
    assert _marker() is None


@pytest.mark.parametrize("state", ["denied", "unknown"])
def test_replicate_refuses_without_confirmed_push(replicate_world, monkeypatch, state):
    from beadhive import hive_local_only

    _probe(monkeypatch, state, "gh-missing" if state == "unknown" else "")
    with pytest.raises(hive_local_only.LocalOnlyError, match=state):
        _replicate()
    assert replicate_world.events == [] and _marker() is not None


def test_replicate_refuses_when_origin_already_carries_data(replicate_world):
    from beadhive import hive_local_only

    replicate_world.origin["data"] = True
    with pytest.raises(hive_local_only.LocalOnlyError, match="already carries refs/dolt/data"):
        _replicate()
    assert replicate_world.events == ["origin?True"]  # no restore, no push
    assert _marker() is not None


def test_replicate_failed_push_leaves_the_field(replicate_world):
    from beadhive import hive_local_only

    replicate_world.push_ok = False
    with pytest.raises(hive_local_only.LocalOnlyError, match="remote rejected"):
        _replicate()
    assert _marker() == {"owner_host": _this_host(), "reason": "push-denied"}


def test_replicate_unverified_push_leaves_the_field(replicate_world):
    from beadhive import hive_local_only

    replicate_world.push_lands = False
    with pytest.raises(hive_local_only.LocalOnlyError, match="does not carry"):
        _replicate()
    assert _marker() is not None


def test_replicate_failed_remote_restore_leaves_the_field(replicate_world, monkeypatch):
    from beadhive import hive_local_only

    def boom(cfg, d):
        raise hive_local_only.LocalOnlyError("could not restore sync.remote")

    monkeypatch.setattr(hive_local_only, "restore_beads_remote", boom)
    with pytest.raises(hive_local_only.LocalOnlyError):
        _replicate()
    assert "push" not in replicate_world.events and _marker() is not None


def test_replicate_refuses_on_a_non_owner(replicate_world, monkeypatch):
    from beadhive import hive_local_only

    monkeypatch.setattr(host, "host_id_or_none", lambda: OTHER)
    with pytest.raises(hive_local_only.LocalOnlyError, match="only its owner"):
        _replicate()
    assert replicate_world.events == []


def test_replicate_refuses_an_external_hive(replicate_world):
    from beadhive import hive_local_only

    registry.set_local_only(
        "github", "acme", "widget", {"owner_host": _this_host(), "reason": "external"}
    )
    with pytest.raises(hive_local_only.LocalOnlyError, match="never replicated"):
        _replicate()
    assert replicate_world.events == []


def test_replicate_publishes_an_unmarked_unpublished_hive(replicate_world):
    registry.set_local_only("github", "acme", "widget", None)
    result = _replicate()
    assert result.published and not result.cleared


def test_replicate_cli_exits_nonzero_on_refusal(replicate_world, monkeypatch, capsys):
    from beadhive import hive_local_only

    _probe(monkeypatch, "denied")
    with pytest.raises(typer.Exit):
        hive_local_only.replicate_cli("widget")
    assert "replicate refused" in capsys.readouterr().err


# ---- bh hive repair --local-only (R9) ------------------------------------------------------


@pytest.fixture
def repair_world(world, monkeypatch):
    from beadhive import hive_local_only, local_only_rollout

    _register()
    (world.ws_root / "github" / "acme" / "widget" / ".beads").mkdir(parents=True)
    monkeypatch.setattr(hive_local_only, "origin_has_dolt_data", lambda d: False)
    monkeypatch.setattr(hive_local_only, "_lease", lambda prefix: None)
    monkeypatch.setattr(hive_local_only, "_now", lambda: 1_000_000.0)
    monkeypatch.setattr(local_only_rollout, "refusal", lambda **k: None)
    return world


def _repair(**kw):
    from beadhive import hive_local_only

    args = {"yes": True, "dry_run": False, "other_modes": [False] * 4} | kw
    hive_local_only.repair_cli("github/acme/widget", **args)


def test_repair_records_migrated_owned_by_this_host(repair_world):
    _repair()
    assert _marker() == {"owner_host": _this_host(), "reason": "migrated"}


def test_repair_records_external_for_an_external_hive(repair_world):
    registry.register("github", "acme", "widget", "widget", "external", upstream="up/widget")
    _repair()
    assert _marker()["reason"] == "external"


def test_repair_dry_run_and_missing_yes_write_nothing(repair_world):
    _repair(dry_run=True, yes=False)
    assert _marker() is None
    with pytest.raises(typer.Exit):
        _repair(yes=False)
    assert _marker() is None


def test_repair_is_idempotent_for_the_owner(repair_world, capsys):
    _repair()
    _repair()
    assert "already local-only to this host" in capsys.readouterr().out


def test_repair_refuses_a_different_owner(repair_world, monkeypatch):
    registry.set_local_only("github", "acme", "widget", {"owner_host": OWNER, "reason": "opt-in"})
    monkeypatch.setattr(host, "host_id_or_none", lambda: OTHER)
    with pytest.raises(typer.Exit):
        _repair()
    assert _marker()["owner_host"] == OWNER


def _lease_at(host_id, expires_at):
    from beadhive.host_lease_contracts import HostLease, now_stamp

    return HostLease(
        host_id=host_id,
        label="studio",
        epoch=3,
        adopted_at=now_stamp(expires_at - 600),
        expires_at=now_stamp(expires_at),
    )


def test_repair_refuses_while_another_host_holds_an_unexpired_lease(repair_world, monkeypatch):
    from beadhive import hive_local_only

    monkeypatch.setattr(hive_local_only, "_lease", lambda p: _lease_at(OWNER, 1_000_600.0))
    with pytest.raises(typer.Exit):
        _repair()
    assert _marker() is None


def test_repair_ignores_an_expired_or_own_lease(repair_world, monkeypatch):
    from beadhive import hive_local_only

    monkeypatch.setattr(hive_local_only, "_lease", lambda p: _lease_at(OWNER, 999_000.0))
    _repair()
    registry.set_local_only("github", "acme", "widget", None)
    monkeypatch.setattr(hive_local_only, "_lease", lambda p: _lease_at(_this_host(), 1_000_600.0))
    _repair()
    assert _marker()["owner_host"] == _this_host()


def test_repair_refuses_an_unreadable_lease(repair_world, monkeypatch):
    from beadhive import hive_local_only

    def boom(prefix):
        raise RuntimeError("HQ unreachable")

    monkeypatch.setattr(hive_local_only, "_lease", boom)
    with pytest.raises(typer.Exit):
        _repair()
    assert _marker() is None


def test_repair_refuses_a_published_hive(repair_world, monkeypatch):
    from beadhive import hive_local_only

    monkeypatch.setattr(hive_local_only, "origin_has_dolt_data", lambda d: True)
    with pytest.raises(typer.Exit):
        _repair()
    assert _marker() is None


def test_repair_honours_the_rollout_floor(repair_world, monkeypatch):
    from beadhive import local_only_rollout

    seen = []

    def gate(*, allow_unknown_readers=False, reader=None):
        seen.append(allow_unknown_readers)
        return (
            None if allow_unknown_readers else "local-only refused: frame-0 (no attested release)"
        )

    monkeypatch.setattr(local_only_rollout, "refusal", gate)
    with pytest.raises(typer.Exit):
        _repair()
    assert _marker() is None
    _repair(allow_unknown_readers=True)
    assert seen == [False, True] and _marker()["reason"] == "migrated"


def test_repair_needs_exactly_one_mode(repair_world):
    with pytest.raises(typer.Exit):
        _repair(other_modes=[False, False, True, False])  # --role --local-only
    assert _marker() is None


def test_repair_cli_dispatches_local_only(repair_world):
    from typer.testing import CliRunner

    from beadhive import cli

    result = CliRunner().invoke(
        cli.app, ["hive", "repair", "--hive", "github/acme/widget", "--local-only", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert _marker()["reason"] == "migrated"


# ---- bh host retire (operator answer 5) ----------------------------------------------------


def test_host_retire_refuses_while_owning_local_only_hives(monkeypatch, tmp_path, capsys):
    from beadhive import host_cli, host_retire

    cfg = {"managed_repos": [_local("tl"), _local("ab", owner=OTHER), _plain("rp")]}
    monkeypatch.setattr(config, "load", lambda: cfg)
    monkeypatch.setattr(host_cli, "_require_hq_dir", lambda: tmp_path)
    monkeypatch.setattr(host_cli, "_require_host_id", lambda: OWNER)
    monkeypatch.setattr(host_retire, "assess", lambda **k: pytest.fail("assessed"))
    for kw in ({}, {"dry_run": True}, {"confirm": True, "backup": True, "purge": True}):
        with pytest.raises(typer.Exit):
            host_retire.retire(**kw)
    err = capsys.readouterr().err
    assert "whose bead database exists only here: tl\n" in err  # not ab (owned elsewhere)
    assert "hive replicate" in err and "no override" in err


def test_host_retire_proceeds_when_it_owns_none(monkeypatch, tmp_path):
    from beadhive import host_retire

    assert host_retire.owned_local_only({"managed_repos": [_local(owner=OTHER)]}, OWNER) == []


# ---- bh doctor detection (R9) --------------------------------------------------------------


def test_doctor_reports_registered_unpublished_hives(monkeypatch, tmp_path):
    from beadhive import doctor

    entries = [_plain("rp"), _local("tl"), _plain("hb") | {"kind": "hq"}, _plain("pb")]
    entries.append(_plain("dr") | {"dolt_remote": {"url": "s3://b/dr"}})
    entries.append(_plain("nb"))
    dirs = {e["prefix"]: tmp_path / e["prefix"] for e in entries}
    for prefix, d in dirs.items():
        if prefix != "nb":  # nb: no local store here
            (d / ".beads").mkdir(parents=True)
    monkeypatch.setattr(registry, "hive_dir", lambda e: dirs[e["prefix"]])
    answers = {dirs["rp"]: False, dirs["pb"]: True}
    probed = []
    monkeypatch.setattr(
        doctor, "_origin_dolt_data_state", lambda p: probed.append(p) or answers.get(p)
    )
    findings = doctor._data_unpublished({"managed_repos": entries})
    assert [f["prefix"] for f in findings] == ["rp"]
    assert any("hive replicate rp" in line for line in findings[0]["remediation"])
    assert any("--local-only --yes" in line for line in findings[0]["remediation"])
    assert set(probed) == {dirs["rp"], dirs["pb"]}  # never probes marked/declared/hq/absent


def test_doctor_never_reports_an_unanswerable_probe(monkeypatch, tmp_path):
    from beadhive import doctor

    (tmp_path / ".beads").mkdir()
    monkeypatch.setattr(registry, "hive_dir", lambda e: tmp_path)
    monkeypatch.setattr(doctor, "_origin_dolt_data_state", lambda p: None)
    assert doctor._data_unpublished({"managed_repos": [_plain()]}) == []


# ---- what other frames see: hive list / hive status (R5) ------------------------------------


def test_hive_list_and_status_show_local_only_owner(world, monkeypatch, capsys):
    from beadhive import hive

    _register({"owner_host": OWNER, "reason": "push-denied"})
    registry.register("github", "acme", "gadget", "gad", "prototype")
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio" if hid == OWNER else hid)
    monkeypatch.setattr(
        hive,
        "available",
        lambda cfg=None: {
            "candidates": [],
            "registered": ["github/acme/gadget", "github/acme/widget"],
        },
    )
    hive.ls()
    out = capsys.readouterr().out
    assert "github/acme/widget  local-only (owner studio)" in out
    assert "github/acme/gadget\n" in out
    monkeypatch.setattr(hive, "available", lambda cfg=None: {"candidates": [], "registered": []})
    hive.status()
    assert "(prototype, local-only (owner studio))" in capsys.readouterr().out
