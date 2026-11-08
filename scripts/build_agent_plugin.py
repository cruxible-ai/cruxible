"""Generate both agent-host packages from one release definition and skill source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("plugins/cruxible")


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
    for source in sorted((root / "skills").rglob("*.md")):
        files[PACKAGE / source.relative_to(root)] = source.read_bytes()
    files[PACKAGE / "README.md"] = (root / "plugins/README.md").read_bytes()
    return files


def synchronize(*, root: Path = ROOT, check: bool = False) -> list[Path]:
    """Check or refresh the generated tree, including removal of obsolete packaged skills."""

    files = plugin_files(root)
    existing = {path.relative_to(root) for path in (root / PACKAGE).rglob("*") if path.is_file()}
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
