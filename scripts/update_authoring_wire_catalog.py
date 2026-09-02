"""Regenerate the frozen authoring wire-model inventory and digest."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

from cruxible_client.contracts.authoring import models
from cruxible_client.contracts.authoring.wire_catalog import (
    AUTHORING_WIRE_CATALOG_VERSION,
    discovered_authoring_wire_model_names,
)
from cruxible_client.contracts.primitives import canonical_json

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_MODULE = (
    REPO_ROOT / "packages/cruxible-client/src/cruxible_client/contracts/authoring/wire_catalog.py"
)
CROSS_CHECK_TEST = REPO_ROOT / "tests/test_client/test_claim_attestation_contract_catalog.py"


def _catalog_digest(model_names: tuple[str, ...]) -> str:
    schemas: dict[str, Any] = {}
    for name in model_names:
        model = cast(type[BaseModel], getattr(models, name))
        model.model_rebuild()
        schemas[name] = model.model_json_schema(ref_template="#/$defs/{model}")
    payload = {
        "catalog_version": AUTHORING_WIRE_CATALOG_VERSION,
        "module": models.__name__,
        "models": schemas,
    }
    content = canonical_json(payload).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _assignment_lines(tree: ast.Module, name: str) -> tuple[int, int]:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and node.end_lineno is not None
        ):
            return node.lineno - 1, node.end_lineno
    raise SystemExit(f"could not locate {name} in {CATALOG_MODULE.relative_to(REPO_ROOT)}")


def main() -> None:
    model_names = discovered_authoring_wire_model_names()
    digest = _catalog_digest(model_names)
    source = CATALOG_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(CATALOG_MODULE))
    lines = source.splitlines(keepends=True)
    replacements = {
        "AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST": (
            f'AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST = (\n    "{digest}"\n)\n'
        ),
        "AUTHORING_WIRE_MODEL_NAMES": (
            "AUTHORING_WIRE_MODEL_NAMES = (\n"
            + "".join(f'    "{name}",\n' for name in model_names)
            + ")\n"
        ),
    }
    spans = [(_assignment_lines(tree, name), rendered) for name, rendered in replacements.items()]
    for (start, end), rendered in sorted(spans, reverse=True):
        lines[start:end] = [rendered]
    CATALOG_MODULE.write_text("".join(lines), encoding="utf-8")
    cross_check = CROSS_CHECK_TEST.read_text(encoding="utf-8")
    cross_check, replacements = re.subn(
        r'(?<=AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST == \(\n        ")sha256:[0-9a-f]{64}',
        digest,
        cross_check,
        count=1,
    )
    if replacements != 1:
        raise SystemExit(f"could not update authoring digest in {CROSS_CHECK_TEST}")
    CROSS_CHECK_TEST.write_text(cross_check, encoding="utf-8")
    print(f"Wrote {CATALOG_MODULE.relative_to(REPO_ROOT)}")
    print(f"Updated {CROSS_CHECK_TEST.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
