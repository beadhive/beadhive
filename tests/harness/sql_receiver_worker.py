"""Isolated integration-test receiver process with a synthetic local credential broker."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from beadhive.hq_sql_receiver import SqlTrustedReceiver


class FixtureBroker:
    def get(self, reference, *, deadline):
        if (
            reference
            != {"config_path": "/fixture/fnox.toml", "profile": "fixture", "key": "SQL"}
            or time.monotonic() >= deadline
        ):
            raise ValueError("fixture observer credential reference unavailable")
        return "fixture-secret"


def main() -> int:
    settings_path, principal, request_id = sys.argv[1:4]
    arguments = sys.argv[4:]
    settings = json.loads(Path(settings_path).read_text())
    if settings.get("runtime") is not None or settings.get("observer") is None:
        raise ValueError("receiver fixture must have only its observer binding")
    # Test-only abrupt process death at the exact SQL COMMIT boundary. The
    # separately credentialed receiver's connection is the sole target;
    # freshness-fence reads and server state remain untouched.
    if arguments and arguments[0] in {"--abort-before-commit", "--abort-after-commit"}:
        from beadhive import hq_sql_receiver

        abort_stage = arguments.pop(0)
        original_connect = hq_sql_receiver.connect

        def connect_with_abort(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            original_commit = connection.commit

            def abort_commit():
                if abort_stage == "--abort-before-commit":
                    os._exit(77)
                original_commit()
                os._exit(78)

            connection.commit = abort_commit
            return connection

        hq_sql_receiver.connect = connect_with_abort
    receiver = SqlTrustedReceiver(settings, broker=FixtureBroker())
    if arguments and arguments[0] == "--hive":
        result = receiver.accept_hive_lease(principal, request_id)
    elif arguments and arguments[0] == "--registration":
        result = receiver.accept_registration(principal, request_id)
    elif arguments:
        recovery = json.loads(Path(arguments[0]).read_text())
        if recovery["principal"] != principal:
            raise ValueError("receiver recovery principal mismatch")
        result = receiver.recover_heartbeat(request_id, **recovery)
    else:
        result = receiver.accept_heartbeat(principal, request_id)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
