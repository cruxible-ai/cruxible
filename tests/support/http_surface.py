"""Frozen-contract manifest of the public HTTP surface."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def generate_openapi_spec() -> dict[str, Any]:
    """Build the live FastAPI OpenAPI document for HTTP surface checks."""
    os.environ.setdefault("CRUXIBLE_SERVER_STATE_DIR", tempfile.mkdtemp(prefix="crx-surface-"))
    from cruxible_core.server.app import create_app

    return create_app().openapi()


def _response_model_name(schema: dict[str, Any]) -> str | None:
    """Resolve a response schema to a model name, including union response models."""
    ref = schema.get("$ref", "")
    if ref:
        return ref.rsplit("/", 1)[-1]
    members = [_response_model_name(member) for member in schema.get("anyOf", [])]
    if members and all(members):
        return " | ".join(name for name in members if name)
    return None


def generate_http_surface_manifest() -> dict[str, Any]:
    """Build {path: {METHOD: response_model_title|None}} from the live app."""
    spec = generate_openapi_spec()
    manifest: dict[str, Any] = {}
    for path, methods in sorted(spec["paths"].items()):
        entry: dict[str, Any] = {}
        for method, operation in sorted(methods.items()):
            schema = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            entry[method.upper()] = _response_model_name(schema)
        manifest[path] = entry
    return manifest


def write_http_surface_snapshot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(generate_http_surface_manifest(), indent=2, sort_keys=True) + "\n")


def load_http_surface_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())
