"""The production outbox wiring (``bh-uavc2``, absorbing ``bh-fehna``).

``intake_services.outbox_wiring`` builds the outbox's tracker, own identity, marker codec and
owned label prefixes from the hive's PUBLIC-store binding through the ``tracker.adapter``
capability: the marker codec is the bound port's optional ``MarkerCodec``, so root never imports
the plugin package. What is pinned here:

* off unless ``intake.external.enabled``, ``intake.external.outbox.enabled`` and
  ``plugins.tracker-github.enabled`` are all on; with any switch off every verb refuses naming the
  key, exactly as before;
* on, the outbox verbs resolve a real ``TrackerAdapters`` over ``MemoryTracker`` with NO seam
  patched: draft, show (the live dry run, digest and marker line), approve, send and reconcile;
* the operator-only approval stays the only way to a post: an agent seat is refused and nothing
  is written before an operator approved the digest;
* ``bh work outbox show <id>`` with no tracker wired prints the stored draft, its state and its
  operations (``bh-fehna``: a merge milestone's draft is inspectable before any credential);
* a binding that refuses, or an adapter without a marker codec, refuses the sending verbs with an
  actionable message and leaves the stored show working;
* the first-party GitHub adapter's ``bh:v1`` codec reaches root through the capability, offline.

No live GitHub, no clock, no sleep: ``MemoryTracker``, an in-process lock and injected entropy.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from tests.test_intake_import_composition import Rig, fingerprint
from tests.unit.bootstrap.test_tracker_composition import plugin, roots
from typer.testing import CliRunner

from beadhive import cli, intake_services, registry, run, store_target, work_intake
from beadhive.modules.intake import IntakeRefused, IntakeStoreError, OwnIdentity, RefusalReason
from beadhive.modules.intake.domain import WorkDraft
from beadhive.modules.tracker.contracts import CommentWriter, MarkerCodec, OwnMarker
from beadhive.testing.intake import draft
from beadhive.testing.tracker import MemoryTracker, at

ENV = {"BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1", "COLUMNS": "200"}
OPERATOR = "ops/alice"
WRITES = {"tracker-github": plugin(comments=True, comment=True)}
MERGE_SHA = "a" * 40


def external(**extra) -> dict:
    return {"intake": {"external": {"enabled": True, "outbox": {"enabled": True}, **extra}}}


class Wired(Rig):
    """The import rig with the PRODUCTION outbox wiring: only ``process_adapters`` is replaced,
    by a real ``TrackerAdapters`` over the rig's ``MemoryWorld`` (``plugins`` is its config)."""

    def __init__(self, tmp_path, monkeypatch, plugins=WRITES) -> None:
        super().__init__(tmp_path, monkeypatch)
        self.plugins = plugins
        self.cfg = {"managed_repos": [self.entry], **external(), "plugins": plugins}
        self.adapters, _factory = roots(plugins, factory=self.factory)
        self.process: object = self.adapters
        self.world.set_write_time(at(2))  # a deterministic tracker clock
        monkeypatch.setattr(work_intake, "config", SimpleNamespace(load=lambda: self.cfg))
        monkeypatch.setattr(registry, "resolve_hive", lambda cfg, hive: self.entry)
        monkeypatch.setattr(registry, "current_hive", lambda cfg: self.entry)
        monkeypatch.setattr(work_intake.identity, "resolve_actor", lambda explicit="": explicit)
        monkeypatch.setattr(intake_services, "process_adapters", lambda cfg: self.process)

    def plugin_off(self) -> None:
        """``plugins.tracker-github.enabled`` off: ``process_adapters`` builds nothing."""
        self.process = None

    def configure(self, cfg: dict) -> None:
        self.cfg = {"managed_repos": [self.entry], "plugins": self.plugins, **cfg}

    def assert_public_store_unread(self) -> None:
        """The public store is never resolved or written (the one approved send is a tracker
        write, not a store write)."""
        assert store_target.SCOPE_PUBLIC not in self.scopes
        assert fingerprint(self.public) == self.before

    def report(self, title: str = "A bug") -> tuple[str, object]:
        issue = self.issue(title)
        imported = bh("import", issue.url, "--hive", "widget", "--json", "--as", "dev/importer")
        assert imported.exit_code == 0, imported.output
        return doc(imported)["report"], issue


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Wired:
    return Wired(tmp_path, monkeypatch)


def bh(*argv: str):
    return CliRunner().invoke(cli.app, ["work", *argv], env=ENV)


def doc(result) -> dict:
    return json.loads(result.stdout)


COMMON = ("--hive", "widget", "--as", OPERATOR, "--json")


# -- the wiring itself ---------------------------------------------------------------------------


def test_with_every_switch_on_the_wiring_comes_from_the_plugin_binding(rig) -> None:
    wiring = intake_services.outbox_wiring(rig.cfg, rig.entry, rig.hive)

    tracker = wiring["tracker"]
    assert isinstance(tracker, CommentWriter) and isinstance(tracker, MarkerCodec)
    identity = wiring["identity"]
    assert identity.hive == "acme/widget" and len(identity.install) == 32
    assert wiring["marker_line"](OwnMarker("acme/widget", identity.install, "ob1_x")) == (
        f"<!-- bh:v1 hive=acme/widget install={identity.install} outbox=ob1_x -->"
    )
    assert wiring["owned_label_prefixes"] == ()  # labels.change is not enabled
    assert wiring["close_enabled"] is False  # issue.close is off by default
    assert rig.world.read_requests() == rig.world.write_requests() == 0  # building reads nothing


def test_the_owned_label_prefixes_come_from_the_plugin_config_only_when_labels_are_enabled(
    tmp_path, monkeypatch
) -> None:
    plugins = {"tracker-github": plugin(comments=True, labels=True, prefixes=("bh:", "status/"))}
    rig = Wired(tmp_path, monkeypatch, plugins)

    wiring = intake_services.outbox_wiring(rig.cfg, rig.entry, rig.hive)

    assert wiring["owned_label_prefixes"] == ("bh:", "status/")


def test_the_close_gate_follows_the_bindings_own_issue_close_enable(tmp_path, monkeypatch) -> None:
    on = plugin(comments=True, comment=True)
    on["write"]["issue_close"] = {"enabled": True}
    rig = Wired(tmp_path, monkeypatch, {"tracker-github": on})
    monkeypatch.setattr(
        work_intake,
        "config",
        SimpleNamespace(load=lambda: rig.cfg, integration_branch=lambda cfg, entry: "main"),
    )

    wiring = intake_services.outbox_wiring(rig.cfg, rig.entry, rig.hive)

    assert wiring["close_enabled"] is True
    report, _issue = rig.report()
    replied = bh("outbox", "draft", report, "--text", "Thanks.", *COMMON)
    assert replied.exit_code == 0
    # past the gate: the record has no close (a CLI draft asks for none), so the send refuses
    # on the missing operation, never on the gate
    oid = doc(replied)["results"][0]["outbox_id"]
    missing = bh("outbox", "send", oid, "--kind", "close", *COMMON)
    assert missing.exit_code == 1 and doc(missing)["reason"] == "unknown_outbox"
    off = bh("outbox", "backfill-close", "--dry-run", *COMMON)
    assert off.exit_code == 0 and doc(off)["items"] == []


def test_the_mention_checks_issue_listing_comes_from_the_binding_lazily(rig) -> None:
    """bh-iamru: the pre-apply mention check lists the bound project's issues through the same
    plugin binding (issues.read only), and building it binds and reads nothing."""
    rig.process = SimpleNamespace(for_store=lambda *a, **k: pytest.fail("bound too early"))
    issues = intake_services.project_issues(rig.cfg, rig.entry, rig.hive)
    rig.process = rig.adapters
    _report, issue = rig.report()
    reads = rig.world.read_requests()

    listed = issues.identifiers()

    assert listed[issue.identifier] == issue.url
    assert rig.world.read_requests() > reads and rig.world.write_requests() == 0


def test_the_mention_checks_listing_without_a_tracker_refuses_naming_the_key(rig) -> None:
    rig.plugin_off()

    with pytest.raises(IntakeRefused) as caught:
        intake_services.project_issues(rig.cfg, rig.entry, rig.hive).identifiers()

    assert caught.value.reason is RefusalReason.INTAKE_DISABLED
    assert "plugins.tracker-github.enabled" in caught.value.detail


@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"intake": {"external": {"enabled": False, "outbox": {"enabled": True}}}},
        {"intake": {"external": {"enabled": True}}},
        {"intake": {"external": {"enabled": True, "outbox": {"enabled": False}}}},
    ],
)
def test_with_intake_or_the_outbox_off_nothing_is_built(rig, cfg) -> None:
    rig.configure(cfg)
    rig.process = SimpleNamespace(for_store=lambda *a, **k: pytest.fail("must not bind"))

    assert intake_services.outbox_wiring(rig.cfg, rig.entry, rig.hive) == {}


def test_with_the_plugin_off_nothing_is_built(rig) -> None:
    rig.plugin_off()

    assert intake_services.outbox_wiring(rig.cfg, rig.entry, rig.hive) == {}
    assert not (rig.hive / ".bh" / "intake" / intake_services.INSTALL_ID_FILE).exists()


def test_the_real_process_adapters_switch_is_the_plugin_enable(tmp_path, monkeypatch) -> None:
    real = intake_services.process_adapters
    rig = Wired(tmp_path, monkeypatch)
    monkeypatch.setattr(intake_services, "process_adapters", real)  # the real plugin switch
    cfg = {**external(), "plugins": {"tracker-github": {"enabled": False}}}

    assert intake_services.outbox_wiring(cfg, rig.entry, rig.hive) == {}


def test_a_binding_that_refuses_is_returned_as_a_refusal_naming_the_key(rig) -> None:
    unbound = {**rig.entry, "stores": {"internal": {"remote": "r"}}}

    wiring = intake_services.outbox_wiring(rig.cfg, unbound, rig.hive)

    assert set(wiring) == {"refusal"}
    assert "stores.public" in wiring["refusal"].detail and "adapter" in wiring["refusal"].detail


class _NoCodec:
    """A conforming adapter (reads, identity, comment writer) WITHOUT the optional codec."""

    def __init__(self, inner: MemoryTracker) -> None:
        self._inner = inner

    def describe(self):
        return self._inner.describe()

    def list_issues(self, query):
        return self._inner.list_issues(query)

    def list_comments(self, query):
        return self._inner.list_comments(query)

    def list_ids(self, query):
        return self._inner.list_ids(query)

    def whoami(self):
        return self._inner.whoami()

    def probe_capabilities(self):
        return self._inner.probe_capabilities()

    def post_comment(self, issue, body, *, marker, grant):
        return self._inner.post_comment(issue, body, marker=marker, grant=grant)


class _NoCodecFactory:
    def __init__(self, world) -> None:
        self.world = world

    def __call__(self, request):
        return _NoCodec(MemoryTracker(self.world, enabled=request.enabled))


def test_an_adapter_without_a_marker_codec_cannot_wire_the_sender(rig) -> None:
    rig.process, _ = roots(WRITES, factory=_NoCodecFactory(rig.world))

    wiring = intake_services.outbox_wiring(rig.cfg, rig.entry, rig.hive)

    assert set(wiring) == {"refusal"} and "marker codec" in wiring["refusal"].detail


# -- the install id ------------------------------------------------------------------------------


def test_the_install_id_is_minted_once_privately_and_never_changes(rig) -> None:
    first = intake_services.install_id(rig.hive, entropy=lambda n: b"\x01" * n)
    again = intake_services.install_id(rig.hive, entropy=lambda n: b"\x02" * n)

    assert first == again == "01" * 16
    path = rig.hive / ".bh" / "intake" / intake_services.INSTALL_ID_FILE
    assert path.read_text() == "01" * 16 + "\n"
    assert list(path.parent.glob(".install-id.*")) == []  # no temporary left behind


def test_a_malformed_install_id_is_refused_never_reminted(rig) -> None:
    path = rig.hive / ".bh" / "intake" / intake_services.INSTALL_ID_FILE
    path.parent.mkdir(parents=True)
    path.write_text("w-12\n")

    with pytest.raises(IntakeStoreError, match="install id"):
        intake_services.install_id(rig.hive)
    assert path.read_text() == "w-12\n"


# -- the verbs over the production wiring -------------------------------------------------------


def test_draft_show_approve_send_and_reconcile_run_over_the_production_wiring(rig) -> None:
    report, issue = rig.report()

    drafted = bh("outbox", "draft", report, "--text", "Thanks, fixed soon.", *COMMON)
    assert drafted.exit_code == 0, drafted.output
    oid = doc(drafted)["results"][0]["outbox_id"]

    shown = bh("outbox", "show", oid, "--hive", "widget", "--json")
    assert shown.exit_code == 0, shown.output
    (render,) = doc(shown)["renders"]
    install = intake_services.install_id(rig.hive)
    assert render["payload"] == (
        f"Thanks, fixed soon.\n\n<!-- bh:v1 hive=acme/widget install={install} outbox={oid} -->"
    )
    assert rig.world.write_requests() == 0  # drafting and reviewing post nothing

    refused = bh(
        "outbox",
        "approve",
        oid,
        "--op",
        f"comment:{render['digest']}",
        "--hive",
        "widget",
        "--as",
        "dev/triager",
        "--json",
    )
    assert refused.exit_code == 1 and rig.world.write_requests() == 0  # an agent seat cannot
    unapproved = bh("outbox", "send", oid, *COMMON)
    assert unapproved.exit_code == 1 and rig.world.write_requests() == 0  # no grant, no post

    approved = bh("outbox", "approve", oid, "--op", f"comment:{render['digest']}", *COMMON)
    assert approved.exit_code == 0, approved.output
    sent = bh("outbox", "send", oid, *COMMON)
    assert sent.exit_code == 0 and doc(sent)["results"][0]["state"] == "sent"
    assert rig.world.write_requests() == 1 and rig.world.comment_count(issue) == 1
    (posted,) = rig.world.comments.values()
    assert posted.marker == OwnMarker("acme/widget", install, oid)

    reconciled = bh("outbox", "reconcile", oid, *COMMON)
    assert reconciled.exit_code == 0 and rig.world.write_requests() == 1
    rig.assert_public_store_unread()


@pytest.mark.parametrize(
    ("cfg", "reason", "key"),
    [
        ({}, "intake_disabled", "opt-in"),
        ({"intake": {"external": {"enabled": True}}}, "outbox_disabled", "outbox.enabled"),
    ],
)
@pytest.mark.parametrize(
    "argv",
    [
        ["draft", "w-1", "--text", "hi"],
        ["show", "ob1_x"],
        ["approve", "ob1_x", "--op", "comment:abc"],
        ["send", "ob1_x"],
        ["reconcile"],
        ["discard", "ob1_x", "--reason", "no"],
        ["retry", "ob1_x"],
    ],
)
def test_with_a_switch_off_every_verb_refuses_naming_it(rig, cfg, reason, key, argv) -> None:
    rig.configure(cfg)

    seat = [] if argv[0] == "show" else ["--as", OPERATOR]  # a read has no acting seat
    result = bh("outbox", *argv, "--hive", "widget", *seat, "--json")

    assert result.exit_code == 1
    assert doc(result)["reason"] == reason and key in doc(result)["error"]
    assert rig.world.write_requests() == 0


# -- bh-fehna: the stored record without a tracker --------------------------------------------


def test_show_prints_the_stored_draft_state_and_operations_with_no_tracker(rig) -> None:
    report, _issue = rig.report()
    oid = doc(bh("outbox", "draft", report, "--text", "We are on it.", *COMMON))["results"][0][
        "outbox_id"
    ]
    rig.plugin_off()
    reads = rig.world.read_requests()

    shown = bh("outbox", "show", oid, "--hive", "widget", "--json")
    human = bh("outbox", "show", oid, "--hive", "widget")

    assert shown.exit_code == human.exit_code == 0, shown.output
    payload = doc(shown)
    assert payload["record"]["outbox_id"] == oid and payload["record"]["report"] == report
    assert payload["stored"]["text"] == "We are on it."
    assert payload["stored"]["operations"] == [{"kind": "comment.create", "state": "drafted"}]
    assert "plugins.tracker-github.enabled" in payload["stored"]["note"]
    assert "renders" not in payload  # no digest without the adapter's marker codec
    assert "We are on it." in human.stdout and "comment.create: drafted" in human.stdout
    assert "stored draft" in human.stdout
    assert rig.world.read_requests() == reads and rig.world.write_requests() == 0


def test_a_merge_milestone_draft_is_inspectable_before_any_tracker_is_wired(rig) -> None:
    rig.plugin_off()
    rig.configure(external(milestones={"draft_on_merge": True}))
    work_id = rig.store.create_work(WorkDraft("fix the crash", "bug", "dev/x"))
    report = rig.store.create(draft("login is broken", bound=True))
    rig.store.track(report, work_id, actor="dev/x")
    rig.store.set_state(report, "delivery", "pending", reason="link", actor="dev/x")
    merged = intake_services.work_merged(
        rig.cfg, rig.entry, rig.hive, [work_id], MERGE_SHA, "merger/test", lock=rig.lock
    )
    (item,) = merged.outcome.items
    oid = item.drafted

    shown = bh("outbox", "show", oid, "--hive", "widget", "--json")

    assert shown.exit_code == 0, shown.output
    payload = doc(shown)
    assert payload["record"]["event"] == "merged"
    assert payload["stored"]["operations"][0]["state"] == "drafted"
    assert payload["stored"]["text"]  # the fixed public wording of the merged milestone
    assert rig.world.read_requests() == rig.world.write_requests() == 0


def test_show_of_an_unknown_or_malformed_id_refuses_without_a_tracker(rig) -> None:
    rig.plugin_off()

    unknown = bh("outbox", "show", "ob1_" + "a" * 26, "--hive", "widget", "--json")
    malformed = bh("outbox", "show", "w-1", "--hive", "widget", "--json")

    assert (unknown.exit_code, doc(unknown)["reason"]) == (1, "unknown_outbox")
    assert (malformed.exit_code, doc(malformed)["reason"]) == (1, "invalid_outbox_id")


def test_a_refusing_binding_still_shows_the_stored_record_and_says_why(rig) -> None:
    report, _issue = rig.report()
    oid = doc(bh("outbox", "draft", report, "--text", "Hello.", *COMMON))["results"][0]["outbox_id"]
    rig.process, _ = roots({"tracker-github": plugin(enabled=False)}, factory=rig.factory)

    shown = bh("outbox", "show", oid, "--hive", "widget", "--json")
    send = bh("outbox", "send", oid, *COMMON)

    assert shown.exit_code == 0 and "disabled" in doc(shown)["stored"]["note"]
    assert send.exit_code == 1 and "plugins.tracker-github.enabled" in doc(send)["error"]
    assert rig.world.write_requests() == 0


# -- the first-party codec, through the capability, offline ---------------------------------------


def test_the_github_adapters_codec_reaches_root_through_the_capability(
    tmp_path, monkeypatch
) -> None:
    pytest.importorskip("beadhive_tracker_github")
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(run, "_derive_gh_auth_token", lambda: pytest.fail("no gh may run"))
    rig = Rig(tmp_path, monkeypatch)
    cfg = {
        **external(),
        "plugins": {"tracker-github": {"enabled": True, "write": {"comment": {"enabled": True}}}},
    }

    wiring = intake_services.outbox_wiring(cfg, rig.entry, rig.hive)

    identity: OwnIdentity = wiring["identity"]
    line = wiring["marker_line"](OwnMarker(identity.hive, identity.install, "ob1_x"))
    assert line == f"<!-- bh:v1 hive=acme/widget install={identity.install} outbox=ob1_x -->"
    assert isinstance(wiring["tracker"], CommentWriter)
