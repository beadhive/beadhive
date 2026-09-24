# Shared adapters, one target per boundary. `cli/` owns its own BUILD.
python_sources(
    name="package",
    sources=["__init__.py"],
    tags=["category:code", "attest:package", "owner:adapters", "role:namespace"],
)

python_sources(
    name="impact",
    sources=["impact_*.py"],
    tags=["category:code", "attest:package", "owner:adapters/impact", "role:shared-adapter"],
)

python_sources(
    name="telemetry",
    sources=["telemetry.py"],
    tags=["category:code", "attest:package", "owner:adapters/telemetry", "role:shared-adapter"],
)
