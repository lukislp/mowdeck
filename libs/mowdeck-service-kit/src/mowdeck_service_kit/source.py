"""Where users get the source code of the running version (AGPL-3.0 section 13).

Every HTTP response of a service carries the header ``X-Source-Code`` with the address of the
source code of exactly this build (LICENSING.md). The address is built from three settings that
the image build bakes in and the operator may override at runtime:

* ``MOWDECK_SOURCE_URL``: the repository (default: the official one). A fork that runs modified
  code must point it to its own public repository.
* ``MOWDECK_SOURCE_REF``: the git ref of the build: the tag ``v<version>`` of a release, a
  commit for a test build. Without it a release version gives ``v<version>``.
* ``MOWDECK_VERSION``: the version of the build (empty for a development build).

``https://github.com/owner/repo`` becomes ``https://github.com/owner/repo/tree/<ref>``. Hosts
with other paths take a template with the placeholder ``{ref}`` instead, for example
``https://codeberg.org/owner/repo/src/tag/{ref}``. Without a ref (a development build) the link
is the repository itself (a template gets ``HEAD``).
"""

import re
from urllib.parse import quote

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SOURCE_HEADER = "X-Source-Code"
"""Response header with the source code address of the running build."""

DEFAULT_SOURCE_URL = "https://github.com/lukislp/mowdeck"
"""The official repository."""

REF_PLACEHOLDER = "{ref}"

_URL = re.compile(r"^https?://[^\s\"'<>\\]+$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.\-]+)?(?:\+[0-9A-Za-z.\-]+)?$")


def release_ref(version: str | None) -> str | None:
    """The tag of a semantic version (``1.2.3`` -> ``v1.2.3``), None for anything else."""
    if not version or not _SEMVER.match(version.strip()):
        return None
    return "v" + version.strip().removeprefix("v")


def source_code_url(
    repository: str | None = None, *, version: str | None = None, ref: str | None = None
) -> str:
    """The address of the source code of a build.

    Args:
        repository: Repository URL, optionally with ``{ref}``; empty or invalid (not http(s),
            or with whitespace or quotes) falls back to :data:`DEFAULT_SOURCE_URL`.
        version: The build's version; a semantic version gives the ref ``v<version>``.
        ref: The build's git ref; wins over ``version``. Invalid refs are ignored.
    """
    base = (repository or "").strip()
    if not _URL.match(base):
        base = DEFAULT_SOURCE_URL
    chosen = (ref or "").strip()
    resolved = chosen if _REF.match(chosen) and ".." not in chosen else release_ref(version)
    if REF_PLACEHOLDER in base:
        return base.replace(REF_PLACEHOLDER, quote(resolved or "HEAD", safe="/"))
    base = base.rstrip("/")
    if base.endswith(".git"):
        base = base[: -len(".git")]
    return f"{base}/tree/{quote(resolved, safe='/')}" if resolved else base


class SourceCodeMiddleware:
    """Pure ASGI middleware adding :data:`SOURCE_HEADER` to every HTTP response."""

    def __init__(self, app: ASGIApp, *, url: str) -> None:
        self.app = app
        self.url = url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[SOURCE_HEADER] = self.url
            await send(message)

        await self.app(scope, receive, send_with_header)
