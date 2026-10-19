"""End-to-end proof: one repository, two bead stores (bh-lniwf.8).

One code checkout with a PRIVATE operational ``internal`` store on an S3-compatible remote and a
distinct PUBLIC projection store published to the code origin's Git Dolt ref
(docs/design/two-store-scope-adr.md; operator guide: docs/TWO-STORES.md). Real bd and Dolt in
server mode, real Git, and nothing else real:

* the internal remote is moto in server mode on loopback (``harness.s3``, shared with the HQ
  remote proof bh-82gpi) — dummy credentials, never real S3;
* GitHub is an offline fake: a loopback HTTP stub replaying the recorded issue page
  ``tests/fixtures/github_projection/github-issues-page1.json`` (the same recording
  ``tests/test_github_projection.py`` replays), reached through bd's own ``GITHUB_API_URL`` — no
  token, no repository, no network;
* every shared server is an isolated instance (own data dir, free port), reaped however a test
  ends; each host gets its own.

Marked ``integration`` + ``dolt_server`` (the attest/push integration key, not the per-bead
submit keys); skips without bd, and skips — never fails — without ``moto[server]``.

bd 1.3.0's server-mode ``bd bootstrap`` waits about 70 s for the not-yet-existing database
before it clones (10 + 20 + 40 s "possible transient restart" retries, bh-mi40l), so only the
main proof pays for one managed clone; the failure cases seed or refuse instead of cloning.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from beadhive import (
    cli,
    config,
    doctor,
    github_projection,
    onboard,
    public_publish,
    publish_export,
    registry,
    store_target,
)
from beadhive.run import run
from harness.beads import embedded_env, skip_if_no_bd
from harness.s3 import MotoS3, moto_s3  # noqa: F401 - pytest fixture
from harness.world import free_port, git, reap_dolt_server

pytestmark = [pytest.mark.integration, pytest.mark.dolt_server, skip_if_no_bd]

FIXTURES = Path(__file__).parent / "fixtures" / "github_projection"
GITHUB_TITLES = {"Crash on start", "Add dark mode"}  # issues #1 and #2; PR #3 is filtered by bd
PRIVATE_TITLE = "PRIVATE acquisition plan for widget"
TOKEN = "ghp_offlineFixtureTokenNeverReal"
_TIMEOUT = 180


# --------------------------------------------------------------------------------------------
# offline GitHub
# --------------------------------------------------------------------------------------------


class GitHubStub:
    """Loopback GitHub REST stub serving one recorded issue page for acme/widget.

    Records every request; ``fail_next`` answers the next N requests with ``fail_status`` (an
    interrupted pull — a 401 by default, which bd does not retry; a 5xx is retried six times
    with backoff, ~80 s). Anything but a GET of the issue list is a 404 — so a write attempt
    would be visible."""

    def __init__(self) -> None:
        self.page = (FIXTURES / "github-issues-page1.json").read_text()
        self.requests: list[tuple[str, str]] = []
        self.fail_next = 0
        self.fail_status = 401
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _answer(self, code: int, body: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):  # noqa: N802 - http.server API
                stub.requests.append(("GET", self.path))
                if stub.fail_next:
                    stub.fail_next -= 1
                    return self._answer(stub.fail_status, '{"message": "interrupted"}')
                url = urllib.parse.urlparse(self.path)
                if url.path.rstrip("/").endswith("/repos/acme/widget/issues"):
                    page = urllib.parse.parse_qs(url.query).get("page", ["1"])[0]
                    return self._answer(200, stub.page if page == "1" else "[]")
                return self._answer(404, '{"message": "Not Found"}')

            def _write(self):
                stub.requests.append((self.command, self.path))
                self._answer(404, '{"message": "Not Found"}')

            do_POST = do_PATCH = do_PUT = do_DELETE = _write  # noqa: N815

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def github(monkeypatch):
    stub = GitHubStub()
    monkeypatch.setenv("GITHUB_API_URL", stub.url)
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield stub
    finally:
        stub.stop()


def _pull(entry):
    return github_projection.project(entry, token_source=lambda _env: TOKEN)


# --------------------------------------------------------------------------------------------
# hosts
# --------------------------------------------------------------------------------------------


@dataclass
class Host:
    name: str
    ws: Path
    checkout: Path
    public_root: Path
    cfg_path: Path
    server_dir: Path
    entry: dict

    @property
    def cfg(self) -> dict:
        return {"managed_repos": [self.entry]}

    def public(self) -> store_target.StoreTarget:
        return store_target.resolve_store(self.entry, store_target.SCOPE_PUBLIC)

    def internal(self) -> store_target.StoreTarget:
        return store_target.operational_store(self.entry)


def _entry(public_root: Path, internal_url: str, **public) -> dict:
    return {
        "provider": "github",
        "org": "acme",
        "repo": "widget",
        "prefix": "widget",
        "dolt_remote": {"name": "widget-private", "url": internal_url},
        "stores": {
            "internal": {"remote": "widget-private"},
            "public": {
                "prefix": "widpub",
                "path": str(public_root),
                "remote": "git-origin",
                "source": "github-issues",
                **public,
            },
        },
    }


def _write_config(path: Path, *entries: dict) -> None:
    # JSON is YAML: the fleet config the CLI reads through BH_CONFIG.
    path.write_text(json.dumps({"schema_version": 1, "managed_repos": list(entries)}))


@pytest.fixture
def world(tmp_path, monkeypatch, moto_s3):  # noqa: F811 - the imported fixture
    """A bare code origin, the internal S3 location, and a scrubbed environment."""
    for key in set(os.environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("BEADS_DIR", raising=False)
    monkeypatch.delenv(onboard.SEED_REMOTE_ENV, raising=False)
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    for key, value in moto_s3.server_env().items():
        monkeypatch.setenv(key, value)
    # bh-dhl6's push-access probe would ask GitHub; nothing here touches the network.
    monkeypatch.setattr(registry, "has_push_access", lambda *a, **k: False)
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git("init", "-q", "--bare", "-b", "main", cwd=origin)
    seed = tmp_path / "code-seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    git("config", "user.email", "t@t.dev", cwd=seed)
    git("config", "user.name", "T", cwd=seed)
    (seed / "README.md").write_text("widget\n")
    git("add", ".", cwd=seed)
    git("commit", "-q", "-m", "init", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-q", "-u", "origin", "main", cwd=seed)
    hosts: list[Host] = []
    yield _World(tmp_path, monkeypatch, moto_s3, origin, hosts)
    for host in hosts:
        reap_dolt_server(host.server_dir)


@dataclass
class _World:
    tmp: Path
    monkeypatch: pytest.MonkeyPatch
    s3: MotoS3
    origin: Path
    hosts: list

    @property
    def internal_url(self) -> str:
        return self.s3.url("internal/github/acme/widget")

    def host(self, name: str, **public) -> Host:
        """Enter host ``name``: its own workspace, Git clone, config and shared server."""
        base = self.tmp / name
        ws = base / "ws"
        checkout = ws / "github" / "acme" / "widget"
        checkout.parent.mkdir(parents=True)
        git("clone", "-q", str(self.origin), str(checkout))
        git("config", "user.email", "t@t.dev", cwd=checkout)
        git("config", "user.name", "T", cwd=checkout)
        public_root = base / "stores" / "widget-public"
        host = Host(
            name=name,
            ws=ws,
            checkout=checkout,
            public_root=public_root,
            cfg_path=base / "config.yaml",
            server_dir=base / "shared-server",
            entry=_entry(public_root, self.internal_url, **public),
        )
        _write_config(host.cfg_path, host.entry)
        mp = self.monkeypatch
        mp.setenv("GIT_WORKSPACE", str(ws))
        mp.setenv("BH_CONFIG", str(host.cfg_path))
        mp.setenv("BH_HOME", str(base / "bhhome"))
        mp.setenv("BEADS_SHARED_SERVER_DIR", str(host.server_dir))
        mp.setenv("BEADS_DOLT_SERVER_PORT", str(free_port()))
        mp.chdir(checkout)
        self.hosts.append(host)
        return host

    def leave(self, host: Host) -> None:
        reap_dolt_server(host.server_dir)


def _ctx(host: Host) -> onboard.Ctx:
    ctx = onboard.Ctx(
        hive="github/acme/widget",
        target=str(host.checkout),
        provider="github",
        org="acme",
        repo="widget",
        cwd=str(host.checkout),
        prefix="widget",
        furnish=False,
        cfg=host.cfg,
    )
    ctx._derived = True  # skip real registry/classify lookups
    return ctx


def _seed_first_host(world: _World, host: Host) -> None:
    """The first host of a fresh partitioned hive seeds the EMPTY internal remote (operator
    intent, BH_ONBOARD_SEED_REMOTE) — no clone, so no bootstrap wait."""
    world.monkeypatch.setenv(onboard.SEED_REMOTE_ENV, "widget")
    try:
        onboard._act_bd_init(_ctx(host))
    finally:
        world.monkeypatch.delenv(onboard.SEED_REMOTE_ENV, raising=False)


# --------------------------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------------------------


def _bd(*args, cwd, check=True, env=None):
    res = run(
        ["bd", "-C", str(cwd), *map(str, args)],
        check=False,
        capture=True,
        timeout=_TIMEOUT,
        env=env,
    )
    if check and res.returncode:
        raise AssertionError(f"bd {' '.join(map(str, args))} -> {res.returncode}\n{res.stderr}")
    return res


def _rows(cwd, env=None) -> list[dict]:
    res = _bd("list", "--all", "--json", "--limit", "0", cwd=cwd, env=env)
    return json.loads(res.stdout or "[]")


def _titles(cwd, env=None) -> set[str]:
    return {row["title"] for row in _rows(cwd, env)}


def _ids(cwd, env=None) -> set[str]:
    return {row["id"] for row in _rows(cwd, env)}


def _history(cwd, bead: str) -> list[str]:
    return [row["CommitHash"] for row in json.loads(_bd("history", bead, "--json", cwd=cwd).stdout)]


def _remotes(cwd) -> dict[str, str]:
    listed = json.loads(_bd("dolt", "remote", "list", "--json", cwd=cwd).stdout or "[]")
    return {row["name"]: row["url"] for row in listed}


def _origin_dolt_ref(origin: Path) -> str:
    return git("ls-remote", str(origin), "refs/dolt/data", cwd=origin).stdout.strip()


def _ordinary_public_clone(world: _World, name: str) -> Path:
    """What anyone with read access to the public repository does: `git clone` and `bd
    bootstrap` — embedded, no HQ config, no credentials, no bh."""
    clone = world.tmp / name
    git("clone", "-q", str(world.origin), str(clone))
    env = {k: v for k, v in embedded_env().items() if not k.startswith("AWS_")}
    res = run(["bd", "bootstrap", "--non-interactive"], cwd=str(clone), check=False, capture=True,
              timeout=_TIMEOUT, env=env)  # fmt: skip
    assert res.returncode == 0, res.stderr
    return clone


def _invoke(*args):
    result = CliRunner().invoke(cli.app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def _ready_ids() -> set[str]:
    result = CliRunner().invoke(cli.app, ["work", "ready", "--json"])
    assert result.exit_code == 0, result.stdout
    return {row["id"] for row in json.loads(result.stdout)}


def _create(cwd, title: str) -> str:
    return _bd("create", title, "-t", "task", "--silent", cwd=cwd).stdout.strip()


# --------------------------------------------------------------------------------------------
# the proof
# --------------------------------------------------------------------------------------------


def test_one_repository_two_stores_end_to_end(world, github):
    s3 = world.s3

    # 1. First host: the partitioned hive is onboarded. Its internal store seeds the EMPTY
    #    S3-compatible remote; its public store is minted at its detached path with its own
    #    prefix. Two prefixes, two databases, two remotes, one checkout.
    first = world.host("first")
    _seed_first_host(world, first)
    internal, public = first.internal(), first.public()
    assert (internal.prefix, public.prefix) == ("widget", "widpub")
    assert internal.beads_dir == first.checkout / ".beads" and public.detached
    assert public.store_root == first.public_root and public.beads_dir.is_dir()
    assert internal.remote == "widget-private" and public.remote == "git-origin"
    metadata = json.loads((first.checkout / ".beads" / "metadata.json").read_text())
    assert metadata["dolt_mode"] == "server" and metadata["dolt_database"] == "widget"
    assert _rows(first.public_root) == []
    #    The internal store's ONLY remote is the private descriptor: bd's own auto-wiring of
    #    the (public) code origin is not left behind for a bare `bd dolt push` to use.
    assert _remotes(first.checkout) == {"widget-private": world.internal_url}

    #    A private bead with history, published to the internal remote by the ordinary verb.
    private = _create(first.checkout, PRIVATE_TITLE)
    _bd("update", private, "--notes", "board-only: do not publish", cwd=first.checkout)
    assert git("status", "--porcelain", cwd=first.checkout).stdout.strip() == ""
    code, out = _invoke("hive", "sync", "remotes", "widget", "--push")
    assert code == 0, out
    assert "pushed dolt: widget-private (s3 remote" in out
    assert s3.keys("internal/github/acme/widget")
    history = _history(first.checkout, private)
    assert len(history) >= 2
    world.leave(first)

    # 2. A second host bootstraps the managed hive: the internal store is CLONED from the S3
    #    descriptor with full history, before (and instead of) any look at the code origin.
    second = world.host("second")
    probed = []
    world.monkeypatch.setattr(onboard, "_origin_has_dolt_data", lambda _ctx: probed.append(1))
    onboard._act_bd_init(_ctx(second))
    assert probed == []
    assert _titles(second.checkout) == {PRIVATE_TITLE}
    assert _history(second.checkout, private) == history
    assert _remotes(second.checkout) == {"widget-private": world.internal_url}
    assert git("status", "--porcelain", cwd=second.checkout).stdout.strip() == ""
    assert _rows(second.public_root) == []

    #    ...and `bh work` sees it as ordinary internal work.
    code, out = _invoke("work", "issue", private)
    assert code == 0 and PRIVATE_TITLE in out, out
    assert private in _ready_ids()

    # 3. GitHub -> public: the fixture issues are pulled exactly once, keep their external
    #    identity, and a re-pull updates in place. Nothing is ever written to GitHub.
    report = _pull(second.entry)
    assert report.ok and (report.created, report.projected) == (2, 2), report
    again = _pull(second.entry)
    assert again.ok and again.created == 0 and again.projected == 2, again
    public_rows = _rows(second.public_root)
    assert {row["title"] for row in public_rows} == GITHUB_TITLES
    assert sorted(row["external_ref"] for row in public_rows) == [
        "https://github.com/acme/widget/issues/1",
        "https://github.com/acme/widget/issues/2",
    ]
    assert all(row["id"].startswith("widpub-") for row in public_rows)
    assert github.writes == [] and github.requests
    public_ids = {row["id"] for row in public_rows}

    #    The projection never becomes dispatchable internal work.
    assert _ids(second.checkout) == {private}
    assert public_ids.isdisjoint(_ready_ids())
    some_public = sorted(public_ids)[0]
    code, out = _invoke("work", "claim", some_public, "--as", "dev/proof")
    assert code == 1 and "refuses" in out and "public store" in out, out
    with pytest.raises(store_target.PublicStoreNotOperational):
        store_target.require_operational(second.public(), "dispatch")

    # 4. Publication: the public store, and only it, goes to the code origin's Git Dolt ref.
    assert _origin_dolt_ref(world.origin) == ""
    published = public_publish.publish_public(second.entry)
    assert published.pushed and published.remote == "git-origin"
    assert _origin_dolt_ref(world.origin)

    # 5. The private bead is absent from the public store, its exported tables, the Git ref
    #    and an ordinary public clone — while the internal store still holds it.
    assert private not in _ids(second.public_root)
    export = publish_export.export_public_snapshot(second.public(), world.tmp / "export")
    exported = export.read_text()
    assert PRIVATE_TITLE not in exported and private not in exported
    assert {json.loads(line)["title"] for line in exported.splitlines()} == GITHUB_TITLES
    clone = _ordinary_public_clone(world, "public-clone")
    assert _titles(clone) == GITHUB_TITLES
    assert _bd("show", private, "--json", cwd=clone, check=False).returncode != 0
    assert "s3://" not in (clone / ".beads" / "config.yaml").read_text()
    with pytest.raises(publish_export.PublishScopeError):
        publish_export.export_public_snapshot(second.internal(), world.tmp / "never")

    # 6. Fleet diagnostics describe both stores honestly.
    rows = {row["scope"]: row for row in doctor._data_stores(config.load())}
    assert rows["internal"]["transport"] == store_target.TRANSPORT_DESCRIPTOR
    assert rows["internal"]["health"] == "unknown" and rows["internal"]["prefix"] == "widget"
    assert rows["public"]["transport"] == store_target.TRANSPORT_GIT_REF
    assert rows["public"]["health"] == "present" and rows["public"]["prefix"] == "widpub"
    assert rows["public"]["operational"] is False and rows["public"]["sync"] == "pull-only"
    code, out = _invoke("hive", "remote", "widget")
    assert code == 0 and "in-sync" in out, out

    # 7. New private work keeps flowing to the internal remote only; a re-publication still
    #    carries nothing private.
    later = _create(second.checkout, "PRIVATE follow-up")
    code, out = _invoke("hive", "sync", "remotes", "widget", "--push")
    assert code == 0, out
    public_publish.publish_public(second.entry)
    _bd("dolt", "pull", cwd=clone, env=embedded_env())
    assert _titles(clone) == GITHUB_TITLES
    assert later not in _ids(second.public_root)


# --------------------------------------------------------------------------------------------
# failure modes: each fails safely, with no cross-store mutation
# --------------------------------------------------------------------------------------------


def _snapshot(world: _World, host: Host) -> dict:
    return {
        "internal": _ids(host.checkout) if (host.checkout / ".beads").is_dir() else None,
        "public": _ids(host.public_root) if host.public_root.is_dir() else None,
        "origin": _origin_dolt_ref(world.origin),
        "s3": world.s3.keys("internal/github/acme/widget"),
    }


def test_bootstrap_order_interrupted_sync_retry_and_wrong_scope_fail_safely(world, github):
    host = world.host("solo")

    # Bootstrap order: with the internal remote unusable (no credentials on this host) the
    # internal phase fails FIRST — the code origin is never probed, no store is left behind,
    # and the public store is never minted.
    world.monkeypatch.setattr(
        onboard, "_origin_has_dolt_data", lambda _ctx: pytest.fail("probed the code origin")
    )
    with world.monkeypatch.context() as mp:
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            mp.delenv(key, raising=False)
        mp.setenv(onboard.SEED_REMOTE_ENV, "widget")
        with pytest.raises(typer.Exit):
            onboard._act_bd_init(_ctx(host))
    assert not (host.checkout / ".beads" / "metadata.json").exists()
    assert not host.public_root.exists()
    assert world.s3.keys() == [] and _origin_dolt_ref(world.origin) == ""

    # Retry: the same onboarding, credentials present, completes both phases in order.
    _seed_first_host(world, host)
    assert host.public().beads_dir.is_dir() and _ids(host.public_root) == set()
    private = _create(host.checkout, PRIVATE_TITLE)

    # Interrupted GitHub pull: the public store keeps nothing half-imported, the internal store
    # is untouched; the retry imports each issue exactly once.
    before = _snapshot(world, host)
    github.fail_next = 1
    with pytest.raises(github_projection.ProjectionError):
        _pull(host.entry)
    assert _snapshot(world, host) == before
    github.fail_next = 0
    assert _pull(host.entry).created == 2
    assert _pull(host.entry).created == 0
    assert _titles(host.public_root) == GITHUB_TITLES and _ids(host.checkout) == {private}
    assert github.writes == []

    # Interrupted internal sync: the S3 endpoint goes away mid-push. The push fails loudly and
    # changes nothing anywhere; the retry lands it in the internal remote only.
    before = _snapshot(world, host)
    world.s3.pause()
    try:
        code, out = _invoke("hive", "sync", "remotes", "widget", "--push")
    finally:
        world.s3.resume()
    assert code == 1 and "failed to push dolt: widget-private (s3 remote" in out, out
    assert _snapshot(world, host) == before
    code, out = _invoke("hive", "sync", "remotes", "widget", "--push")
    assert code == 0, out
    after = _snapshot(world, host)
    assert after["s3"] != before["s3"]
    assert {k: after[k] for k in ("internal", "public", "origin")} == {
        k: before[k] for k in ("internal", "public", "origin")
    }

    # Wrong scope: every operational or cross-scope request is refused before it reads or
    # writes a store.
    before = _snapshot(world, host)
    public_id = sorted(before["public"])[0]
    for verb in ("claim", "assign", "resume"):
        args = ["work", verb, public_id] + (["--to", "dev/x"] if verb == "assign" else [])
        code, out = _invoke(*args)
        assert code == 1 and "public store" in out, (verb, out)
    legacy = {k: v for k, v in host.entry.items() if k != "stores"}
    internal_only = dict(host.entry, stores={"internal": {"remote": "widget-private"}})
    for entry in (legacy, internal_only):
        with pytest.raises(store_target.StoreNotConfigured):
            _pull(entry)
        with pytest.raises(store_target.StoreNotConfigured):
            public_publish.publish_public(entry)
    with pytest.raises(publish_export.PublishScopeError):
        publish_export.export_public_snapshot(host.internal(), world.tmp / "never")
    with pytest.raises(store_target.PublicStoreNotOperational):
        store_target.require_operational(host.public(), "claim")
    assert _snapshot(world, host) == before


def test_accidental_public_export_is_refused_without_overwriting_anything(world, github):
    host = world.host("solo")
    _seed_first_host(world, host)
    _create(host.checkout, PRIVATE_TITLE)
    assert _pull(host.entry).created == 2

    # The code origin carries a LEGACY corpus on refs/dolt/data (a repository whose old single
    # store still publishes there): publishing the public store over it must not replace it.
    # (Seeded after the host above: bd's own `bd init` would otherwise auto-clone it — bh-wy2ia.)
    legacy = world.tmp / "legacy"
    git("clone", "-q", str(world.origin), str(legacy))
    env = embedded_env()
    init = ["bd", "init", "--prefix", "widget", "--quiet", "--skip-hooks", "--skip-agents"]
    assert run(init, cwd=str(legacy), check=False, capture=True, env=env).returncode == 0
    _bd("q", "LEGACY internal plan", cwd=legacy, env=env)
    _bd("dolt", "push", cwd=legacy, env=env)
    legacy_ref = _origin_dolt_ref(world.origin)
    assert legacy_ref
    before = _snapshot(world, host)

    with pytest.raises(public_publish.PublishError) as caught:
        public_publish.publish_public(host.entry)
    assert "nothing was forced" in str(caught.value)
    assert _origin_dolt_ref(world.origin) == legacy_ref
    assert _snapshot(world, host) == before
    assert _titles(_ordinary_public_clone(world, "legacy-clone")) == {"LEGACY internal plan"}

    # A public store aimed at a non-Git remote, or at the internal store's own remote, is
    # refused before anything is registered or pushed.
    s3_public = dict(
        host.entry,
        stores={
            "internal": {"remote": "widget-private"},
            "public": {
                **host.entry["stores"]["public"],
                "remote": "widget-pub",
                "dolt_remote": {"name": "widget-pub", "url": world.s3.url("public/widget")},
            },
        },
    )
    with pytest.raises(public_publish.PublishError, match="only to a Git Dolt ref"):
        public_publish.publish_public(s3_public)
    with pytest.raises(public_publish.PublishError, match="internal store's remote"):
        public_publish.plan_publication(
            dict(host.entry, dolt_remote={"name": "widget-private", "url": "git+file:///x.git"}),
            origin_url="/x.git",
        )
    assert _snapshot(world, host) == before


def test_seeding_never_reads_the_code_origins_legacy_corpus(world):
    """bh-wy2ia: with a legacy corpus already on the code origin's refs/dolt/data, seeding the
    EMPTY internal remote mints a FRESH store (bd init must not auto-clone the origin) and
    leaves the origin ref untouched."""
    legacy = world.tmp / "legacy"
    git("clone", "-q", str(world.origin), str(legacy))
    env = embedded_env()
    init = ["bd", "init", "--prefix", "widget", "--quiet", "--skip-hooks", "--skip-agents"]
    assert run(init, cwd=str(legacy), check=False, capture=True, env=env).returncode == 0
    _bd("q", "LEGACY internal plan", cwd=legacy, env=env)
    _bd("dolt", "push", cwd=legacy, env=env)
    legacy_ref = _origin_dolt_ref(world.origin)
    assert legacy_ref

    host = world.host("solo")
    _seed_first_host(world, host)

    assert _titles(host.checkout) == set()  # fresh, not the legacy corpus
    assert _origin_dolt_ref(world.origin) == legacy_ref
    assert _remotes(host.checkout) == {"widget-private": world.internal_url}
    assert world.s3.keys("internal/github/acme/widget")
    _create(host.checkout, PRIVATE_TITLE)
    assert _titles(host.checkout) == {PRIVATE_TITLE}
    assert _origin_dolt_ref(world.origin) == legacy_ref


def test_store_collisions_are_refused_before_any_store_exists(world):
    host = world.host("solo")
    other = {
        "provider": "github",
        "org": "acme",
        "repo": "gadget",
        "prefix": "widpub",
    }
    collisions = {
        "public prefix equals the internal prefix": [_entry(host.public_root, world.internal_url)],
        "public prefix taken by another hive": [
            _entry(host.public_root, world.internal_url),
            other,
        ],
        "public names the internal descriptor": [
            _entry(host.public_root, world.internal_url, remote="widget-private")
        ],
        "public descriptor shares the internal location": [
            _entry(
                host.public_root,
                world.internal_url,
                remote="widget-pub",
                dolt_remote={"name": "widget-pub", "url": world.internal_url},
            )
        ],
    }
    collisions["public prefix equals the internal prefix"][0]["stores"]["public"]["prefix"] = (
        "widget"
    )
    for case, entries in collisions.items():
        _write_config(host.cfg_path, *entries)
        with pytest.raises(config.ConfigError):
            config.load()
        assert not (host.checkout / ".beads").exists(), case
        assert not host.public_root.exists(), case

    # A public path inside the code checkout is refused at onboarding, before any store exists.
    inside = _entry(host.checkout / "public", world.internal_url)
    host.entry = inside
    _write_config(host.cfg_path, inside)
    with pytest.raises(typer.Exit):
        onboard._act_bd_init(_ctx(host))
    assert not (host.checkout / ".beads").exists()
    assert not (host.checkout / "public").exists()
    assert world.s3.keys() == [] and _origin_dolt_ref(world.origin) == ""
