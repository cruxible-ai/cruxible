#!/usr/bin/env python3
"""Import an LLM wiki (a directory of Markdown pages) into a Cruxible instance
as digest-pinned source artifacts.

This is Stage 1 of the wiki-to-instance pipeline and it is deliberately dumb:
every page registers deterministically as a source artifact with a stable id,
a content hash, and a parsed chunk manifest. Nothing here reads prose meaning.
Turning wiki text into typed state (work items, decisions, risks, notes) is
agent judgment and belongs in Stage 2, where every claim flows through
proposals or evidence-carrying writes that cite the chunks this script
registers. See docs/recipes/llm-wiki-to-instance.md.

Registration shells out to the `cruxible` CLI (`cruxible source register`), so
this script works against a remote daemon with only the CLI installed; it never
imports cruxible_core. Stdlib only.

Determinism and idempotence:
- Artifact ids are `<prefix>_<slug-of-relative-path>` (lowercase, runs of
  non-alphanumerics collapse to `_`). Ids must match the service constraint:
  3-64 chars of [A-Za-z0-9._-] starting alphanumeric.
- On overflow (>64 chars) or slug collision, the slug is truncated and an
  8-char content-hash suffix is appended. The id stays stable while the file
  is unchanged; if the file's content changes, the suffixed id changes and the
  page re-registers as a NEW artifact — correct for digest-pinned evidence,
  since the old artifact's refs must keep reporting drift, not silently rebind.
- Re-runs are idempotent: `cruxible source register --id` refuses duplicates
  ("... is already registered"), which this script records as status
  "skipped", not an error. A skipped page's chunk manifest is not re-emitted,
  but it is recoverable from the daemon: `cruxible source get <artifact-id>
  --json` returns the full chunk list.

Auth: the CLI reads CRUXIBLE_SERVER_BEARER_TOKEN from the environment. This
script passes the environment through untouched and never prints the token.

Daemon path containment: `source register` resolves paths on the daemon side
and refuses paths outside the instance root / CRUXIBLE_ALLOWED_ROOTS. If your
wiki lives outside the instance root, start the daemon with
CRUXIBLE_ALLOWED_ROOTS=<wiki-root> (daemon environment, not client).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")  # service _SOURCE_ARTIFACT_ID_RE
ID_MAX_LEN = 64
HASH_SUFFIX_LEN = 8
DEFAULT_EXCLUDES = (".git", "node_modules", ".obsidian")
ALREADY_REGISTERED_MARKER = "is already registered"


def slugify(value: str) -> str:
    """Slugify like the kits do: lowercase, [^a-z0-9]+ -> _, strip."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def build_artifact_id(prefix: str, rel_path: str, content: bytes, taken: dict[str, str]) -> str:
    """Deterministic artifact id for one page; hash-suffix on overflow/collision."""
    slug = slugify(rel_path) or "page"
    candidate = f"{prefix}_{slug}"
    if len(candidate) > ID_MAX_LEN or (candidate in taken and taken[candidate] != rel_path):
        suffix = sha256_hex(content)[:HASH_SUFFIX_LEN]
        keep = ID_MAX_LEN - len(prefix) - len(suffix) - 2  # two joining underscores
        if keep < 1:
            raise ValueError(f"--id-prefix '{prefix}' leaves no room for a slug")
        candidate = f"{prefix}_{slug[:keep].rstrip('_')}_{suffix}"
    if not ID_RE.fullmatch(candidate):
        raise ValueError(
            f"Artifact id '{candidate}' (from '{rel_path}') violates the id constraint: "
            "3-64 chars of [A-Za-z0-9._-] starting alphanumeric"
        )
    if candidate in taken and taken[candidate] != rel_path:
        raise ValueError(
            f"Artifact id collision even after hash suffix: '{candidate}' "
            f"({taken[candidate]!r} vs {rel_path!r})"
        )
    taken[candidate] = rel_path
    return candidate


def excluded(rel_path: Path, patterns: list[str]) -> bool:
    rel_posix = rel_path.as_posix()
    for pattern in patterns:
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in rel_path.parts):
            return True
    return False


def discover(root: Path, include: str, exclude: list[str]) -> list[Path]:
    files = []
    for path in sorted(root.glob(include)):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if excluded(rel, exclude):
            continue
        files.append(rel)
    return files


def scrub(text: str) -> str:
    """Remove the bearer token from any text we print or persist."""
    token = os.environ.get("CRUXIBLE_SERVER_BEARER_TOKEN")
    if token:
        text = text.replace(token, "***")
    return text


def register_one(
    cruxible: list[str],
    transport: list[str],
    path: Path,
    artifact_id: str,
    label: str,
) -> tuple[str, dict]:
    """Run `cruxible source register`; return (status, extra manifest fields)."""
    cmd = [
        *cruxible,
        *transport,
        "source",
        "register",
        "--path",
        str(path),
        "--id",
        artifact_id,
        "--kind",
        "markdown",
        "--label",
        label,
        "--json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == 0:
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return "failed", {"error": "register succeeded but returned non-JSON output"}
        chunks = [
            {
                "chunk_id": chunk["chunk_id"],
                "heading_path": chunk.get("heading_path", []),
                "block_selector": chunk.get("block_selector"),
                "line_start": chunk.get("line_start"),
                "line_end": chunk.get("line_end"),
            }
            for chunk in result.get("chunks", [])
        ]
        return "registered", {
            "content_hash": result.get("content_hash"),
            "chunks": chunks,
        }
    stderr = scrub(proc.stderr.strip())
    if ALREADY_REGISTERED_MARKER in stderr:
        return "skipped", {}
    return "failed", {"error": stderr or f"cruxible exited {proc.returncode}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Register every Markdown page under --dir as a digest-pinned Cruxible "
            "source artifact (Stage 1 of the wiki-to-instance pipeline). "
            "Dry-run by default; pass --server-url or --socket to register."
        )
    )
    parser.add_argument("--dir", required=True, help="Root of the wiki / vault to import.")
    parser.add_argument(
        "--include", default="**/*.md", help="Glob (relative to --dir) selecting pages."
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=None,
        metavar="PATTERN",
        help=(
            "Exclude pattern (fnmatch, against the relative path or any path part). "
            f"Repeatable. Defaults always apply: {', '.join(DEFAULT_EXCLUDES)}."
        ),
    )
    parser.add_argument("--id-prefix", default="wiki", help="Artifact id prefix (default: wiki).")
    parser.add_argument("--server-url", default=None, help="Cruxible daemon base URL.")
    parser.add_argument("--socket", default=None, help="Cruxible daemon Unix socket path.")
    parser.add_argument(
        "--instance-id",
        default=None,
        help="Target instance id (defaults to the CLI's remembered context).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would register without calling the daemon (also the default "
        "when neither --server-url nor --socket is given).",
    )
    parser.add_argument("--manifest", default=None, metavar="OUT.JSON", help="Write manifest here.")
    parser.add_argument(
        "--cruxible-bin",
        default="cruxible",
        help='cruxible CLI invocation (default: "cruxible"; e.g. "uv run cruxible").',
    )
    args = parser.parse_args(argv)

    root = Path(args.dir).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"--dir is not a directory: {root}")
    prefix = args.id_prefix
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", prefix):
        parser.error("--id-prefix must start alphanumeric and use only [A-Za-z0-9_-]")

    dry_run = args.dry_run or (args.server_url is None and args.socket is None)
    excludes = list(DEFAULT_EXCLUDES) + (args.exclude or [])

    cruxible = shlex.split(args.cruxible_bin)
    transport: list[str] = []
    if args.server_url:
        transport += ["--server-url", args.server_url]
    if args.socket:
        transport += ["--server-socket", args.socket]
    if args.instance_id:
        transport += ["--instance-id", args.instance_id]

    rel_paths = discover(root, args.include, excludes)
    if not rel_paths:
        print(f"No files matched {args.include!r} under {root}", file=sys.stderr)

    taken: dict[str, str] = {}
    entries = []
    counts = {"planned": 0, "registered": 0, "skipped": 0, "failed": 0}
    for rel in rel_paths:
        path = root / rel
        rel_str = rel.as_posix()
        content = path.read_bytes()
        try:
            artifact_id = build_artifact_id(prefix, rel_str, content, taken)
        except ValueError as exc:
            entries.append(
                {"path": rel_str, "artifact_id": None, "bytes": len(content),
                 "status": "failed", "error": str(exc)}
            )
            counts["failed"] += 1
            print(f"failed      -            {rel_str}: {exc}", file=sys.stderr)
            continue

        entry: dict = {"path": rel_str, "artifact_id": artifact_id, "bytes": len(content)}
        if dry_run:
            status, extra = "planned", {"content_hash": f"sha256:{sha256_hex(content)}"}
        else:
            status, extra = register_one(cruxible, transport, path, artifact_id, rel_str)
        entry["status"] = status
        entry.update(extra)
        entries.append(entry)
        counts[status] += 1
        line = f"{status:<11} {artifact_id}  {rel_str}"
        if status == "failed":
            line += f": {entry.get('error', '')}"
        print(line, file=sys.stderr if status == "failed" else sys.stdout)

    manifest = {
        "root": str(root),
        "generated_by": "scripts/import_markdown.py",
        "dry_run": dry_run,
        "files": entries,
    }
    if args.manifest:
        manifest_path = Path(args.manifest).expanduser()
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"manifest -> {manifest_path}")

    summary = ", ".join(f"{count} {name}" for name, count in counts.items() if count)
    print(f"{'dry-run: ' if dry_run else ''}{len(entries)} files ({summary or 'nothing to do'})")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
