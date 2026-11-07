"""A world where Procedures use pair Subjects as their rows (S3a).

Ends ``pp.inventory`` (key ``name``), ``pp.component`` (``purl``) and
``pp.vuln`` (``vid``); pairs ``pp.occurrence`` (inventory, component) with the
membership fact ``scope`` (R3) and ``pp.app_exposure`` (occurrence,
vulnerability), a pair of a pair, with the fact ``note``. ``pp.inventory.holds``
is a many-valued Subject-valued fact naming an occurrence (a value-side pair
target), with an object-keyed inverse. ``pp.inventory`` is a ``hint`` kind
under ``hint=True`` (an end key two Subjects may hold).

Each Line reads one workspace document ``{inventory, component, entries,
count}``: one record per (inventory, component) pair, both in its extract key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import (
    ClaimInverse,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from cruxible_client.contracts.shapes import Contract, contract_path, render_contract
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from tests.core_support._identity_support import accept_tree
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.support.claim_types import claim_type as build_claim_type
from tests.support.mandates import procedure_mandate
from tests.support.operation_world import author_line, author_procedure, operation_build, world
from tests.support.procedures import procedure_limits, procedure_spec

INVENTORY = "pp.inventory"
COMPONENT = "pp.component"
VULN = "pp.vuln"
OCCURRENCE = "pp.occurrence"
APP_EXPOSURE = "pp.app_exposure"
NAME = f"{INVENTORY}.name"
PURL = f"{COMPONENT}.purl"
VID = f"{VULN}.vid"
O_INVENTORY = f"{OCCURRENCE}.inventory"
O_COMPONENT = f"{OCCURRENCE}.component"
A_OCCURRENCE = f"{APP_EXPOSURE}.occurrence"
A_VULN = f"{APP_EXPOSURE}.vulnerability"
SCOPE = f"{OCCURRENCE}.scope"
NOTE = f"{APP_EXPOSURE}.note"
SEEN_IN = f"{INVENTORY}.holds"
CAPTURE = "test.orders-v1"
ROW = Contract.model_validate(
    {
        "name": "test.pair-row",
        "version": 1,
        "fields": {
            "scope": "enum[required, optional, excluded]",
            "inventory": "string{max_len: 256}",
            "purl": "string{max_len: 256}?",
            "vid": "string{max_len: 256}?",
        },
    }
)
ROLES = ("derivation", "normative", "observation")


def _capture_contract() -> Any:
    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )
    from tests.core_support._light_capture_support import JSON_DECODING

    return lower_capture_contract_input(
        CaptureContractInputRecord(
            name=CAPTURE,
            shape="database_snapshot",
            replay="exact",
            max_bytes=4_194_304,
            decoding=JSON_DECODING,
        ),
        named=True,
    )


def _fact(predicate: str, kind: str, value: Any, **changes: Any) -> Any:
    return build_claim_type(
        predicate=predicate,
        allowed_subject_kinds=[kind],
        value_type=value,
        permitted_roles=list(ROLES),
        default_role="observation",
        revision_evidence="replace",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="fixture",
                    claim_roles=ROLES,
                    capture_contracts=(CAPTURE,),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
        **changes,
    )


def _key(predicate: str, kind: str, value: Any) -> Any:
    return build_claim_type(
        predicate=predicate,
        allowed_subject_kinds=[kind],
        value_type=value,
        default_role="observation",
        evidence_requirement="none",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=()),
    )


def vocabulary(*, hint: bool = False) -> dict[str, bytes]:
    types = (
        _key(NAME, INVENTORY, {"type": "string"}),
        _key(PURL, COMPONENT, {"type": "string"}),
        _key(VID, VULN, {"type": "string"}),
        _key(O_INVENTORY, OCCURRENCE, (INVENTORY,)),
        _key(O_COMPONENT, OCCURRENCE, (COMPONENT,)),
        _key(A_OCCURRENCE, APP_EXPOSURE, (OCCURRENCE,)),
        _key(A_VULN, APP_EXPOSURE, (VULN,)),
        _fact(SCOPE, OCCURRENCE, {"enum": ["excluded", "optional", "required"]}),
        _fact(NOTE, APP_EXPOSURE, {"type": "string"}),
        _fact(
            SEEN_IN,
            INVENTORY,
            (OCCURRENCE,),
            cardinality="many",
            resolution_policy=ClaimResolutionPolicy(
                cardinality="many", eligible_verdicts=("supported",), selector="all"
            ),
            inverse=ClaimInverse(key={"kind": "object"}, evidence_rules=("fixture",)),
        ),
    )
    kinds = (
        keyed_subject_kind(INVENTORY, (NAME,), uniqueness="hint" if hint else "unique"),
        keyed_subject_kind(COMPONENT, (PURL,)),
        keyed_subject_kind(VULN, (VID,)),
        keyed_subject_kind(OCCURRENCE, (O_INVENTORY, O_COMPONENT)),
        keyed_subject_kind(APP_EXPOSURE, (A_OCCURRENCE, A_VULN)),
    )
    capture = _capture_contract()
    return {
        capture_contract_path(capture.identity.name): render_capture_contract(capture),
        contract_path(ROW.name, ROW.version): render_contract(ROW),
        **{subject_kind_path(k.identity.name): render_subject_kind(k) for k in kinds},
        **{claim_type_path(t.predicate): render_claim_type(t) for t in types},
    }


def pair_procedure_world(tmp_path: Path, *, hint: bool = False) -> tuple[Any, Path]:
    instance = world(tmp_path, operation_build())
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True)
    accept_tree(instance, vocabulary(hint=hint), name="pair-vocabulary")
    return instance, root


def write_doc(
    root: Path,
    name: str,
    entries: list[Any],
    *,
    inventory: str | None = None,
    component: str | None = None,
    count: int | None = None,
) -> None:
    """One source document: rows ``(inventory, purl, scope)`` (a non-dict is a bad record)."""

    document: dict[str, Any] = {
        "entries": entries,
        "count": len(entries) if count is None else count,
    }
    if inventory is not None:
        document["inventory"] = inventory
    if component is not None:
        document["component"] = component
    (root / "data" / f"{name}.json").write_text(json.dumps(document, sort_keys=True))


def rows(inventory: str, purls: list[str], scope: str = "required") -> list[dict[str, Any]]:
    return [{"inventory": inventory, "purl": purl, "scope": scope} for purl in purls]


def end(kind: str, key: dict[str, Any], create: str = "if_absent") -> dict[str, Any]:
    return {"kind": kind, "key": key, "create": create}


def occurrence_key(*, header_inventory: bool = False, create: str = "if_absent") -> Any:
    return {
        "inventory": end(
            INVENTORY,
            {"name": "$steps.entries.header.inventory" if header_inventory else "$item.key.inv"},
            create,
        ),
        "component": end(COMPONENT, {"purl": "$item.key.purl"}, create),
    }


def pair_spec(
    name: str,
    *,
    doc: str,
    source: str,
    by: str | None = "inventory",
    claims: list[dict[str, Any]] | None = None,
    subject: dict[str, Any] | None = None,
    header_inventory: bool = False,
    items: int | None = None,
    expect: Any = None,
    role: str | None = None,
    vid_key: bool = False,
    within: bool = False,
) -> Any:
    """fetch the document -> extract one record per pair -> claims about the pair."""

    mapping: dict[str, Any] = {
        "type": SCOPE,
        "value": "$item.fields.scope",
        "rationale": "The source lists the component in the inventory.",
    }
    if role is not None:
        mapping["role"] = role
    if by is not None:
        mapping["when_absent"] = "mirror"
    write: dict[str, Any] = {
        "id": "write",
        "for_each": "$steps.entries.records",
        "subject": subject
        or {
            "kind": OCCURRENCE,
            "key": occurrence_key(header_inventory=header_inventory),
            "create": "if_absent",
        },
        "claims": claims or [mapping],
    }
    if within:
        write["subject"] = {**write["subject"], "within": "$params.within"}
    if expect is not None:
        write["expect"] = expect
    elif by is not None:
        write["expect"] = {"by": by, "ends": [f"$steps.entries.header.{by}"]}
    return procedure_spec(
        name,
        reference_text=True,
        input={},
        output={},
        **({"params": {"within": "list<string{max_len: 512}>{max_items: 64}"}} if within else {}),
        slots={
            "fetch": {
                "in": "workspace.file.request@1",
                "out": "workspace.file.result@1",
                "effect": "pure",
            }
        },
        hard_caps=procedure_limits(
            provider_calls=1, capture_bytes=4_194_304, items=items or 20_000, wall_clock="PT10M"
        ),
        steps=[
            {
                "call": "doc",
                "slot": "fetch",
                "captures": {"capture": CAPTURE},
                "input": {
                    "logical_source": source,
                    "relative_path": f"data/{doc}.json",
                    "coordinate_type": "database-snapshot-v1",
                    "coordinate": {"lsn": "workspace"},
                    "selector_type": "relation-primary-key-v1",
                    "selector": {"id": 7, "relation": doc},
                },
            },
            {
                "extract": "entries",
                "capture": "$steps.doc.capture",
                "records": "/entries/*",
                "row": ROW.ref.spelling,
                "key": {
                    "inv": {"at": "inventory", "type": f"key_part<{INVENTORY}:name>"},
                    "purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"},
                    **({"vid": {"at": "vid", "type": f"key_part<{VULN}:vid>"}} if vid_key else {}),
                },
                "duplicates": "refuse_key",
                "complete_when": {"count_at": "/count"},
                "fields": {
                    "scope": {"at": "scope"},
                    "inventory": {"at": "inventory"},
                    **({} if vid_key else {"vid": {"at": "vid"}}),
                },
                "header": {
                    "inventory": {"at": "/inventory", "type": "string{max_len: 256}"},
                    "component": {"at": "/component", "type": "string{max_len: 256}"},
                },
            },
            write,
        ],
    )


def install(
    instance: Any,
    spec: Any,
    *,
    line: str,
    namespace: tuple[str, ...] = (),
    parameters: dict[str, Any] | None = None,
) -> None:
    """The Line with a propose grant over Claims and Subjects."""

    author_procedure(instance, spec, name=line + "-procedure")
    author_line(
        instance,
        name=line,
        procedure=spec.procedure,
        parameters=parameters or {},
        max_authority="propose",
        findings={"inbox": []},
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(line)
    accepted = parse_line_spec(tree[path], path=path)
    mandate = procedure_mandate(
        line,
        namespace=namespace or ("claims", "subjects"),
        line=ArtifactPin(
            role="line", target=accepted.identity, artifact_digest=line_spec_digest(accepted).tagged
        ),
    )
    accept_tree(
        instance,
        {procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate)},
        name=line + "-mandate",
    )


def run(instance: Any, root: Path, *, line: str, minute: int = 0) -> Any:
    from tests.test_procedures.test_claims_terminal import run as run_line

    return run_line(instance, root, minute=minute, line=line)


def activate(instance: Any, state: Any) -> None:
    from cruxible_core.service.authoring.documents import service_activate_playbill_proposal

    assert state.status == "succeeded", state.terminal
    proposal = state.terminal_egress[0].proposal_id
    assert proposal is not None, state.terminal_egress
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposal, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt


def live_claims(instance: Any) -> list[Any]:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return [
        claim
        for path in tree
        if path.startswith("claims/")
        for claim in (parse_claim(tree[path], path=path),)
        if claim.lifecycle.state == "live"
    ]


def keys_by_path(claims: list[Any]) -> dict[str, Any]:
    """Each Subject's key: a literal end's value, a pair's ``(end, end)`` keys."""

    literal = {
        c.statement.subject.artifact_path: c.statement.object.value
        for c in claims
        if c.statement.predicate in {NAME, PURL, VID}
    }
    parts: dict[str, dict[str, str]] = {}
    for c in claims:
        if c.statement.predicate in {O_INVENTORY, O_COMPONENT, A_OCCURRENCE, A_VULN}:
            parts.setdefault(c.statement.subject.artifact_path, {})[c.statement.predicate] = (
                c.statement.object.address.artifact_path
            )
    found: dict[str, Any] = dict(literal)
    for _round in range(2):  # pairs of pairs name their ends' keys
        for path, ends in parts.items():
            found[path] = tuple(found.get(ends[p], ends[p]) for p in sorted(ends))
    return found


def memberships(instance: Any) -> dict[str, dict[str, str]]:
    """inventory name -> {purl: scope} of the live ``scope`` facts."""

    claims = live_claims(instance)
    keys = keys_by_path(claims)
    found: dict[str, dict[str, str]] = {}
    for c in claims:
        if c.statement.predicate != SCOPE:
            continue
        component, inventory = keys[c.statement.subject.artifact_path]
        found.setdefault(inventory, {})[component] = c.statement.object.value
    return found


def subjects_of(instance: Any, kind: str) -> list[str]:
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return sorted(p for p in tree if p.startswith(f"subjects/{kind}/"))


__all__ = [
    "APP_EXPOSURE",
    "COMPONENT",
    "INVENTORY",
    "NOTE",
    "OCCURRENCE",
    "SCOPE",
    "SEEN_IN",
    "VULN",
    "activate",
    "end",
    "install",
    "keys_by_path",
    "live_claims",
    "memberships",
    "occurrence_key",
    "pair_procedure_world",
    "pair_spec",
    "rows",
    "run",
    "subjects_of",
    "write_doc",
]
