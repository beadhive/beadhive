# Keep this resolve aligned with pyproject.toml's direct runtime, optional, and dev inputs.
# Pants' uv lock is secondary build metadata; canonical dependency authority remains uv.lock.
python_requirement(name="typer", requirements=["typer>=0.12"])
python_requirement(
    name="ruamel-yaml",
    requirements=["ruamel.yaml>=0.18"],
    modules=["ruamel", "ruamel.yaml"],
)
python_requirement(name="structlog", requirements=["structlog>=24"])
python_requirement(name="fastmcp", requirements=["fastmcp>=3.4,<4"])
python_requirement(name="starlette", requirements=["starlette>=0.47,<2"])
python_requirement(name="uvicorn", requirements=["uvicorn>=0.34,<1"])
python_requirement(name="joserfc", requirements=["joserfc>=1.7,<2"])
python_requirement(name="httpx", requirements=["httpx>=0.28,<1"])
python_requirement(name="mcp", requirements=["mcp>=1.30,<2"], modules=["mcp"])
python_requirement(name="platformdirs", requirements=["platformdirs>=4"])
python_requirement(name="pydantic", requirements=["pydantic>=2"], modules=["pydantic"])
python_requirement(
    name="pydantic-core",
    requirements=["pydantic-core>=2"],
    modules=["pydantic_core"],
)
python_requirement(
    name="pydantic-settings",
    requirements=["pydantic-settings>=2.11"],
    modules=["pydantic_settings"],
)
python_requirement(name="pyyaml", requirements=["pyyaml>=6"], modules=["yaml"])
python_requirement(
    name="opentelemetry-api",
    requirements=["opentelemetry-api>=1.37,<2"],
    modules=["opentelemetry"],
)
python_requirement(name="opentelemetry-sdk", requirements=["opentelemetry-sdk>=1.37,<2"])
python_requirement(
    name="opentelemetry-exporter-otlp",
    requirements=["opentelemetry-exporter-otlp>=1.37,<2"],
)

python_requirement(name="pytest", requirements=["pytest>=8"])
python_requirement(name="pytest-xdist", requirements=["pytest-xdist>=3"], modules=["xdist"])
python_requirement(name="pytest-cov", requirements=["pytest-cov>=5"], modules=["pytest_cov"])
python_requirement(name="ruff", requirements=["ruff>=0.6"])
python_requirement(name="jsonschema", requirements=["jsonschema>=4"])
# Transitive dep of joserfc, already resolved in the lock; declared directly because
# tests/harness/frame_bridge_live.py imports it at module scope (bh-1j3ei.2).
python_requirement(
    name="cryptography",
    requirements=["cryptography>=42"],
    modules=["cryptography"],
)
# Transitive dep of fastmcp, already resolved in the lock; imported directly by tests that
# exercise the websocket transport (bh-1j3ei.2).
python_requirement(
    name="websockets",
    requirements=["websockets>=13"],
    modules=["websockets"],
)
# Transitive dep (pulled in by several resolved packages), already resolved in the lock;
# imported directly by tests that parse version strings (bh-1j3ei.2).
python_requirement(
    name="packaging",
    requirements=["packaging>=24"],
    modules=["packaging"],
)
# Transitive dep of typer (click), already resolved in the lock; imported directly by tests
# that drive the CLI's click internals (bh-1j3ei.2).
python_requirement(name="click", requirements=["click>=8"], modules=["click"])
# Transitive dep of jsonschema, already resolved in the lock; imported directly by tests
# that build jsonschema Resource/Registry objects (bh-1j3ei.2).
python_requirement(
    name="referencing",
    requirements=["referencing>=0.35"],
    modules=["referencing"],
)
python_requirement(name="commitizen", requirements=["commitizen>=4.16.4"], modules=["commitizen"])

# Ownership only (bh-1j3ei.2): uv-lock metadata Pants itself writes/reads; not a source of
# any target, but a tracked file that still needs an owner.
files(
    name="lock-metadata",
    sources=["beadhive.lock.metadata"],
    tags=["category:build-system"],
)
