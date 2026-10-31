"""The standalone ``beadlog`` command (stdlib ``argparse``; no BeadHive needed).

Subcommands:

- ``beadlog merge OUT IN [IN ...]`` — union-merge archives into one plain archive;
- ``beadlog reconcile PROPOSED BASELINE -o PLAN`` — write a reconcile plan; judge settings come
  from ``--judge-*`` flags, then ``BEADLOG_JUDGE_*`` env, then the defaults (judge off), the same
  keys as the root bh ``backfill.judge`` section;
- ``beadlog inspect ARCHIVE [--json]`` — manifest summary (and plan classification);
- ``beadlog verify ARCHIVE [--no-ids]`` — full integrity check, event ids included by default;
- ``beadlog extract <name> ...`` — one subcommand per source strategy registered in the
  ``beadlog.sources`` entry-point group (discovered at run time).

Exit codes: 0 success, 1 a failed verification or operation, 2 usage errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import IO, Any

from beadlog import (
    ArchiveError,
    ArchiveReader,
    BeadEvent,
    Generator,
    Group,
    IdCollisionError,
    ManifestError,
    read_archive,
    union_merge,
    write_archive,
)

from .contracts import ENTRY_POINT_GROUP, ExtractRequest, SourceStrategy
from .judges import JudgeSettings, Runner, build_judge, parse_auto_accept, parse_bool, settings_from
from .reconcile import PLAN_ANNOTATION, prior_verdicts, read_plan, reconcile, write_plan

VERSION = "0.1.0"
GENERATOR = Generator("beadlog-sources", VERSION)


class CliError(Exception):
    """An operation failure reported as ``beadlog: <message>`` with exit status 1."""


def discover_strategies(
    entry_points: Callable[[], Iterable[Any]] | None = None,
) -> dict[str, Any]:
    """Entry points of the ``beadlog.sources`` group by name (not loaded yet)."""
    if entry_points is None:

        def entry_points() -> Iterable[Any]:
            return metadata.entry_points(group=ENTRY_POINT_GROUP)

    return {point.name: point for point in sorted(entry_points(), key=lambda p: p.name)}


def _load_strategy(point: Any) -> SourceStrategy:
    loaded = point.load()
    strategy = loaded() if isinstance(loaded, type) else loaded
    if not isinstance(strategy, SourceStrategy):
        raise CliError(f"entry point {point.name!r} is not a SourceStrategy")
    return strategy


def _merge_groups(manifests: Iterable[Any]) -> list[Group]:
    by_id: dict[str, Group] = {}
    for manifest in manifests:
        for group in manifest.groups:
            known = by_id.get(group.id)
            if known is not None and known.dir != group.dir:
                raise CliError(f"group {group.id!r} uses dir {known.dir!r} and {group.dir!r}")
            by_id.setdefault(group.id, group)
    return [by_id[key] for key in sorted(by_id)]


def _read(path: str) -> tuple[Any, tuple[BeadEvent, ...]]:
    return read_archive(path)


def _settings(args: argparse.Namespace, environ: Mapping[str, str]) -> JudgeSettings:
    overrides = {
        "enabled": args.judge_enabled,
        "command": args.judge_command,
        "timeout_seconds": args.judge_timeout_seconds,
        "model": args.judge_model,
        "auto_accept": args.judge_auto_accept,
        "max_calls_per_run": args.judge_max_calls_per_run,
    }
    try:
        return settings_from(overrides, environ)
    except ValueError as exc:
        raise CliError(f"judge settings: {exc}") from exc


def _cmd_merge(args: argparse.Namespace, out: IO[str], **_: Any) -> int:
    loaded = [_read(path) for path in args.inputs]
    groups = _merge_groups(manifest for manifest, _ in loaded)
    merged = union_merge(events for _, events in loaded)
    manifest = write_archive(args.output, merged, groups=groups, generator=GENERATOR)
    print(f"merged {len(loaded)} archives: {manifest.counts['events']} events", file=out)
    return 0


def _cmd_reconcile(
    args: argparse.Namespace,
    out: IO[str],
    *,
    environ: Mapping[str, str],
    runner: Runner | None,
    **_: Any,
) -> int:
    proposed_manifest, proposed = _read(args.proposed)
    _, baseline = _read(args.baseline)
    settings = _settings(args, environ)
    prior = {}
    if args.prior:
        _, entries = read_plan(args.prior)
        prior = prior_verdicts(entries)
    judge = build_judge(settings, runner=runner)
    plan = reconcile(proposed, baseline, judge, auto_accept=settings.auto_accept, prior=prior)
    write_plan(args.output, plan, groups=list(proposed_manifest.groups), generator=GENERATOR)
    summary = {
        "plan": str(args.output),
        "events": len(plan.events),
        "counts": plan.counts(),
        "review_required": plan.annotation()["review_required"],
        "judge_calls": plan.calls,
        "judge": dict(plan.judge),
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True), file=out)
    else:
        counts = " ".join(f"{name}={value}" for name, value in summary["counts"].items())
        print(
            f"plan {args.output}: {summary['events']} events; {counts}; "
            f"{summary['review_required']} need review; {plan.calls} judge calls",
            file=out,
        )
    return 0


def _cmd_inspect(args: argparse.Namespace, out: IO[str], **_: Any) -> int:
    with ArchiveReader(args.archive) as reader:
        manifest = reader.manifest
    data = manifest.to_json()
    summary: dict[str, Any] = {
        "format_version": data["format_version"],
        "generator": data["generator"],
        "groups": data["groups"],
        "capabilities": data["capabilities"],
        "counts": data["counts"],
    }
    if "plan" in data:
        summary["plan"] = data["plan"]
    reconcile_block = (data.get("annotations") or {}).get(PLAN_ANNOTATION)
    if isinstance(reconcile_block, Mapping):
        summary["reconcile"] = {
            "counts": reconcile_block.get("counts"),
            "review_required": reconcile_block.get("review_required"),
        }
    if args.json:
        print(json.dumps(summary, sort_keys=True, indent=2), file=out)
        return 0
    print(f"format {summary['format_version']} by {data['generator']['name']}", file=out)
    print(f"groups: {', '.join(group['id'] for group in data['groups'])}", file=out)
    counts = data["counts"]
    print(f"events: {counts.get('events', 0)} across {counts.get('beads', 0)} beads", file=out)
    for kind, number in sorted((counts.get("by_kind") or {}).items()):
        print(f"  {kind}: {number}", file=out)
    caps = data["capabilities"]
    print(
        f"capabilities: required={caps['required']} optional={caps['optional']}",
        file=out,
    )
    if "plan" in summary:
        plan = summary["plan"]
        print(f"plan: pass={plan['pass']} plan_hash={plan['plan_hash']}", file=out)
    if "reconcile" in summary:
        block = summary["reconcile"]
        print(f"reconcile: {block['counts']} review_required={block['review_required']}", file=out)
    return 0


def _cmd_verify(args: argparse.Namespace, out: IO[str], **_: Any) -> int:
    with ArchiveReader(args.archive) as reader:
        reader.verify(ids=not args.no_ids)
        events = reader.manifest.counts.get("events", 0)
    print(f"ok: {args.archive} ({events} events)", file=out)
    return 0


def _cmd_extract(
    args: argparse.Namespace, out: IO[str], *, strategies: Mapping[str, Any], **_: Any
) -> int:
    strategy = _load_strategy(strategies[args.strategy])
    baseline: tuple[BeadEvent, ...] = ()
    if args.baseline:
        _, baseline = _read(args.baseline)
    options: dict[str, str] = {}
    for item in args.option or ():
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise CliError(f"--option {item!r} is not key=value")
        options[key] = value
    request = ExtractRequest(
        root=Path(args.root),
        group=args.group,
        extracted_at=args.extracted_at,
        baseline=baseline,
        options=options,
    )
    events = list(strategy.extract(request))
    group = Group(args.group, args.group_dir)
    manifest = write_archive(
        args.output,
        events,
        groups=[group],
        generator=GENERATOR,
        description=f"extracted by {strategy.name} {strategy.version}",
    )
    print(f"extracted {manifest.counts['events']} events with {strategy.name}", file=out)
    return 0


def _judge_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group(
        "judge", "backfill.judge keys; flags win over BEADLOG_JUDGE_<KEY> env over defaults"
    )
    group.add_argument("--judge-enabled", type=parse_bool, metavar="BOOL", default=None)
    group.add_argument("--judge-command", default=None, metavar="CMD")
    group.add_argument("--judge-timeout-seconds", type=float, default=None, metavar="SECONDS")
    group.add_argument("--judge-model", default=None, metavar="MODEL")
    group.add_argument(
        "--judge-auto-accept",
        type=parse_auto_accept,
        default=None,
        metavar="FAMILY=THRESHOLD[,...]",
    )
    group.add_argument("--judge-max-calls-per-run", type=int, default=None, metavar="N")


def build_parser(strategies: Mapping[str, Any]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="beadlog", description="Build, merge, reconcile and check BeadLog (.bdlg) archives."
    )
    parser.add_argument("--version", action="version", version=f"beadlog {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    merge = commands.add_parser("merge", help="union-merge archives into one archive")
    merge.add_argument("output", help="archive to write")
    merge.add_argument("inputs", nargs="+", help="archives to merge")
    merge.set_defaults(handler=_cmd_merge)

    rec = commands.add_parser("reconcile", help="classify a proposal against a baseline")
    rec.add_argument("proposed", help="proposed .bdlg")
    rec.add_argument("baseline", help="baseline .bdlg (the target exported as a BeadLog)")
    rec.add_argument("-o", "--output", required=True, help="plan .bdlg to write")
    rec.add_argument("--prior", help="an earlier plan whose recorded verdicts are reused")
    rec.add_argument("--json", action="store_true", help="print a JSON summary")
    _judge_flags(rec)
    rec.set_defaults(handler=_cmd_reconcile)

    inspect = commands.add_parser("inspect", help="summarize an archive's manifest")
    inspect.add_argument("archive")
    inspect.add_argument("--json", action="store_true", help="print JSON")
    inspect.set_defaults(handler=_cmd_inspect)

    verify = commands.add_parser("verify", help="verify an archive's integrity and event ids")
    verify.add_argument("archive")
    verify.add_argument("--no-ids", action="store_true", help="skip recomputing event ids")
    verify.set_defaults(handler=_cmd_verify)

    extract = commands.add_parser(
        "extract",
        help="run a source strategy (entry-point group beadlog.sources)",
        description="Installed strategies: " + (", ".join(strategies) or "none"),
    )
    names = extract.add_subparsers(dest="strategy", required=True, metavar="NAME")
    for name in strategies:
        sub = names.add_parser(name, help=f"extract with the {name} strategy")
        sub.add_argument("--root", default=".", help="repository to read")
        sub.add_argument("--group", required=True, help="durable bead group id")
        sub.add_argument("--group-dir", required=True, help="archive dir for the group")
        sub.add_argument("--extracted-at", required=True, help="extraction timestamp (UTC)")
        sub.add_argument("--baseline", help="baseline .bdlg for bead-id resolution")
        sub.add_argument("--option", action="append", help="strategy option key=value")
        sub.add_argument("-o", "--output", required=True, help="archive to write")
    extract.set_defaults(handler=_cmd_extract)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    environ: Mapping[str, str] | None = None,
    entry_points: Callable[[], Iterable[Any]] | None = None,
    runner: Runner | None = None,
) -> int:
    """Console entry point of ``beadlog``."""
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    strategies = discover_strategies(entry_points)
    parser = build_parser(strategies)
    args = parser.parse_args(argv)
    try:
        return int(
            args.handler(
                args,
                out,
                environ=os.environ if environ is None else environ,
                runner=runner,
                strategies=strategies,
            )
        )
    except (CliError, ArchiveError, ManifestError, IdCollisionError, OSError, ValueError) as exc:
        print(f"beadlog: {type(exc).__name__}: {exc}", file=err)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
