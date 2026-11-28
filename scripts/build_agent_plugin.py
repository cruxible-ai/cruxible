"""Generate both agent-host packages from one release definition and skill source."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Collection
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("plugins/cruxible")
#: Core docs the skills link, copied into each skill's own ``references/`` folder
#: (for standalone ``npx skills add`` installs) and into ``plugins/cruxible/docs/``.
BUNDLED_DOCS = (
    "documents-and-notes.md",
    "evolving-your-vocabulary.md",
    "kits.md",
    "lines-and-automation.md",
)
#: Shared reference sheets copied from ``skills/_shared/references/`` into each
#: skill's own ``references/`` directory so every skill folder is self-contained.
SKILL_SHARED_REFS: dict[str, tuple[str, ...]] = {
    "adopt-kit": ("copy-in.md", "governance-flow.md", "concurrent-edits.md"),
    "automate-state": ("copy-in.md", "governance-flow.md", "concurrent-edits.md"),
    "create-state": (
        "copy-in.md",
        "data-readiness.md",
        "governance-flow.md",
        "concurrent-edits.md",
    ),
    "review-state": ("governance-flow.md", "concurrent-edits.md"),
    "survey": ("copy-in.md", "governance-flow.md"),
}
#: Core docs copied into each skill's own ``references/`` directory.
SKILL_BUNDLED_DOCS: dict[str, tuple[str, ...]] = {
    "adopt-kit": ("kits.md",),
    "automate-state": ("lines-and-automation.md",),
    "create-state": ("evolving-your-vocabulary.md", "documents-and-notes.md"),
    "review-state": (),
    "survey": ("evolving-your-vocabulary.md",),
}
PLUGIN_ICON_SOURCE = Path("assets/brand/cruxible-org-avatar.png")
PLUGIN_ICON_TARGET = Path("assets/icon.png")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def bundled_doc(
    text: str,
    name: str,
    version: str,
    *,
    allowed_docs: Collection[str] = BUNDLED_DOCS,
) -> bytes:
    """One core doc as packaged: links to docs left out of the bundle keep only their text."""

    allowed = set(allowed_docs)

    def link(match: re.Match[str]) -> str:
        target = match.group(2)
        local = target.split("#", 1)[0]
        if "://" in target or not local or local in allowed:
            return match.group(0)
        return match.group(1)

    header = f"<!-- Bundled from cruxible {version} docs/{name}. -->\n\n"
    return (header + _LINK.sub(link, text)).encode()


def plugin_files(root: Path = ROOT) -> dict[Path, bytes]:
    """The complete generated package and host catalogs (all paths relative to root)."""

    release = json.loads((root / "plugins/release.json").read_text())
    metadata = {key: release[key] for key in ("name", "version", "description")}
    server = {
        "type": "stdio",
        "command": "uvx",
        "args": ["--from", f"cruxible=={release['version']}", "cruxible", "mcp"],
        "env": {
            "CRUXIBLE_MODE": "admin",
            "CRUXIBLE_MCP_PROFILE": "full",
            "CRUXIBLE_MCP_TOOLS": ",".join(release["tools"]),
        },
    }
    icon_ref = f"./{PLUGIN_ICON_TARGET.as_posix()}"
    documents = {
        PACKAGE / ".claude-plugin/plugin.json": {
            **metadata,
            "author": {"name": release["publisher"]},
        },
        PACKAGE / "plugin.json": {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            **metadata,
            "extensions": {
                "com.openai": {
                    "interface": {
                        "displayName": "Cruxible",
                        "shortDescription": release["description"],
                        "developerName": release["publisher"],
                        "category": "Productivity",
                        "composerIcon": icon_ref,
                        "logo": icon_ref,
                    },
                },
            },
        },
        PACKAGE / ".mcp.json": {"mcpServers": {"cruxible": server}},
        PACKAGE / "mcp.json": {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {"cruxible": server},
        },
        Path(".claude-plugin/marketplace.json"): {
            "name": release["marketplace"],
            "description": release["description"],
            "owner": {"name": release["publisher"]},
            "plugins": [{**metadata, "source": "./plugins/cruxible"}],
        },
        Path(".agents/plugins/marketplace.json"): {
            "name": release["marketplace"],
            "interface": {"displayName": "Cruxible"},
            "plugins": [
                {
                    "name": release["name"],
                    "source": {"source": "local", "path": "./plugins/cruxible"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    "category": "Productivity",
                }
            ],
        },
    }
    files = {
        path: (json.dumps(value, indent=2) + "\n").encode() for path, value in documents.items()
    }
    # Sync per-skill self-contained references/ so `npx skills add cruxible-ai/cruxible`
    # works cleanly without dangling relative links outside each skill directory.
    generated_skill_refs: dict[Path, bytes] = {}
    for skill_name, shared_refs in SKILL_SHARED_REFS.items():
        for ref in shared_refs:
            rel = Path("skills") / skill_name / "references" / ref
            generated_skill_refs[rel] = (root / "skills/_shared/references" / ref).read_bytes()
    for skill_name, doc_names in SKILL_BUNDLED_DOCS.items():
        for doc_name in doc_names:
            rel = Path("skills") / skill_name / "references" / doc_name
            text = (root / "docs" / doc_name).read_text()
            generated_skill_refs[rel] = bundled_doc(
                text,
                doc_name,
                release["version"],
                allowed_docs=doc_names,
            )
    files.update(generated_skill_refs)
    for source in sorted((root / "skills").rglob("*.md")):
        rel = source.relative_to(root)
        if rel in generated_skill_refs:
            continue
        files[PACKAGE / rel] = source.read_bytes()
    for rel, body in generated_skill_refs.items():
        files[PACKAGE / rel] = body
    for name in BUNDLED_DOCS:
        text = (root / "docs" / name).read_text()
        files[PACKAGE / "docs" / name] = bundled_doc(text, name, release["version"])
    files[PACKAGE / PLUGIN_ICON_TARGET] = (root / PLUGIN_ICON_SOURCE).read_bytes()
    files[PACKAGE / "README.md"] = (root / "plugins/README.md").read_bytes()
    return files


def synchronize(*, root: Path = ROOT, check: bool = False) -> list[Path]:
    """Check or refresh the generated tree, including removal of obsolete packaged skills."""

    files = plugin_files(root)
    existing = {path.relative_to(root) for path in (root / PACKAGE).rglob("*") if path.is_file()}
    for skill_name in SKILL_SHARED_REFS:
        refs_dir = root / "skills" / skill_name / "references"
        if refs_dir.is_dir():
            existing.update(
                path.relative_to(root) for path in refs_dir.rglob("*") if path.is_file()
            )
    stale = existing - files.keys()
    changed = {
        path
        for path, body in files.items()
        if not (root / path).is_file() or (root / path).read_bytes() != body
    }
    if not check:
        for path in stale:
            (root / path).unlink()
        for path in changed:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(files[path])
    return sorted(stale | changed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Refuse generated package drift.")
    args = parser.parse_args()
    changed = synchronize(check=args.check)
    if args.check and changed:
        parser.exit(
            1,
            "Regenerate with python scripts/build_agent_plugin.py: "
            + ", ".join(map(str, changed))
            + "\n",
        )
    print(
        f"{'Checked' if args.check else 'Generated'} Cruxible plugin ({len(changed)} changed files)"
    )


if __name__ == "__main__":
    main()
