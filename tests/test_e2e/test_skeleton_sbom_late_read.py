"""The SBOM-import shape with a late read (D-R38): fetch -> derive -> extract -> read -> claims.

One ``workspace.file`` fetch reads a CycloneDX-shaped SBOM; a pure derive over
it emits one derived Capture: the inventory the SBOM names (its header) and
one record per component. The extract reads them, and only THEN does the
Procedure read state: the memberships of that one inventory, keyed by the
inventory the derive found. That read could not run at admission (its key is
runtime output), so it runs when its input is ready, at the run's pinned
accepted coordinate, and its result enters the run's result digest rather
than its admission identity. ``expect`` is that read's rows: mirror retires a
component the next import no longer lists, and never touches another
inventory's memberships.

Work counts: with the SBOM fixed, growing the unrelated inventories leaves the
late read's rows and the run's SQL statements where they were.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
    ClaimResolutionPolicy,
)
from cruxible_client.contracts.query.authored import QuerySpecInput
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from tests.core_support._identity_support import accept_tree
from tests.core_support._light_capture_support import JSON_DECODING
from tests.core_support._subject_kind_support import keyed_subject_kind
from tests.support.capabilities import ensure_capabilities
from tests.support.layouts import record_set, with_layout
from tests.support.operation_world import NOW, Invoker, Operator, actor, author, build_files
from tests.support.procedures import boundary_contract, contract_files, procedure_limits
from tests.support.procedures import procedure_spec as build_spec
from tests.support.provider_checkout import require_provider_checkout
from tests.test_e2e.test_skeleton_capture_lists import _wire, install
from tests.test_e2e.test_skeleton_kev_epss import (
    _claim_type,
    _key_type,
    _web_contracts,
    activate,
    proposed,
    skeleton_world,
)

COMPONENT = "test.component"
INVENTORY = "test.inventory"
PURL = COMPONENT + ".purl"
INVENTORY_ID = INVENTORY + ".inventory_id"
IN_INVENTORY = COMPONENT + ".in_inventory"
#: The same membership as a literal (the inventory's id): its Query is one indexed
#: lookup by value, where the Subject-valued one walks a reverse hop.
LISTED_IN = COMPONENT + ".listed_in"
SBOM_KIND = "test.sbom"
COMPONENTS_KIND = "test.sbom-components"
MEMBERS = "test.inventory_members"
LISTED_MEMBERS = "test.listed_members"
PROVIDER = "test-sbom"
LINE = "test.sbom-line"
SBOM_PATH = "sboms/app.json"
SOURCE = "test.sbom.file"

MEMBER_ROW = boundary_contract("test.member-row", {"purl": "string"})
SBOM_ROW = boundary_contract(
    "test.sbom-row", {"name": "string{max_len: 256}?", "inventory": "string{max_len: 256}"}
)
DERIVE_REQUEST = boundary_contract("test.sbom-derive.request", {"sbom": "capture<*>"})


def _derive_build(checkout: Path) -> Any:
    """A core test-double provider: one pure derive over the SBOM file."""

    from cruxible_client.contracts.providers import (
        ProviderDistributionRef,
        ProviderImplementationManifest,
        ProviderLocalDistributionPin,
        ProviderLocalEnvBackendPin,
        ProviderRuntimeManifest,
    )
    from tests.support.declared_operations import declared_operation, registration_document

    served = (
        declared_operation(
            {
                "name": "sbom_components",
                "serves": ["test.sbom-derive"],
                "in": "test.sbom-derive.request@1",
                "out": "web.fetch.result@1",
                "effect": "pure",
                "refusals": {},
                "entrypoint": "demo.runtime:Sbom",
            }
        ),
    )
    manifest = ProviderRuntimeManifest(
        provider_id=PROVIDER,
        distribution=ProviderDistributionRef(name=PROVIDER, version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Sbom", backends=("local_env",), deterministic=True
            ),
        ),
    )
    document = registration_document(manifest, served, (DERIVE_REQUEST, *_web_contracts(checkout)))
    return document.provider_definition(
        distribution=ProviderLocalDistributionPin(
            name=PROVIDER,
            version="1.0.0",
            filename="test_sbom-1.0.0-py3-none-any.whl",
            sha256="sha256:" + "e" * 64,
        ),
        local_env=ProviderLocalEnvBackendPin(
            lock_sha256="sha256:" + "b" * 64,
            materialization_digests={"linux-cp311": "sha256:" + "c" * 64},
        ),
        control_domain=PROVIDER,
        operations=document.pinned_operations(),
    )


#: The purl key every SBOM record set reads, typed as the component's key part.
_PURL_KEY = {"purl": {"at": "purl", "type": f"key_part<{COMPONENT}:purl>"}}
#: The fields of an SBOM row (``name`` is optional: a members list carries none).
_SBOM_FIELDS = {"name": {"at": "name"}, "inventory": {"at": "inventory"}}
#: The SBOM file kind: a members list (the seed and glob inventory files).
SBOM_RECORDS = {
    "members": record_set(
        at="/members/*", row=SBOM_ROW.ref.spelling, key=_PURL_KEY, fields=_SBOM_FIELDS
    )
}
#: The derived components kind: one record per component, counted, and its inventory.
COMPONENTS_RECORDS = {
    "components": record_set(
        at="/components/*",
        row=SBOM_ROW.ref.spelling,
        key=_PURL_KEY,
        fields=_SBOM_FIELDS,
        count_at="/count",
    )
}
COMPONENTS_HEADER = {"inventory": {"at": "/inventory", "type": "string{max_len: 256}"}}


def _kind(name: str, *, grade: str = "observed") -> Any:
    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )

    kind = lower_capture_contract_input(
        CaptureContractInputRecord(
            name=name,
            shape="http_response" if grade == "derived" else "database_snapshot",
            grade=grade,
            replay="attested" if grade == "derived" else "exact",
            max_bytes=1_048_576,
            decoding=JSON_DECODING,
        ),
        named=True,
    )
    if grade == "derived":
        return with_layout(kind, COMPONENTS_RECORDS, COMPONENTS_HEADER)
    return with_layout(kind, SBOM_RECORDS)


def sbom_world(tmp_path: Path, *, purl_type: str = "string") -> tuple[Any, Path]:
    """Components keyed by purl, inventories by id, and the SBOM derive provider."""

    instance = skeleton_world(tmp_path)

    def membership_type(predicate: str, value: str) -> Any:
        return _claim_type(
            predicate,
            value,
            allowed_subject_kinds=[COMPONENT],
            cardinality="many",
            resolution_policy=ClaimResolutionPolicy(
                cardinality="many", eligible_verdicts=("supported",), selector="all"
            ),
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
            **contract_files(MEMBER_ROW, SBOM_ROW, DERIVE_REQUEST),
            **{
                capture_contract_path(kind.identity.name): render_capture_contract(kind)
                for kind in (_kind(SBOM_KIND), _kind(COMPONENTS_KIND, grade="derived"))
            },
            subject_kind_path(COMPONENT): render_subject_kind(
                keyed_subject_kind(COMPONENT, (PURL,))
            ),
            subject_kind_path(INVENTORY): render_subject_kind(
                keyed_subject_kind(INVENTORY, (INVENTORY_ID,))
            ),
            **{
                claim_type_path(t.predicate): render_claim_type(t)
                for t in (
                    membership_type(IN_INVENTORY, f"subject<{INVENTORY}>"),
                    membership_type(LISTED_IN, "string{max_len: 256}"),
                    _claim_type(
                        PURL,
                        purl_type,
                        allowed_subject_kinds=[COMPONENT],
                        evidence_requirement="none",
                        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(rules=()),
                    ),
                    _key_type(INVENTORY_ID, INVENTORY),
                )
            },
        },
        name="sbom-vocabulary",
    )
    ensure_capabilities(
        instance,
        ("test.sbom-derive", "test.sbom-derive.request@1", "web.fetch.result@1", "pure"),
        name="sbom-capabilities",
    )
    accept_tree(instance, build_files(_derive_build(require_provider_checkout())), name="sbom")
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": MEMBERS,
                    "description": "One inventory's held components.",
                    "params": {"inventory": "string"},
                    "from": {"i": f"{INVENTORY} = $inventory"},
                    "hops": [{"c": f"<i.{IN_INVENTORY}!: {COMPONENT}"}],
                    "row": MEMBER_ROW.ref.spelling,
                    "fields": [{"purl": f"c.{PURL}"}],
                    "limit": 2000,
                }
            ),
        ),
        name="members-query",
    )
    author(
        instance,
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=QuerySpecInput.model_validate(
                {
                    "query": LISTED_MEMBERS,
                    "description": "One inventory's held components, by its id.",
                    "params": {"inventory": "string"},
                    "from": {"c": COMPONENT},
                    "where": f"c.{LISTED_IN} == $inventory",
                    "row": MEMBER_ROW.ref.spelling,
                    "fields": [{"purl": f"c.{PURL}"}],
                    "limit": 2000,
                }
            ),
        ),
        name="listed-members-query",
    )
    root = tmp_path / "workspace"
    (root / "sboms").mkdir(parents=True)
    return instance, root


def write_sbom(root: Path, inventory: str, purls: Sequence[object]) -> None:
    """The SBOM; a member that is not a purl string is written as it is (a bad record)."""

    document = {
        "metadata": {"component": {"purl": inventory}},
        "components": [
            {"purl": purl, "name": purl.rsplit("/", 1)[-1]}
            if isinstance(purl, str) and purl.startswith("pkg:")
            else purl
            for purl in purls
        ],
    }
    (root / SBOM_PATH).write_text(json.dumps(document, sort_keys=True))


def components(sbom: bytes) -> bytes:
    """The derive: the SBOM's inventory, and one record per component naming it."""

    document = json.loads(sbom)
    inventory = document["metadata"]["component"]["purl"]
    listed = [
        {"purl": item["purl"], "name": item.get("name"), "inventory": inventory}
        if isinstance(item, dict)
        else item  # a provider passes on what it cannot read; extract refuses it
        for item in document["components"]
    ]
    derived = {"inventory": inventory, "components": listed, "count": len(listed)}
    return json.dumps(derived, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Sbom:
    """Answers the derive over the file the workspace read served."""

    def __init__(self) -> None:
        self.calls = 0

    def invoker(self) -> Invoker:
        import base64

        def output(context: Any) -> dict[str, Any]:
            self.calls += 1
            sbom = base64.b64decode(context.input["sbom"]["content_base64"])
            wire = _wire(components(sbom), "https://derive.test/sbom", source="test.sbom-view")
            # A Capture is a named field of the result: web.fetch.result's ``capture``.
            return {"capture": {**wire, "tag": "playbill-provider-result-to-external-capture-v1"}}

        return Invoker(output=output)


class _BuiltinOperator(Operator):
    """The test Operator, with core's built-in workspace.file served in process."""

    def invoker_for(self, instance, *, accepted_oid, pins=()):  # type: ignore[no-untyped-def]
        from cruxible_core.providers.builtin_runtime import BuiltinDispatchingInvoker

        del instance, accepted_oid, pins
        return BuiltinDispatchingInvoker(self.invoker)

    def admit_line_provider(  # type: ignore[no-untyped-def]
        self, accepted_provider, bound, implementation_digest, *, eligible_environment_pin_keys
    ):
        from cruxible_core.providers.builtin_runtime import builtin_provider_binding

        builtin = builtin_provider_binding(accepted_provider, bound, implementation_digest)
        if builtin is not None:
            return builtin
        return super().admit_line_provider(
            accepted_provider,
            bound,
            implementation_digest,
            eligible_environment_pin_keys=eligible_environment_pin_keys,
        )


def membership(shape: str) -> dict[str, Any]:
    """The membership Claim a component's SBOM record writes, by shape."""

    if shape == "literal":
        return {"type": LISTED_IN, "value": "$item.fields.inventory"}
    return {
        "type": IN_INVENTORY,
        "value": {
            "kind": INVENTORY,
            "key": {"inventory_id": "$item.fields.inventory"},
            "create": "if_absent",
        },
    }


def sbom_spec(*, read_rows: int = 500, shape: str = "subject") -> Any:
    """fetch -> derive -> extract -> read (late: keyed by the derive) -> claims."""

    return build_spec(
        "test.sbom-import",
        reference_text=True,
        input={},
        output={},
        slots={
            "read": {"capability": "workspace.file@1"},
            "derive": {"capability": "test.sbom-derive@1"},
        },
        hard_caps=procedure_limits(provider_calls=2, capture_bytes=2_097_152, read_rows=read_rows),
        steps=[
            {
                "call": "sbom",
                "slot": "read",
                "captures": {"capture": SBOM_KIND},
                "input": {
                    "logical_source": SOURCE,
                    "relative_path": SBOM_PATH,
                    "coordinate_type": "database-snapshot-v1",
                    "coordinate": {"lsn": "workspace"},
                    "selector_type": "relation-primary-key-v1",
                    "selector": {"id": 1, "relation": "sbom"},
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
            # The late read: keyed by the inventory only the derive knows.
            {
                "read": "held",
                "query": LISTED_MEMBERS if shape == "literal" else MEMBERS,
                "params": {"inventory": "$steps.listed.header.inventory.value"},
            },
            {
                "id": "write",
                "for_each": "$steps.listed.records",
                "subject": {
                    "kind": COMPONENT,
                    "key": {"purl": "$item.key.purl"},
                    "create": "if_absent",
                },
                "expect": "$steps.held.rows",
                "claims": [
                    {
                        **membership(shape),
                        "when_absent": "mirror",
                        "rationale": "The SBOM lists the component in its inventory.",
                    }
                ],
            },
        ],
    )


def run(instance: Any, root: Path, source: Sbom, *, minute: int = 0, line: str = LINE) -> Any:
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.test_procedures.test_completeness_transfer import _Clock

    return service_run_playbill_line(
        instance,
        path_identity_digest=line,
        request=LineRunRequest(line=line),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW + timedelta(minutes=minute)),
        provider_runtime_operator=_BuiltinOperator(source.invoker()),  # type: ignore[arg-type]
        workspace_file_reader=WorkspaceFileReader(
            instance_id=instance.descriptor.instance_id,
            operating_profile="local",
            attached_roots=(root,),
            managed_roots=(instance.root,),
        ),
    )


def memberships(instance: Any) -> dict[str, set[str]]:
    """inventory id -> the purls its live memberships name."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    keys: dict[str, str] = {}
    for path in tree:
        if path.startswith("claims/"):
            claim = parse_claim(tree[path], path=path)
            if claim.statement.predicate in {PURL, INVENTORY_ID} and (
                claim.lifecycle.state == "live"
            ):
                keys[claim.statement.subject.artifact_path] = str(claim.statement.object.value)
    found: dict[str, set[str]] = {}
    for path in tree:
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(tree[path], path=path)
        if claim.statement.predicate != IN_INVENTORY or claim.lifecycle.state != "live":
            continue
        inventory = keys[claim.statement.object.address.artifact_path]
        found.setdefault(inventory, set()).add(keys[claim.statement.subject.artifact_path])
    return found


def _changes(state: Any) -> list[str]:
    (egress,) = state.terminal_egress
    return sorted(child.change_kind for child in egress.children if child.change_kind)


def _late_reads(instance: Any, state: Any) -> list[dict[str, Any]]:
    assert state.receipt is not None
    return list(state.receipt.late_reads)


APP = "pkg:generic/app@1"
A, B, C = "pkg:pypi/a@1", "pkg:pypi/b@1", "pkg:pypi/c@1"


def test_an_sbom_import_reads_its_own_inventory_late_and_mirrors_by_it(tmp_path: Path) -> None:
    instance, root = sbom_world(tmp_path)
    install(instance, sbom_spec(), line=LINE)
    write_sbom(root, APP, [A, B])
    source = Sbom()

    first = run(instance, root, source)
    assert first.status == "succeeded", json.dumps(first.terminal.model_dump(mode="json"))
    assert source.calls == 1
    # The inventory and both components are created, each membership written.
    assert _changes(first) == ["claim", "claim", "subject", "subject", "subject"]
    claims = [c for c in proposed(instance, first) if c.statement.predicate == IN_INVENTORY]
    assert len(claims) == 2
    # The late read is listed apart from the admitted inputs, at the run's coordinate.
    (late,) = _late_reads(instance, first)
    assert late["input_name"] == "held"
    assert late["read_coordinate"] == first.receipt.bound_coordinate.model_dump(mode="json")
    assert "held" not in {item["input_name"] for item in first.receipt.admitted_inputs}
    activate(instance, first)
    assert memberships(instance) == {APP: {A, B}}

    # B leaves the SBOM, C joins: B's membership retires against the late read.
    write_sbom(root, APP, [A, C])
    second = run(instance, root, source, minute=1)
    assert second.status == "succeeded", json.dumps(second.terminal.model_dump(mode="json"))
    assert _changes(second) == ["claim", "reobservation", "retirement", "subject"]
    activate(instance, second)
    assert memberships(instance) == {APP: {A, C}}

    # The same SBOM again changes nothing.
    third = run(instance, root, source, minute=2)
    assert third.status == "succeeded", third.terminal
    assert third.terminal_egress[0].proposal_id is None


def test_absence_never_crosses_into_an_inventory_the_late_read_did_not_select(
    tmp_path: Path,
) -> None:
    instance, root = sbom_world(tmp_path)
    install(instance, sbom_spec(), line=LINE)
    source = Sbom()
    write_sbom(root, "pkg:generic/other@1", [A, B])
    other = run(instance, root, source)
    activate(instance, other)
    write_sbom(root, APP, [C])
    mine = run(instance, root, source, minute=1)
    assert mine.status == "succeeded", mine.terminal
    activate(instance, mine)
    assert memberships(instance) == {"pkg:generic/other@1": {A, B}, APP: {C}}


def test_a_late_read_over_the_read_ceiling_refuses_the_run(tmp_path: Path) -> None:
    instance, root = sbom_world(tmp_path)
    install(instance, sbom_spec(read_rows=2), line=LINE)
    source = Sbom()
    write_sbom(root, APP, [A, B, C])
    first = run(instance, root, source)
    activate(instance, first)

    second = run(instance, root, source, minute=1)
    assert second.status == "node_refused", second.status
    terminal = second.terminal.model_dump(mode="json")
    assert terminal["code"] == "read_ceiling_exceeded", terminal
    assert terminal.get("node_id") == "held", terminal
    assert memberships(instance) == {APP: {A, B, C}}  # nothing retired


# -- work counts: the late read is O(this SBOM), whatever else the instance holds --


SEED_LINE = "test.seed-line"
SEED_PATH = "sboms/seed.json"


def seed_spec(shape: str) -> Any:
    """One import of many inventories' memberships at once: the unrelated state."""

    return build_spec(
        "test.seed-memberships",
        reference_text=True,
        input={},
        output={},
        slots={"read": {"capability": "workspace.file@1"}},
        hard_caps=procedure_limits(provider_calls=1, capture_bytes=2_097_152),
        steps=[
            {
                "call": "seed",
                "slot": "read",
                "captures": {"capture": SBOM_KIND},
                "input": {
                    "logical_source": "test.seed.file",
                    "relative_path": SEED_PATH,
                    "coordinate_type": "database-snapshot-v1",
                    "coordinate": {"lsn": "workspace"},
                    "selector_type": "relation-primary-key-v1",
                    "selector": {"id": 2, "relation": "seed"},
                },
            },
            {"extract": "members", "capture": "$steps.seed.capture", "records": "members"},
            {
                "id": "write",
                "for_each": "$steps.members.records",
                "subject": {
                    "kind": COMPONENT,
                    "key": {"purl": "$item.key.purl"},
                    "create": "if_absent",
                },
                "claims": [{**membership(shape), "rationale": "Seeded."}],
            },
        ],
    )


def _seed_unrelated(instance: Any, root: Path, inventories: int, *, per: int = 3) -> None:
    """``inventories`` unrelated inventories with ``per`` held memberships each."""

    members = [
        {"purl": f"pkg:pypi/u{index}-{member}@1", "inventory": f"pkg:generic/unrelated-{index}@1"}
        for index in range(inventories)
        for member in range(per)
    ]
    (root / SEED_PATH).write_text(json.dumps({"members": members}, sort_keys=True))
    seeded = run(instance, root, Sbom(), line=SEED_LINE, minute=1)
    assert seeded.status == "succeeded", seeded.terminal
    activate(instance, seeded)


def _counted(
    tmp_path: Path, unrelated: int, shape: str, monkeypatch: pytest.MonkeyPatch
) -> dict[str, int]:
    """One measured import of the fixed SBOM beside ``unrelated`` other inventories.

    ``query_sql`` is the SQL the late read's Query evaluation ran (the Query
    engine's own cost); ``run_sql`` is every other statement of the run.
    """

    from cruxible_core.service.procedures.procedures import PlaybillProcedureStateTapReader
    from tests.test_scale._counters import counting, trace_sqlite

    held: dict[str, Any] = {"window": None, "query_sql": 0}
    original = PlaybillProcedureStateTapReader.read_accepted_state

    def measured(self: Any, **kwargs: Any) -> Any:
        window = held["window"]
        before = 0 if window is None else window.counts["sql_statements"]
        try:
            return original(self, **kwargs)
        finally:
            if window is not None:
                held["query_sql"] += window.counts["sql_statements"] - before

    monkeypatch.setattr(PlaybillProcedureStateTapReader, "read_accepted_state", measured)
    with trace_sqlite(tmp_path):
        instance, root = sbom_world(tmp_path)
        install(instance, sbom_spec(shape=shape), line=LINE)
        install(instance, seed_spec(shape), line=SEED_LINE)
        source = Sbom()
        write_sbom(root, APP, [A, B, C])
        activate(instance, run(instance, root, source))
        _seed_unrelated(instance, root, unrelated)
        # The projection catches up on the seeding once, lazily: before the window.
        with instance.bind_accepted_projection(instance.accepted_coordinate()):
            pass
        with counting(tmp_path) as window:
            held["window"] = window
            state = run(instance, root, source, minute=2)
        held["window"] = None
    assert state.status == "succeeded", state.terminal
    assert not window.unexplained, window.unexplained
    return {
        "read_rows": state.receipt.budget.observed.read_rows,
        "query_sql": held["query_sql"],
        "run_sql": window.counts["sql_statements"] - held["query_sql"],
        "projection_binds": window.counts["projection_binds"],
    }


_UNRELATED = (10, 40)


@pytest.mark.parametrize("shape", ["literal", "subject"])
def test_a_fixed_sbom_imports_in_constant_work_beside_growing_unrelated_inventories(
    tmp_path: Path, shape: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The late read returns this SBOM's memberships only, at any instance size.

    Its rows are constant for both membership shapes, and so is every statement
    of the run outside the membership Query's own evaluation: reading late adds
    no per-instance work. The Query's evaluation is the Query engine's cost
    (reported, not asserted): today it loads every held Claim of the Query's
    predicates for a reverse hop or a Claim-value filter.
    """

    counted = {}
    for size in _UNRELATED:
        (tmp_path / f"n{size}").mkdir()
        counted[size] = _counted(tmp_path / f"n{size}", size, shape, monkeypatch)
    small, large = (counted[size] for size in _UNRELATED)
    assert small["read_rows"] == large["read_rows"] == 3, counted
    assert large["run_sql"] == small["run_sql"], counted
    assert large["projection_binds"] == small["projection_binds"], counted
    report = __import__("os").environ.get("LATE_READ_COUNTS")
    if report:
        with open(report, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"shape": shape, "measured": counted}) + "\n")
