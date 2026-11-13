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
    activate,
    candidate_claims,
    claim_type,
    install,
    run,
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


def _entries(advisory: dict[str, Any], count_at: str | None = None) -> dict[str, Any]:
    from tests.support.layouts import record_set

    return record_set(
        at="/entries/*",
        row=ROW.ref.spelling,
        key={"purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"}, "advisory": advisory},
        fields={"cve": {"at": "cve"}, "since": {"at": "since", "parse": "datetime"}},
        count_at=count_at,
    )


def _capture_contract(*, minted: bool = False) -> Any:
    """The kind: ``entries`` keyed by component and advisory (``entries_counted`` with
    the document's count), ``entries_plain`` with the advisory a plain string; the
    header lists the components queried. Over a minted vulnerability kind only the
    plain reading names no vulnerability key."""

    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )
    from tests.core_support._light_capture_support import JSON_DECODING
    from tests.support.layouts import with_layout

    advisory = {"at": "cve", "type": f"key_part<{KIND}:cve_id>"}
    records = {"entries_plain": _entries({"at": "cve", "type": "string{max_len: 256}"})}
    if not minted:
        records.update(entries=_entries(advisory), entries_counted=_entries(advisory, "/count"))
    bare = lower_capture_contract_input(
        CaptureContractInputRecord(
            name=CAPTURE,
            shape="database_snapshot",
            replay="exact",
            max_bytes=262144,
            decoding=JSON_DECODING,
        ),
        named=True,
    )
    return with_layout(
        bare,
        records,
        {
            "queried": {"at": "/queried", "type": "list<string{max_len: 256}>{max_items: 8}"},
            # The first entry's CVE: a header a late read can key on (D-R38).
            "first": {"at": "/entries/0/cve", "type": "string{max_len: 256}"},
        },
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
            capture_contract_path(CAPTURE): render_capture_contract(
                _capture_contract(minted=minted)
            ),
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


def _record_set_of(extract: dict[str, Any]) -> str:
    """The record set a test's extract variant reads: a plain-string advisory key, the
    counted set, or the default."""

    if "key" in extract:
        return "entries_plain"
    return "entries_counted" if "complete_when" in extract else "entries"


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
        slots={"fetch": {"capability": "workspace.file@1"}},
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
                "records": _record_set_of(extract or {}),
                **(
                    {"header": sorted((extract or {})["header"])}
                    if (extract or {}).get("header")
                    else {}
                ),
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


# -- V1: a missing target is created; one Subject item per target -----------------


def test_a_missing_target_is_created_by_one_subject_item_per_key(tmp_path: Path) -> None:
    entries = [
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"},
        {"purl": "pkg:pypi/b@1", "cve": "CVE-2026-0001"},
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0002"},
    ]
    instance, root = upsert_world(tmp_path, entries)
    install(instance, upsert_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    created = _subject_items(state)
    assert sorted(c.key[KIND + ".cve_id"] for c in created) == ["CVE-2026-0001", "CVE-2026-0002"]
    assert all(c.subject_kind == KIND for c in created)
    claims = candidate_claims(instance, state)
    assert len([c for c in claims if c.statement.predicate == AFFECTED]) == 3
    activate(instance, state)

    vulns = _paths(instance, KIND)
    components = _paths(instance, COMPONENT)
    assert set(vulns) == {"CVE-2026-0001", "CVE-2026-0002"}
    held = _objects(_live_claims(instance))
    assert held == {
        (components["pkg:pypi/a@1"], vulns["CVE-2026-0001"]),
        (components["pkg:pypi/b@1"], vulns["CVE-2026-0001"]),
        (components["pkg:pypi/a@1"], vulns["CVE-2026-0002"]),
    }
    # The same source again changes nothing: the targets exist now.
    again = run(instance, root, minute=1)
    assert again.status == "succeeded", again.terminal
    assert not _subject_items(again)
    assert again.terminal_egress[0].proposal_id is None


def _live_claims(instance: Any) -> list[Any]:
    from cruxible_client.contracts.claims import parse_claim

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    return [
        claim
        for path in tree
        if path.startswith("claims/")
        for claim in (parse_claim(tree[path], path=path),)
        if claim.lifecycle.state == "live"
    ]


def test_a_missing_target_without_create_is_refused_per_item(tmp_path: Path) -> None:
    entries = [
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"},
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0009"},
    ]
    instance, root = upsert_world(tmp_path, entries, vulnerabilities=("CVE-2026-0001",))
    install(instance, upsert_spec(value=target(create=None)))
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert not _subject_items(state)
    (claim,) = candidate_claims(instance, state)
    assert claim.statement.object.address.artifact_path == _paths(instance, KIND)["CVE-2026-0001"]
    refused = [d for d in _diagnostics(state) if d.problem == "value_invalid"]
    assert len(refused) == 1


def test_a_target_held_at_base_is_used_and_never_created(tmp_path: Path) -> None:
    entries = [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"}]
    instance, root = upsert_world(tmp_path, entries, vulnerabilities=("CVE-2026-0001",))
    install(instance, upsert_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert not _subject_items(state)
    (claim,) = candidate_claims(instance, state)
    assert claim.statement.object.address.selector.scheme == "artifact-v1"
    assert claim.statement.object.address.artifact_path == _paths(instance, KIND)["CVE-2026-0001"]


def test_two_spellings_of_one_key_are_one_subject_item(tmp_path: Path) -> None:
    """V5: the kind's R12 normalizer makes CVE-... and cve-... one key, one item."""

    entries = [
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"},
        {"purl": "pkg:pypi/b@1", "cve": "cve-2026-0001"},
    ]
    instance, root = upsert_world(tmp_path, entries, lowercase=True)
    install(instance, upsert_spec())
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (created,) = _subject_items(state)
    assert created.key == {KIND + ".cve_id": "cve-2026-0001"}
    activate(instance, state)
    (vuln,) = _paths(instance, KIND).values()
    assert {obj for _, obj in _objects(_live_claims(instance))} == {vuln}


def test_a_value_target_that_is_a_rows_subject_shares_its_subject_item(tmp_path: Path) -> None:
    """V5: rows create vulnerabilities; one row's value names another row's Subject."""

    related = claim_type(RELATED, f"subject<{KIND}>", value_type=(KIND,))
    entries = [
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0002"},
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"},
    ]
    instance, root = upsert_world(tmp_path, entries, components=(), types=(related,))
    # Each row's Subject is its own CVE, created if absent; each value names
    # CVE-2026-0001, the second row's Subject.
    spec = upsert_spec(
        subject={"kind": KIND, "key": {"cve_id": "$item.key.advisory"}, "create": "if_absent"},
        mappings=[{"type": RELATED, "value": target(key="CVE-2026-0001")}],
    )
    install(instance, spec)
    state = run(instance, root)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    created = _subject_items(state)
    assert sorted(c.key.get("cve_id") or c.key[KIND + ".cve_id"] for c in created) == [
        "CVE-2026-0001",
        "CVE-2026-0002",
    ]
    activate(instance, state)
    vulns = _paths(instance, KIND)
    held = {
        (c.statement.subject.artifact_path, c.statement.object.address.artifact_path)
        for c in _live_claims(instance)
        if c.statement.predicate == RELATED
    }
    assert held == {
        (vulns["CVE-2026-0002"], vulns["CVE-2026-0001"]),
        (vulns["CVE-2026-0001"], vulns["CVE-2026-0001"]),
    }


def test_two_spellings_of_one_row_key_are_one_subject_item(tmp_path: Path) -> None:
    """V5 on the row side: the latent two-item refusal on raw-byte keys is gone."""

    related = claim_type(
        RELATED,
        f"subject<{KIND}>",
        value_type=(KIND,),
        cardinality="many",
        resolution_policy=ClaimResolutionPolicy(
            cardinality="many", eligible_verdicts=("supported",), selector="all"
        ),
    )
    entries = [
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"},
        {"purl": "pkg:pypi/b@1", "cve": "cve-2026-0001"},
    ]
    instance, root = upsert_world(
        tmp_path,
        entries,
        components=(),
        vulnerabilities=("CVE-2026-0008", "CVE-2026-0009"),
        lowercase=True,
        types=(related,),
    )
    spec = upsert_spec(
        extract={
            "key": {
                "purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"},
                "advisory": {"at": "cve", "type": "string{max_len: 256}"},
            }
        },
        subject={"kind": KIND, "key": {"cve_id": "$item.key.advisory"}, "create": "if_absent"},
        mappings=[
            {
                "type": RELATED,
                "value": {"kind": KIND, "key": {"cve_id": "CVE-2026-0008"}},
                "when": {
                    "op": "eq",
                    "left": {"$ref": "item", "path": ["fields", "cve"]},
                    "right": {"literal": "CVE-2026-0001"},
                },
            },
            {
                "type": RELATED,
                "value": {"kind": KIND, "key": {"cve_id": "CVE-2026-0009"}},
                "when": {
                    "op": "eq",
                    "left": {"$ref": "item", "path": ["fields", "cve"]},
                    "right": {"literal": "cve-2026-0001"},
                },
            },
        ],
    )
    install(instance, spec)
    state = run(instance, root)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    (created,) = _subject_items(state)
    assert created.subject_kind == KIND
    activate(instance, state)
    vulns = _paths(instance, KIND)
    held = {
        (c.statement.subject.artifact_path, c.statement.object.address.artifact_path)
        for c in _live_claims(instance)
        if c.statement.predicate == RELATED
    }
    assert held == {
        (vulns["cve-2026-0001"], vulns["cve-2026-0008"]),
        (vulns["cve-2026-0001"], vulns["cve-2026-0009"]),
    }


# -- D-R37 g1: one identity from the creating run on ---------------------------------


def _identities(instance: Any, state: Any) -> dict[str, str]:
    """identity -> change kind, as the run read serves them."""

    from tests.test_procedures.test_claims_item_changes import _changes

    return {identity: kind for identity, (kind, _, _) in _changes(instance, state).items()}


def test_a_created_targets_claim_keeps_one_identity_through_reobserve_revise_and_retire(
    tmp_path: Path,
) -> None:
    from cruxible_client.contracts.semantic import SemanticAddress
    from cruxible_core.procedures.claims_units import claims_item_identity_digest
    from tests.support.procedures import boundary_contract, contract_files

    a = "pkg:pypi/a@1"
    entries = [{"purl": a, "cve": "CVE-2026-0001", "since": "2026-01-01T00:00:00Z"}]
    instance, root = upsert_world(tmp_path, entries)
    expect_row = boundary_contract("test.expect-component", {"purl": "string"})
    accept_tree(instance, contract_files(expect_row), name="expect-row")
    raw = upsert_spec(
        mappings=[
            {
                "type": AFFECTED,
                "value": target(),
                "effective_from": "$item.fields.since",
                "when_absent": "mirror",
            }
        ],
        extract={"complete_when": {"count_at": "/count"}},
        expect="$params.expected",
    ).model_dump(mode="python", by_alias=True)
    raw["params"] = {"expected": f"list<{expect_row.ref.spelling}>{{max_items: 4}}"}
    install(
        instance,
        procedure_spec(base=raw, reference_text=True),
        parameters={"expected": [{"purl": a}]},
    )

    created = run(instance, root)
    before = _identities(instance, created)
    assert sorted(before.values()) == ["claim", "subject"]
    (claim_identity,) = [k for k, kind in before.items() if kind == "claim"]
    (subject_identity,) = [k for k, kind in before.items() if kind == "subject"]
    activate(instance, created)
    vulnerability = _paths(instance, KIND)["CVE-2026-0001"]
    # The Subject item is identified by the address lowering assigned it.
    assert subject_identity == claims_item_identity_digest(
        node_id="write", subject=SemanticAddress.whole_artifact(vulnerability)
    )

    # Re-observed: the same unit from new bytes.
    _write(root, entries, note="re-read")
    assert _identities(instance, run(instance, root, minute=1)) == {claim_identity: "reobservation"}
    # Revised: the same unit, a new effective period.
    revised = [{**entries[0], "since": "2026-02-01T00:00:00Z"}]
    _write(root, revised)
    revision = run(instance, root, minute=2)
    assert _identities(instance, revision) == {claim_identity: "claim"}
    activate(instance, revision)
    # Retired: the complete source no longer lists the CVE for the component.
    _write(root, [])
    assert _identities(instance, run(instance, root, minute=3)) == {claim_identity: "retirement"}


# -- an ambiguous or invalid key never creates ------------------------------------


class _Context:
    """A Claims context whose key lookups answer as configured."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.canonicals = 0

    def subject(self, kind: str, key: Any) -> Any:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def named(self, kind: str, key: Any) -> Any:
        from cruxible_core.procedures.claims_units import NamedSubject, ValueTarget

        address = self.subject(kind, key)
        if address is not None:
            return NamedSubject(address, None)
        return NamedSubject(None, ValueTarget(kind=kind, key=self.canonical(kind, key)))

    def canonical(self, kind: str, key: Any) -> Any:
        self.canonicals += 1
        if any(not isinstance(value, str) for value in key.values()):
            raise ValueError("a key part is a string, an integer or a boolean")
        return tuple((KIND + "." + name, value) for name, value in key.items())


@pytest.mark.parametrize(
    "answer",
    [
        ValueError("subject_key_ambiguous: security.vulnerability has several holders"),
        ValueError("cve_id: not a key value"),
    ],
    ids=["ambiguous", "invalid"],
)
def test_an_ambiguous_or_invalid_key_never_creates(answer: Exception) -> None:
    from cruxible_core.procedures.claims_units import resolve_value

    context = _Context(answer)
    obj, created = resolve_value(target(key="CVE-2026-0001"), affected_type(), context, None)
    assert obj is None and created is None
    assert context.canonicals == 0


def test_only_a_missing_target_with_create_resolves_to_its_key() -> None:
    from cruxible_core.procedures.claims_units import resolve_value

    obj, created = resolve_value(target(key="CVE-2026-0001"), affected_type(), _Context(None), None)
    assert created is not None and created.slot[0] == KIND
    assert obj is not None and obj["address"]["selector"]["scheme"] == "subject-key-v1"
    assert resolve_value(
        target(create=None, key="CVE-2026-0001"), affected_type(), _Context(None), None
    ) == (None, None)
    # A present field result names its value.
    present = {"tag": "present", "value": "CVE-2026-0001", "at": "/entries/0/cve"}
    _obj, from_field = resolve_value(target(key=present), affected_type(), _Context(None), None)
    assert from_field == created
    absent = {"tag": "absent", "value": None}
    assert resolve_value(target(key=absent), affected_type(), _Context(None), None) == (None, None)


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


# -- V2: absence and inverses follow the same value rules ---------------------------


def _absence_line(
    instance: Any,
    *,
    value: Any,
    when_absent: str,
    expected: list[str],
    inverse: bool = False,
) -> None:
    from tests.support.procedures import boundary_contract, contract_files

    expect_row = boundary_contract("test.expect-component", {"purl": "string"})
    accept_tree(instance, contract_files(expect_row), name="expect-row")
    raw = upsert_spec(
        mappings=[{"type": AFFECTED, "value": value, "when_absent": when_absent}],
        extract={"complete_when": {"count_at": "/count"}},
        expect="$params.expected",
    ).model_dump(mode="python", by_alias=True)
    raw["params"] = {"expected": f"list<{expect_row.ref.spelling}>{{max_items: 4}}"}
    install(
        instance,
        procedure_spec(base=raw, reference_text=True),
        parameters={"expected": [{"purl": purl} for purl in expected]},
    )


def test_a_new_target_never_suppresses_mirror_retirement(tmp_path: Path) -> None:
    """A component's CVE moves from A to a new B: B is created and A retires, one run."""

    a = "pkg:pypi/a@1"
    instance, root = upsert_world(tmp_path, [{"purl": a, "cve": "CVE-2026-0001"}])
    _absence_line(instance, value=target(), when_absent="mirror", expected=[a])
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    activate(instance, first)
    old = _paths(instance, KIND)["CVE-2026-0001"]

    _write(root, [{"purl": a, "cve": "CVE-2026-0002"}])
    moved = run(instance, root, minute=1)
    assert moved.status == "succeeded", moved.terminal
    kinds = sorted(type(change).__name__ for change in state_changes(moved))
    assert kinds == ["CoreClaimChange", "CoreRetirementChange", "CoreSubjectChange"]
    activate(instance, moved)
    vulns = _paths(instance, KIND)
    assert {obj for _, obj in _objects(_live_claims(instance))} == {vulns["CVE-2026-0002"]}
    assert old in vulns.values()  # the Subject stays; only its Claim retired


@pytest.mark.parametrize("unit", ["object", "object_key"])
def test_an_inverse_creates_its_missing_x_and_keeps_one_identity(tmp_path: Path, unit: str) -> None:
    """V2: none of X, X missing with create: X is created in the same change."""

    a = "pkg:pypi/a@1"
    inverse = {"kind": unit, **({"predicate": KIND + ".cve_id"} if unit == "object_key" else {})}
    instance, root = upsert_world(
        tmp_path, [], components=(a,), types=(affected_type(inverse=inverse),)
    )
    _absence_line(
        instance,
        value=target(key="CVE-2026-0001"),
        when_absent="inverse",
        expected=[a],
    )
    first = run(instance, root)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    (created,) = _subject_items(first)
    assert created.key == {KIND + ".cve_id": "CVE-2026-0001"}
    before = _identities(instance, first)
    (inverse_identity,) = [k for k, kind in before.items() if kind == "claim"]
    activate(instance, first)
    vulnerability = _paths(instance, KIND)["CVE-2026-0001"]
    (held,) = [c for c in _live_claims(instance) if c.statement.predicate == AFFECTED]
    assert held.statement.object.kind == "none"
    if unit == "object_key":
        assert held.statement.object.of.model_dump(mode="json") == {
            "kind": "literal",
            "value": "CVE-2026-0001",
        }
    else:
        assert held.statement.object.of.address.artifact_path == vulnerability

    # The same complete absence from new bytes re-confirms it, same identity.
    _write(root, [], note="re-read")
    again = run(instance, root, minute=1)
    assert again.status == "succeeded", again.terminal
    assert not _subject_items(again)
    assert _identities(instance, again) == {inverse_identity: "reobservation"}


def test_an_inverse_whose_x_is_missing_without_create_is_refused_per_item(
    tmp_path: Path,
) -> None:
    """V2: replaces the whole-run claims_absence_invalid refusal; the run completes."""

    a, b = "pkg:pypi/a@1", "pkg:pypi/b@1"
    instance, root = upsert_world(
        tmp_path,
        [{"purl": b, "cve": "CVE-2026-0001"}],
        components=(a, b),
        types=(affected_type(inverse={"kind": "object"}),),
    )
    _absence_line(
        instance,
        value=target(create=None, key="CVE-2026-0001"),
        when_absent="inverse",
        expected=[a, b],
    )
    state = run(instance, root)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert not _subject_items(state)
    refused = [d for d in _diagnostics(state) if d.problem == "value_invalid"]
    # b's positive and a's inverse each refuse alone.
    assert len(refused) == 2
    assert state.terminal_egress[0].proposal_id is None


def _subject_id(path: str, kind: str) -> str:
    return path.removeprefix(f"subjects/{kind}/").removesuffix(".json")


@pytest.mark.parametrize("unit", ["object", "object_key"])
def test_an_inverse_names_an_existing_target_from_a_string_value(tmp_path: Path, unit: str) -> None:
    """V2: a ``kind/id`` reference string resolves like a positive value, never a literal.

    Authoring types a literal Subject string only in this exact-id form (the
    dict ``{kind, key, create}`` form names any key); at runtime the same
    string must name its target through the shared resolver, not fall back
    to ``{kind: literal, value: <text>}``.
    """

    a = "pkg:pypi/a@1"
    inverse = {"kind": unit, **({"predicate": KIND + ".cve_id"} if unit == "object_key" else {})}
    instance, root = upsert_world(
        tmp_path,
        [],
        components=(a,),
        vulnerabilities=("CVE-2026-0001",),
        types=(affected_type(inverse=inverse),),
    )
    subject_id = _subject_id(_paths(instance, KIND)["CVE-2026-0001"], KIND)
    _absence_line(
        instance,
        value=f"{KIND}/{subject_id}",
        when_absent="inverse",
        expected=[a],
    )
    state = run(instance, root)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert not _subject_items(state)
    assert not [d for d in _diagnostics(state) if d.problem == "value_invalid"]
    activate(instance, state)
    vulnerability = _paths(instance, KIND)["CVE-2026-0001"]
    (held,) = [c for c in _live_claims(instance) if c.statement.predicate == AFFECTED]
    assert held.statement.object.kind == "none"
    if unit == "object_key":
        assert held.statement.object.of.model_dump(mode="json") == {
            "kind": "literal",
            "value": "CVE-2026-0001",
        }
    else:
        assert held.statement.object.of.address.artifact_path == vulnerability


@pytest.mark.parametrize("unit", ["object", "object_key"])
def test_an_inverse_whose_string_value_is_unresolved_is_refused_per_item(
    tmp_path: Path, unit: str
) -> None:
    """V2: a string value never creates; an unresolved target is value_invalid, per item."""

    a = "pkg:pypi/a@1"
    inverse = {"kind": unit, **({"predicate": KIND + ".cve_id"} if unit == "object_key" else {})}
    instance, root = upsert_world(
        tmp_path,
        [],
        components=(a,),
        types=(affected_type(inverse=inverse),),
    )
    _absence_line(
        instance,
        value=f"{KIND}/does-not-exist",
        when_absent="inverse",
        expected=[a],
    )
    state = run(instance, root)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert not _subject_items(state)
    refused = [d for d in _diagnostics(state) if d.problem == "value_invalid"]
    assert len(refused) == 1
    assert state.terminal_egress[0].proposal_id is None


# -- V3: Mandate scope is kind-based ------------------------------------------------


def _install_grants(
    instance: Any,
    spec: Any,
    *,
    settle: tuple[str, ...] | None = None,
    propose: tuple[str, ...] | None = None,
    subject_scope: tuple[str, ...] | None = None,
    binding: str = "subject",
) -> None:
    """The Line with a settle grant creating ``settle`` kinds, and/or a propose grant."""

    from datetime import UTC, datetime

    from cruxible_client.contracts.artifacts import ArtifactPin
    from cruxible_client.contracts.claim_types import claim_type_digest, parse_claim_type
    from cruxible_client.contracts.procedure_mandates import (
        MandateClaimScope,
        procedure_mandate_path,
        render_procedure_mandate,
    )
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_digest,
        line_spec_path,
        parse_line_spec,
    )
    from cruxible_client.contracts.semantic import SemanticAddress
    from tests.support.mandates import procedure_mandate, scope_changes
    from tests.support.operation_world import author_line, author_procedure

    line_name = "test.claims-line"
    author_procedure(instance, spec)
    author_line(
        instance,
        name=line_name,
        procedure=spec.procedure,
        parameters={},
        max_authority="settle" if settle is not None else "propose",
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = line_spec_path(line_name)
    line = parse_line_spec(tree[path], path=path)
    pin = ArtifactPin(
        role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
    )
    grants = []
    if settle is not None:
        affected = parse_claim_type(tree[claim_type_path(AFFECTED)], path=claim_type_path(AFFECTED))
        grants.append(
            procedure_mandate(
                "settle-grant",
                line=pin,
                grants="settle",
                valid_from=datetime(2020, 1, 1, tzinfo=UTC),
                expires_at=datetime(2099, 1, 1, tzinfo=UTC),
                scope=(
                    MandateClaimScope(
                        claim_type=ArtifactPin(
                            role="claim-type",
                            target=affected.identity,
                            artifact_digest=claim_type_digest(affected).tagged,
                        ),
                        changes=scope_changes("create"),
                        binding_subject_role=binding,  # type: ignore[arg-type]
                    ),
                ),
                subject_creation=tuple(sorted(settle)),
                subject_scope=None
                if subject_scope is None
                else tuple(
                    sorted(
                        (SemanticAddress.whole_artifact(p) for p in subject_scope),
                        key=lambda item: item.artifact_path,
                    )
                ),
            )
        )
    if propose is not None:
        grants.append(procedure_mandate("propose-grant", line=pin, namespace=propose))
    accept_tree(
        instance,
        {
            procedure_mandate_path(grant.identity.name): render_procedure_mandate(grant)
            for grant in grants
        },
        name="grants",
    )


def _accepted_affected(instance: Any) -> set[str]:
    vulns = {path: cve for cve, path in _paths(instance, KIND).items()}
    return {vulns[obj] for _, obj in _objects(_live_claims(instance)) if obj in vulns}


def test_a_settle_grant_listing_both_kinds_settles_mixed_row_and_value_creation(
    tmp_path: Path,
) -> None:
    entries = [
        {"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"},
        {"purl": "pkg:pypi/b@1", "cve": "CVE-2026-0002"},
    ]
    instance, root = upsert_world(tmp_path, entries, components=())
    spec = upsert_spec(
        subject={"kind": COMPONENT, "key": {"purl": "$item.key.purl"}, "create": "if_absent"}
    )
    _install_grants(instance, spec, settle=(COMPONENT, KIND))
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress
    assert len(_subject_items(state)) == 4  # two components (rows), two CVEs (values)
    assert _accepted_affected(instance) == {"CVE-2026-0001", "CVE-2026-0002"}


def test_a_settle_grant_without_the_value_kind_never_settles_its_creation(
    tmp_path: Path,
) -> None:
    """The row law, unchanged for values: an unlisted kind's creation is no settle target.

    It refuses ``settle_mandate_missing`` exactly as a row Subject of an unlisted
    kind does (``test_a_settle_grant_naming_no_kind_never_creates_a_subject``);
    nothing is accepted. The same Line capped at propose proposes it.
    """

    entries = [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"}]
    instance, root = upsert_world(tmp_path, entries)
    _install_grants(instance, upsert_spec(), settle=(COMPONENT,))
    base = instance.accepted_coordinate()
    state = run(instance, root)
    (egress,) = state.terminal_egress
    assert (egress.verdict, egress.refusal_code) == ("refused", "settle_mandate_missing"), egress
    assert egress.settle_outcome is None
    assert instance.accepted_coordinate() == base
    assert _accepted_affected(instance) == set()


def test_a_propose_grant_reaches_value_created_subjects_through_its_namespace(
    tmp_path: Path,
) -> None:
    entries = [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"}]
    instance, root = upsert_world(tmp_path, entries)
    _install_grants(instance, upsert_spec(), propose=("claims", f"subjects/{KIND}"))
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered" and egress.proposal_id is not None, egress
    assert any(path.startswith(f"subjects/{KIND}/") for path in egress.target_paths)


def test_an_object_bound_subject_scope_covers_a_just_created_target(tmp_path: Path) -> None:
    """V3 edge: subject_scope names Subjects in advance; a created object is in scope."""

    entries = [{"purl": "pkg:pypi/a@1", "cve": "CVE-2026-0001"}]
    instance, root = upsert_world(tmp_path, entries, vulnerabilities=("CVE-2026-0009",))
    listed = _paths(instance, KIND)["CVE-2026-0009"]
    _install_grants(
        instance,
        upsert_spec(),
        settle=(KIND,),
        subject_scope=(listed,),
        binding="object",
        propose=("claims", f"subjects/{KIND}"),
    )
    state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    assert (egress.verdict, egress.settle_outcome) == ("delivered", "settled"), egress
    assert _accepted_affected(instance) == {"CVE-2026-0001"}


# -- V4: expect from a header field, bare strings by the text-reference order ------


def _header_line(instance: Any) -> None:
    spec = upsert_spec(
        mappings=[{"type": AFFECTED, "value": target(), "when_absent": "mirror"}],
        extract={
            "complete_when": {"count_at": "/count"},
            "header": {
                "queried": {"at": "/queried", "type": "list<string{max_len: 256}>{max_items: 8}"}
            },
        },
        expect="$steps.entries.header.queried",
    )
    install(instance, spec)


def test_expect_is_the_purls_a_header_lists(tmp_path: Path) -> None:
    """Mirror retires a dropped CVE of a queried component; an unqueried one is untouched."""

    a, b, c = "pkg:pypi/a@1", "pkg:pypi/b@1", "pkg:pypi/c@1"
    entries = [
        {"purl": a, "cve": "CVE-2026-0001"},
        {"purl": b, "cve": "CVE-2026-0002"},
        {"purl": c, "cve": "CVE-2026-0003"},
    ]
    instance, root = upsert_world(tmp_path, entries)
    _write(root, entries, queried=[a, b, c])
    _header_line(instance)
    first = run(instance, root)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    activate(instance, first)
    assert _accepted_affected(instance) == {"CVE-2026-0001", "CVE-2026-0002", "CVE-2026-0003"}

    # b is checked and clean now; c was not checked this time.
    _write(root, entries[:1], queried=[a, b])
    second = run(instance, root, minute=1)
    assert second.status == "succeeded", second.terminal
    kinds = sorted(type(change).__name__ for change in state_changes(second))
    assert kinds == ["CoreReobservationChange", "CoreRetirementChange"]  # a re-read, b retired
    activate(instance, second)
    assert _accepted_affected(instance) == {"CVE-2026-0001", "CVE-2026-0003"}


@pytest.mark.parametrize("header", ["absent", "invalid"])
def test_an_absent_or_invalid_header_leaves_the_source_incomplete(
    tmp_path: Path, header: str
) -> None:
    a = "pkg:pypi/a@1"
    entries = [{"purl": a, "cve": "CVE-2026-0001"}]
    instance, root = upsert_world(tmp_path, entries)
    _write(root, entries, queried=[a])
    _header_line(instance)
    first = run(instance, root)
    assert first.status == "succeeded", first.terminal
    activate(instance, first)

    if header == "absent":
        _write(root, [])
    else:
        _write(root, [], queried="not-a-list")
    state = run(instance, root, minute=1)
    assert state.status == "succeeded", json.dumps(state.terminal.model_dump(mode="json"))
    assert not [c for c in state_changes(state) if type(c).__name__ == "CoreRetirementChange"]
    assert any(d.problem == "source_incomplete" for d in _diagnostics(state))
    assert _accepted_affected(instance) == {"CVE-2026-0001"}


def test_an_expect_text_resolves_id_first_then_by_key(tmp_path: Path) -> None:
    """V4: a bare string is an exact id, else an alias, else the kind's single key."""

    a = "pkg:pypi/a@1"
    instance, _root = upsert_world(tmp_path, [{"purl": a, "cve": "CVE-2026-0001"}])
    path = _paths(instance, COMPONENT)[a]
    subject_id = path.removeprefix(f"subjects/{COMPONENT}/").removesuffix(".json")
    from cruxible_core.procedures.claims_context import AcceptedClaimsContext

    context = AcceptedClaimsContext(instance, instance.accepted_coordinate())
    assert context.reference(COMPONENT, a).artifact_path == path  # by key
    assert context.reference(COMPONENT, subject_id).artifact_path == path  # by id
    assert context.reference(COMPONENT, f"{COMPONENT}/{subject_id}").artifact_path == path
    assert context.reference(COMPONENT, path).artifact_path == path
    assert context.reference(COMPONENT, "pkg:pypi/unknown@1") is None
    assert context.reference(KIND, a) is None  # another kind's key names nothing here


# -- work counts: one creation item per (kind, canonical key); flat per item --------

#: Records per measured run: the marginal cost of a record must not grow with N.
_SIZES = (10, 55, 100)
#: The marginal per-record cost at the large end may exceed the small end's by
#: this ratio plus slack (fixed costs and digest-length noise), never more.
_SLOPE_RATIO = 1.15
_SLOPE_SLACK = 2.0


def _counted_run(
    tmp_path: Path, entries: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> dict[str, float]:
    from collections import Counter

    from cruxible_core.procedures.claims_context import AcceptedClaimsContext
    from tests.test_scale._counters import counting, trace_sqlite

    lookups: Counter[str] = Counter()
    original_subject = AcceptedClaimsContext._subject

    def counted_subject(self: Any, kind: str, key: Any) -> Any:
        if (kind, key) not in self._subjects:
            lookups[kind] += 1  # a lookup that reaches the projection
        return original_subject(self, kind, key)

    monkeypatch.setattr(AcceptedClaimsContext, "_subject", counted_subject)
    with trace_sqlite(tmp_path):
        instance, root = upsert_world(tmp_path, entries, components=())
        install(
            instance,
            upsert_spec(
                subject={
                    "kind": COMPONENT,
                    "key": {"purl": "$item.key.purl"},
                    "create": "if_absent",
                }
            ),
        )
        lookups.clear()
        with counting(tmp_path) as window:
            state = run(instance, root)
    assert state.status == "succeeded", state.terminal
    assert not window.unexplained, window.unexplained
    created = Counter(c.subject_kind for c in _subject_items(state))
    return {
        "component_items": created[COMPONENT],
        "vulnerability_items": created[KIND],
        "component_lookups": lookups[COMPONENT],
        "vulnerability_lookups": lookups[KIND],
        "sql_statements": window.counts["sql_statements"],
        "projection_binds": window.counts["projection_binds"],
    }


def _records(size: int, *, targets: int) -> list[dict[str, Any]]:
    """``size`` new components; record i names CVE ``i mod targets`` (all new)."""

    return [
        {"purl": f"pkg:pypi/c{index}@1", "cve": f"CVE-2026-{index % targets:04d}"}
        for index in range(size)
    ]


@pytest.mark.parametrize("shape", ["one_target", "many_targets"])
def test_value_upsert_work_is_one_item_per_key_and_flat_per_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    measured: dict[int, dict[str, float]] = {}
    for size in _SIZES:
        targets = 1 if shape == "one_target" else size
        found = _counted_run(
            _subdir(tmp_path, f"n{size}"), _records(size, targets=targets), monkeypatch
        )
        # One Subject item per distinct (kind, canonical key), rows and values alike,
        # and one key lookup per distinct key: the context caches each (kind, key).
        assert found["component_items"] == size
        assert found["vulnerability_items"] == targets
        assert found["component_lookups"] == size
        assert found["vulnerability_lookups"] == targets
        measured[size] = found
    small, middle, large = (measured[size] for size in _SIZES)
    step = (_SIZES[1] - _SIZES[0], _SIZES[2] - _SIZES[1])
    for counter in ("sql_statements", "projection_binds"):
        low = (middle[counter] - small[counter]) / step[0]
        high = (large[counter] - middle[counter]) / step[1]
        assert high <= low * _SLOPE_RATIO + _SLOPE_SLACK, (counter, measured)
    report = __import__("os").environ.get("VALUE_UPSERT_COUNTS")
    if report:
        with open(report, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"shape": shape, "measured": measured}) + "\n")


def _subdir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path
