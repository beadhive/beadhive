"""pgwire authentication probe. Usage: python auth.py <port> -- run once against a no-password
container and once against a container started with -e XTDB_PASSWORD=spike-secret."""

import sys

import psycopg

port = int(sys.argv[1])
for user, pw in [
    ("xtdb", None),
    ("xtdb", "wrong-password"),
    ("xtdb", "spike-secret"),
    ("bob", "spike-secret"),
]:
    try:
        c = psycopg.connect(
            host="127.0.0.1", port=port, user=user, password=pw, dbname="xtdb", connect_timeout=5
        )
        print(
            f"user={user!r} password={pw!r}: CONNECTED as",
            c.execute("SELECT current_user").fetchone()[0],
        )
    except Exception as e:  # noqa: BLE001
        print(f"user={user!r} password={pw!r}: REJECTED:", str(e).strip().splitlines()[0][:90])
