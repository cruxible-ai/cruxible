"""Pilot definitions authored through the same typed builders as every fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
from cruxible_client.contracts.query.authored import QuerySpecInput
from tests.core_support._identity_support import accept_tree
from tests.support.operation_world import author
from tests.support.procedures import (
    boundary_contract,
    contract_files,
    procedure_limits,
    procedure_spec,
)
from tests.test_procedures.test_claims_terminal import KIND, claim_type, claims_world, install

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pilot"
KEV = KIND + ".kev_"
EPSS = KIND + ".epss_"
KEV_FIELDS = {
    "date_added": "date?",
    "due_date": "date?",
    "ransomware_use": f"enum_of {KEV}ransomware_use?",
}
KEV_ROW = boundary_contract("security.kev-entry", KEV_FIELDS)
# D-R23: typing belongs in the extract, so absence and writes read one typed record.
EPSS_ROW = boundary_contract(
    "security.epss-row", {"score": "decimal{0..1}", "percentile": "decimal{0..1}"}
)
WATCH_ROW = boundary_contract(
    "security.watched-row", {"subject_kind": "string", "subject_id": "string", "cve_id": "string"}
)


#: The KEV catalog's record fields as the kind's layout reads them.
KEV_RULES: dict[str, Any] = {
    "date_added": {"at": "dateAdded", "parse": "date"},
    "due_date": {"at": "dueDate", "parse": "date"},
    "ransomware_use": {"at": "knownRansomwareCampaignUse", "canonicalize": "claim_type"},
    "listed_from": {"at": "dateAdded", "parse": "date", "project": "utc_start_of_day"},
}
EPSS_RULES: dict[str, Any] = {
    "score": {"at": "epss", "parse": "decimal"},
    "percentile": {"at": "percentile", "parse": "decimal"},
}


def pilot_records(kev: bool, rows: Any = None) -> dict[str, Any]:
    """The world kind's KEV record sets (``vulnerabilities`` counted, ``vulnerabilities_all``
    not) or its EPSS record set (``data``), each keyed by the vulnerability's CVE."""

    from tests.support.layouts import record_set

    row = rows or (KEV_ROW if kev else EPSS_ROW)
    rules = KEV_RULES if kev else EPSS_RULES
    fields = {name: rules.get(name, {"at": name}) for name in row.fields}
    key_at = "cveID" if kev else "cve"
    key = {"cve_id": {"at": key_at, "type": f"key_part<{KIND}:cve_id>"}}
    if not kev:
        return {"data": record_set(at="/data/*", row=row.ref.spelling, key=key, fields=fields)}
    return {
        name: record_set(
            at="/vulnerabilities/*",
            row=row.ref.spelling,
            key=key,
            fields=fields,
            count_at=count_at,
        )
        for name, count_at in (("vulnerabilities", "/count"), ("vulnerabilities_all", None))
    }


def _watched(instance: Any) -> None:
    accept_tree(instance, contract_files(WATCH_ROW), name="watch-row")
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": "security.watched-vulnerabilities",
                    "from": {"v": KIND},
                    "row": WATCH_ROW.ref.spelling,
                    "fields": ["v.subject_kind", "v.subject_id", "v.cve_id"],
                }
            ),
        ),
        name="watched-query",
    )


def _fetch(file: str) -> dict[str, Any]:
    return {
        "call": "catalog",
        "slot": "fetch",
        "captures": {"capture": "test.orders-v1"},
        "input": {
            "logical_source": "$params.logical_source",
            "relative_path": "$params.relative_path",
        },
    }


def _spec(name: str, nodes: list[dict[str, Any]], *, may_truncate: bool = False) -> Any:
    return procedure_spec(
        name,
        reference_text=True,
        input={},
        output={},
        # Typed to fit workspace.file@1's request, which the read is typed against.
        params={
            "logical_source": 'string{pattern: "^[a-z][a-z0-9_.-]{0,255}$"}',
            "relative_path": "string{min_len: 1, max_len: 1024}",
        },
        slots={"fetch": {"capability": "workspace.file@1", "may_truncate": may_truncate}},
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=65536, read_rows=500),
        steps=nodes,
    )


def kev_spec(
    *, role: str = "observation", may_truncate: bool = False, negative: bool = False
) -> Any:
    return _spec(
        "security.kev-triage",
        [
            _fetch("kev"),
            {"read": "watched", "query": "security.watched-vulnerabilities"},
            {
                "extract": "entries",
                "capture": "$steps.catalog.capture",
                "records": "vulnerabilities",
                "header": ["catalog_version"],
                "duplicates": "refuse_key",
            },
            {
                "id": "write",
                "for_each": "$steps.entries.records",
                "subject": {
                    "kind": KIND,
                    "key": {"cve_id": "$item.key.cve_id"},
                    "create": "never",
                    "within": "$steps.watched.rows",
                },
                "claims": [
                    {
                        "type": KEV + "listed",
                        "value": True,
                        "role": role,
                        "rationale": "CISA KEV lists this vulnerability.",
                    },
                    {"type": KEV + "date_added", "value": "$item.fields.date_added", "role": role},
                    {"type": KEV + "due_date", "value": "$item.fields.due_date", "role": role},
                    {
                        "type": KEV + "ransomware_use",
                        "value": "$item.fields.ransomware_use",
                        "role": role,
                    },
                ],
            },
        ],
        may_truncate=may_truncate,
    )


def epss_spec() -> Any:
    return _spec(
        "security.epss-triage",
        [
            _fetch("epss"),
            {"read": "watched", "query": "security.watched-vulnerabilities"},
            {
                "transform": "watch_keys",
                "kind": "shape",
                "spec": {
                    "input": "$steps.watched.rows",
                    "fields": {"cve_id": {"from": "$item.fields.cve_id"}},
                },
            },
            {
                "extract": "scores",
                "capture": "$steps.catalog.capture",
                "records": "data",
                "header": ["score_date"],
                "only_keys": "$steps.watch_keys.rows",
            },
            {
                "id": "write",
                "for_each": "$steps.scores.records",
                "subject": {
                    "kind": KIND,
                    "key": {"cve_id": "$item.key.cve_id"},
                    "create": "never",
                    "within": "$steps.watched.rows",
                },
                "claims": [
                    {"type": EPSS + "score", "value": "$item.fields.score"},
                    {"type": EPSS + "percentile", "value": "$item.fields.percentile"},
                    {"type": EPSS + "score_date", "value": "$steps.scores.header.score_date"},
                ],
            },
        ],
    )


def pilot_world(
    tmp_path: Path,
    fixture: str,
    *,
    spec: Any = None,
    authority: str = "propose",
    rows: Any = None,
    capture_body_access: str = "open",
) -> tuple[Any, Path]:
    data = json.loads((FIXTURES / fixture).read_bytes())
    kev = fixture.startswith("kev")
    keys = [r["cveID" if kev else "cve"] for r in data["vulnerabilities" if kev else "data"]]
    types = (
        (
            claim_type(KEV + "listed", "bool"),
            claim_type(KEV + "date_added", "date"),
            claim_type(KEV + "due_date", "date"),
            claim_type(KEV + "ransomware_use", "enum[Known, Unknown]"),
        )
        if kev
        else (
            claim_type(EPSS + "score", "decimal{0..1}"),
            claim_type(EPSS + "percentile", "decimal{0..1}"),
            claim_type(EPSS + "score_date", "date"),
        )
    )
    instance, root = claims_world(
        tmp_path,
        {"entries": [{"id": k} for k in keys]},
        types=types,
        rows=rows or (KEV_ROW if kev else EPSS_ROW),
        capture_body_access=capture_body_access,
        records=pilot_records(kev, rows),
    )
    (root / "data" / fixture).write_bytes((FIXTURES / fixture).read_bytes())
    _watched(instance)
    selected = spec or (kev_spec() if kev else epss_spec())
    install(
        instance,
        selected,
        authority=authority,
        findings=("extract_refused", "duplicate_key"),
        parameters={
            "logical_source": "cisa.kev.catalog" if kev else "first.epss",
            "relative_path": "data/" + fixture,
        },
    )
    return instance, root


#: §8.1: the KEV settle grant's scope, by ClaimType field.
KEV_SETTLE_SCOPE: dict[str, dict[str, Any]] = {
    "listed": {"create": {"basis": ["extracted"]}, "retire": {"basis": ["absence"]}},
    "date_added": {
        "create": {"basis": ["extracted"]},
        "revise": {"basis": ["extracted"]},
        "retire": {"basis": ["absence"]},
    },
    "due_date": {
        "create": {"basis": ["extracted"]},
        "revise": {"basis": ["extracted"]},
        "retire": {"basis": ["absence"]},
    },
    "ransomware_use": {"create": {"basis": ["extracted"]}, "revise": {"basis": ["extracted"]}},
}


def grant_kev_settle(
    instance: Any,
    *,
    scope: dict[str, dict[str, Any]] | None = None,
    values: dict[str, dict[str, Any]] | None = None,
    line_name: str = "test.claims-line",
) -> str:
    """Author the §8.1 settle Mandate (no condition, ``subjects: expected``); its digest."""

    from datetime import timedelta

    from cruxible_client.contracts.authoring.inputs import ProcedureMandateInput
    from cruxible_client.contracts.authoring.models import MandateScopeAuthoring
    from cruxible_client.contracts.procedure_mandates import (
        parse_procedure_mandate,
        procedure_mandate_digest,
    )
    from tests.support.operation_world import NOW

    chosen = KEV_SETTLE_SCOPE if scope is None else scope
    author(
        instance,
        ProcedureMandateInput(
            kind="procedure_mandate",
            name="security.kev-triage-settle",
            line_name=line_name,
            grants="settle",
            namespace=("claims",),
            valid_from=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=30),
            subjects="expected",
            scope=tuple(
                MandateScopeAuthoring.model_validate(
                    {
                        "claim_type": KEV + field,
                        "changes": changes,
                        **({"value": values[field]} if values and field in values else {}),
                    }
                )
                for field, changes in chosen.items()
            ),
        ),
        name="kev-settle-grant",
    )
    path = "procedure-mandates/security.kev-triage-settle.json"
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return procedure_mandate_digest(parse_procedure_mandate(tree[path], path=path)).tagged
