#!/usr/bin/env python3
"""``bh role <role> --bead B --outcome-sink bead --session-id S`` as a Gas City session runs it,
for ``tests/test_runtime_loop_contract.py`` (bh-lxv3v).

Usage::

    role_sink_driver.py --hive <store> --workspace <dir> --seat-actor <seat> -- <bh role argv>

The real ``bh role`` command runs in this process with only the host-resolution seams pinned:

- the headless launch plan (no BAML artifact is installed on a test host);
- the workspace attach (``bh work claim`` + worktree lookup): the loop already holds the claim,
  and a Gas City session is started in the workspace the runtime was handed;
- the registry lookups for the scratch store, which is not a registered hive;
- the seat's stamped identity (normally the worktree's git identity).

Everything that matters to the loop contract is real: the CLI parse, the settled seat argv
(``localloop.seat_argv`` over ``work.dispatch.seat_command`` from the test's config), the
foreground run that keeps the seat's stdout, and ``outcome.write`` through ``bd`` into the
scratch store. The seat inherits this process's stdin, which is how the session host releases
its turn.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hive", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--seat-actor", required=True)
    args, rest = parser.parse_known_args(argv)
    if rest[:1] == ["--"]:
        rest = rest[1:]

    os.environ["BH_SKIP_SETUP_CHECK"] = "1"
    os.chdir(args.workspace)
    hive = Path(args.hive)
    entry = {"provider": "github", "org": "loop-contract", "repo": "scratch"}

    from beadhive import cli, identity, role_execution

    plan = role_execution.RoleLaunchPlan(
        backend="baml", detail="loop-contract stand-in seat", provider=None
    )
    role_execution.resolve_headless_plan = lambda *_a, **_kw: plan
    cli._apply_role_workspace = lambda *_a: None
    cli.registry.entry_for_dir = lambda *_a: entry
    cli.registry.hive_dir = lambda _entry: hive
    identity.resolve_actor = lambda *_a, **_kw: args.seat_actor

    cli.app(args=rest, prog_name="bh")  # raises SystemExit with bh role's exit code
    return 0


if __name__ == "__main__":
    sys.exit(main())
