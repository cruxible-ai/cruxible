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
KEV_ROW = boundary_contract(
    "security.kev-entry",
    {"date_added": "date?", "due_date": "date?", "ransomware_use": f"enum_of {KEV}ransomware_use?"},
)
EPSS_ROW = boundary_contract("security.epss-raw", {"score": "string", "percentile": "string"})
WATCH_ROW = boundary_contract(
    "security.watched-row", {"subject_kind": "string", "subject_id": "string", "cve_id": "string"}
)


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
            "coordinate_type": "database-snapshot-v1",
            "coordinate": {"lsn": "recorded-fixture"},
            "selector_type": "relation-primary-key-v1",
            "selector": {"id": 7, "relation": file},
        },
    }


def _spec(name: str, nodes: list[dict[str, Any]], *, may_truncate: bool = False) -> Any:
    return procedure_spec(
        name,
        reference_text=True,
        input={},
        output={},
        params={"logical_source": "string", "relative_path": "string"},
        slots={
            "fetch": {
                "in": "workspace.file.request@1",
                "out": "workspace.file.result@1",
                "effect": "pure",
                "may_truncate": may_truncate,
            }
        },
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
                "records": "/vulnerabilities/*",
                "row": KEV_ROW.ref.spelling,
                "key": {"cve_id": {"at": "cveID", "type": f"key_part<{KIND}:cve_id>"}},
                "complete_when": {"count_at": "/count"},
                "duplicates": "refuse_key",
                "fields": {
                    "date_added": {"at": "dateAdded", "parse": "date"},
                    "due_date": {"at": "dueDate", "parse": "date"},
                    "ransomware_use": {
                        "at": "knownRansomwareCampaignUse",
                        "canonicalize": "claim_type",
                    },
                },
                "header": {"catalog_version": {"at": "/catalogVersion"}},
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
                "records": "/data/*",
                "row": EPSS_ROW.ref.spelling,
                "key": {"cve_id": {"at": "cve", "type": f"key_part<{KIND}:cve_id>"}},
                "only_keys": "$steps.watch_keys.rows",
                "fields": {"score": {"at": "epss"}, "percentile": {"at": "percentile"}},
                "header": {
                    "score_date": {"at": "/created", "parse": "datetime", "project": "utc_date"}
                },
            },
            {
                "transform": "typed",
                "kind": "shape",
                "spec": {
                    "input": "$steps.scores.records",
                    "fields": {
                        "score": {"from": "$item.fields.score", "as": "decimal{0..1}"},
                        "percentile": {"from": "$item.fields.percentile", "as": "decimal{0..1}"},
                    },
                },
            },
            {
                "id": "write",
                "for_each": "$steps.typed.records",
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


def pilot_world(tmp_path: Path, fixture: str, *, spec: Any = None) -> tuple[Any, Path]:
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
        rows=KEV_ROW if kev else EPSS_ROW,
    )
    (root / "data" / fixture).write_bytes((FIXTURES / fixture).read_bytes())
    _watched(instance)
    selected = spec or (kev_spec() if kev else epss_spec())
    install(
        instance,
        selected,
        findings=("extract_refused", "duplicate_key"),
        parameters={
            "logical_source": "cisa.kev.catalog" if kev else "first.epss",
            "relative_path": "data/" + fixture,
        },
    )
    return instance, root
