"""Shared helpers for the bh-w56ts XTDB document-shape spike (throwaway, not product code).

Run every script with:
    uv run --no-project --with 'psycopg[binary]' --with transit-python2 python <script>.py

Environment:
    XTDB_DSN     default host=127.0.0.1 port=15432 user=xtdb dbname=xtdb
    SPIKE_DATA   default /data/bees/beadhive/scratch/xtdb-spike/data
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.adapt import Dumper
from psycopg.types.string import StrDumper
from transit.reader import Reader

DSN = os.environ.get("XTDB_DSN", "host=127.0.0.1 port=15432 user=xtdb dbname=xtdb")
DATA = Path(os.environ.get("SPIKE_DATA", "/data/bees/beadhive/scratch/xtdb-spike/data"))

# Top-level bead fields that are timestamps in the bd export (all "YYYY-MM-DDTHH:MM:SSZ").
TS_FIELDS = {
    "created_at",
    "updated_at",
    "closed_at",
    "started_at",
    "lease_expires_at",
    "heartbeat_at",
    "defer_until",
}
# Nested list-of-struct fields whose elements carry a created_at timestamp.
NESTED_TS_LISTS = {"dependencies", "comments"}
# `_type` is refused by XTDB ("Cannot put documents with columns: [_type]"): all `_`-prefixed
# top-level keys are reserved. The loader renames it.
RENAMES = {"_type": "bd_record_type"}


class Transit:
    """Marker wrapper: send a value as a transit-json parameter (XTDB pgwire OID 16384)."""

    def __init__(self, value: Any) -> None:
        self.value = value


class TransitDumper(Dumper):
    oid = 16384  # XTDB's `transit` type in pg_catalog.pg_type

    def dump(self, obj: Transit) -> bytes:
        return json.dumps(obj.value, ensure_ascii=False).encode()


def connect(*, autocommit: bool = True, transit_out: bool = False) -> psycopg.Connection:
    conn = psycopg.connect(DSN, autocommit=autocommit)
    conn.adapters.register_dumper(Transit, TransitDumper)
    # psycopg sends str as OID 0 (unknown); XTDB DML refuses untyped params.
    conn.adapters.register_dumper(str, StrDumper)
    if transit_out:
        set_output(conn, "transit")
    return conn


def set_output(conn: psycopg.Connection, fmt: str) -> None:
    """fmt: 'json' (default) or 'transit' for nested/union-typed columns."""
    prev = conn.autocommit
    conn.autocommit = True
    conn.execute(f"SET fallback_output_format='{fmt}'")
    conn.autocommit = prev


def tts(iso: str) -> str:
    """Transit tag for an instant."""
    return "~t" + iso


def tset(items: list) -> dict:
    return {"~#set": list(items)}


def esc(s: str) -> str:
    """Transit-json: strings starting with ~ or ^ must be escaped with a leading ~."""
    if s and s[0] in "~^`":
        return "~" + s
    return s


def to_transit(value: Any, key: str | None = None) -> Any:
    """Encode a plain JSON value for transit, escaping strings."""
    if isinstance(value, str):
        return esc(value)
    if isinstance(value, list):
        return [to_transit(v) for v in value]
    if isinstance(value, dict):
        return {esc(k): to_transit(v, k) for k, v in value.items()}
    return value


def bead_doc_nested(bead: dict) -> dict:
    """Layout (a): one document per bead, labels/deps/comments nested."""
    doc: dict[str, Any] = {"_id": bead["id"]}
    for k, v in bead.items():
        k2 = RENAMES.get(k, k)
        if k in TS_FIELDS:
            doc[k2] = tts(v)
        elif k == "labels":
            doc[k2] = tset([esc(x) for x in v])
        elif k in NESTED_TS_LISTS:
            doc[k2] = [
                {ek: (tts(ev) if ek == "created_at" else to_transit(ev)) for ek, ev in el.items()}
                for el in v
            ]
        else:
            doc[k2] = to_transit(v)
    return doc


def bead_doc_split(bead: dict) -> dict:
    """Layout (b): the bead document minus labels/dependencies/comments."""
    doc = bead_doc_nested(bead)
    for k in ("labels", "dependencies", "comments"):
        doc.pop(k, None)
    return doc


def label_id(bead_id: str, label: str) -> str:
    return f"{bead_id}|{label}"


def edge_id(src: str, typ: str, dst: str) -> str:
    return f"{src}|{typ}|{dst}"


def insert_records(conn: psycopg.Connection, table: str, docs: list[dict], batch: int = 500) -> int:
    """INSERT INTO <table> RECORDS $1, $2, ... in batches, one XTDB tx per batch."""
    n = 0
    for i in range(0, len(docs), batch):
        chunk = docs[i : i + batch]
        placeholders = ", ".join(["%s"] * len(chunk))
        with conn.transaction():
            conn.execute(f"INSERT INTO {table} RECORDS {placeholders}", [Transit(d) for d in chunk])
        n += len(chunk)
    return n


_reader = Reader("json")


def decode_transit(text: str | None) -> Any:
    if text is None:
        return None
    return _reader.read(io.StringIO(text))


def load_export() -> list[dict]:
    with open(DATA / "bh-export.jsonl", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def load_events() -> list[dict]:
    with open(DATA / "bh-events.json", encoding="utf-8") as fh:
        return json.load(fh)


def iso_z(value: Any) -> str:
    """Normalise a datetime (or XTDB zoned-date-time string) back to the export's ISO Z form."""
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(value)
    if s.endswith("[UTC]"):
        s = s[: -len("[UTC]")]
    parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    return parsed.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
