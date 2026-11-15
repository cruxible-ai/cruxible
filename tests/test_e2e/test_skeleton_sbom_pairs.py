"""The SBOM import over pair Subjects (S3a, D-R46 R3): fetch -> derive -> extract -> claims.

One ``workspace.file`` fetch reads a CycloneDX-shaped SBOM; a pure derive emits
its inventory (a header) and one record per component. The claims step's row is
the occurrence ``(inventory, component)``: both ends are nested templates
created if absent (the inventory named by the derive's header), so one run
creates the inventory, its components and their occurrences, ends first, and
writes each occurrence's ``scope`` (R3: the membership fact). ``expect: {by:
inventory, ends: [header]}`` judges absence by the inventory alone: the
occurrences of it holding a live ``scope`` come from the pair index, and the
complete SBOM no longer listing a component retires that occurrence's scope.

The Line settles under a grant naming the three kinds it creates (V3) and the
``scope`` changes it makes (create on the record, retire on absence): every
change is accepted at admission's coordinate, with no proposal to activate.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from tests.core_support._identity_support import accept_tree
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.support.operation_world import NOW, author
from tests.support.procedures import procedure_limits
from tests.support.procedures import procedure_spec as build_spec
from tests.test_e2e.test_skeleton_kev_epss import _claim_type
from tests.test_e2e.test_skeleton_sbom_late_read import (
    COMPONENT,
    COMPONENTS_KIND,
    INVENTORY,
    INVENTORY_ID,
    PURL,
    SBOM_KIND,
    SBOM_PATH,
    SOURCE,
    Sbom,
    run,
    sbom_world,
    write_sbom,
)

OCCURRENCE = "test.occurrence"
O_INVENTORY = OCCURRENCE + ".inventory"
O_COMPONENT = OCCURRENCE + ".component"
SCOPE = OCCURRENCE + ".scope"
LINE = "test.sbom-pairs-line"
APP = "pkg:generic/app@1"
A, B, C = "pkg:pypi/a@1", "pkg:pypi/b@1", "pkg:pypi/c@1"


def pairs_world(tmp_path: Path) -> tuple[Any, Path]:
    """The SBOM world, plus the occurrence pair kind and its ``scope`` fact."""

    instance, root = sbom_world(tmp_path)

    def part(predicate: str, end: str) -> Any:
        return _claim_type(
            predicate,
            f"subject<{end}>",
            value_type=(end,),
            allowed_subject_kinds=[OCCURRENCE],
            evidence_requirement="none",
            evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=()),
        )

    scope = _claim_type(
        SCOPE,
        "enum[excluded, optional, required]",
        allowed_subject_kinds=[OCCURRENCE],
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="captured",
                    claim_roles=("observation",),
                    capture_contracts=(SBOM_KIND, COMPONENTS_KIND),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )
    accept_tree(
        instance,
        {
            subject_kind_path(OCCURRENCE): render_subject_kind(
                keyed_subject_kind(OCCURRENCE, (O_INVENTORY, O_COMPONENT))
            ),
            **{
                claim_type_path(t.predicate): render_claim_type(t)
                for t in (
                    part(O_INVENTORY, INVENTORY),
                    part(O_COMPONENT, COMPONENT),
                    scope,
                )
            },
        },
        name="occurrence-vocabulary",
    )
    return instance, root


def sbom_pairs_spec() -> Any:
    """fetch -> derive -> extract -> claims: one occurrence per listed component."""

    return build_spec(
        "test.sbom-pairs",
        reference_text=True,
        input={},
        output={},
        slots={
            "read": {"capability": "workspace.file@1"},
            "derive": {"capability": "test.sbom-derive@1"},
        },
        hard_caps=procedure_limits(provider_calls=2, capture_bytes=2_097_152),
        steps=[
            {
                "call": "sbom",
                "slot": "read",
                "captures": {"capture": SBOM_KIND},
                "input": {
                    "logical_source": SOURCE,
                    "relative_path": SBOM_PATH,
                },
            },
            {
                "call": "derived",
                "slot": "derive",
                "captures": {"capture": COMPONENTS_KIND},
                "input": {"sbom": "$steps.sbom.capture"},
            },
            {
                "extract": "listed",
                "capture": "$steps.derived.capture",
                "records": "components",
                "duplicates": "refuse_key",
                "header": ["inventory"],
            },
            {
                "id": "write",
                "for_each": "$steps.listed.records",
                "subject": {
                    "kind": OCCURRENCE,
                    "key": {
                        "inventory": {
                            "kind": INVENTORY,
                            "key": {"inventory_id": "$steps.listed.header.inventory"},
                            "create": "if_absent",
                        },
                        "component": {
                            "kind": COMPONENT,
                            "key": {"purl": "$item.key.purl"},
                            "create": "if_absent",
                        },
                    },
                    "create": "if_absent",
                },
                "expect": {"by": "inventory", "ends": ["$steps.listed.header.inventory"]},
                "claims": [
                    {
                        "type": SCOPE,
                        # CycloneDX's default scope (the kit's derive fills it).
                        "value": "required",
                        "when_absent": "mirror",
                        "rationale": "The SBOM lists the component in its inventory.",
                    }
                ],
            },
        ],
    )


def grant_settle(instance: Any) -> None:
    from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
    from cruxible_client.contracts.authoring.models import MandateScopeAuthoring

    author(
        instance,
        ProcedureMandateInput(
            kind="procedure_mandate",
            name=LINE + "-settle",
            line_name=LINE,
            grants="settle",
            namespace=("claims",),
            valid_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=30),
            subject_creation=tuple(sorted((COMPONENT, INVENTORY, OCCURRENCE))),
            scope=(
                MandateScopeAuthoring.model_validate(
                    {
                        "claim_type": SCOPE,
                        "changes": {
                            "create": {"basis": ["extracted"]},
                            "retire": {"basis": ["absence"]},
                        },
                    }
                ),
            ),
        ),
        name=LINE + "-settle-grant",
    )


def _settle_line(instance: Any) -> None:
    from tests.support.operation_world import author_line, author_procedure

    author_procedure(instance, sbom_pairs_spec(), name=LINE + "-procedure")
    author_line(
        instance,
        name=LINE,
        procedure="test.sbom-pairs",
        parameters={},
        max_authority="settle",
        label=LINE,
    )
    grant_settle(instance)


def memberships(instance: Any) -> dict[str, dict[str, str]]:
    """inventory id -> {purl: scope} of the live ``scope`` facts."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claims = [
        claim
        for path in tree
        if path.startswith("claims/")
        for claim in (parse_claim(tree[path], path=path),)
        if claim.lifecycle.state == "live"
    ]
    literal = {
        c.statement.subject.artifact_path: c.statement.object.value
        for c in claims
        if c.statement.predicate in {PURL, INVENTORY_ID}
    }
    ends: dict[str, dict[str, str]] = {}
    for c in claims:
        if c.statement.predicate in {O_INVENTORY, O_COMPONENT}:
            ends.setdefault(c.statement.subject.artifact_path, {})[c.statement.predicate] = literal[
                c.statement.object.address.artifact_path
            ]
    found: dict[str, dict[str, str]] = {}
    for c in claims:
        if c.statement.predicate == SCOPE:
            pair = ends[c.statement.subject.artifact_path]
            found.setdefault(pair[O_INVENTORY], {})[pair[O_COMPONENT]] = c.statement.object.value
    return found


def _changes(state: Any) -> list[str]:
    (egress,) = state.terminal_egress
    return sorted(child.change_kind for child in egress.children if child.change_kind)


def _settled(state: Any) -> None:
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    (egress,) = state.terminal_egress
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress


def test_an_sbom_import_settles_occurrences_and_retires_by_its_inventory(tmp_path: Path) -> None:
    instance, root = pairs_world(tmp_path)
    _settle_line(instance)
    source = Sbom()

    # 1. The first import: the inventory, both components, both occurrences
    #    (ends first) and their scopes, settled in one change.
    write_sbom(root, APP, [A, B])
    first = run(instance, root, source, line=LINE)
    _settled(first)
    assert _changes(first) == ["claim", "claim", *["subject"] * 5]
    assert memberships(instance) == {APP: {A: "required", B: "required"}}

    # 2. B leaves the SBOM, C joins: B's scope retires on the complete read; its
    #    occurrence stays as history. Absence was judged by the inventory alone.
    write_sbom(root, APP, [A, C])
    second = run(instance, root, source, line=LINE, minute=1)
    _settled(second)
    assert _changes(second) == ["claim", "reobservation", "retirement", "subject", "subject"]
    assert memberships(instance) == {APP: {A: "required", C: "required"}}

    # 3. Another inventory's SBOM never touches this one's occurrences.
    write_sbom(root, "pkg:generic/other@1", [A])
    other = run(instance, root, source, line=LINE, minute=2)
    _settled(other)
    assert "retirement" not in _changes(other)
    assert memberships(instance) == {
        APP: {A: "required", C: "required"},
        "pkg:generic/other@1": {A: "required"},
    }

    # 4. B is listed again: its occurrence is found, not created again.
    write_sbom(root, APP, [A, B, C])
    again = run(instance, root, source, line=LINE, minute=3)
    _settled(again)
    assert _changes(again) == ["claim", "reobservation", "reobservation"]

    # 5. An incomplete read (a refused record) retires nothing (D-R40).
    write_sbom(root, APP, [A, "not a record"])
    incomplete = run(instance, root, source, line=LINE, minute=4)
    assert incomplete.status == "succeeded", incomplete.terminal
    assert incomplete.completeness is not None and not incomplete.completeness.complete
    assert memberships(instance)[APP] == {A: "required", B: "required", C: "required"}

    # 6. The same SBOM as step 4 changes nothing.
    write_sbom(root, APP, [A, B, C])
    unchanged = run(instance, root, source, line=LINE, minute=5)
    assert unchanged.status == "succeeded", unchanged.terminal
    assert not _changes(unchanged) or set(_changes(unchanged)) == {"reobservation"}
