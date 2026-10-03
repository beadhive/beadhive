#!/usr/bin/env python3
"""Render by default. Explicit apply is SERVER-LOCAL only; never remote root.

The exact schema is the committed v3 fresh-initialization contract. This command
does not run unless an operator explicitly selects --apply on the server host.
No driver needed by this bootstrap script; application driver must be packaged.
"""

import argparse
import ipaddress
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

DB = "beadhive_hq_config"
READER = "bh_hq_config_reader"
PUBLISHER = "bh_hq_config_publisher"
TABLES = ("hq_config_meta", "hq_config_documents", "hq_config_publications")


def literal(value):
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def pattern(value):
    return literal(value.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%"))


def protected_secret(path):
    p = Path(path)
    st = p.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError("credential file must be an owned regular file with mode0600 or0400")
    value = p.read_text().rstrip("\n")
    if not value or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("credential file must contain one nonempty line")
    return value


def sql(reader_host, reader_password, publisher_password, publisher_host="localhost"):
    reader = f"{literal(READER)}@{literal(reader_host)}"
    publisher = f"{literal(PUBLISHER)}@{literal(publisher_host)}"
    text = Path(__file__).with_name("schema.sql").read_text()
    text += f"\nCREATE USER {reader} IDENTIFIED BY {literal(reader_password)};\n"
    text += f"CREATE USER {publisher} IDENTIFIED BY {literal(publisher_password)};\n"
    for table in TABLES:
        text += f"GRANT SELECT ON `{DB}`.`{table}` TO {reader};\n"
        text += f"GRANT SELECT ON `{DB}`.`{table}` TO {publisher};\n"
    text += f"GRANT INSERT, UPDATE, DELETE ON `{DB}`.hq_config_documents TO {publisher};\n"
    text += f"GRANT INSERT, UPDATE ON `{DB}`.hq_config_meta TO {publisher};\n"
    text += f"GRANT INSERT ON `{DB}`.hq_config_publications TO {publisher};\n"
    for proc in ("dolt_add", "dolt_commit"):
        text += f"GRANT EXECUTE ON PROCEDURE `{DB}`.{proc} TO {publisher};\n"
    # Required status inspection stays limited to this dedicated database.
    for table in (
        "dolt_status",
        "dolt_log",
        "dolt_commit_ancestors",
        "dolt_history_hq_config_publications",
    ):
        text += f"GRANT SELECT ON `{DB}`.{table} TO {publisher};\n"
    # Scope to the NEW database only. Do not remove global/default/shared rules.
    # Literal underscores must be escaped because branch controls use LIKE patterns.
    dbpat = pattern(DB)
    text += f"INSERT INTO dolt_branch_control VALUES ({dbpat}, '%', '%', '%', 'read');\n"
    text += f"INSERT INTO dolt_branch_control VALUES ({dbpat}, '%', 'root', '%', 'admin');\n"
    text += (
        f"INSERT INTO dolt_branch_control VALUES ({dbpat}, 'main', "
        f"{pattern(PUBLISHER)}, '%', 'write,merge');\n"
    )
    text += f"INSERT INTO dolt_branch_namespace_control VALUES ({dbpat}, '%', 'root', '%');\n"
    text += f"SHOW GRANTS FOR {reader};\nSHOW GRANTS FOR {publisher};\n"
    text += "SELECT * FROM dolt_branch_control;\nSELECT * FROM dolt_branch_namespace_control;\n"
    return text


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--reader-host", required=True, help="exact LAN source IP as seen by Dolt; no wildcard"
    )
    p.add_argument(
        "--publisher-host",
        default="localhost",
        help="localhost initially, or explicit restricted operator source IP",
    )
    p.add_argument("--apply", action="store_true")
    p.add_argument("--server-hostname")
    p.add_argument(
        "--local-port",
        type=int,
        required=True,
        help="operator-confirmed local listener port; external3308 is not an inferred local port",
    )
    p.add_argument("--reader-password-file")
    p.add_argument("--publisher-password-file")
    root_auth = p.add_mutually_exclusive_group()
    root_auth.add_argument("--root-password-file")
    root_auth.add_argument(
        "--root-passwordless",
        action="store_true",
        help="explicit server-local opt-in only; never inferred or tried as fallback",
    )
    p.add_argument("--dolt", default="dolt")
    a = p.parse_args()
    try:
        ipaddress.ip_address(a.reader_host)
        if a.publisher_host != "localhost":
            ipaddress.ip_address(a.publisher_host)
    except ValueError:
        p.error("principal hosts must be exact IP addresses (publisher also permits localhost)")
    if not 1 <= a.local_port <= 65535:
        p.error("invalid local port")
    if not a.apply:
        print(
            sql(
                a.reader_host,
                "<operator-provided-reader-secret>",
                "<operator-provided-publisher-secret>",
                a.publisher_host,
            )
        )
        return 0
    if a.server_hostname != socket.gethostname():
        p.error(
            "apply requires explicit server-hostname matching this machine; run ON the Dolt server"
        )
    if not all((a.reader_password_file, a.publisher_password_file)) or not (
        a.root_password_file or a.root_passwordless
    ):
        p.error(
            "apply requires credential files and explicit root authentication; "
            "no defaults or guessing"
        )
    try:
        reader = protected_secret(a.reader_password_file)
        publisher = protected_secret(a.publisher_password_file)
        root_password = "" if a.root_passwordless else protected_secret(a.root_password_file)
        env = os.environ.copy()
        env["DOLT_CLI_PASSWORD"] = root_password
        cmd = [
            a.dolt,
            "--host=127.0.0.1",
            f"--port={a.local_port}",
            "--no-tls",
            "--user=root",
            "sql",
        ]
        # Safe repeated invocation: never alter an existing dedicated schema/accounts.
        # Existing/partial state requires explicit inspection and a separately reviewed repair.
        preflight = subprocess.run(
            cmd
            + [
                "-r",
                "json",
                "-q",
                "SELECT COUNT(*) AS present FROM information_schema.schemata "
                "WHERE schema_name='beadhive_hq_config';",
            ],
            text=True,
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if preflight.returncode:
            raise ValueError("local root preflight failed")
        if int(json.loads(preflight.stdout)["rows"][0]["present"]) != 0:
            print(
                "Dedicated database already exists; no writes performed. "
                "Compare validation.sql and review any repair separately."
            )
            return 2
        result = subprocess.run(
            cmd,
            input=sql(a.reader_host, reader, publisher, a.publisher_host),
            text=True,
            env=env,
            capture_output=True,
            timeout=180,
            check=False,
        )
        # SQL failures may echo CREATE USER literals: never emit captured stdout/stderr.
        if result.returncode:
            print(
                "Provisioning failed; partial setup may exist. "
                "Inspect server locally before any retry.",
                file=sys.stderr,
            )
            return 1
        print(
            "Provisioning returned success. Qualify effective grants, denials, "
            "and TLS before seed or activation."
        )
        return 0
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
        print(
            "Provisioning refused; inspect protected inputs or local server. "
            "No credential details emitted.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
