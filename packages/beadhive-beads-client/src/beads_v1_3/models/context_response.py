from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ContextResponse")


@_attrs_define
class ContextResponse:
    """The server's identity handshake. Every member is a deliberate, permanent choice; the field set is an allowlist
    frozen by a test that checks it against BOTH this document and the generated Go struct, so a field cannot arrive
    here as a side effect of the server's configuration growing one. In particular the workspace's sync remote is
    EXCLUDED, in this and every future version, because remote URLs routinely embed credentials — as are the database
    bind host/port (advertising them invites clients to bypass this API and dial the database directly) and the
    loopback/non-loopback bind mode.

    TWO MEMBERS ARE OPTIONAL, and they are the only two that describe the SERVER's filesystem rather than the
    workspace's logical identity: `beads_dir` and `repo_root`. A client must be able to read them as absent. Everything
    a remote caller identifies a workspace by is elsewhere and stays required — `project_id`, `database`, `backend`,
    `dolt_mode` — and an absolute host path is not something a remote caller can act on in any case: it cannot open it.
    `bd serve` publishes both, so a client reading a `bd serve` today sees no change; what the relaxation buys is that a
    deployment which does not want to disclose its filesystem layout can withhold them and still serve a body that
    conforms to this document.

        Attributes:
            api_version (str): The path major this server serves. `v0` for this document.
            bd_version (str): The release version of the serving binary. The only field a client may compare as a version,
                and only for behavioral changes tied to a release.
            schema_version (int): The shared JSON schema version — the same constant the CLI's stdout JSON envelope reports.
                Diagnostic only: it can move for CLI-only reasons with no HTTP wire change, so clients MUST NOT branch on it.
            backend (str): Storage backend name.
            dolt_mode (str): Which storage mode this workspace is served from.
            database (str): Logical database name (not a host or a DSN).
            project_id (str): Logical project identifier.
            capabilities (list[str]): The tokens this server advertises: the OPERATIONS it implements, derived from its
                route table, and the server-wide BEHAVIORS it enforces. v0's operation vocabulary is `ready.list`,
                `ready.count`, `issues.list`, `issues.query`, `issues.count`, `issues.get`, `issues.related`, `issues.create`,
                `issues.addComment`, `issues.batchClose`, `issues.claim`, `issues.claimNext`, `issues.release`, `issues.close`,
                `issues.reopen`, `issues.update`, `issues.sweep`, `issues.delete`, `issues.batchCreate`, `issues.batchApply`,
                `stats.get`, `config.list`, `config.get`, `config.set`, `config.unset`, `dependencies.cycles`,
                `dependencies.list`, `dependencies.count`, `dependencies.blocking`, `dependencies.tree`, `dependencies.add`,
                `dependencies.remove`, `memories.list`, `memories.get`, `memories.remember`, `memories.forget`, `events.list`,
                `events.watch`, `issues.casMetadata`; the one behavior token is `project.enforce`, which announces that a `Bd-
                Project-Id` stamp for the wrong workspace is refused here rather than silently ignored. The list grows
                additively, and an operation never appears here unless it is fully implemented. This is how a client checks for
                an operation or a behavior — never the version string.

                THIS LIST IS BUILD-LEVEL, NOT WORKSPACE-LEVEL. It says which operations this binary serves, and for every entry
                but two that is the whole answer. `events.list` and `events.watch` are the exceptions: the durable events
                journal is a per-workspace setting that is OFF by default, so a server that advertises them may still refuse
                every request to both with 409 `events_journal_disabled` — correctly, because the operations exist and the
                workspace has no journal. A consumer of either MUST treat the capability as "this server speaks it" and the 409
                as "not on this workspace", and must not read the capability as a promise that records will arrive.
            beads_dir (str | Unset): Absolute path of the served workspace's `.beads` directory, when the server discloses
                it. A host path: `bd serve` publishes it because it is a single-workspace server's most legible workspace-
                identity handshake for the operator reading it, and disclosing it to network peers is part of what that operator
                accepts when binding beyond loopback.

                OPTIONAL, and absent means only that this server does not disclose its filesystem layout — never that it has no
                workspace. A client MUST NOT require it, MUST NOT treat absence as an error, and has no use for the value beyond
                display: it is a path on the SERVER's filesystem, which the client cannot open. Identify the workspace by
                `project_id` and `database`, which are required.
            repo_root (str | Unset): Absolute path of the served repository root, when the server discloses it. OPTIONAL on
                the same terms as `beads_dir`; see it.
    """

    api_version: str
    bd_version: str
    schema_version: int
    backend: str
    dolt_mode: str
    database: str
    project_id: str
    capabilities: list[str]
    beads_dir: str | Unset = UNSET
    repo_root: str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        api_version = self.api_version

        bd_version = self.bd_version

        schema_version = self.schema_version

        backend = self.backend

        dolt_mode = self.dolt_mode

        database = self.database

        project_id = self.project_id

        capabilities = self.capabilities

        beads_dir = self.beads_dir

        repo_root = self.repo_root

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "api_version": api_version,
                "bd_version": bd_version,
                "schema_version": schema_version,
                "backend": backend,
                "dolt_mode": dolt_mode,
                "database": database,
                "project_id": project_id,
                "capabilities": capabilities,
            }
        )
        if beads_dir is not UNSET:
            field_dict["beads_dir"] = beads_dir
        if repo_root is not UNSET:
            field_dict["repo_root"] = repo_root

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        api_version = d.pop("api_version")

        bd_version = d.pop("bd_version")

        schema_version = d.pop("schema_version")

        backend = d.pop("backend")

        dolt_mode = d.pop("dolt_mode")

        database = d.pop("database")

        project_id = d.pop("project_id")

        capabilities = cast(list[str], d.pop("capabilities"))

        beads_dir = d.pop("beads_dir", UNSET)

        repo_root = d.pop("repo_root", UNSET)

        context_response = cls(
            api_version=api_version,
            bd_version=bd_version,
            schema_version=schema_version,
            backend=backend,
            dolt_mode=dolt_mode,
            database=database,
            project_id=project_id,
            capabilities=capabilities,
            beads_dir=beads_dir,
            repo_root=repo_root,
        )

        context_response.additional_properties = d
        return context_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
