"""Compare lifecycle timestamps between two `bd export` JSONL files (bh-o1x4r).

Usage: python3 -I compare_ts.py SOURCE.jsonl ROUNDTRIP.jsonl
Reports, per field, how many issues kept an identical value.
"""

import json
import sys

FIELDS = ("created_at", "updated_at", "started_at", "closed_at")


def load(path):
    with open(path, encoding="utf-8") as fh:
        rows = (json.loads(line) for line in fh if line.strip())
        return {r["id"]: r for r in rows if r.get("_type", "issue") == "issue" and "id" in r}


def norm(value):
    # bd stores second/ms granularity; compare to the second, drop zone suffix variants
    return None if not value else value[:19]


def main(src_path, out_path):
    src, out = load(src_path), load(out_path)
    missing = len(set(src) - set(out))
    print(f"source issues={len(src)} roundtrip issues={len(out)} missing={missing}")
    for field in FIELDS:
        present = same = 0
        for issue_id, row in src.items():
            if not row.get(field):
                continue
            present += 1
            if issue_id in out and norm(out[issue_id].get(field)) == norm(row[field]):
                same += 1
        print(f"{field}: present_in_source={present} identical_after_import={same}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
