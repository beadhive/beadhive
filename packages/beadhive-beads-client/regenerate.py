"""Check or replace the pinned generated SDK from the checked-in OpenAPI source."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[1]
SPEC = PACKAGE / "spec/openapi.v0.yaml"
GENERATED = PACKAGE / "src/beads_v1_3"
EXPECTED = "9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd"


def files_at(path: Path) -> dict[str, str]:
    return {
        str(file.relative_to(path)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(path.rglob("*.py"))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="replace generated files")
    args = parser.parse_args()
    digest = hashlib.sha256(SPEC.read_bytes()).hexdigest()
    pinned = (PACKAGE / "spec/SHA256").read_text().split()[0]
    if digest != EXPECTED or pinned != EXPECTED:
        parser.error(f"OpenAPI digest drift: expected {EXPECTED}, got {digest}, pin {pinned}")
    # The generator runs Ruff from the output tree. Keep it under this repository
    # so it sees the same root formatting configuration as the checked-in SDK.
    with tempfile.TemporaryDirectory(prefix=".beads-sdk-", dir=PACKAGE) as directory:
        output = Path(directory) / "beads_v1_3"
        subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "--offline",
                "openapi-python-client",
                "generate",
                "--path",
                str(SPEC),
                "--meta",
                "none",
                "--output-path",
                str(output),
                "--fail-on-warning",
            ],
            cwd=ROOT,
            check=True,
        )
        before, after = files_at(GENERATED), files_at(output)
        if before != after:
            changed = sorted(set(before) | set(after))
            changed = [name for name in changed if before.get(name) != after.get(name)]
            if not args.write:
                print("Generated SDK drift:", *changed, sep="\n  ")
                return 1
            shutil.rmtree(GENERATED)
            shutil.copytree(output, GENERATED)
            print(f"Regenerated {len(after)} Python files")
        else:
            print(f"Generated SDK matches {len(after)} Python files")
    print(f"OpenAPI SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
