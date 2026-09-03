"""Every ruled time-bearing contract/runtime field declares one Playbill clock."""

from __future__ import annotations

import ast
from pathlib import Path

from cruxible_client.contracts.clock_taxonomy import (
    CLOCK_DOMAINS,
    CLOCK_FIELD_DECLARATIONS,
    NON_CLOCK_DECLARED_FIELDS,
    classify_clock_field,
    clock_description,
    declared_clock,
    is_time_bearing_field,
)

ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    ROOT / "packages" / "cruxible-client" / "src" / "cruxible_client" / "contracts",
    ROOT / "src" / "cruxible_core" / "playbill",
    ROOT / "src" / "cruxible_core" / "service",
)


def _discovered_fields() -> tuple[tuple[str, str, str, str, str], ...]:
    """Return (path, class, field, annotation, in-source declaration) rows."""

    rows: list[tuple[str, str, str, str, str]] = []
    for scan_root in SCAN_ROOTS:
        for path in sorted(scan_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for class_node in (item for item in ast.walk(tree) if isinstance(item, ast.ClassDef)):
                for item in class_node.body:
                    if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                        continue
                    annotation = ast.unparse(item.annotation)
                    if not is_time_bearing_field(item.target.id, annotation):
                        continue
                    default = "" if item.value is None else ast.unparse(item.value)
                    declared = ""
                    if "Reads " in default:
                        declared = default.split("Reads ")[1].split(".")[0]
                    rows.append(
                        (
                            path.relative_to(ROOT).as_posix(),
                            class_node.name,
                            item.target.id,
                            annotation,
                            declared,
                        )
                    )
    return tuple(rows)


def test_every_discovered_time_field_is_declared_exactly_once() -> None:
    """A field discovered by the ruled predicate and declared nowhere fails."""

    undeclared: list[str] = []
    for path, class_name, field_name, annotation, _declared in _discovered_fields():
        if (class_name, field_name) in NON_CLOCK_DECLARED_FIELDS:
            continue
        clock = classify_clock_field(class_name, field_name, annotation)
        if clock not in CLOCK_DOMAINS:
            undeclared.append(f"{path}:{class_name}.{field_name}: {annotation}")

    assert not undeclared, (
        "time-bearing fields carry no clock declaration; add each to "
        "CLOCK_FIELD_DECLARATIONS (or NON_CLOCK_DECLARED_FIELDS with a reason):\n"
        + "\n".join(undeclared)
    )


def test_declarations_and_exemptions_carry_no_dead_entries() -> None:
    discovered = {(row[1], row[2]) for row in _discovered_fields()}

    assert not set(CLOCK_FIELD_DECLARATIONS) - discovered
    assert not NON_CLOCK_DECLARED_FIELDS - discovered
    assert not set(CLOCK_FIELD_DECLARATIONS) & NON_CLOCK_DECLARED_FIELDS


def test_in_source_field_descriptions_agree_with_the_declaration() -> None:
    """The served description and the taxonomy cannot drift apart."""

    disagreements: list[str] = []
    for path, class_name, field_name, _annotation, declared in _discovered_fields():
        if not declared:
            continue
        expected = declared_clock(class_name, field_name)
        if declared != expected:
            disagreements.append(f"{path}:{class_name}.{field_name} says {declared!r}")

    assert not disagreements, disagreements


def test_prepared_at_reads_the_evaluation_instant() -> None:
    """P2-C D-1: the terminal request's prepared_at is an evaluation instant."""

    assert declared_clock("TerminalEgressRequestV1", "prepared_at") == "EVALUATION INSTANT"
    assert clock_description("EVALUATION INSTANT") == "Reads EVALUATION INSTANT."


def test_discovery_predicate_rejects_lookalike_names_and_holds_the_word_boundary() -> None:
    assert not is_time_bearing_field("artifact_digest", "str")
    assert not is_time_bearing_field("consequence", "Literal['next_claim_attestation_threshold']")
    assert is_time_bearing_field("sequence", "int")
    assert is_time_bearing_field("partition_sequence", "int")
    assert is_time_bearing_field("stale_after", "CanonicalDurationV1")
    assert len(CLOCK_DOMAINS) == 4


def test_one_field_name_may_read_two_clocks_in_two_owners() -> None:
    """The capture observes at an evaluation instant; the attestor asserts."""

    assert declared_clock("CaptureEnvelopeV2", "observed_at") == "EVALUATION INSTANT"
    assert declared_clock("ClaimAttestationStatement", "observed_at") == "ASSERTION TIME"
