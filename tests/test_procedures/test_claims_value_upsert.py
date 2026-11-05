"""Value-side upsert (V1-V5): a Subject-valued template creates its missing target.

A Subject-valued value names its target by kind and key, as ``subject``
names a row's Subject, and may say ``create: if_absent``. A missing target is
then created in the same change: the Claim's object is the key reference
(``subject-key-v1``) shared lowering lands on the one Subject item creating
it. Creation deduplicates by (kind, canonical key) across rows and values
(V5), an ambiguous or invalid key never creates, and an item's identity is
the address lowering assigns, so the creating run and every later run agree
(D-R37 g1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import SubjectInput
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicy, ClaimResolutionPolicy
from cruxible_client.contracts.shapes import Contract, contract_path, render_contract
from cruxible_client.contracts.subject_kinds import (
    SubjectKeyRule,
    SubjectKind,
    SubjectKindKeys,
    render_subject_kind,
    subject_kind_path,
)
from tests.core_support._identity_support import accept_tree
from tests.core_support._subject_kind_support import keyed_subject_kind, minted_subject_kind
from tests.support.operation_world import author, operation_build, world
from tests.support.procedures import procedure_limits, procedure_spec
from tests.test_procedures.test_claims_terminal import (
    KIND,
    claim_type,
    install,
)

COMPONENT = "security.component"
AFFECTED = COMPONENT + ".affected_by"
PRIMARY = COMPONENT + ".primary_vulnerability"
RELATED = KIND + ".related"
CAPTURE = "test.orders-v1"
ROW = Contract.model_validate(
    {
        "name": "test.affected-row",
        "version": 1,
        "fields": {"cve": "string{max_len: 256}", "since": "datetime?"},
    }
)


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
            max_bytes=262144,
            decoding=JSON_DECODING,
        ),
        named=True,
    )


def _kind(kind: str, key: str, *, lowercase: bool = False) -> SubjectKind:
    if not lowercase:
        return keyed_subject_kind(kind, (key,))
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.identity_normalizers import LowercaseNormalizer

    return SubjectKind(
        identity=ArtifactIdentity(kind="SubjectKind", name=kind),
        identity_rule=SubjectKindKeys(
            parts=(SubjectKeyRule(claim_type=key, normalizer=LowercaseNormalizer(version=1)),),
            uniqueness="unique",
        ),
    )


def _key_type(predicate: str, kind: str) -> Any:
    return claim_type(
        predicate,
        "string",
        allowed_subject_kinds=[kind],
        evidence_requirement="none",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=()),
    )


def affected_type(*, inverse: dict[str, Any] | None = None) -> Any:
    from cruxible_client.contracts.claim_types import ClaimInverse

    return claim_type(
        AFFECTED,
        f"subject<{KIND}>",
        allowed_subject_kinds=[COMPONENT],
        cardinality="many",
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
        **(
            {}
            if inverse is None
            else {"inverse": ClaimInverse(key=inverse, evidence_rules=("fixture",))}
        ),
    )


def upsert_world(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    *,
    components: tuple[str, ...] | None = None,
    vulnerabilities: tuple[str, ...] = (),
    lowercase: bool = False,
    types: tuple[Any, ...] | None = None,
    minted: bool = False,
) -> tuple[Any, Path]:
    """Components keyed by purl, vulnerabilities by cve_id; ``entries`` the source rows."""

    instance = world(tmp_path, operation_build())
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True)
    _write(root, entries)
    all_types = types if types is not None else (affected_type(),)
    keys = (_key_type(KIND + ".cve_id", KIND), _key_type(COMPONENT + ".purl", COMPONENT))
    accept_tree(
        instance,
        {
            capture_contract_path(CAPTURE): render_capture_contract(_capture_contract()),
            contract_path(ROW.name, ROW.version): render_contract(ROW),
            subject_kind_path(KIND): render_subject_kind(
                minted_subject_kind(KIND)
                if minted
                else _kind(KIND, KIND + ".cve_id", lowercase=lowercase)
            ),
            subject_kind_path(COMPONENT): render_subject_kind(
                _kind(COMPONENT, COMPONENT + ".purl")
            ),
            **{
                claim_type_path(t.predicate): render_claim_type(t)
                for t in (*all_types, *(keys[1:] if minted else keys))
            },
        },
        name="upsert-vocabulary",
    )
    if components is None:
        components = tuple(dict.fromkeys(e["purl"] for e in entries))
    for index, purl in enumerate(components):
        _seed(instance, COMPONENT, {"purl": purl}, f"component-{index}")
    for index, cve in enumerate(vulnerabilities):
        _seed(instance, KIND, {"cve_id": cve}, f"vulnerability-{index}")
    return instance, root


def _seed(instance: Any, kind: str, key: dict[str, str], name: str) -> None:
    author(
        instance,
        SubjectInput(kind="subject", subject_kind=kind, key=key, rationale="Seeded."),
        name=name,
    )


def _write(root: Path, entries: list[dict[str, Any]], **extra: Any) -> None:
    (root / "data" / "doc.json").write_text(
        json.dumps({"entries": entries, "count": len(entries), **extra})
    )


def target(create: str | None = "if_absent", key: object = "$item.fields.cve") -> dict[str, Any]:
    value: dict[str, Any] = {"kind": KIND, "key": {"cve_id": key}}
    if create is not None:
        value["create"] = create
    return value


def upsert_spec(
    *,
    value: Any = None,
    mappings: list[dict[str, Any]] | None = None,
    subject: dict[str, Any] | None = None,
    name: str = "test.claims",
    extract: dict[str, Any] | None = None,
    **write: Any,
) -> Any:
    """read the file -> extract one record per (component, CVE) -> claims."""

    return procedure_spec(
        name,
        reference_text=True,
        input={},
        output={},
        slots={
            "fetch": {
                "in": "workspace.file.request@1",
                "out": "workspace.file.result@1",
                "effect": "pure",
            }
        },
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=262144),
        steps=[
            {
                "call": "doc",
                "slot": "fetch",
                "captures": {"capture": CAPTURE},
                "input": {
                    "logical_source": "commerce.production.orders",
                    "relative_path": "data/doc.json",
                    "coordinate_type": "database-snapshot-v1",
                    "coordinate": {"lsn": "workspace"},
                    "selector_type": "relation-primary-key-v1",
                    "selector": {"id": 7, "relation": "orders"},
                },
            },
            {
                "extract": "entries",
                "capture": "$steps.doc.capture",
                "records": "/entries/*",
                "row": ROW.ref.spelling,
                "key": {
                    "purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"},
                    "advisory": {"at": "cve", "type": f"key_part<{KIND}:cve_id>"},
                },
                "fields": {"cve": {"at": "cve"}, "since": {"at": "since", "parse": "datetime"}},
                **(extract or {}),
            },
            {
                "id": "write",
                "for_each": "$steps.entries.records",
                "subject": subject
                or {"kind": COMPONENT, "key": {"purl": "$item.key.purl"}, "create": "never"},
                "claims": mappings or [{"type": AFFECTED, "value": value or target()}],
                **write,
            },
        ],
    )


def _subject_items(state: Any) -> list[Any]:
    from cruxible_core.procedures.claims_changes import CoreSubjectChange

    return [c for c in state_changes(state) if isinstance(c, CoreSubjectChange)]


def state_changes(state: Any) -> tuple[Any, ...]:
    return _LAST_CHANGES[state.run_id]


_LAST_CHANGES: dict[str, tuple[Any, ...]] = {}


@pytest.fixture(autouse=True)
def _record_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep each run's terminal changes (the executor's own expansion), by run."""

    from cruxible_core.procedures import claims_terminal

    original = claims_terminal.expand_claims

    def recorded(node: Any, **kwargs: Any) -> Any:
        expanded = original(node, **kwargs)
        _LAST_CHANGES[kwargs["admission"].run_id] = expanded.changes
        return expanded

    monkeypatch.setattr(claims_terminal, "expand_claims", recorded)


def _paths(instance: Any, kind: str) -> dict[str, str]:
    """Each live Subject of ``kind`` by its key value."""

    from cruxible_client.contracts.claims import parse_claim

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    key_predicate = {KIND: KIND + ".cve_id", COMPONENT: COMPONENT + ".purl"}[kind]
    found: dict[str, str] = {}
    for path in tree:
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.statement.predicate == key_predicate and claim.lifecycle.state == "live":
            found[str(claim.statement.object.value)] = claim.statement.subject.artifact_path
    return found


def _objects(claims: list[Any]) -> set[tuple[str, str]]:
    return {
        (c.statement.subject.artifact_path, c.statement.object.address.artifact_path)
        for c in claims
        if c.statement.predicate == AFFECTED and c.statement.object.kind == "subject"
    }


def _diagnostics(state: Any) -> list[Any]:
    return [d for egress in state.terminal_egress for d in egress.claims_diagnostics]


# -- V1: authoring validation ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({**target(), "note": "x"}, "spells only kind, key and create; drop note"),
        (target(create="always"), "create must be never or if_absent"),
        ({"kind": KIND, "key": {}, "create": "if_absent"}, "exactly once"),
        ({"kind": KIND, "key": {"cve_id": "a", "security.vulnerability.cve_id": "b"}}, "once"),
        ({"kind": KIND, "key": {"slug": "x"}}, "exactly once"),
        ({"kind": KIND, "key": "CVE-2026-0001"}, "key must map each key part"),
        ({"kind": KIND, "key": {"cve_id": "$item.fields.since"}}, "not assignable"),
    ],
    ids=["unknown-field", "create", "no-part", "twice", "unknown-part", "bare-key", "typed"],
)
def test_a_subject_value_is_validated_at_authoring(
    tmp_path: Path, value: dict[str, Any], message: str
) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, _root = upsert_world(tmp_path, [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"}])
    with pytest.raises(AuthoringLoweringError) as refused:
        install(instance, upsert_spec(value=value))
    assert refused.value.code == "cruxible.authoring.claims_type_invalid"
    assert message in refused.value.message
    assert refused.value.offending_element.startswith("$.steps[2].claims[0].value")


def test_create_on_a_minted_kind_is_refused_at_authoring(tmp_path: Path) -> None:
    from cruxible_core.authoring.lowering import AuthoringLoweringError

    instance, _root = upsert_world(
        tmp_path, [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"}], minted=True
    )
    spec = upsert_spec(
        value=target(),
        extract={
            "key": {
                "purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"},
                "advisory": {"at": "cve"},
            }
        },
    )
    with pytest.raises(AuthoringLoweringError) as refused:
        install(instance, spec)
    assert refused.value.code == "cruxible.authoring.claims_type_invalid"
    assert "is minted" in refused.value.message
