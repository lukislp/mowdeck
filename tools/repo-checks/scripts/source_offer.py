"""Write the GPL source offer of a release (SOURCE-OFFER.md) from the images' SPDX SBOMs.

The release workflow (job ``publish``) runs this over the ``sbom-<image>.spdx.json`` files it
attaches to the GitHub release. Every package whose declared or concluded licence belongs to the
GPL family (GPL, LGPL, AGPL, also as ``LicenseRef-...GPL...``) is listed with its version, the
images that contain it and where its corresponding source is: the Debian snapshot archive for
Debian packages, Alpine's aports for Alpine packages, PyPI for Python distributions, upstream for
a few statically classified binaries (busybox), and this repository at the release tag for
MowDeck's own packages. A written offer to provide the source on request for at least three
years precedes the list.

    python tools/repo-checks/scripts/source_offer.py --version 0.1.0 \
        --repository lukislp/mowdeck --out SOURCE-OFFER.md sbom-*.spdx.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

GPL_FAMILY = re.compile(r"(?<![A-Za-z])(A|L)?GPL", re.IGNORECASE)

# Binaries syft recognises by their content only (pkg:generic, no licence in the SBOM). Name ->
# (licence, source URL template with {version}).
KNOWN_BINARIES: dict[str, tuple[str, str]] = {
    "busybox": ("GPL-2.0-only", "https://busybox.net/downloads/busybox-{version}.tar.bz2"),
    "ffmpeg": ("GPL-2.0-or-later", "https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz"),
}

IMAGE_FROM_FILE = re.compile(r"^sbom-(?P<image>.+)\.spdx\.json$")


@dataclass
class Component:
    """One GPL-family package version, with the images that ship it."""

    kind: str
    name: str
    version: str
    licence: str
    source: str
    images: set[str] = field(default_factory=set)


def parse_purl(purl: str) -> tuple[str, str, str, str, dict[str, str]]:
    """Split a package URL into type, namespace, name, version and qualifiers."""
    rest = purl.removeprefix("pkg:")
    rest, _, qualifier_text = rest.partition("?")
    rest = rest.partition("#")[0]
    path, _, version = rest.partition("@")
    parts = path.split("/")
    kind = parts[0]
    name = unquote(parts[-1])
    namespace = unquote("/".join(parts[1:-1]))
    qualifiers: dict[str, str] = {}
    for pair in filter(None, qualifier_text.split("&")):
        key, _, value = pair.partition("=")
        qualifiers[key] = unquote(value)
    return kind, namespace, name, unquote(version), qualifiers


def own_packages(repo_root: Path) -> set[str]:
    """Names of the Python and npm packages built from this repository."""
    names: set[str] = set()
    skip = {".venv", "node_modules", ".git", "site", "build"}
    for path in repo_root.rglob("pyproject.toml"):
        if skip.intersection(path.relative_to(repo_root).parts):
            continue
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        if "name" in project:
            names.add(normalise(project["name"]))
    for path in repo_root.rglob("package.json"):
        if skip.intersection(path.relative_to(repo_root).parts):
            continue
        name = json.loads(path.read_text(encoding="utf-8")).get("name")
        if isinstance(name, str):
            names.add(normalise(name))
    return names


def normalise(name: str) -> str:
    """PEP 503 style name comparison (also used for npm names)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def licence_of(package: dict[str, Any]) -> str:
    """The most specific licence expression the SBOM has for a package."""
    for key in ("licenseConcluded", "licenseDeclared"):
        value = package.get(key) or ""
        if value and value not in {"NOASSERTION", "NONE"}:
            return value
    return ""


def debian_source(name: str, version: str, qualifiers: dict[str, str]) -> str:
    """Debian source package of a binary package in the snapshot archive."""
    upstream = qualifiers.get("upstream", "")
    source, _, source_version = upstream.partition("@")
    source = source or name
    # A binNMU (+bN) is a rebuild of the same source version.
    source_version = source_version or re.sub(r"\+b\d+$", "", version)
    encoded = quote(source_version, safe="")
    return (
        f"Debian source package `{source}` {source_version}: "
        f"<https://snapshot.debian.org/package/{source}/{encoded}/>"
    )


def alpine_source(name: str, version: str, qualifiers: dict[str, str]) -> str:
    """APKBUILD, patches and distfiles of an Alpine package in aports."""
    origin = qualifiers.get("upstream", "").partition("@")[0] or name
    distro = qualifiers.get("distro", "")
    match = re.match(r"alpine-(\d+\.\d+)", distro)
    branch = f"{match.group(1)}-stable" if match else "master"
    release = f"v{match.group(1)}" if match else "edge"
    return (
        f"Alpine aports `{origin}` {version} (APKBUILD and patches under main/ or community/): "
        f"<https://gitlab.alpinelinux.org/alpine/aports/-/tree/{branch}>, source archives "
        f"<https://distfiles.alpinelinux.org/distfiles/{release}/>"
    )


def component_for(
    package: dict[str, Any], own: set[str], repository: str, tag: str
) -> Component | None:
    """The GPL component a package stands for, or None when it is not GPL-family."""
    purls = [
        ref["referenceLocator"]
        for ref in package.get("externalRefs", [])
        if ref.get("referenceType") == "purl"
    ]
    if not purls:
        return None
    kind, _, name, version, qualifiers = parse_purl(purls[0])
    licence = licence_of(package)
    if kind == "generic" and not licence and name in KNOWN_BINARIES:
        licence, template = KNOWN_BINARIES[name]
        return Component(kind, name, version, licence, f"<{template.format(version=version)}>")
    if not licence or not GPL_FAMILY.search(licence):
        return None
    own_repo = f"https://github.com/{repository}/tree/{tag}"
    if kind in {"pypi", "npm"} and normalise(name) in own:
        source = f"this repository at `{tag}`: <{own_repo}>"
    elif kind == "deb":
        source = debian_source(name, version, qualifiers)
    elif kind == "apk":
        source = alpine_source(name, version, qualifiers)
    elif kind == "pypi":
        source = f"PyPI source distribution: <https://pypi.org/project/{name}/{version}/#files>"
    elif kind == "npm":
        source = (
            f"npm package (contains the source): <https://www.npmjs.com/package/{name}/v/{version}>"
        )
    elif kind == "generic" and name in KNOWN_BINARIES:
        source = f"<{KNOWN_BINARIES[name][1].format(version=version)}>"
    else:
        source = f"upstream of `{purls[0]}`; on request (see the offer above)"
    return Component(kind, name, version, licence, source)


def collect(sboms: list[Path], own: set[str], repository: str, tag: str) -> list[Component]:
    """Every GPL-family component of the given SBOMs, merged across images."""
    found: dict[tuple[str, str, str], Component] = {}
    for path in sboms:
        match = IMAGE_FROM_FILE.match(path.name)
        image = match.group("image") if match else path.stem
        document = json.loads(path.read_text(encoding="utf-8"))
        for package in document.get("packages", []):
            component = component_for(package, own, repository, tag)
            if component is None:
                continue
            key = (component.kind, component.name, component.version)
            merged = found.setdefault(key, component)
            merged.images.add(image)
    return sorted(found.values(), key=lambda c: (c.kind, c.name.lower(), c.version))


def cell(text: str) -> str:
    """Escape a value for a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def render(components: list[Component], version: str, repository: str, images: list[str]) -> str:
    """SOURCE-OFFER.md."""
    tag = f"v{version}"
    issues = f"https://github.com/{repository}/issues/new"
    private = f"https://github.com/{repository}/security/advisories/new"
    lines = [
        f"# MowDeck {version}: source code offer",
        "",
        f"The container images of MowDeck {version} (`ghcr.io/{repository.split('/')[0].lower()}"
        f"/mowdeck/<image>:{version}`) contain software under the GNU General Public License, "
        "the GNU Lesser General Public License and the GNU Affero General Public License. This "
        "file lists every such component found in the images' SBOMs (the `sbom-<image>.spdx.json` "
        "files of this release), with the place where its complete corresponding source code is "
        "available.",
        "",
        f"MowDeck's own code (AGPL-3.0, Apache-2.0, and the GPL-3.0 adapter "
        f"`mowdeck-control-mammotion`) is this repository at the tag `{tag}`: "
        f"<https://github.com/{repository}/tree/{tag}>, as an archive "
        f"<https://github.com/{repository}/archive/refs/tags/{tag}.tar.gz>. The third-party "
        "packages are used unmodified as the distributions ship them; the build instructions "
        "are the Dockerfiles at the same tag.",
        "",
        "## Written offer",
        "",
        "For at least three years after the publication of this release, and for as long as "
        "this release is offered for download, any third party may request a complete "
        "machine-readable copy of the corresponding source code of every component listed "
        "below, for the version listed here. The source is provided on a medium customarily "
        "used for software interchange (normally a download link), at no charge beyond the "
        "cost of physically performing the source distribution. To request it, open an issue "
        f"titled `Source request {tag}` at <{issues}>; if you prefer a non-public request, use "
        f"the repository's private reporting channel at <{private}> and say that it is a "
        "source request.",
        "",
        "## Components",
        "",
        "Generated from the SBOMs of the linux/amd64 images; the linux/arm64 images contain the "
        "same source versions. Images: " + ", ".join(f"`{i}`" for i in images) + ".",
        "",
        "| Package | Version | Licence | Images | Corresponding source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for c in components:
        lines.append(
            f"| `{cell(c.name)}` ({c.kind}) | {cell(c.version)} | {cell(c.licence)} | "
            f"{', '.join(sorted(c.images))} | {cell(c.source)} |"
        )
    if not components:
        lines.append("| (none found) | | | | |")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--version", required=True, help="release version without the v")
    parser.add_argument("--repository", required=True, help="owner/name on GitHub")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("sboms", nargs="+", type=Path)
    args = parser.parse_args(argv)
    tag = f"v{args.version}"
    own = own_packages(args.repo_root)
    components = collect(args.sboms, own, args.repository, tag)
    images = sorted(m.group("image") for p in args.sboms if (m := IMAGE_FROM_FILE.match(p.name)))
    args.out.write_text(render(components, args.version, args.repository, images), encoding="utf-8")
    print(f"{args.out}: {len(components)} GPL-family components in {len(images)} images")
    return 0


if __name__ == "__main__":
    sys.exit(main())
