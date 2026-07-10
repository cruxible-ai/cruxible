"""Generate llms.txt and llms-full.txt for docs.cruxible.ai.

llms.txt (https://llmstxt.org): a curated index of the documentation
with one-line descriptions, served at the site root for LLM crawlers
and agents. llms-full.txt: the full markdown of every published doc
concatenated, for single-fetch ingestion. Both are generated from
mkdocs.yml's nav so they can't drift from the published site.

Usage:
    uv run --with pyyaml python scripts/generate_llms_txt.py
Writes docs/llms.txt and docs/llms-full.txt (copied into the built
site by mkdocs as plain files).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
SITE_URL = "https://docs.cruxible.ai"

SUMMARY = (
    "Cruxible is one shared truth for humans and AI agents: a typed, "
    "governed state layer where every write is reviewed before it is "
    "accepted and every answer carries a receipt. Open source, Apache 2.0. "
    "Kit catalog: https://cruxible.ai/kits · Skills: https://cruxible.ai/skills"
)


def first_paragraph(md_path: Path) -> str:
    """First prose paragraph after the H1, collapsed to one line."""
    lines = md_path.read_text().splitlines()
    out: list[str] = []
    seen_h1 = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            seen_h1 = True
            continue
        if not seen_h1:
            continue
        if not stripped:
            if out:
                break
            continue
        if stripped.startswith(("#", "|", "```", "-", "*", ">")):
            if out:
                break
            continue
        out.append(stripped)
    text = " ".join(out)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # strip links
    text = text.replace("`", "")
    return text[:220]


def page_url(md_name: str) -> str:
    if md_name == "index.md":
        return f"{SITE_URL}/"
    return f"{SITE_URL}/{md_name.removesuffix('.md')}/"


def walk_nav(nav, acc):
    for item in nav:
        if isinstance(item, dict):
            for title, value in item.items():
                if isinstance(value, str):
                    acc.append((title, value, None))
                else:
                    acc.append((title, None, "section"))
                    walk_nav(value, acc)


def main() -> None:
    config = yaml.safe_load((REPO_ROOT / "mkdocs.yml").read_text())
    entries: list[tuple[str, str | None, str | None]] = []
    walk_nav(config["nav"], entries)

    # llms.txt — curated index
    lines = [f"# {config['site_name']}", "", f"> {SUMMARY}", ""]
    for title, md_name, kind in entries:
        if kind == "section":
            lines += [f"## {title}", ""]
        elif md_name:
            desc = first_paragraph(DOCS / md_name)
            lines.append(f"- [{title}]({page_url(md_name)}): {desc}")
    lines.append("")
    (DOCS / "llms.txt").write_text("\n".join(lines))

    # llms-full.txt — every published page, concatenated
    full: list[str] = [f"# {config['site_name']} — full documentation", "", f"> {SUMMARY}", ""]
    for title, md_name, kind in entries:
        if md_name:
            full += [
                "",
                "=" * 72,
                f"SOURCE: {page_url(md_name)}",
                "=" * 72,
                "",
                (DOCS / md_name).read_text(),
            ]
    (DOCS / "llms-full.txt").write_text("\n".join(full))

    pages = sum(1 for _, m, _ in entries if m)
    print(f"llms.txt: {pages} pages indexed")
    print(f"llms-full.txt: {(DOCS / 'llms-full.txt').stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
