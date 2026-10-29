"""What the security center shows about a device: a label from the user agent and a shortened
address (ADR 0065).

Pure functions without any lookup service: MowDeck sends no address to a third party, so there
is no city or country. The address is shortened to its network (``203.0.113.x``,
``2001:db8:1::/48``) and private, loopback and link-local addresses are called local.
"""

import ipaddress
import re
from dataclasses import dataclass
from enum import StrEnum

APP_MARKER = "MowDeckApp"
"""Token the native shells append to the web view's user agent (``appendUserAgent``)."""


class ClientKind(StrEnum):
    """Where a session lives."""

    BROWSER = "browser"
    APP = "app"
    """The native MowDeck app (its web view)."""
    SCRIPT = "script"
    """No browser: curl, a script, Home Assistant's HTTP client."""
    UNKNOWN = "unknown"


class NetworkKind(StrEnum):
    """What kind of address a session came from."""

    LOCAL = "local"
    """Private, loopback or link-local: the home network or the same machine."""
    PUBLIC = "public"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """A device as far as its user agent tells."""

    client: ClientKind
    browser: str | None
    os: str | None

    @property
    def label(self) -> str:
        """``Firefox on Windows``, ``MowDeck app on iOS``, ``curl``; English, the web app
        builds its own text from the parts."""
        name = "MowDeck app" if self.client is ClientKind.APP else self.browser
        if name and self.os:
            return f"{name} on {self.os}"
        return name or self.os or "Unknown device"


# Order matters: Edge and Opera carry "Chrome", Chrome carries "Safari", iOS browsers carry
# "Safari" and name themselves with their own token.
_BROWSERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Edge", re.compile(r"\bEdg(e|A|iOS)?/")),
    ("Opera", re.compile(r"\b(OPR|Opera)/")),
    ("Samsung Internet", re.compile(r"\bSamsungBrowser/")),
    ("Firefox", re.compile(r"\b(Firefox|FxiOS)/")),
    ("Chrome", re.compile(r"\b(HeadlessChrome|Chrome|Chromium|CriOS)/")),
    ("Safari", re.compile(r"\bVersion/[\d.]+.*\bSafari/")),
)
_SCRIPTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("curl", re.compile(r"^curl/", re.IGNORECASE)),
    ("Python", re.compile(r"^(python-httpx|python-requests|aiohttp|Python-urllib)/", re.I)),
    ("Home Assistant", re.compile(r"HomeAssistant", re.IGNORECASE)),
    ("Node.js", re.compile(r"^(node|undici|axios)", re.IGNORECASE)),
    ("Playwright", re.compile(r"Playwright", re.IGNORECASE)),
)
_SYSTEMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iPadOS", re.compile(r"\biPad\b")),
    ("iOS", re.compile(r"\b(iPhone|iPod)\b")),
    ("Android", re.compile(r"\bAndroid\b")),
    ("Windows", re.compile(r"\bWindows\b")),
    ("ChromeOS", re.compile(r"\bCrOS\b")),
    ("macOS", re.compile(r"\bMac OS X\b|\bMacintosh\b")),
    ("Linux", re.compile(r"\bLinux\b|\bX11\b")),
)


def _first(table: tuple[tuple[str, re.Pattern[str]], ...], text: str) -> str | None:
    return next((name for name, pattern in table if pattern.search(text)), None)


def describe_user_agent(user_agent: str | None) -> DeviceInfo:
    """Client kind, browser family and operating system of ``user_agent`` (no versions: a
    browser update must not look like a new device)."""
    text = (user_agent or "").strip()
    if not text:
        return DeviceInfo(ClientKind.UNKNOWN, None, None)
    system = _first(_SYSTEMS, text)
    if re.search(rf"\b{APP_MARKER}\b", text):
        return DeviceInfo(ClientKind.APP, None, system)
    browser = _first(_BROWSERS, text)
    if browser is None and "Mozilla/" not in text:
        script = _first(_SCRIPTS, text)
        return DeviceInfo(ClientKind.SCRIPT, script or text.split("/", 1)[0][:40], None)
    if browser is None and system in ("iOS", "iPadOS") and "AppleWebKit" in text:
        browser = "Safari"
    return DeviceInfo(ClientKind.BROWSER, browser, system)


def ip_prefix(ip: str | None) -> tuple[str | None, NetworkKind]:
    """The address shortened to its network, and whether it is local.

    IPv4 keeps three octets (``203.0.113.x``), IPv6 the /48 (``2001:db8:1::/48``); an
    IPv4-mapped IPv6 address counts as IPv4. Anything unparsable is unknown.
    """
    if not ip:
        return None, NetworkKind.UNKNOWN
    try:
        address = ipaddress.ip_address(ip.strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None, NetworkKind.UNKNOWN
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    local = address.is_private or address.is_loopback or address.is_link_local
    kind = NetworkKind.LOCAL if local else NetworkKind.PUBLIC
    if isinstance(address, ipaddress.IPv4Address):
        head = str(address).rsplit(".", 1)[0]
        return f"{head}.x", kind
    network = ipaddress.IPv6Network(f"{address}/48", strict=False)
    return str(network), kind
