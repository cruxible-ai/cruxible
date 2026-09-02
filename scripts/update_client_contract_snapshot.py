"""Regenerate the checked-in cruxible-client contract snapshot."""

from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.support.client_contracts import (  # noqa: E402
    generate_contract_manifest,
    write_contract_snapshot,
)

from cruxible_client.contracts.primitives import canonical_json  # noqa: E402

SNAPSHOT_PATH = REPO_ROOT / "tests/goldens/cruxible_client/contracts_snapshot.json"
AUTHORING_MODELS_PATH = (
    REPO_ROOT / "packages/cruxible-client/src/cruxible_client/contracts/authoring/models.py"
)


def _replace_snapshot_digest(digest: str) -> None:
    source = AUTHORING_MODELS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(AUTHORING_MODELS_PATH))
    assignment = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST"
                for target in node.targets
            )
        ),
        None,
    )
    if assignment is None or assignment.end_lineno is None:
        raise SystemExit("could not locate AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST")
    lines = source.splitlines(keepends=True)
    lines[assignment.lineno - 1 : assignment.end_lineno] = [
        f'AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST = (\n    "{digest}"\n)\n'
    ]
    AUTHORING_MODELS_PATH.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    manifest = generate_contract_manifest()
    rendered = canonical_json(manifest)
    write_contract_snapshot(SNAPSHOT_PATH)
    digest = "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    _replace_snapshot_digest(digest)
    print(f"Wrote {SNAPSHOT_PATH.relative_to(REPO_ROOT)}")
    print(f"Updated {AUTHORING_MODELS_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
