"""SDK query handles bind schema and state; results reuse the engine's types."""

from types import SimpleNamespace

import pytest

from cruxible_client.authoring.queries import QueryBinding, QueryParameters
from cruxible_client.authoring.sdk import Playbill
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_client.contracts.query.grammar import QueryParameterDeclarationV1
from tests.support.scoped_query_oracle import _scoped_facts_answer_as_whole_facts  # noqa: F401
from tests.test_client.test_playbill_sdk import _Client, _workspace
from tests.test_query.test_query_definitions import active_work_query


def test_parameters_use_query_types_and_reject_unknown_or_missing_fields():
    query = active_work_query(
        parameters=(QueryParameterDeclarationV1(name="status", value_type="string"),)
    )
    constructor = QueryParameters(query)
    assert constructor(status="ready").status == "ready"
    for fields in ({}, {"status": False}, {"status": "ready", "typo": True}):
        with pytest.raises(ValueError):
            constructor(**fields)


@pytest.mark.parametrize(
    "kind,valid,invalid",
    [
        ("integer", 3, True),
        ("boolean", False, 0),
        ("decimal", "12.50", []),
        ("timestamp", "2026-09-21T12:00:00Z", "2026-09-21"),
        ("subject_reference", "Subject:security.asset/app", 1),
    ],
)
def test_parameters_reuse_the_evaluator_type_rules(kind, valid, invalid):
    query = active_work_query(
        parameters=(QueryParameterDeclarationV1(name="status", value_type=kind),)
    )
    constructor = QueryParameters(query)
    assert constructor(status=valid)["status"] == valid
    with pytest.raises(ValueError):
        constructor(status=invalid)


def test_sdk_resolves_once_and_runs_at_the_bound_coordinate(tmp_path):
    _workspace(tmp_path)
    client = _Client()
    pb = Playbill._from_client(client, instance_id="inst_test", workspace=tmp_path)
    coordinate = pb.coordinate
    definition = active_work_query()
    calls = []

    def get(instance_id, name, *, at):
        calls.append(("get", name, at))
        return SimpleNamespace(
            name=definition.identity.name,
            coordinate=coordinate,
            envelope=definition.model_dump(mode="json"),
            artifact_digest=query_definition_digest(definition).tagged,
        )

    def run(instance_id, name, **kwargs):
        calls.append(("run", name, kwargs))
        return "typed-wire-response"

    client.get_playbill_query_definition = get
    client.run_playbill_query = run
    binding = pb.query_binding(definition.identity.name)
    assert isinstance(binding, QueryBinding)
    assert (
        pb.run_query(binding, parameters=binding.parameters(status="ready"))
        == "typed-wire-response"
    )
    assert calls[-1][2]["at"].git_oid == coordinate.git_oid
    assert calls[-1][2]["parameters"] == {"status": "ready"}
    with pytest.raises(TypeError, match="binding.parameters"):
        pb.run_query(binding, parameters={"status": "ready"})
    with pytest.raises(ValueError, match="digest"):
        QueryBinding(binding.ref, definition, "sha256:" + "0" * 64)
