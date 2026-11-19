"""Served Source acquisition honors accepted eligibility rules (Line acquisition, B2)."""

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.acquisition_rules import (
    AcquisitionRule,
    LineAcquisitionRule,
    acquisition_rules_digest,
    resolve_acquisition_rules,
)
from cruxible_core.evidence.source_readers import (
    ExternalSourceReadRequestV1,
    ProducerBindingV1,
)
from cruxible_core.procedures.acquisition import (
    ProcedureSourceAcquisitionResultV1,
    apply_acquisition_result,
)
from tests.core_support._pc_c_support import (
    NOW,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)
from tests.support.external_sources import FakeVersionedExternalSourceReader
from tests.test_procedures import test_procedure_source_runs as source

# A test double for the unit-level `apply_acquisition_result` checks below: a
# real acquired Capture, not read through any Procedure run.


def _acquired(tmp_path: Path) -> ProcedureSourceAcquisitionResultV1:
    contract = capture_contract()
    adapter = provider(contract)
    binding = ProducerBindingV1(
        provider=adapter.identity,
        logical_source_identity="commerce.production.orders",
        adapter_digest=digest("adapter", "source-policy"),
    )
    reader = FakeVersionedExternalSourceReader()
    reader.seed(
        source_identity=binding.logical_source_identity,
        coordinate_type="postgres-lsn-v1",
        coordinate={"lsn": "1"},
        selector_type="relation-primary-key-v1",
        selector={"id": 7, "relation": "orders"},
        value={"order_id": 7},
    )
    result = reader.acquire(
        ExternalSourceReadRequestV1(
            contract=contract,
            provider=adapter,
            binding=binding,
            coordinate_type="postgres-lsn-v1",
            coordinate={"lsn": "1"},
            selector_type="relation-primary-key-v1",
            selector={"id": 7, "relation": "orders"},
            materialization="cas",
            run_coordinate=provider_run(adapter),
            observed_at=NOW,
            resource_budget=contract.selection_budget,
        ),
        store=body_store(tmp_path),
    )
    return ProcedureSourceAcquisitionResultV1(
        node_id="source", input_name="advisory", outcome="acquired", acquisition=result
    )


def test_capture_at_maximum_age_remains_eligible(tmp_path: Path) -> None:
    acquired = _acquired(tmp_path)
    rule = AcquisitionRule(
        input_name="advisory", declared_by="line", max_age=CanonicalDuration(microseconds=1_000_000)
    )
    decision = apply_acquisition_result(
        rule, acquired, default_authorized=False, evaluation_time=NOW + timedelta(seconds=1)
    )
    assert decision.disposition == "selected"
    assert acquired.acquisition is not None
    assert decision.selected_capture_digests == (acquired.acquisition.capture_digest,)


def test_a_capture_older_than_the_rule_refuses_by_default_and_omits_when_declared(
    tmp_path: Path,
) -> None:
    acquired = _acquired(tmp_path)
    refusing = AcquisitionRule(
        input_name="advisory", declared_by="line", max_age=CanonicalDuration(microseconds=1_000_000)
    )
    refused = apply_acquisition_result(
        refusing, acquired, evaluation_time=NOW + timedelta(seconds=2)
    )
    assert refused.disposition == "refused"
    assert refused.reason_codes == ("cruxible.acquisition.stale",)
    assert refused.selected_capture_digests == ()
    assert acquired.acquisition is not None
    assert refused.considered_capture_digests == (acquired.acquisition.capture_digest,)

    omitting = AcquisitionRule(
        input_name="advisory",
        declared_by="line",
        max_age=CanonicalDuration(microseconds=1_000_000),
        on_stale="omit",
    )
    omitted = apply_acquisition_result(
        omitting, acquired, evaluation_time=NOW + timedelta(seconds=2)
    )
    assert omitted.disposition == "omitted"
    assert omitted.reason_codes == ("cruxible.acquisition.stale",)


@pytest.mark.parametrize("outcome", ["unavailable", "stale", "oversized", "refused"])
def test_current_policy_refuses_every_failed_fetch(outcome: str) -> None:
    rule = resolve_acquisition_rules(("advisory",), line_rules={"advisory": source._policy()})[
        "advisory"
    ]
    result = ProcedureSourceAcquisitionResultV1.model_validate(
        {
            "node_id": "source",
            "input_name": "advisory",
            "outcome": outcome,
            "reason_code": "cruxible.acquisition." + outcome,
        }
    )
    assert apply_acquisition_result(rule, result, evaluation_time=NOW).disposition == "refused"


def test_selection_does_not_restate_contract_replayability(tmp_path: Path) -> None:
    acquired = _acquired(tmp_path)
    decision = apply_acquisition_result(
        AcquisitionRule(input_name="advisory", declared_by="line"), acquired, evaluation_time=NOW
    )
    assert decision.disposition == "selected"


# NOTE: the former `test_stale_input_refusal_names_alias_age_and_repair` exercised a
# full end-to-end stale refusal by reading a retained Capture back through a
# second provider build, with the node naming `provider`/`interface` fields
# directly (removed: a call node now only names a `slot`, bound through a
# Line). B2 changes only binding and acquisition lookup for Source/Call; §11.2
# explicitly defers the Source/Call node rework (including this
# retained-observation read pattern) to B3. The unit-level stale/omit behavior
# this test exercised is covered directly above
# (`test_a_capture_older_than_the_rule_refuses_by_default_and_omits_when_declared`);
# a full-run regression for the retained-observation path belongs with B3's
# Source/Call merge.


def test_line_without_named_policy_runs_with_the_core_default(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_path,
        render_line_spec,
    )

    instance, owner, procedure, root, policy = source._world(tmp_path)
    line = source._served_line(instance, procedure, policy)
    # No rule for "advisory": the Line runs it under the stated core default.
    line = line.model_copy(update={"acquisition": {}})
    source._accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(line)},
        name="core-default-line",
    )
    state, invoker = source._run_line(instance, root, line)
    assert state.status == "succeeded", state.terminal
    assert invoker.spawn_calls == 1
    rules = resolve_acquisition_rules((source.SOURCE_CAPTURE,), line_rules={})
    assert rules[source.SOURCE_CAPTURE].declared_by == "core_default"
    expected = acquisition_rules_digest(rules)
    assert source._admission(instance, state).acquisition_policy_digest == expected


def test_a_direct_runs_rule_is_its_request_and_a_lines_is_its_own(tmp_path: Path) -> None:
    from cruxible_client.contracts.procedures.line_specs import line_spec_path, render_line_spec

    instance, owner, procedure, root, default = source._world(tmp_path)
    custom = LineAcquisitionRule(max_age=CanonicalDuration(microseconds=7_200_000_000))
    # A Procedure declares no acquisition rule: the direct run's ephemeral Line
    # spec carries the request's, and the Line its own ("default").
    line = source._served_line(instance, procedure, default)
    source._accept_more(
        instance,
        owner,
        {line_spec_path(line.identity.name): render_line_spec(line)},
        name="default-line",
    )
    direct, _ = source._run(instance, root, acquisition={source.SOURCE_CAPTURE: custom})
    served, _ = source._run_line(instance, root, line)
    assert direct.status == served.status == "succeeded"
    assert source._admission(
        instance, direct
    ).acquisition_policy_digest == acquisition_rules_digest(
        resolve_acquisition_rules(
            (source.SOURCE_CAPTURE,), line_rules={source.SOURCE_CAPTURE: custom}
        )
    )
    assert source._admission(
        instance, served
    ).acquisition_policy_digest == acquisition_rules_digest(
        resolve_acquisition_rules(
            (source.SOURCE_CAPTURE,), line_rules={source.SOURCE_CAPTURE: default}
        )
    )
