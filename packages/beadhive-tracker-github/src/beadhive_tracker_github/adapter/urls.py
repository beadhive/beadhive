"""Where the bound repository lives, and the URL grammar of the optional ``IssueResolver``.

:class:`GitHubLocation` derives everything URL-shaped from the port's ``ProjectRef("github",
"owner/name")`` and the optional ``base_url`` (a GitHub Enterprise Server API prefix):

* the **API base** requests go to (``https://api.github.com`` by default);
* the **web root** issue URLs live under (``https://github.com``; for GHES the API host without
  its ``/api/v3`` path; for an ``api.``-prefixed host, the host without ``api.``);
* the canonical issue URL, ``<web root>/<owner>/<name>/issues/<number>``, which is
  ``IssueKey.url``, the consumers' join key, and equal to the ``html_url`` GitHub reports.

:meth:`GitHubLocation.resolve` is pure (amendment A1): it never sends a request and each refusal
is its own port error, in this order: ``UrlNotRecognized`` (not an issue or pull-request URL of
this GitHub host), ``OtherProject`` (another repository), ``ChangeRequestUrl`` (a pull request).
GitHub owner and repository names are case-insensitive, so they are compared case-folded and the
canonical URL carries the bound project's spelling. A refusal never repeats the URL.

:meth:`GitHubLocation.check_next` guards the one URL this adapter follows that it did not build:
the ``Link: rel="next"`` URL a page cursor carries. A cursor is opaque but comes back from the
consumer; only a URL on the API origin and under the API prefix is followed, so a tampered or
foreign cursor can never send the credential to another host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from beadhive.modules.tracker.contracts import (
    ChangeRequestUrl,
    IssueKey,
    OtherProject,
    ProjectRef,
    UrlNotRecognized,
)

__all__ = ["PUBLIC_API", "PUBLIC_WEB", "GitHubLocation"]

PUBLIC_API = "https://api.github.com"
PUBLIC_WEB = "https://github.com"

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_NUMBER = re.compile(r"[0-9]{1,18}")
_ISSUE_KINDS = frozenset({"issues"})
_CHANGE_KINDS = frozenset({"pull", "pulls"})


def _origin(scheme: str, netloc: str) -> str:
    return f"{scheme.lower()}://{netloc.lower()}"


@dataclass(frozen=True)
class GitHubLocation:
    owner: str
    name: str
    api_base: str  # no trailing slash
    web_root: str  # no trailing slash

    @classmethod
    def from_project(cls, project: ProjectRef, base_url: str | None = None) -> GitHubLocation:
        """Static messages: neither the locator nor the URL is repeated (either may be pasted)."""
        if project.tracker != "github":
            raise ValueError("the GitHub adapter serves only a 'github' project")
        owner, _, name = project.locator.partition("/")
        if not (_OWNER.fullmatch(owner) and _NAME.fullmatch(name) and name not in {".", ".."}):
            raise ValueError("the project locator must be 'owner/name'")
        api_base = (base_url or PUBLIC_API).rstrip("/")
        parts = urlsplit(api_base)
        if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc:
            raise ValueError("base_url must be an https URL without userinfo")
        if parts.query or parts.fragment:
            raise ValueError("base_url must not carry a query or fragment")
        host = parts.netloc.lower()
        if api_base.lower() == PUBLIC_API:
            web_root = PUBLIC_WEB
        elif host.startswith("api."):
            web_root = f"https://{host.removeprefix('api.')}"
        else:
            web_root = f"https://{host}"
        return cls(owner, name, api_base, web_root)

    @property
    def locator(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def repo_path(self) -> str:
        """The repository's REST path, relative to the API base."""
        return f"/repos/{self.owner}/{self.name}"

    def issue_url(self, number: int) -> str:
        return f"{self.web_root}/{self.owner}/{self.name}/issues/{number}"

    def issue_key(self, number: int, *, internal_id: str | None = None) -> IssueKey:
        return IssueKey(f"#{number}", self.issue_url(number), internal_id=internal_id)

    def same_repository(self, owner: str, name: str) -> bool:
        return owner.casefold() == self.owner.casefold() and name.casefold() == self.name.casefold()

    # -- the IssueResolver grammar -------------------------------------------------------------

    def _segments(self, url: str) -> tuple[str, list[str]] | None:
        """``("web" | "api", path segments below the root)`` for a URL on this host, else None."""
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return None
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return None
        origin = _origin("https", parts.netloc)
        path = parts.path
        for kind, root in (("api", self.api_base), ("web", self.web_root)):
            root_parts = urlsplit(root)
            if origin != _origin("https", root_parts.netloc):
                continue
            prefix = root_parts.path.rstrip("/")
            if prefix and not (path == prefix or path.startswith(prefix + "/")):
                continue
            below = path[len(prefix) :]
            return kind, below.split("/")[1:]
        return None

    def _parse(self, url: str) -> tuple[str, str, str, int] | None:
        """``(owner, name, kind, number)`` of an issue or pull-request URL, else None."""
        found = self._segments(url)
        if found is None:
            return None
        where, segments = found
        if where == "api":
            if len(segments) < 5 or segments[0] != "repos":
                return None
            segments = segments[1:]
        if segments and segments[-1] == "":  # one trailing slash
            segments = segments[:-1]
        if len(segments) < 4:
            return None
        owner, name, kind, number = segments[:4]
        rest = segments[4:]
        if not (_OWNER.fullmatch(owner) and _NAME.fullmatch(name) and _NUMBER.fullmatch(number)):
            return None
        if int(number) < 1:
            return None
        if kind in _ISSUE_KINDS and not rest:
            return owner, name, "issue", int(number)
        if kind in _CHANGE_KINDS and (where == "web" or not rest):
            return owner, name, "change", int(number)
        return None

    def resolve(self, url: str) -> IssueKey:
        parsed = self._parse(url)
        if parsed is None:
            raise UrlNotRecognized("not an issue URL of this GitHub host")
        owner, name, kind, number = parsed
        if not self.same_repository(owner, name):
            raise OtherProject("the URL names an issue of another repository")
        if kind == "change":
            raise ChangeRequestUrl("the URL names a pull request, not an issue")
        return self.issue_key(number)

    def parse_html_url(self, url: str) -> tuple[str, str, str, int] | None:
        """The parsed form of an ``html_url`` GitHub reported (used to spot a transfer)."""
        return self._parse(url)

    # -- following a page cursor ---------------------------------------------------------------

    def check_next(self, url: str) -> str:
        """``url`` when it is safe to follow verbatim (API origin, under the API prefix)."""
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        base = urlsplit(self.api_base)
        prefix = base.path.rstrip("/")
        if (
            parts is None
            or parts.scheme.lower() != "https"
            or "@" in parts.netloc
            or _origin(parts.scheme, parts.netloc) != _origin(base.scheme, base.netloc)
            or not (parts.path.startswith(prefix + "/") if prefix else parts.path.startswith("/"))
        ):
            raise ValueError("a page cursor from another adapter or host")
        return url
