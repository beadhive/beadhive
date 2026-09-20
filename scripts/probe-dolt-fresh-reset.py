#!/usr/bin/env python3
"""Smoke-test fresh database reset behavior on an isolated Dolt SQL server."""

from __future__ import annotations

import argparse
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pymysql


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dolt", type=Path)
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")

    dolt = args.dolt.resolve(strict=True)
    port = _unused_loopback_port()
    with tempfile.TemporaryDirectory(prefix="dolt-fresh-reset-") as scratch:
        scratch_path = Path(scratch)
        data_dir = scratch_path / "data"
        cfg_dir = scratch_path / "cfg"
        data_dir.mkdir()
        cfg_dir.mkdir()
        server_log = scratch_path / "server.log"
        with server_log.open("w+") as log:
            server = subprocess.Popen(
                [
                    str(dolt),
                    "sql-server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--data-dir",
                    str(data_dir),
                    "--doltcfg-dir",
                    str(cfg_dir),
                    "--socket",
                    str(scratch_path / "mysql.sock"),
                    "--loglevel",
                    "error",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            connection = None
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if server.poll() is not None:
                        break
                    try:
                        connection = pymysql.connect(
                            host="127.0.0.1",
                            port=port,
                            user="root",
                            autocommit=True,
                            connect_timeout=1,
                        )
                        break
                    except pymysql.MySQLError:
                        time.sleep(0.1)
                if connection is None:
                    log.seek(0)
                    raise RuntimeError(f"Dolt server did not become ready:\n{log.read()}")

                version_output = subprocess.run(
                    [str(dolt), "version"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                print(f"binary={dolt}")
                print(f"version={version_output}")
                print(f"scratch={scratch_path}")
                print(f"endpoint=127.0.0.1:{port}")
                with connection.cursor() as cursor:
                    for trial in range(1, args.trials + 1):
                        database = f"fresh_reset_{trial:03d}"
                        cursor.execute(f"CREATE DATABASE `{database}`")
                        cursor.execute(f"USE `{database}`")
                        cursor.execute("CALL DOLT_RESET('--hard')")
                        while cursor.nextset():
                            pass
                        cursor.execute("SELECT DATABASE()")
                        selected = cursor.fetchone()[0]
                        if selected != database:
                            message = (
                                f"trial {trial}: active database {selected!r}, "
                                f"expected {database!r}"
                            )
                            raise RuntimeError(message)
                        print(f"trial={trial} database={database} reset=ok query=ok")
                print(f"summary=0/{args.trials} failures")
            finally:
                if connection is not None:
                    connection.close()
                if server.poll() is None:
                    server.terminate()
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=10)
        print("scratch_removed=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
