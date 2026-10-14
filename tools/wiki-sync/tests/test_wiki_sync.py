from __future__ import annotations

import re
from pathlib import Path

import pytest
from wiki_sync.core import build, build_sidebar, page_name, rewrite_links

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("rel", "expected"),
    [
        ("index.md", "Home"),
        ("phases.md", "Phases"),
        ("architecture/05-building-blocks.md", "Architecture-05-Building-Blocks"),
        ("adr/0009-command-and-event-envelopes.md", "ADR-0009-Command-And-Event-Envelopes"),
        ("adr/README.md", "ADR-Index"),
        ("dev/getting-started.md", "Dev-Getting-Started"),
        ("ops/compose.md", "Ops-Compose"),
        (
            "spikes/0001-ble-bridge-and-pymammotion.md",
            "Spike-0001-Ble-Bridge-And-Pymammotion",
        ),
        ("spikes/index.md", "Spikes"),
        ("guides/index.md", "Guides"),
        (
            "guides/esp32-bridge-purchase-and-setup.md",
            "Guide-Esp32-Bridge-Purchase-And-Setup",
        ),
    ],
)
def test_page_name(rel: str, expected: str) -> None:
    assert page_name(rel) == expected


KNOWN = {
    "index.md",
    "adr/0001-x.md",
    "adr/0002-y.md",
    "guides/esp32.md",
    "architecture/05-building-blocks.md",
}


def test_rewrite_relative_up_and_down() -> None:
    text = "[a](../adr/0001-x.md) [c](../guides/esp32.md)"
    out = rewrite_links(text, "architecture/05-building-blocks.md", KNOWN)
    assert "[a](ADR-0001-X)" in out
    assert "[c](Guide-Esp32)" in out
    assert rewrite_links("[b](0002-y.md#anchor)", "adr/0001-x.md", KNOWN) == (
        "[b](ADR-0002-Y#anchor)"
    )
    out3 = rewrite_links("[d](architecture/05-building-blocks.md)", "index.md", KNOWN)
    assert out3 == "[d](Architecture-05-Building-Blocks)"


def test_rewrite_keeps_external_anchor_image_and_code() -> None:
    text = (
        "[e](https://example.com/a.md) [f](#local) ![i](img/pic.png) ![j](other.md)\n"
        "`[k](0001-x.md)`\n```\n[l](0001-x.md)\n```\n"
    )
    assert rewrite_links(text, "adr/0002-y.md", KNOWN) == text


def test_rewrite_plan_link_becomes_blob_url() -> None:
    out = rewrite_links("[p](plan/project-plan.de.md)", "index.md", KNOWN)
    assert out == "[p](https://github.com/lukislp/mowdeck/blob/main/docs/plan/project-plan.de.md)"


def test_sidebar_from_small_nav() -> None:
    nav = [
        {"Home": "index.md"},
        {"Decisions": ["adr/README.md", "adr/0001-x.md"]},
        {"Original plan": "plan/p.md"},
    ]
    sidebar = build_sidebar(nav, {"adr/0001-x.md": "ADR 1: X"})
    assert "- [Home](Home)" in sidebar
    assert "- **Decisions**" in sidebar
    assert "  - [ADR 1: X](ADR-0001-X)" in sidebar
    assert "  - [ADR-Index](ADR-Index)" in sidebar
    assert "https://github.com/lukislp/mowdeck/blob/main/docs/plan/p.md" in sidebar
    assert sidebar.index("Home") < sidebar.index("Decisions") < sidebar.index("Original plan")


def _make_docs(tmp_path: Path) -> tuple[Path, Path]:
    docs = tmp_path / "docs"
    (docs / "adr").mkdir(parents=True)
    (docs / "plan").mkdir()
    (docs / "index.md").write_text("# Home\n\n[adr](adr/0001-x.md)\n", encoding="utf-8")
    (docs / "adr" / "0001-x.md").write_text("# ADR 1\n\n[home](../index.md)\n", encoding="utf-8")
    (docs / "adr" / "template.md").write_text("# T\n", encoding="utf-8")
    (docs / "plan" / "p.md").write_text("# P\n", encoding="utf-8")
    mk = tmp_path / "mkdocs.yml"
    mk.write_text(
        "nav:\n  - Home: index.md\n  - ADR:\n      - adr/0001-x.md\n"
        "markdown_extensions:\n  - pymdownx.superfences:\n      custom_fences:\n"
        "        - format: !!python/name:pymdownx.superfences.fence_code_format\n",
        encoding="utf-8",
    )
    return docs, mk


def _snapshot(out: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(out.glob("*.md"))}


def test_build_excludes_and_footer(tmp_path: Path) -> None:
    docs, mk = _make_docs(tmp_path)
    out = tmp_path / "out"
    build(docs, mk, out, "abc123")
    assert sorted(p.name for p in out.glob("*.md")) == [
        "ADR-0001-X.md",
        "Home.md",
        "_Footer.md",
        "_Sidebar.md",
    ]
    page = (out / "ADR-0001-X.md").read_text(encoding="utf-8")
    assert "[home](Home)" in page
    assert page.endswith(
        "---\n\nMirrored from `docs/adr/0001-x.md` at commit abc123. "
        "Edit the file in the repository, not this wiki page.\n"
    )


def test_build_removes_stale_files_but_not_git(tmp_path: Path) -> None:
    docs, mk = _make_docs(tmp_path)
    out = tmp_path / "out"
    (out / ".git").mkdir(parents=True)
    (out / ".git" / "note.md").write_text("keep", encoding="utf-8")
    (out / "Stale.md").write_text("old", encoding="utf-8")
    result = build(docs, mk, out)
    assert result.removed == ["Stale.md"]
    assert not (out / "Stale.md").exists()
    assert (out / ".git" / "note.md").exists()


def test_build_is_idempotent(tmp_path: Path) -> None:
    docs, mk = _make_docs(tmp_path)
    out = tmp_path / "out"
    build(docs, mk, out, "abc")
    first = _snapshot(out)
    build(docs, mk, out, "abc")
    assert _snapshot(out) == first


def test_build_real_docs(tmp_path: Path) -> None:
    out = tmp_path / "wiki"
    build(REPO_ROOT / "docs", REPO_ROOT / "mkdocs.yml", out, "deadbeef")
    for name in ("Home.md", "_Sidebar.md", "ADR-0001-Openfga-For-Authorization.md"):
        assert (out / name).is_file()
    assert not (out / "ADR-Template.md").exists()
    link = re.compile(r"\]\((?!https?://)[^)]*\.md[)#]")
    for page in out.glob("*.md"):
        in_fence = False
        for line in page.read_text(encoding="utf-8").split("\n"):
            if line.startswith("```"):
                in_fence = not in_fence
            elif not in_fence:
                assert not link.search(line), f"{page.name}: {line}"
