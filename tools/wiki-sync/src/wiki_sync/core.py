"""Core logic: page naming, link rewriting, sidebar and the build itself."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

REPO_BLOB_URL = "https://github.com/lukislp/mowdeck/blob/main/docs"

DIR_PREFIXES = {
    "architecture": "Architecture",
    "adr": "ADR",
    "dev": "Dev",
    "ops": "Ops",
    "spikes": "Spike",
    "guides": "Guide",
}
SECTION_INDEX_NAMES = {"adr": "ADR-Index", "spikes": "Spikes", "guides": "Guides"}
INDEX_STEMS = {"index", "readme"}
EXCLUDED_PREFIXES = ("plan/",)
EXCLUDED_FILES = frozenset({"adr/template.md"})

_LINK_RE = re.compile(r"(?<!!)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)")
_CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


class _NavLoader(yaml.SafeLoader):
    """SafeLoader that tolerates MkDocs' ``!!python/name:`` tags by returning the name."""


def _python_name(_loader: yaml.SafeLoader, suffix: str, _node: yaml.Node) -> str:
    return suffix


_NavLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", _python_name)


def is_excluded(rel_path: str) -> bool:
    return rel_path in EXCLUDED_FILES or rel_path.startswith(EXCLUDED_PREFIXES)


def _title_case(stem: str) -> str:
    return "-".join(seg[:1].upper() + seg[1:] for seg in stem.split("-") if seg)


def page_name(rel_path: str) -> str:
    """Map a docs-relative path (posix) to a flat wiki page name without extension."""
    path = PurePosixPath(rel_path)
    stem = path.stem
    parts = path.parent.parts
    if not parts:
        return "Home" if stem.lower() == "index" else _title_case(stem)
    section = parts[0]
    prefix = DIR_PREFIXES.get(section, _title_case(section))
    if stem.lower() in INDEX_STEMS:
        return SECTION_INDEX_NAMES.get(section, f"{prefix}-Index")
    return f"{prefix}-{_title_case(stem)}"


def rewrite_links(text: str, rel_path: str, known_pages: set[str]) -> str:
    """Rewrite relative links to other docs pages into wiki links.

    ``known_pages`` holds the docs-relative paths that become wiki pages. Links to other
    ``.md`` files below docs (for example the excluded plan) become GitHub blob URLs.
    Fenced code blocks and inline code are left untouched.
    """
    base = posixpath.dirname(rel_path)

    def replace(match: re.Match[str]) -> str:
        target = match.group("target")
        if target.startswith("#") or _SCHEME_RE.match(target) or target.startswith("//"):
            return match.group(0)
        path, sep, anchor = target.partition("#")
        if not path.endswith(".md"):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(base, path))
        if resolved.startswith(".."):
            return match.group(0)
        title = match.group("title") or ""
        if resolved in known_pages:
            new_target = page_name(resolved) + sep + anchor
        else:
            new_target = f"{REPO_BLOB_URL}/{resolved}{sep}{anchor}"
        return f"[{match.group('text')}]({new_target}{title})"

    out: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
        elif in_fence:
            out.append(line)
        else:
            pieces = _CODE_SPAN_RE.split(line)
            out.append(
                "".join(p if i % 2 else _LINK_RE.sub(replace, p) for i, p in enumerate(pieces))
            )
    return "\n".join(out)


def load_nav(mkdocs_yml: Path) -> list[Any]:
    config = yaml.load(mkdocs_yml.read_text(encoding="utf-8"), Loader=_NavLoader)  # noqa: S506
    nav = config.get("nav", []) if isinstance(config, dict) else []
    return list(nav)


def first_heading(text: str, fallback: str) -> str:
    in_fence = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and line.startswith("# "):
            return line[2:].strip()
    return fallback


def _page_link(rel_path: str, title: str | None, titles: dict[str, str]) -> str:
    if is_excluded(rel_path):
        return f"[{title or rel_path}]({REPO_BLOB_URL}/{rel_path})"
    return f"[{title or titles.get(rel_path) or page_name(rel_path)}]({page_name(rel_path)})"


def _nav_lines(items: list[Any], titles: dict[str, str], depth: int) -> list[str]:
    lines: list[str] = []
    indent = "  " * depth
    for item in items:
        if isinstance(item, str):
            lines.append(f"{indent}- {_page_link(item, None, titles)}")
        elif isinstance(item, dict):
            for title, value in item.items():
                if isinstance(value, list):
                    lines.append(f"{indent}- **{title}**")
                    lines.extend(_nav_lines(value, titles, depth + 1))
                else:
                    lines.append(f"{indent}- {_page_link(str(value), str(title), titles)}")
    return lines


def build_sidebar(nav: list[Any], titles: dict[str, str] | None = None) -> str:
    """Render the wiki sidebar from the MkDocs nav structure, in nav order."""
    lines = ["**MowDeck documentation**", ""]
    lines.extend(_nav_lines(nav, titles or {}, 0))
    return "\n".join(lines) + "\n"


def mirror_footer(source: str, commit: str | None) -> str:
    at = f" at commit {commit}" if commit else ""
    return (
        f"\n---\n\nMirrored from `{source}`{at}. "
        "Edit the file in the repository, not this wiki page.\n"
    )


@dataclass(frozen=True)
class BuildResult:
    written: list[str]
    removed: list[str]


def build(docs: Path, mkdocs_yml: Path, out: Path, commit: str | None = None) -> BuildResult:
    """Generate the wiki pages into ``out`` and delete stale ``*.md`` files there."""
    sources = sorted(
        p.relative_to(docs).as_posix()
        for p in docs.rglob("*.md")
        if not is_excluded(p.relative_to(docs).as_posix())
    )
    names: dict[str, str] = {}
    for rel in sources:
        name = page_name(rel)
        if name in names:
            msg = f"wiki page name collision: {rel} and {names[name]} both map to {name}"
            raise ValueError(msg)
        names[name] = rel

    known = set(sources)
    out.mkdir(parents=True, exist_ok=True)
    titles: dict[str, str] = {}
    generated: dict[str, str] = {}
    for rel in sources:
        text = (docs / rel).read_text(encoding="utf-8")
        titles[rel] = first_heading(text, page_name(rel))
        body = rewrite_links(text, rel, known).rstrip("\n") + "\n"
        generated[f"{page_name(rel)}.md"] = body + mirror_footer(f"docs/{rel}", commit)

    generated["_Sidebar.md"] = build_sidebar(load_nav(mkdocs_yml), titles)
    at = f" at commit {commit}" if commit else ""
    generated["_Footer.md"] = (
        f"Mirrored from the `docs/` directory{at}. "
        "Edit the files in the repository, not this wiki.\n"
    )

    removed: list[str] = []
    for existing in sorted(out.rglob("*.md")):
        rel_out = existing.relative_to(out)
        if ".git" in rel_out.parts:
            continue
        if rel_out.as_posix() not in generated:
            existing.unlink()
            removed.append(rel_out.as_posix())
    for name, content in generated.items():
        (out / name).write_text(content, encoding="utf-8", newline="\n")
    return BuildResult(written=sorted(generated), removed=removed)
