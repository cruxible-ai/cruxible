"""Standing law for release-stable gate, selector, and exemption predicates.

Moving aliases may choose production defaults, but equality and identity tests
must select semantics by an exact retained coordinate/enum member or by
membership in an explicit installed lineage. Examples of allowed forms are
``installed.coordinate == CLAIM_LAW_V3_REVISION_8``,
``codec is ArtifactCodec.CURRENT_PRETTY_JSON``, and
``compiler in PC_HR_ARTIFACT_CODEC_COMPILERS``.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    ROOT / "src" / "cruxible_core",
    ROOT / "packages" / "cruxible-client" / "src" / "cruxible_client",
)
MOVING_ALIAS_NAMES = frozenset(
    {
        "CLAIM_LAW_V3",
        "CURRENT_ARTIFACT_CODEC",
        "current_compiler_coordinate",
    }
)
EQUALITY_OPERATORS = (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)


def _mentions_moving_alias(node: ast.AST) -> bool:
    return any(
        (isinstance(child, ast.Name) and child.id in MOVING_ALIAS_NAMES)
        or (isinstance(child, ast.Attribute) and child.attr in MOVING_ALIAS_NAMES)
        for child in ast.walk(node)
    )


def test_gate_selectors_never_compare_against_moving_current_aliases() -> None:
    violations: list[str] = []
    for root in SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                if not any(isinstance(operator, EQUALITY_OPERATORS) for operator in node.ops):
                    continue
                if _mentions_moving_alias(node):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert violations == [], (
        "moving current aliases cannot define gate/selector/exemption equality; "
        "compare an exact retained coordinate or use installed-lineage membership: "
        f"{violations!r}"
    )


def test_cli_and_sdk_consume_the_one_daemon_compatibility_function() -> None:
    import cruxible_client.compatibility as client_compatibility
    from cruxible_client.authoring import sdk
    from cruxible_core.cli.commands import _common

    assert (
        sdk.client_compatibility.check_daemon_compatibility
        is client_compatibility.check_daemon_compatibility
    )
    assert (
        _common.client_compatibility.check_daemon_compatibility
        is client_compatibility.check_daemon_compatibility
    )
    consumers = (
        ROOT / "src" / "cruxible_core" / "cli" / "commands" / "_common.py",
        ROOT / "packages" / "cruxible-client" / "src" / "cruxible_client" / "authoring" / "sdk.py",
    )
    for path in consumers:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "check_daemon_compatibility" in calls, path.relative_to(ROOT)
        assert "SUPPORTED_DAEMON_CONTRACTS" not in path.read_text(encoding="utf-8")
