"""Contract tests for the root shell's named compatibility routes."""

from __future__ import annotations

import subprocess
from pathlib import Path

from beadhive_bd_cli import CliRoutes, public_snapshot_argv


class RecordingBd:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(
        self,
        args,
        cwd,
        actor="",
        capture=False,
        text_input=None,
        **_kwargs,
    ):
        self.calls.append(("run", list(args), cwd, actor, capture, text_input))
        return subprocess.CompletedProcess(args, 0, "", "")

    def json(self, args, cwd, *, strict=False):
        self.calls.append(("json", list(args), cwd, strict))
        return []


def test_named_read_routes_own_every_argv_shape() -> None:
    bd = RecordingBd()
    routes = CliRoutes(bd, Path("/hive"))

    routes.issue_list(label="x", status="closed", all_=True, include_infra=True, limit=0)
    routes.issue_show_raw("bh-1")
    routes.ready_all()
    routes.molecule_children("bh-m")
    routes.ready_explanation_all()
    routes.ready_including_ephemeral_all()
    routes.gate_list(include_resolved=True)
    routes.gate_list_raw(include_resolved=True)
    routes.dependency_list("bh-1", direction="up", type_="relates-to")
    routes.swarm_list(strict=True)
    routes.swarm_status("bh-e", strict=True)
    routes.find_duplicates(threshold=0.7, method="mechanical")

    assert bd.calls == [
        (
            "json",
            [
                "list",
                "--all",
                "--include-infra",
                "--limit",
                "0",
                "--status",
                "closed",
                "--label",
                "x",
            ],
            Path("/hive"),
            False,
        ),  # fmt: skip
        ("json", ["show", "bh-1"], Path("/hive"), False),
        ("json", ["ready", "--limit", "0"], Path("/hive"), False),
        (
            "run",
            ["show", "bh-m", "--children", "--json"],
            Path("/hive"),
            "",
            True,
            None,
        ),  # fmt: skip
        (
            "run",
            ["ready", "--include-ephemeral", "--explain", "--limit", "0", "--json"],
            Path("/hive"),
            "",
            True,
            None,
        ),  # fmt: skip
        (
            "run",
            ["ready", "--include-ephemeral", "--limit", "0", "--json"],
            Path("/hive"),
            "",
            True,
            None,
        ),  # fmt: skip
        ("json", ["gate", "list", "--limit", "0", "--all"], Path("/hive"), False),
        (
            "run",
            ["gate", "list", "--limit", "0", "--all", "--json"],
            Path("/hive"),
            "",
            True,
            None,
        ),  # fmt: skip
        (
            "json",
            ["dep", "list", "bh-1", "--direction", "up", "--type", "relates-to"],
            Path("/hive"),
            False,
        ),  # fmt: skip
        ("json", ["swarm", "list"], Path("/hive"), True),
        ("json", ["swarm", "status", "bh-e"], Path("/hive"), True),
        (
            "json",
            ["find-duplicates", "--threshold", "0.7", "--method", "mechanical"],
            Path("/hive"),
            False,
        ),  # fmt: skip
    ]


def test_named_issue_routes_keep_actor_capture_and_optional_fields() -> None:
    bd = RecordingBd()
    routes = CliRoutes(bd, "/hive")

    routes.issue_claim("bh-1", actor="dev/a")
    routes.issue_release_claim("bh-1", actor="dev/a", capture=True)
    routes.issue_update_fields("bh-1", issue_type="bug", priority="1", actor="dir/a", capture=True)
    routes.issue_assign("bh-1", "dev/b", actor="disp/a", capture=True)
    routes.issue_close(["bh-1", "bh-2"], reason="landed", actor="merge/a", force=True, capture=True)
    routes.issue_close("bh-3", reason="")
    routes.issue_reopen("bh-1")
    routes.issue_note("bh-1", "note")
    routes.issue_set_state("bh-1", "review=pending", reason="submitted", actor="dev/a")
    routes.issue_add_label("bh-1", "wave:x", actor="dev/a")
    routes.issue_remove_label("bh-1", "review:pending", actor="dev/a")
    routes.issue_set_external_ref("bh-1", "gh-7", actor="contrib/a", capture=True)
    routes.issue_set_metadata("bh-1", "git.commits=[]")
    routes.issue_update_metadata("bh-1", '{"proof":1}', capture=True)

    argv = [call[1] for call in bd.calls]
    assert argv == [
        ["update", "bh-1", "--claim"],
        ["update", "bh-1", "--status", "open", "--assignee", ""],
        ["update", "bh-1", "--type", "bug", "--priority", "1"],
        ["assign", "bh-1", "dev/b"],
        ["close", "bh-1", "bh-2", "--reason", "landed", "--force"],
        ["close", "bh-3"],
        ["reopen", "bh-1"],
        ["note", "bh-1", "note"],
        ["set-state", "bh-1", "review=pending", "--reason", "submitted"],
        ["label", "add", "bh-1", "wave:x"],
        ["label", "remove", "bh-1", "review:pending"],
        ["update", "bh-1", "--external-ref", "gh-7"],
        ["update", "bh-1", "--set-metadata", "git.commits=[]"],
        ["update", "bh-1", "--metadata", '{"proof":1}'],
    ]
    assert bd.calls[0] == ("run", ["update", "bh-1", "--claim"], "/hive", "dev/a", False, None)
    assert bd.calls[1] == (
        "run",
        ["update", "bh-1", "--status", "open", "--assignee", ""],
        "/hive",
        "dev/a",
        True,
        None,
    )
    assert bd.calls[2] == (
        "run",
        ["update", "bh-1", "--type", "bug", "--priority", "1"],
        "/hive",
        "dir/a",
        True,
        None,
    )
    assert bd.calls[3] == (
        "run",
        ["assign", "bh-1", "dev/b"],
        "/hive",
        "disp/a",
        True,
        None,
    )
    assert bd.calls[4] == (
        "run",
        ["close", "bh-1", "bh-2", "--reason", "landed", "--force"],
        "/hive",
        "merge/a",
        True,
        None,
    )
    assert bd.calls[8] == (
        "run",
        ["set-state", "bh-1", "review=pending", "--reason", "submitted"],
        "/hive",
        "dev/a",
        False,
        None,
    )
    assert bd.calls[9] == ("run", ["label", "add", "bh-1", "wave:x"], "/hive", "dev/a", False, None)
    assert bd.calls[10] == (
        "run",
        ["label", "remove", "bh-1", "review:pending"],
        "/hive",
        "dev/a",
        False,
        None,
    )
    assert bd.calls[11] == (
        "run",
        ["update", "bh-1", "--external-ref", "gh-7"],
        "/hive",
        "contrib/a",
        True,
        None,
    )
    assert bd.calls[13] == (
        "run",
        ["update", "bh-1", "--metadata", '{"proof":1}'],
        "/hive",
        "",
        True,
        None,
    )


def test_named_gate_dependency_and_coordination_routes() -> None:
    bd = RecordingBd()
    routes = CliRoutes(bd, "/hive")

    routes.gate_create("bh-1", "human", "review abc", actor="dev/a", capture=True)
    routes.gate_resolve("g-1", reason="approved", actor="review/a")
    routes.dependency_add("bh-1", "bh-2", "blocks")
    routes.dependency_remove("bh-1", "bh-2")
    routes.merge_slot_create()
    routes.merge_slot_check()
    routes.merge_slot_acquire("merge/a|pid=1")
    routes.merge_slot_release()

    assert [call[1] for call in bd.calls] == [
        ["gate", "create", "--blocks", "bh-1", "--type", "human", "--reason", "review abc"],
        ["gate", "resolve", "g-1", "--reason", "approved"],
        ["dep", "add", "bh-1", "bh-2", "-t", "blocks"],
        ["dep", "remove", "bh-1", "bh-2"],
        ["merge-slot", "create"],
        ["merge-slot", "check", "--json"],
        ["merge-slot", "acquire", "--holder", "merge/a|pid=1"],
        ["merge-slot", "release"],
    ]
    assert bd.calls[0] == (
        "run",
        ["gate", "create", "--blocks", "bh-1", "--type", "human", "--reason", "review abc"],
        "/hive",
        "dev/a",
        True,
        None,
    )
    assert bd.calls[1] == (
        "run",
        ["gate", "resolve", "g-1", "--reason", "approved"],
        "/hive",
        "review/a",
        False,
        None,
    )
    assert bd.calls[5] == ("run", ["merge-slot", "check", "--json"], "/hive", "", True, None)


def test_named_filing_contributor_and_presentation_routes() -> None:
    bd = RecordingBd()
    routes = CliRoutes(bd, "/hive")

    routes.comments("bh-1")
    routes.comment_add("bh-1", "body", actor="ops/a")
    routes.import_records("records", actor="plan/a")
    routes.github_push_issue("bh-1", actor="contrib/a")
    routes.create_report("Title", "bug", "org:o", description="Details", actor="dev/a")
    routes.presentation_show("bh-1", ["--long"])
    routes.presentation_list(["--all"])

    assert [call[1] for call in bd.calls] == [
        ["comments", "bh-1"],
        ["comments", "add", "bh-1", "body"],
        ["import", "-", "--json"],
        ["github", "push", "--issues", "bh-1"],
        ["--json", "create", "Title", "--type", "bug", "-l", "org:o", "-d", "Details"],
        ["show", "bh-1", "--long"],
        ["list", "--all"],
    ]
    assert bd.calls[0] == ("run", ["comments", "bh-1"], "/hive", "", False, None)
    assert bd.calls[1] == (
        "run",
        ["comments", "add", "bh-1", "body"],
        "/hive",
        "ops/a",
        False,
        None,
    )
    assert bd.calls[2] == (
        "run",
        ["import", "-", "--json"],
        "/hive",
        "plan/a",
        True,
        "records",
    )
    assert bd.calls[3] == (
        "run",
        ["github", "push", "--issues", "bh-1"],
        "/hive",
        "contrib/a",
        True,
        None,
    )
    assert bd.calls[4] == (
        "run",
        ["--json", "create", "Title", "--type", "bug", "-l", "org:o", "-d", "Details"],
        "/hive",
        "dev/a",
        True,
        None,
    )
    assert bd.calls[5] == ("run", ["show", "bh-1", "--long"], "/hive", "", True, None)
    assert bd.calls[6] == ("run", ["list", "--all"], "/hive", "", True, None)


def test_public_snapshot_route_is_narrow_and_shared_with_the_root_contract() -> None:
    bd = RecordingBd()
    output = Path("/out/issues.jsonl")
    assert public_snapshot_argv(output) == ["export", "-o", str(output)]
    CliRoutes(bd, "/hive").public_snapshot_export(output)
    assert bd.calls[-1][1] == public_snapshot_argv(output)


def test_database_ping_route_owns_its_argv_and_timeout() -> None:
    class Timed(RecordingBd):
        def run(self, args, cwd, actor="", capture=False, text_input=None, **kwargs):
            self.calls.append(("run", list(args), cwd, capture, kwargs.get("timeout")))
            return subprocess.CompletedProcess(args, 0, "", "")

    bd = Timed()
    CliRoutes(bd, "/hive").database_ping(timeout=7)
    assert bd.calls == [("run", ["ping", "--json"], "/hive", True, 7)]
