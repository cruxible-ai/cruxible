"""Generate the skills manifest (skills.json) from skills/*/SKILL.md.

One entry per shipped skill: slug, name, description (frontmatter), and
the "This skill is for:" bullets when present. Consumed by the
cruxible.ai /skills page — entries for indexing, not content mirrors;
the SKILL.md on GitHub stays the artifact agents actually run. Future
agent-facing artifacts (beyond skills) join this manifest or a sibling.

Usage:
    uv run python scripts/generate_skill_docs.py [--out dist/skills.json]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
GITHUB_BASE = "https://github.com/cruxible-ai/cruxible/tree/main/skills"

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_skill(skill_dir: Path) -> dict | None:
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    text = md.read_text()

    fm = FRONTMATTER.match(text)
    fields: dict[str, str] = {}
    if fm:
        for line in fm.group(1).splitlines():
            key, _, value = line.partition(":")
            if value:
                fields[key.strip()] = value.strip()

    # "This skill is for:" bullets, when the section exists
    bullets: list[str] = []
    marker = text.find("This skill is for:")
    if marker != -1:
        for line in text[marker:].splitlines()[1:]:
            stripped = line.strip()
            if stripped.startswith("- "):
                bullets.append(stripped[2:])
            elif bullets and stripped:
                break

    return {
        "slug": skill_dir.name,
        "name": fields.get("name", skill_dir.name),
        "description": fields.get("description", ""),
        "for": bullets,
        "github": f"{GITHUB_BASE}/{skill_dir.name}",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO_ROOT / "dist" / "skills.json"))
    args = parser.parse_args()

    skills = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if skill_dir.name.startswith("_") or not skill_dir.is_dir():
            continue
        entry = parse_skill(skill_dir)
        if entry:
            skills.append(entry)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"skills": skills}, indent=2) + "\n")
    for skill in skills:
        print(f"{skill['slug']:<26} for-bullets={len(skill['for'])}")
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes, {len(skills)} skills)")


if __name__ == "__main__":
    main()
