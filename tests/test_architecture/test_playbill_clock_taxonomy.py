"""Every ruled time-bearing contract/runtime field declares one Playbill clock."""

from __future__ import annotations

import ast
from pathlib import Path

from cruxible_client.contracts.clock_taxonomy import CLOCK_DOMAINS, classify_clock_field

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    ROOT / "packages" / "cruxible-client" / "src" / "cruxible_client" / "contracts",
    ROOT / "src" / "cruxible_core" / "playbill",
)


def _discovered_fields() -> tuple[tuple[str, str, str, str], ...]:
    rows: list[tuple[str, str, str, str]] = []
    for scan_root in SCAN_ROOTS:
        for path in sorted(scan_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for class_node in (item for item in ast.walk(tree) if isinstance(item, ast.ClassDef)):
                for item in class_node.body:
                    if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                        continue
                    annotation = ast.unparse(item.annotation)
                    if classify_clock_field(item.target.id, annotation) is not None:
                        rows.append(
                            (
                                path.relative_to(ROOT).as_posix(),
                                class_node.name,
                                item.target.id,
                                annotation,
                            )
                        )
    return tuple(rows)


def test_ast_inventory_classifies_every_ruled_time_field_exactly_once() -> None:
    fields = _discovered_fields()

    assert fields
    for path, class_name, field_name, annotation in fields:
        clock = classify_clock_field(field_name, annotation)
        assert clock in CLOCK_DOMAINS, f"{path}:{class_name}.{field_name}"


def test_classifier_rejects_ordinary_fields_and_has_no_double_domain() -> None:
    assert classify_clock_field("artifact_digest", "str") is None
    assert classify_clock_field("evaluation_time", "datetime") == "EVALUATION INSTANT"
    assert classify_clock_field("recorded_at", "datetime") == "ASSERTION TIME"
    assert classify_clock_field("retain_until", "datetime") == "VALIDITY WINDOW"
    assert classify_clock_field("sequence", "int") == "SETTLEMENT ORDER"
    assert len(CLOCK_DOMAINS) == 4
