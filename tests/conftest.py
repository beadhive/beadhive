"""Universal pytest registration with no environment or runtime initialization."""

pytest_plugins = ("harness.watchdog_diagnostics", "stateful_fixtures")
