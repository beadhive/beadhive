#!/usr/bin/env python3
"""Record a successful full gate executed outside ``worktree.clean_checkout``."""

from __future__ import annotations

import argparse

from beadhive import config, registry, selective_validation, validation_ledger


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("rev")
    parser.add_argument("cmd")
    parser.add_argument("--phase", default="push-main")
    args = parser.parse_args()

    cfg = config.load()
    entry = registry.current_hive(cfg)
    if not entry:
        parser.error("cwd does not belong to a managed hive")
    configured = config.validate_cmd(cfg, entry, phase=args.phase)
    if configured.strip() != args.cmd.strip():
        parser.error(
            f"work.validate.{args.phase} is {configured!r}, not the completed gate {args.cmd!r}"
        )
    validation_ledger.record(entry, args.rev, args.cmd, 0, cfg=cfg, phase=args.phase)
    selective_validation.record_full_gate_keys(entry, cfg, args.rev, args.cmd, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
