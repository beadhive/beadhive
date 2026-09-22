# ruff: noqa: E501 -- compact frozen signatures are easier to audit one operation per line
"""Canonical declarative operation catalog kernel.

The catalog describes operation identity and transport shape.  It deliberately contains no
callables and performs no dispatch: application handlers remain the behavior truth and are bound
by the CLI/MCP composition roots.  ``scripts/render_operation_catalog.py`` publishes this source
as language-neutral JSON beside the wire schemas.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

CATALOG_SCHEMA_ARTIFACT_ID = "urn:beadhive:wire-schema:operation-catalog:1"
CATALOG_INSTANCE_ARTIFACT_ID = "urn:beadhive:wire-catalog:operations:1"
JSON_VALUE_ARTIFACT_ID = "urn:beadhive:wire-schema:json-value:1"
HIVE_STATUS_ARTIFACT_ID = "urn:beadhive:wire-schema:bh.hive-status:1"
HIVE_SURVEY_ARTIFACT_ID = "urn:beadhive:wire-schema:bh.hive-survey:1"
HIVE_ONBOARD_ARTIFACT_ID = "urn:beadhive:wire-schema:bh.hive-onboard:1"
HIVE_READY_ARTIFACT_ID = "urn:beadhive:wire-schema:bh.hive-ready:1"


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    schema: dict[str, Any]
    required: bool
    privilege: str = "inherited"


@dataclass(frozen=True)
class OperationSpec:
    name: str
    parameters: tuple[ParameterSpec, ...]
    result_schema: str
    kind: str
    privilege: str
    constraints: dict[str, Any]
    surfaces: dict[str, Any]
    alias_of: str | None = None


# This is the authoritative CLI inventory, not a scrape of Typer.  Signatures are intentionally
# compact here and fully materialized in the published JSON artifact.  Adding/removing a Typer
# leaf without changing this declaration fails tests/test_operation_catalog.py.
_CLI_ROWS = """
alerts show|as_json:boolean:o
backfill-complexity|hive:string:o,apply:boolean:o,dry_run:boolean:o,plan_path:string:o,pre_state:string:o,audit:string:o,as_json:boolean:o
backup export|dest:string:o
backup migrate-layout|dry_run:boolean:o,confirm:boolean:o
backup reclaim|root:string:o,hive_id:string:o,dry_run:boolean:o,confirm:boolean:o,force:boolean:o
backup usage|as_json:boolean:o
bd|
beads schema capture|repo:string:o,bd_binary:string:o
checkpoint run|bead:string:r,key:string:r,value:string:r,step:string:o,hive:string:o
config get|key:string:r,scope:string:o
config init|force:boolean:o
config path|
config schema|as_json:boolean:o
config set|key:string:r,value:string:r,as_json:boolean:o,scope:string:o
config show|
config split|dry_run:boolean:o
config unset|key:string:r,scope:string:o
config validate|fix:boolean:o
contrib outbound|hive:string:r,as_json:boolean:o
contrib publish|hive:string:r,bead:string:r,external_ref:string:o,as_seat:string:o
dep auth|name:string:o,check:boolean:o
dep install|name:string:r,version:string:o,yes:boolean:o
dep list|kind:string:o,missing:boolean:o
dep show|name:string:r
doctor|as_json:boolean:o,verbose:boolean:o,seats:boolean:o
dolt down|
dolt logs|
dolt provision|
dolt ps|
dolt sql|
dolt up|
escalate|title:string:r,tool:string:o,as_seat:string:o
git|
harness auth|name:string:o,check:boolean:o
harness install|name:string:r,version:string:o,yes:boolean:o
harness list|
hive add|hive_id:string:r,prefix:string:o,kind:string:o,upstream:string:o
hive archive list|as_json:boolean:o
hive archive prune|older_than:string:o,all_repos:boolean:o,dry_run:boolean:o
hive check-push-fence|hive_dir:string:r
hive classify|provider:string:r,org:string:r,repo:string:r
hive context|hook_json:boolean:o
hive contrib-profile build|hive:string:r
hive contrib-profile show|hive:string:r,as_json:boolean:o
hive disable|feature:string:r,hive_id:string:o
hive enable|feature:string:r,hive_id:string:o
hive hook install|hive_id:string:o
hive hook pre-push|hive_id:string:o
hive hook push-main|rev:string:r,gate:string:o,hive_id:string:o
hive init|furnish:boolean:o,claude:boolean:o,skills:boolean:o,observaloop:boolean:o,agents:boolean:o,opencode:boolean:o,codex:boolean:o,global_grant:boolean:o,force:boolean:o,kind:string:o,prefix:string:o,yes:boolean:o,plugin:array:o,dry_run:boolean:o,skip_check:string:o
hive list|available:boolean:o,as_json:boolean:o,limit:integer:o,cursor:string:o
hive migrate|hive_id:string:o,dry_run:boolean:o
hive migrate-storage|hive_id:string:o,dry_run:boolean:o,confirm:boolean:o,keep_pre_migrate:boolean:o
hive onboard|hive_id:string:r,clone_url:string:o,furnish:boolean:o,claude:boolean:o,skills:boolean:o,observaloop:boolean:o,agents:boolean:o,opencode:boolean:o,codex:boolean:o,global_grant:boolean:o,force:boolean:o,kind:string:o,prefix:string:o,yes:boolean:o,plugin:array:o,dry_run:boolean:o,skip_check:string:o,hub_sync:boolean:o,as_json:boolean:o
hive prefix|provider:string:r,org:string:r,repo:string:r,kind:string:o
hive ready|verbose:boolean:o,as_json:boolean:o
hive reclaim|hive_id:string:r,dry_run:boolean:o,backup:boolean:o,confirm:boolean:o,purge:boolean:o
hive repair|prefix:string:o,node_id:boolean:o,role:boolean:o,server_database:boolean:o,hive:string:o,yes:boolean:o,dry_run:boolean:o
hive retire|hive_id:string:r,dry_run:boolean:o,backup:boolean:o,confirm:boolean:o,purge:boolean:o
hive rm|hive_id:string:r,dry_run:boolean:o,confirm:boolean:o
hive status|hive_id:string:o,as_json:boolean:o
hive survey|available:boolean:o,as_json:boolean:o,sort:string:o
hive sync peers|hive:string:o,all_hives:boolean:o,peer:string:o,strategy:string:o,dry_run:boolean:o,as_json:boolean:o
hive sync remotes|hive:string:o,all_hives:boolean:o,remote:string:o,pull:boolean:o,push:boolean:o,dry_run:boolean:o,force:boolean:o,verbose:boolean:o
hive sync-remote|all_hives:boolean:o,dry_run:boolean:o,verbose:boolean:o
host adopt|hive:string:r,force:boolean:o
host daemon install|as_json:boolean:o
host daemon remove|as_json:boolean:o
host daemon rm|as_json:boolean:o
host daemon serve|listener_host:string:o,listener_port:integer:o,shutdown_budget:number:o
host daemon start|as_json:boolean:o
host daemon status|as_json:boolean:o
host daemon stop|as_json:boolean:o
host dispatch disable|hive:string:o,as_json:boolean:o
host dispatch enable|hive:string:o,as_json:boolean:o,dry_run:boolean:o,seat_binary:string:o
host dispatch logs|hive:string:o,lines:integer:o,as_json:boolean:o
host dispatch run|hive:string:r,passes:integer:o,dry_run:boolean:o,seat_binary:string:o
host dispatch runs|hive:string:o,as_json:boolean:o
host dispatch status|hive:string:o,all_hives:boolean:o,as_json:boolean:o
host identity|dry_run:boolean:o
host init|role:string:r,label:string:o,identity_kind:string:o,identity_value:string:o,remote_only_hive:array:o,force:boolean:o
host lease adopt|hive:string:r,force:boolean:o
host lease release|hive:string:o,all_hives:boolean:o
host list|as_json:boolean:o,lease_hive:string:o
host packup|
host provision|role:string:o,answers:string:o,auto:boolean:o,dry_run:boolean:o,force:boolean:o
host release|hive:string:o,all_hives:boolean:o
host retire|dry_run:boolean:o,backup:boolean:o,confirm:boolean:o,purge:boolean:o
host rm|host_id:string:r,dry_run:boolean:o,confirm:boolean:o,force:boolean:o
host show|host_id:string:r,as_json:boolean:o
hq bd|
hq clone|auto:boolean:o
hq init|dry_run:boolean:o,auto:boolean:o,create:boolean:o
hq intake|
hq prune-aggregate|dry_run:boolean:o,confirm:boolean:o
hq push|dry_run:boolean:o
hq restore|list_only:boolean:o,from_dir:string:o,level:string:o,dry_run:boolean:o,confirm:boolean:o
hq status|as_json:boolean:o
hub|
label allowed|
label docs|
label report|
label sync|
label validate|enforce:boolean:o,advisory:boolean:o
mcp install|scope:string:o
mcp serve|
otel disable|
otel down|
otel enable|
otel endpoint|url:string:r
otel logs|
otel ps|
otel up|
plan adopt|beads:string:r,out:string:o,hive:string:o
plan approve|epic:string:r,hive:string:o
plan check|ref:string:r,as_json:boolean:o,hive:string:o
plan file|spec:string:r,dry_run:boolean:o,save:string:o,hive:string:o
plan repair|epic:string:r,hive:string:o
plan show|ref:string:r,hive:string:o
plan status|epic:string:o,hive:string:o
plan verify|epic:string:r,hive:string:o
plugin git-workspace groups|
plugin herdr add|local:string:o,managed_ref:string:o,yes:boolean:o,dry_run:boolean:o,as_json:boolean:o,operation_id:string:o
plugin herdr attach|target:string:r,session:string:o,as_json:boolean:o,operation_id:string:o
plugin herdr dispatch|target:string:r,prompt:string:o,from_stdin:boolean:o,prompt_file:string:o,session:string:o,as_json:boolean:o,operation_id:string:o
plugin herdr integrate|kind:string:r
plugin herdr launch|bead:string:o,hive:string:o,kind:string:o,session:string:o,as_:string:o,adopt_expired:boolean:o,direction:string:o,focus:boolean:o,as_json:boolean:o,profile_json:string:o,recover_after_pane:string:o,session_checkout:string:o
plugin herdr ps|session:string:o,as_json:boolean:o,operation_id:string:o
plugin herdr reap|target:string:r,session:string:o,pane:string:o,as_json:boolean:o,operation_id:string:o,generation:integer:o,launch_spec_digest:string:o
plugin herdr spawn|hive:string:r,bead:string:r,kind:string:r,session:string:o,as_json:boolean:o,operation_id:string:o
plugin herdr status|session:string:o,as_json:boolean:o,operation_id:string:o
plugin herdr view agent|target:string:r,session:string:o,as_json:boolean:o
plugin herdr view bead|hive:string:r,bead:string:r,session:string:o,as_json:boolean:o
plugin herdr view crew|hive:string:r,session:string:o,limit:integer:o,cursor:string:o,as_json:boolean:o
plugin herdr view deck|hive:string:r,session:string:o,limit:integer:o,cursor:string:o,width:integer:o,as_json:boolean:o
plugin herdr view layout|hive:string:o,session:string:o,context_json:string:o,as_json:boolean:o
plugin herdr view picker|session:string:o,limit:integer:o,cursor:string:o,as_json:boolean:o
plugin herdr view presentation|hive:string:r,session:string:o,ttl_ms:integer:o,as_json:boolean:o
plugin herdr view stream|hive:string:r,session:string:o,since:string:o,limit:integer:o,width:integer:o
plugin herdr watch|target:string:r,session:string:o,timeout:number:o,as_json:boolean:o,operation_id:string:o
plugin hitch up|target:string:r,profile:string:r,workspace:string:o,task:string:o,detached:boolean:o,role_:string:o,explain:boolean:o
plugin observaloop down|
plugin observaloop status|
plugin orca fix-settings|
plugin orca sync|dry_run:boolean:o
plugin repowise index|all_hives:boolean:o
plugin repowise status|
release attest|rev:string:o,gate:string:o,hive:string:o,background:boolean:o,if_needed:boolean:o
release await|rev:string:o,gate:string:o,hive:string:o,timeout:integer:o,poll:number:o,if_pending:boolean:o
release order|hive:string:o
release pending|rev:string:o,gate:string:o,hive:string:o
release preflight|rev:string:o,gate:string:o,hive:string:o
release preview|rev:string:o,gate:string:o,hive:string:o,tag:string:o,remote:string:o,next_:boolean:o
release recover|rev:string:o,tag:string:o,remote:string:o,branch:string:o,hive:string:o,apply:boolean:o,dry_run:boolean:o
report|hive:string:r,title:string:r,report_type:string:o,as_actor:string:o,description:string:o
report-target|as_json:boolean:o
role|name:string:o,harness:string:o,no_hitch:boolean:o,baml_required:boolean:o,seats:boolean:o,bead:string:o,hive:string:o,task:string:o,detached:boolean:o,explain:boolean:o,available_seat:array:o,current_seat:string:o,model:string:o,effort:string:o
setup check|as_json:boolean:o
setup guide|wizard:boolean:o,handoff:boolean:o,force:boolean:o,dry_run:boolean:o
setup show|
setup toolchain|dry_run:boolean:o
statusline|
stream|scope:string:r,format_:string:o,hive:string:o,since:string:o
sync|
toolchain exec|hive:string:o
toolchain list|hive:string:o,as_json:boolean:o
toolchain show|name:string:r,hive:string:o,as_json:boolean:o
work abandon|bead:string:r,hive:string:o,rm:boolean:o
work accept|bead:string:r,type_:string:o,priority:string:o,as_:string:o,hive:string:o
work approve|bead:string:r,as_:string:o,hive:string:o
work artifacts-uploaded|run_id:string:r,hive:string:o
work assign|bead:string:r,to:string:r,as_:string:o,hive:string:o,preview:boolean:o,as_json:boolean:o
work bounce|bead:string:r,message:string:o,as_:string:o,hive:string:o
work brief|bead:string:r,hive:string:o
work check|bead:string:r,hive:string:o
work claim|bead:string:o,as_:string:o,group:string:o,collapse:string:o,hive:string:o,preview:boolean:o,as_json:boolean:o
work finish|epic:string:r,hive:string:o
work intake|hive:string:o,source:string:o,as_json:boolean:o,no_dupes:boolean:o
work issue|bead:string:r,hive:string:o
work land|bead:string:r,hive:string:o
work list|hive:string:o
work loop|epic:string:r,as_:string:o,hive:string:o,passes:integer:o,as_json:boolean:o,dry_run:boolean:o,seat_binary:string:o,harness:string:o,baml_required:boolean:o
work merge|bead:string:o,hive:string:o,rm:boolean:o,molecule:boolean:o,group:string:o
work next|as_:string:o,hive:string:o,as_json:boolean:o,epic:string:o
work promote|bead:string:r,as_:string:o,hive:string:o
work readiness|molecule:string:r,hive:string:o,as_json:boolean:o
work ready|hive:string:o
work refine|bead:string:r,plan:string:o,autosquash:boolean:o,since:string:o,dry_run:boolean:o,hive:string:o
work reject|bead:string:r,reason:string:r,as_:string:o,hive:string:o
work reroute|bead:string:r,to:string:o,super_:string:o,as_:string:o,hive:string:o
work resume|bead:string:r,as_:string:o,hive:string:o
work review|bead:string:r,run_validate:boolean:o,demo:boolean:o,fresh:boolean:o,view:array:o,hive:string:o
work schedule|epic:string:r,hive:string:o,as_json:boolean:o
work show|bead:string:r,view:array:o,as_json:boolean:o,hive:string:o
work start|epic:string:r,as_:string:o,hive:string:o
work submit|bead:string:o,as_:string:o,hive:string:o,group:string:o
worktree add|hive:string:o,bead:string:o,branch:string:o,dry_run:boolean:o,as_json:boolean:o
worktree init|path:string:r
worktree list|as_json:boolean:o,hive:string:o,state:string:o,limit:integer:o,cursor:string:o
worktree mark-abandoned|ref:string:r,reason:string:r,retained_for:string:o,superseded_by:string:o,hive:string:o
worktree mark-landed|ref:string:r,hive:string:o
worktree path|ref:string:o,bead:string:o,hive:string:o
worktree prune|hive:string:o
worktree rm|ref:string:o,bead:string:o,hive:string:o,force:boolean:o,as_json:boolean:o
worktree status|hive:string:o,as_json:boolean:o
""".strip()

_READ_PATHS = {
    "alerts show",
    "backup usage",
    "config get",
    "config path",
    "config schema",
    "config show",
    "dep list",
    "dep show",
    "doctor",
    "harness list",
    "hive archive list",
    "hive classify",
    "hive contrib-profile show",
    "hive list",
    "hive prefix",
    "hive ready",
    "hive status",
    "hive survey",
    "host daemon status",
    "host dispatch logs",
    "host dispatch runs",
    "host dispatch status",
    "host list",
    "host show",
    "hq intake",
    "hq status",
    "label allowed",
    "label validate",
    "plan check",
    "plan show",
    "plan status",
    "plugin git-workspace groups",
    "plugin herdr ps",
    "plugin herdr status",
    "plugin herdr view agent",
    "plugin herdr view bead",
    "plugin herdr view crew",
    "plugin herdr view deck",
    "plugin herdr view layout",
    "plugin herdr view picker",
    "plugin herdr view presentation",
    "plugin herdr view stream",
    "plugin observaloop status",
    "plugin repowise status",
    "release order",
    "release pending",
    "release preflight",
    "release preview",
    "report-target",
    "setup check",
    "setup show",
    "statusline",
    "stream",
    "toolchain list",
    "toolchain show",
    "work brief",
    "work intake",
    "work issue",
    "work list",
    "work next",
    "work readiness",
    "work ready",
    "work review",
    "work schedule",
    "work show",
    "worktree list",
    "worktree path",
    "worktree status",
}
_HIDDEN_PATHS = {
    "hive check-push-fence",
    "hive context",
    "hive sync-remote",
    "host adopt",
    "host daemon remove",
    "host dispatch run",
    "host packup",
    "host release",
    "statusline",
}

# Every CLI parent is declared here so child projections can record EFFECTIVE presentation
# metadata (a visible leaf beneath a hidden parent is still hidden).  ``panel`` is local; the
# published document also materializes the nearest inherited panel.
_CLI_PARENT_ROWS = """
alerts|false|Fleet / HQ
backup|false|Admin / infra
beads|false|Admin / infra
beads schema|false|
checkpoint|false|Integration plane
config|false|Admin / infra
contrib|false|Integration plane
dep|false|Admin / infra
dolt|true|
harness|true|
hive|false|Hive
hive archive|false|
hive contrib-profile|false|
hive hook|false|
hive sync|false|Hive
host|false|Fleet / HQ
host daemon|false|
host dispatch|false|
host lease|false|
hq|false|Fleet / HQ
label|false|Hive
mcp|false|Admin / infra
otel|true|
plan|false|Planning plane
plugin|false|Admin / infra
plugin git-workspace|false|
plugin herdr|false|
plugin herdr view|false|
plugin hitch|false|
plugin observaloop|false|
plugin orca|false|
plugin repowise|false|
release|false|Integration plane
setup|false|Admin / infra
toolchain|false|Hive
work|false|Integration plane
worktree|false|Integration plane
wt|true|
""".strip()

# CLI paths that are presentations of an already-named operation.  Alias projections retain
# their own effective-hidden bit, signature and forced defaults; they never create a second
# operation identity.
_CLI_ALIAS_TARGETS: dict[str, tuple[str, dict[str, Any], str]] = {
    "harness auth": ("dep.auth", {}, "hidden harness-filter alias onto dep auth"),
    "harness install": ("dep.install", {}, "hidden harness-filter alias onto dep install"),
    "harness list": ("dep.list", {"kind": "harness"}, "hidden harness-filter alias onto dep list"),
    "hive sync-remote": ("hive.sync.remotes", {"push": True}, "deprecated push-only alias"),
    "host adopt": ("host.lease.adopt", {}, "hidden flat compatibility alias"),
    "host daemon remove": (
        "host.daemon.rm",
        {},
        "hidden compatibility alias for the canonical short remove command",
    ),
    "host packup": (
        "host.lease.release",
        {"all_hives": True},
        "hidden release-all compatibility alias",
    ),
    "host release": ("host.lease.release", {}, "hidden flat compatibility alias"),
    **{
        f"wt {verb}": (f"worktree.{verb}", {}, "hidden short group alias")
        for verb in (
            "add",
            "init",
            "list",
            "mark-abandoned",
            "mark-landed",
            "path",
            "prune",
            "rm",
            "status",
        )
    },
}

_SEMANTIC_ALIASES = {
    "work.finish": "work.merge",
    "work.start": "work.claim",
}

_CLI_PARAMETER_MAP = {
    "backup reclaim": {"hive_id": "hive"},
    "contrib publish": {"as_seat": "as_"},
    "escalate": {"as_seat": "as_"},
    "hive hook push-main": {"hive_id": "hive"},
    "hive status": {"hive_id": "hive"},
    "host dispatch logs": {"lines": "limit"},
    "plugin hitch up": {"explain": "dry_run"},
    "report": {"report_type": "type_", "as_actor": "as_"},
    "role": {"explain": "dry_run"},
}

_PASSTHROUGH_PATHS = {
    "bd",
    "checkpoint run",
    "git",
    "hq bd",
    "hq intake",
    "hub",
    "toolchain exec",
    "work issue",
    "work list",
    "work ready",
}

# Prompt capability is projection policy, not a guess from option names.  Each source seam is
# named so the completeness test can derive the live ``typer.prompt``/``typer.confirm`` calls
# from the AST and fail when a new prompt is added without a non-interactive route.  Aliases carry
# their own declaration because a generator must be able to render them without consulting the
# target command.
_CLI_PROMPT_POLICY: dict[str, dict[str, Any]] = {
    "doctor": {
        "guard_parameters": [],
        "guard_conditions": ["stdin-not-tty", "mcp-uses-pure-doctor-payload"],
        "prompt_seams": ["beadhive.doctor._offer_workspace_init:typer.confirm"],
        "reason": (
            "an unseeded internal workspace is offered only on a TTY; JSON and headless use "
            "never prompt"
        ),
    },
    "dep install": {
        "guard_parameters": ["yes"],
        "guard_conditions": [],
        "prompt_seams": ["beadhive.harness.install:typer.confirm"],
        "reason": "proprietary installs require consent unless --yes selects the headless route",
    },
    "harness install": {
        "guard_parameters": ["yes"],
        "guard_conditions": [],
        "prompt_seams": ["beadhive.harness.install:typer.confirm"],
        "reason": "alias preserves dep install's proprietary-license consent and --yes guard",
    },
    "escalate": {
        "guard_parameters": [],
        "guard_conditions": ["stdin-not-tty"],
        "prompt_seams": ["beadhive.escalate._offer_hq_init:typer.confirm"],
        "reason": "missing HQ may offer initialization only on a TTY; headless use never prompts",
    },
    "host provision": {
        "guard_parameters": ["auto", "answers", "dry_run"],
        "guard_conditions": ["stdin-not-tty"],
        "prompt_seams": ["beadhive.hq._confirm_remote:typer.prompt"],
        "reason": "HQ remote confirmation is bypassed by --auto, --answers, --dry-run, or non-TTY input",
    },
    "hq clone": {
        "guard_parameters": ["auto"],
        "guard_conditions": ["stdin-not-tty"],
        "prompt_seams": ["beadhive.hq._confirm_remote:typer.prompt"],
        "reason": "HQ remote confirmation is bypassed by --auto or non-TTY input",
    },
    "hq init": {
        "guard_parameters": ["auto", "dry_run"],
        "guard_conditions": ["stdin-not-tty"],
        "prompt_seams": [
            "beadhive.hq._confirm_remote:typer.prompt",
            "beadhive.hq._should_create:typer.confirm",
        ],
        "reason": "HQ remote selection/creation prompts are bypassed by --auto, --dry-run, or non-TTY input",
    },
    "setup guide": {
        "guard_parameters": ["handoff", "dry_run"],
        "guard_conditions": ["stdin-not-tty-and-wizard-false"],
        "prompt_seams": [
            "beadhive.setup_guide.run_guide:typer.confirm",
            "beadhive.setup_guide.wizard:typer.prompt",
        ],
        "reason": "the guided walk prompts; --handoff/--dry-run or non-TTY default mode is non-interactive",
    },
}

_MCP_NOTIFICATION_URIS = {
    "bd.create": [
        "beadhive://work/ready",
        "beadhive://work/intake",
        "beadhive://alerts",
    ],
    "config.set": [
        "beadhive://config",
        "beadhive://config/{key}",
        "beadhive://alerts",
    ],
    "hive.add": [
        "beadhive://hive/status",
        "beadhive://hive/list",
        "beadhive://hive/survey",
        "beadhive://alerts",
    ],
    "hive.onboard": [
        "beadhive://hive/status",
        "beadhive://hive/list",
        "beadhive://hive/survey",
        "beadhive://alerts",
    ],
    "plan.file": [
        "beadhive://work/ready",
        "beadhive://plan/list",
        "beadhive://alerts",
    ],
}

_MCP_COARSE_GRAINED = {
    "bd.create": "one MCP call batch-creates multiple bead records",
    "hive.onboard": "one MCP call composes clone, initialization, registration, and hub sync",
    "toolchain.exec": "one MCP call carries an opaque downstream argv vector",
}
# A composite is a deliberately hand-authored MCP handler which coordinates multiple canonical
# operations.  Keep this separate from merely coarse batch/opaque tools: those still implement one
# operation, whereas these entries sanction a wider agent-facing unit over catalog operations.
# Every component is validated in ``operations()`` so a rename cannot leave a dangling composite.
_MCP_COMPOSITES = {
    "hive.onboard": ("hive.init", "sync"),
}
_SECRET_PATHS = {"dep auth", "harness auth"}
_HQ_READS = {"hq intake", "hq status"}
_OVERRIDE_NAMES = {"force", "yes", "skip_check"}

_MCP_RESOURCE_DIVERGENCE = {
    "alerts.show": "alerts is the established singleton collection resource",
    "config.get": "a dotted config key is addressed directly under the config resource",
    "config.show": "the root config resource is the established collection representation",
    "doctor": "doctor is the established singleton diagnostic resource",
    "label.validate": "the resource names the validation result rather than the CLI verb",
    "work.intake-dupes": "the cheap duplicate view is nested under the intake collection",
    "plan.status": "the established template identifies a plan directly by reference",
}

# Explicit MCP allowlist.  A missing mapping means absent; there is no denylist.  Parameter lists
# are the transport signature and intentionally omit Context plus privileged CLI override flags.
_MCP_TOOLS = {
    "plan.check": ("plan_check", ("spec",)),
    "plan.file": ("plan_file", ("spec", "hive", "dry_run")),
    "work.refine": (
        "work_refine",
        ("bead", "squash_plan", "autosquash", "since", "hive", "dry_run"),
    ),
    "bd.create": ("bd_create", ("issues", "hive")),
    "hive.list": ("hive_list", ()),
    "config.set": ("config_set", ("key", "value", "type")),
    "hive.add": ("hive_add", ("provider", "org", "repo", "prefix", "kind", "upstream")),
    "hive.onboard": (
        "hive_onboard",
        ("provider", "org", "repo", "clone_url", "furnish", "claude", "skills", "observaloop"),
    ),
    "hive.status": ("hive_status", ()),
    "toolchain.exec": ("toolchain_exec", ("argv", "hive")),
}
_MCP_RESOURCES = {
    "probe.health": ("beadhive://probe/health", ()),
    "config.show": ("beadhive://config", ()),
    "config.get": ("beadhive://config/{key}", ("key",)),
    "doctor": ("beadhive://doctor", ()),
    "alerts.show": ("beadhive://alerts", ()),
    "hive.list": ("beadhive://hive/list", ()),
    "hive.status": ("beadhive://hive/status", ()),
    "hive.survey": ("beadhive://hive/survey", ()),
    "label.validate": ("beadhive://label/validation", ()),
    "worktree.list": ("beadhive://worktree/list", ()),
    "work.ready": ("beadhive://work/ready", ()),
    "work.intake": ("beadhive://work/intake", ()),
    "work.intake-dupes": ("beadhive://work/intake/dupes", ()),
    "work.issue": ("beadhive://work/issue/{id}", ("id",)),
    "work.show": ("beadhive://work/show/{id}", ("id",)),
    "work.schedule": ("beadhive://work/schedule/{epic}", ("epic",)),
    "plan.list": ("beadhive://plan/list", ()),
    "plan.status": ("beadhive://plan/{ref}", ("ref",)),
    "hq.intake": ("beadhive://hq/intake", ()),
    "toolchain.list": ("beadhive://toolchain/list", ()),
    "toolchain.show": ("beadhive://toolchain/show/{name}", ("name",)),
}


def _operation_name(path: str) -> str:
    return path.replace(" ", ".")


def _parse_parameters(encoded: str) -> list[ParameterSpec]:
    if not encoded:
        return []
    parameters = []
    for token in encoded.split(","):
        name, type_, required = token.split(":")
        schema: dict[str, Any] = {"type": type_}
        if type_ == "array":
            schema["items"] = {}
        parameters.append(
            ParameterSpec(
                name=name,
                schema=schema,
                required=required == "r",
                privilege="privileged-override" if name in _OVERRIDE_NAMES else "inherited",
            )
        )
    return parameters


def _extra_parameter(name: str) -> ParameterSpec:
    type_ = (
        "array"
        if name in {"argv", "issues"}
        else "object"
        if name in {"spec", "squash_plan"}
        else "boolean"
        if name in {"dry_run", "autosquash", "furnish", "claude", "skills", "observaloop"}
        else "string"
    )
    required = name in {"spec", "bead", "issues", "key", "value", "provider", "org", "repo", "argv"}
    schema: dict[str, Any] = {"type": type_}
    if type_ == "array":
        schema["items"] = {}
    return ParameterSpec(name, schema, required)


def _raw_cli_rows() -> dict[str, list[ParameterSpec]]:
    rows: dict[str, list[ParameterSpec]] = {}
    for row in _CLI_ROWS.splitlines():
        path, encoded = row.split("|", 1)
        rows[path] = _parse_parameters(encoded)
    return rows


def _canonical_parameters(path: str, parameters: list[ParameterSpec]) -> list[ParameterSpec]:
    names = _CLI_PARAMETER_MAP.get(path, {})
    return [
        replace(parameter, name=names.get(parameter.name, parameter.name))
        for parameter in parameters
    ]


def _cli_rows() -> dict[str, tuple[str, list[ParameterSpec]]]:
    return {
        _operation_name(path): (path, _canonical_parameters(path, parameters))
        for path, parameters in _raw_cli_rows().items()
        if path not in _CLI_ALIAS_TARGETS
    }


def _parent_rows() -> dict[str, dict[str, Any]]:
    parents: dict[str, dict[str, Any]] = {}
    for row in _CLI_PARENT_ROWS.splitlines():
        path, hidden, panel = row.split("|", 2)
        parents[path] = {"declared_hidden": hidden == "true", "panel": panel or None}
    return parents


def _effective_parent_value(path: str, key: str) -> Any:
    parents = _parent_rows()
    parts = path.split()
    value = False if key == "declared_hidden" else None
    for length in range(1, len(parts) + 1):
        parent = parents.get(" ".join(parts[:length]))
        if not parent:
            continue
        if key == "declared_hidden":
            value = value or parent[key]
        elif parent[key] is not None:
            value = parent[key]
    return value


def cli_parents() -> list[dict[str, Any]]:
    """Return declared and effective CLI parent metadata in stable path order."""
    result = []
    for path, metadata in sorted(_parent_rows().items()):
        alias_of = "worktree" if path == "wt" else "dep" if path == "harness" else None
        result.append(
            {
                "path": path,
                "declared_hidden": metadata["declared_hidden"],
                "effective_hidden": bool(_effective_parent_value(path, "declared_hidden")),
                "panel": metadata["panel"],
                "effective_panel": _effective_parent_value(path, "panel"),
                "alias_of": alias_of,
                "divergence": (
                    "hidden short group alias"
                    if path == "wt"
                    else "hidden harness-filter compatibility group"
                    if path == "harness"
                    else None
                ),
            }
        )
    return result


def _interactivity(path: str) -> dict[str, Any]:
    policy = _CLI_PROMPT_POLICY.get(path)
    if policy is None:
        return {
            "mode": "none",
            "guard_parameters": [],
            "guard_conditions": [],
            "prompt_seams": [],
            "reason": None,
        }
    return {"mode": "guarded-prompt", **policy}


def _cli_granularity(path: str, *, alias_reason: str | None = None) -> dict[str, Any]:
    if alias_reason is not None:
        return {"mode": "divergent", "reason": alias_reason}
    if path in _PASSTHROUGH_PATHS:
        return {
            "mode": "coarse",
            "reason": "opaque downstream argv is intentionally broader than one typed operation",
        }
    return {"mode": "fine", "reason": None}


def _cli_progress(path: str, parameters: list[ParameterSpec]) -> dict[str, Any]:
    if any(parameter.name == "as_json" for parameter in parameters):
        return {
            "mode": "structured",
            "notification_uris": [],
            "reason": "--json carries machine-readable status/result data instead of a separate progress channel",
        }
    if path in _READ_PATHS:
        return {
            "mode": "none",
            "notification_uris": [],
            "reason": "read result is emitted directly; there is no distinct progress stream",
        }
    return {
        "mode": "stdout",
        "notification_uris": [],
        "reason": "CLI reports human-oriented progress on stdout/stderr",
    }


def _mcp_granularity(name: str, *, tool: bool, resource: bool) -> dict[str, Any]:
    if tool and resource:
        return {
            "mode": "divergent",
            "reason": "one read operation is dual-projected as a tool and resource for legacy clients",
        }
    if reason := _MCP_COARSE_GRAINED.get(name):
        return {"mode": "coarse", "reason": reason}
    return {"mode": "fine", "reason": None}


def _mcp_progress(name: str) -> dict[str, Any]:
    notification_uris = _MCP_NOTIFICATION_URIS.get(name, [])
    if notification_uris:
        return {
            "mode": "notifications",
            "notification_uris": notification_uris,
            "reason": "MCP reports completed mutations through resources/updated notifications",
        }
    return {
        "mode": "none",
        "notification_uris": [],
        "reason": "MCP returns one structured result and emits no progress notification",
    }


def _cli_projection(path: str, canonical_parameters: list[ParameterSpec]) -> dict[str, Any]:
    actual_parameters = _raw_cli_rows()[path]
    projection: dict[str, Any] = {
        "path": path,
        "parameters": [parameter.name for parameter in actual_parameters],
        "parameter_map": _CLI_PARAMETER_MAP.get(path, {}),
        "declared_hidden": path in _HIDDEN_PATHS,
        "effective_hidden": bool(
            path in _HIDDEN_PATHS or _effective_parent_value(path, "declared_hidden")
        ),
        "granularity": _cli_granularity(path),
        "progress": _cli_progress(path, canonical_parameters),
        "interactivity": _interactivity(path),
        "aliases": [],
    }
    if path in _PASSTHROUGH_PATHS:
        projection["passthrough"] = {
            "mode": "opaque-argv",
            "allow_extra_args": True,
            "ignore_unknown_options": True,
        }
    canonical_name = _operation_name(path)
    for alias_path, (target, defaults, reason) in sorted(_CLI_ALIAS_TARGETS.items()):
        if target != canonical_name:
            continue
        source_path = path if alias_path.startswith("wt ") else alias_path
        alias_parameters = _raw_cli_rows()[source_path]
        projection["aliases"].append(
            {
                "path": alias_path,
                "parameters": [parameter.name for parameter in alias_parameters],
                "parameter_map": _CLI_PARAMETER_MAP.get(alias_path, {}),
                "declared_hidden": alias_path in _HIDDEN_PATHS,
                "effective_hidden": bool(
                    alias_path in _HIDDEN_PATHS
                    or _effective_parent_value(alias_path, "declared_hidden")
                ),
                "parameter_defaults": defaults,
                "divergence": reason,
                "granularity": _cli_granularity(alias_path, alias_reason=reason),
                "progress": _cli_progress(path, canonical_parameters),
                "interactivity": _interactivity(alias_path),
            }
        )
    # Requiring the canonical signature at this boundary prevents a path-only projection from
    # being constructed independently of its operation declaration.
    assert canonical_parameters is not None
    return projection


def _result_schema(name: str) -> str:
    if name == "hive.onboard":
        return HIVE_ONBOARD_ARTIFACT_ID
    if name == "hive.ready":
        return HIVE_READY_ARTIFACT_ID
    if name == "hive.status":
        return HIVE_STATUS_ARTIFACT_ID
    if name == "hive.survey":
        return HIVE_SURVEY_ARTIFACT_ID
    return JSON_VALUE_ARTIFACT_ID


def operations() -> tuple[OperationSpec, ...]:
    rows = _cli_rows()
    names = set(rows) | set(_MCP_TOOLS) | set(_MCP_RESOURCES)
    result = []
    for name in sorted(names):
        path, parameters = rows.get(name, ("", []))
        param_by_name = {parameter.name: parameter for parameter in parameters}
        for _surface_name, surface_params in filter(
            None, (_MCP_TOOLS.get(name), _MCP_RESOURCES.get(name))
        ):
            for parameter_name in surface_params:
                param_by_name.setdefault(parameter_name, _extra_parameter(parameter_name))
        parameters = list(param_by_name.values())
        hq_write = name.startswith("hq.") and name not in {_operation_name(p) for p in _HQ_READS}
        secret_material = path in _SECRET_PATHS
        privilege = (
            "privileged"
            if hq_write or secret_material
            else (
                "unprivileged-read"
                if path in _READ_PATHS or name in _MCP_RESOURCES
                else "ordinary-mutation"
            )
        )
        kind = "read-resource" if name in _MCP_RESOURCES or path in _READ_PATHS else "action"
        cli_projection: dict[str, Any] | None = None
        if path:
            cli_projection = _cli_projection(path, parameters)
        mcp_projection: dict[str, Any] | None = None
        tool_spec = _MCP_TOOLS.get(name)
        resource_spec = _MCP_RESOURCES.get(name)
        if tool_spec or resource_spec:
            mcp_projection = {
                "allowlisted": True,
                "granularity": _mcp_granularity(
                    name, tool=tool_spec is not None, resource=resource_spec is not None
                ),
                "progress": _mcp_progress(name),
                "interactivity": {
                    "mode": "none",
                    "guard_parameters": [],
                    "guard_conditions": [],
                    "prompt_seams": [],
                    "reason": None,
                },
            }
            if tool_spec:
                mcp_projection["tool"] = tool_spec[0]
                mcp_projection["tool_parameters"] = list(tool_spec[1])
                if name in _MCP_COMPOSITES:
                    mcp_projection["composes"] = list(_MCP_COMPOSITES[name])
            if resource_spec:
                mcp_projection["resource"] = resource_spec[0]
                mcp_projection["resource_parameters"] = list(resource_spec[1])
                if name in _MCP_RESOURCE_DIVERGENCE:
                    mcp_projection["divergence"] = _MCP_RESOURCE_DIVERGENCE[name]
            if tool_spec and kind == "read-resource":
                mcp_projection["divergence"] = (
                    "legacy dual exposure retained for tool-only MCP clients"
                )
        result.append(
            OperationSpec(
                name=name,
                parameters=tuple(parameters),
                result_schema=_result_schema(name),
                kind=kind,
                privilege=privilege,
                constraints={
                    # Intrinsic operation constraint: an MCP-projected operation remains pure
                    # only when its CLI adapter wraps a separately pure operation (doctor renders
                    # doctor_payload first, while its resource calls doctor_payload directly).
                    # All other prompt-capable operations stay intrinsically interactive so a
                    # malformed MCP allowlist mutation continues to fail closed.
                    "interactive": path in _CLI_PROMPT_POLICY and path != "doctor",
                    "hq_write": hq_write,
                    "secret_material": secret_material,
                    "override_parameters": [
                        p.name for p in parameters if p.name in _OVERRIDE_NAMES
                    ],
                },
                surfaces={
                    **({"cli": cli_projection} if cli_projection else {}),
                    **({"mcp": mcp_projection} if mcp_projection else {}),
                },
                alias_of=_SEMANTIC_ALIASES.get(name),
            )
        )
    operation_names = {operation.name for operation in result}
    operations_by_name = {operation.name: operation for operation in result}
    for composite, components in _MCP_COMPOSITES.items():
        if composite not in _MCP_TOOLS:
            raise ValueError(f"MCP composite {composite!r} is not an allowlisted tool")
        missing = set(components) - operation_names
        if missing:
            raise ValueError(
                f"MCP composite {composite!r} references unknown operations: {sorted(missing)}"
            )
        unsafe = []
        for component in components:
            operation = operations_by_name[component]
            if operation.privilege == "privileged" or any(
                operation.constraints[key] for key in ("hq_write", "secret_material", "interactive")
            ):
                unsafe.append(component)
        if unsafe:
            raise ValueError(
                f"MCP composite {composite!r} bypasses component policy: {sorted(unsafe)}"
            )
    return tuple(result)


class MCPProjectionError(ValueError):
    """The requested operation is not safe and declared for the requested MCP surface."""


def _validated_mcp_projection(operation: OperationSpec) -> dict[str, Any]:
    """Return one operation's MCP projection after enforcing the catalog safety policy.

    This is the transport generator's only catalog entry point.  It intentionally checks positive
    permission instead of deriving exposure from all non-privileged operations: absence from the
    MCP projection is denial.  The repeated constraint checks make a malformed/monkeypatched
    catalog fail closed before FastMCP registers a callable.
    """
    name = operation.name
    projection = operation.surfaces.get("mcp")
    if not projection or projection.get("allowlisted") is not True:
        raise MCPProjectionError(f"operation {name!r} is not allowlisted for MCP")
    unsafe_constraints = [
        key for key in ("hq_write", "secret_material", "interactive") if operation.constraints[key]
    ]
    if operation.privilege == "privileged" or unsafe_constraints:
        detail = ", ".join(unsafe_constraints) or operation.privilege
        raise MCPProjectionError(f"operation {name!r} is privileged for MCP: {detail}")
    parameter_privilege = {
        parameter.name: parameter.privilege for parameter in operation.parameters
    }
    projected_parameters = set(projection.get("tool_parameters", ())) | set(
        projection.get("resource_parameters", ())
    )
    privileged_parameters = sorted(
        parameter
        for parameter in projected_parameters
        if parameter_privilege.get(parameter) == "privileged-override"
        or parameter in operation.constraints["override_parameters"]
    )
    if privileged_parameters:
        raise MCPProjectionError(
            f"operation {name!r} projects privileged MCP parameters: {privileged_parameters}"
        )
    return projection


def _allowlisted_mcp_operation(name: str) -> tuple[OperationSpec, dict[str, Any]]:
    operation = next((row for row in operations() if row.name == name), None)
    if operation is None:
        raise MCPProjectionError(f"unknown catalog operation {name!r}")
    return operation, _validated_mcp_projection(operation)


def _tool_projection(operation: OperationSpec, projection: dict[str, Any]):
    tool_name = projection.get("tool")
    if not tool_name:
        raise MCPProjectionError(f"operation {operation.name!r} has no MCP tool projection")
    if operation.kind != "action" and not projection.get("divergence"):
        raise MCPProjectionError(
            f"read operation {operation.name!r} needs a declared tool divergence"
        )
    return str(tool_name), tuple(projection.get("tool_parameters", ()))


def _resource_projection(operation: OperationSpec, projection: dict[str, Any]):
    uri = projection.get("resource")
    if not uri:
        raise MCPProjectionError(f"operation {operation.name!r} has no MCP resource projection")
    if operation.kind != "read-resource":
        raise MCPProjectionError(f"action {operation.name!r} cannot project as an MCP resource")
    return str(uri), tuple(projection.get("resource_parameters", ()))


def mcp_tool_projection(name: str) -> tuple[str, tuple[str, ...]]:
    """Return the catalog-generated ``(tool name, public parameters)`` for an operation."""
    operation, projection = _allowlisted_mcp_operation(name)
    return _tool_projection(operation, projection)


def mcp_resource_projection(name: str) -> tuple[str, tuple[str, ...]]:
    """Return the catalog-generated ``(resource URI template, parameters)`` for an operation."""
    operation, projection = _allowlisted_mcp_operation(name)
    return _resource_projection(operation, projection)


def mcp_tool_projections() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Materialize the deterministic safe MCP tool inventory from one catalog snapshot."""
    result = {}
    for operation in operations():
        projection = operation.surfaces.get("mcp")
        if projection and projection.get("tool"):
            result[operation.name] = _tool_projection(
                operation, _validated_mcp_projection(operation)
            )
    return result


def mcp_tool_composites() -> dict[str, tuple[str, ...]]:
    """Return explicit application-operation constituents for coarse MCP tools.

    Most MCP tools project one canonical operation and therefore have no entry here.  A tool
    appears only when its adapter intentionally coordinates multiple catalog operations.  The
    result is derived from the same validated catalog snapshot as names and signatures so server
    construction never needs a second composite allowlist.
    """
    result = {}
    for operation in operations():
        projection = operation.surfaces.get("mcp")
        if projection and projection.get("tool"):
            validated = _validated_mcp_projection(operation)
            composes = tuple(validated.get("composes", ()))
            if composes:
                result[operation.name] = composes
    return result


def mcp_resource_projections() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Materialize the deterministic safe MCP resource inventory from one catalog snapshot."""
    result = {}
    for operation in operations():
        projection = operation.surfaces.get("mcp")
        if projection and projection.get("resource"):
            result[operation.name] = _resource_projection(
                operation, _validated_mcp_projection(operation)
            )
    return result


def mcp_tool_operations() -> tuple[str, ...]:
    """Return the deterministic operation inventory the FastMCP tool generator must bind."""
    return tuple(mcp_tool_projections())


def mcp_resource_operations() -> tuple[str, ...]:
    """Return the deterministic operation inventory the FastMCP resource generator must bind."""
    return tuple(mcp_resource_projections())


def mcp_notification_uris(name: str, **parameters: Any) -> tuple[str, ...]:
    """Generate one tool's completed-mutation resource notifications from the catalog.

    URI templates stay in the declarative projection; handlers supply only runtime values such as
    the config key.  An unbound template is an adapter error, never a partially formatted URI.
    """
    _operation, projection = _allowlisted_mcp_operation(name)
    uris = projection["progress"]["notification_uris"]
    try:
        return tuple(uri.format(**parameters) for uri in uris)
    except KeyError as exc:
        raise MCPProjectionError(
            f"notification for {name!r} needs parameter {exc.args[0]!r}"
        ) from exc


def document() -> dict[str, Any]:
    """Return the deterministic language-neutral catalog document."""
    operation_rows = []
    for operation in operations():
        row = asdict(operation)
        row["parameters"] = list(row["parameters"])
        operation_rows.append(row)
    return {
        "format_version": 1,
        "catalog_version": "1.0.0",
        "$id": CATALOG_INSTANCE_ARTIFACT_ID,
        "artifact_id": CATALOG_INSTANCE_ARTIFACT_ID,
        "policy": {
            "catalog_role": "build-time declaration; never a runtime dispatcher or service locator",
            "mcp_projection": "explicit allowlist",
            "privilege": "MCP excludes privileged operations and privileged override parameters",
            "projection_semantics": {
                "granularity": "fine is one typed operation; coarse is batch/composite/opaque; divergent requires a reason",
                "progress": "each surface declares stdout, structured, notifications, or no progress plus the transport reason",
                "interactivity": "a prompt-capable operation projects only with a declared non-interactive guard and prompt seam; MCP never prompts",
            },
            "naming": {
                "generation_rules": [
                    {
                        "convention": 1,
                        "rule": "singular per-entity names; fan-out uses --all",
                    },
                    {
                        "convention": 2,
                        "rule": "collections use a list verb and singular resource group",
                    },
                    {
                        "convention": 3,
                        "rule": "machine output uses --json bound to as_json",
                    },
                    {
                        "convention": 4,
                        "rule": "--hive is long-only and has no short flag",
                    },
                    {
                        "convention": 5,
                        "rule": "every work and plan verb carries its derived trace_verb marker",
                    },
                    {
                        "convention": 6,
                        "rule": "MCP 1:1 tools derive as group_verb; exceptions are explicit",
                    },
                    {
                        "convention": 7,
                        "rule": "resources derive as beadhive://<singular-group>/<view>[/{parameter}]",
                    },
                    {
                        "convention": 8,
                        "rule": "generated names, docstrings, probe payloads, and test filenames reject retired workspace abbreviations and pre-hive nouns",
                    },
                ],
                "cli": "singular kebab-case group/verb paths; collections use list",
                "cli_aliases": "aliases are structured projections with their own signature, hidden state, defaults, and divergence reason",
                "mcp_tool": "group_verb for 1:1 projections",
                "mcp_resource": "beadhive://<singular-group>/<view>[/{parameter}]",
                "mcp_exceptions": [
                    {"name": "bd_create", "reason": "maps to the opaque bd passthrough"},
                    {"name": "probe.health", "reason": "health resource has no CLI operation"},
                ],
                "parameters": {
                    "machine_output": {"flag": "--json", "name": "as_json"},
                    "target_hive": {"flag": "--hive", "name": "hive", "short": None},
                    "row_limit": {"flag": "--limit", "name": "limit", "short": "-n"},
                    "dry_run": {"flag": "--dry-run", "name": "dry_run"},
                    "force": {"flag": "--force", "name": "force", "short": "-f"},
                    "yes": {"flag": "--yes", "name": "yes", "short": "-y"},
                    "actor": {"flag": "--as", "name": "as_"},
                },
                "flag_scope": {
                    "hive": "hive-scoped commands only; no short flag",
                    "all": "passthrough routing and explicit aggregate reads; never arbitrary per-entity mutation",
                },
                "panels": [
                    "Planning plane",
                    "Integration plane",
                    "Hive",
                    "Fleet / HQ",
                    "Admin / infra",
                    "Passthrough",
                ],
                "hidden_groups": ["dolt", "harness", "otel", "wt"],
            },
            "divergence": "A non-derived name or dual projection requires a non-empty divergence reason in its projection.",
        },
        "cli_parents": cli_parents(),
        "operations": operation_rows,
        "exclusions": [
            {
                "surface": "cli",
                "name": "root callback",
                "reason": "transport routing/version/help mechanics, not an application operation",
            }
        ],
    }


# Bind the catalog before importing the executor.  The private registry is sealed and survives an
# executor reload, while this package's deterministic catalog continues to own ``OperationSpec``.
from ._registry import (  # noqa: E402
    bind_registered_operation_names as _bind_registered_operation_names,
)

_bind_registered_operation_names(frozenset(operation.name for operation in operations()))
del _bind_registered_operation_names


# Imported after the catalog declarations so executor typing can remain structural.
from .executor import (  # noqa: E402
    OperationExecutor as OperationExecutor,
)
