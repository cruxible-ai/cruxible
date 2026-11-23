"""An observation settle grant across a pure, non-widening provider build move keeps settle.

Compiler revision 74 (maintainer ruling 3): ``provider move`` carries a settle
Mandate whole when its Line changes only slot bindings, every moved slot's new
operation is no wider than the old (capability, effect, endpoints,
``may_truncate``), every scoped ClaimType is observation-only, the grant
creates no Subjects and no live condition query reads its scope. Anything else
demotes the carried grant to propose and says why. An enabled Line whose grant
kept settle stays enabled on its successor (ruling 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.claim_types import (
    claim_type_digest,
    claim_type_path,
    parse_claim_type,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.procedure_mandates import (
    MandateClaimScope,
    MandateCondition,
    parse_procedure_mandate,
    procedure_mandate_digest,
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from cruxible_client.contracts.provider_installation import ProviderMoveRequest
from cruxible_client.contracts.providers import ProviderV2
from cruxible_client.contracts.query.definitions import (
    query_definition_digest,
    query_definition_path,
    render_query_definition,
)
from cruxible_client.contracts.query.grammar import QueryClaimValueRef, QueryProjectionField
from cruxible_core.providers.observation_carry import build_move_refusal
from cruxible_core.service.procedures.provider_builds import service_move_provider
from tests.core_support._identity_support import accept_tree
from tests.support.mandates import procedure_mandate, scope_changes
from tests.support.operation_world import operation_build
from tests.support.procedures import procedure_spec
from tests.test_procedures.test_b2_operations import install as install_builds
from tests.test_procedures.test_claims_fix import run_measured
from tests.test_procedures.test_claims_terminal import (
    KIND,
    claim_type,
    claims_spec,
    claims_world,
    install,
)
from tests.test_procedures.test_procedure_settlement_delivery import _condition

pytestmark = pytest.mark.executor_only

LINE = "test.claims-line"
GRANT = "test.claims-line-settle"
MOVED_AT = "2026-10-29T11:30:00.000000Z"


def _score(observation_only: bool) -> Any:
    roles = ("observation",) if observation_only else ("derivation", "normative", "observation")
    return claim_type(
        KIND + ".score",
        "int",
        value_type={"type": "integer"},
        permitted_roles=list(roles),
        default_role="observation",
        evidence_admission_policy=ClaimEvidenceAdmissionPolicy(
            rules=(
                ClaimEvidenceAdmissionRule(
                    rule_id="fixture",
                    claim_roles=roles,
                    capture_contracts=("test.orders-v1",),
                    admission="direct",
                    subject_binding="exact_claim_subject",
                ),
            )
        ),
    )


def _world(tmp_path: Path, *, observation_only: bool = True) -> tuple[Any, Path, Any]:
    """The measured settle Line (one provider-computed value), its ClaimType chosen."""

    score = _score(observation_only)
    instance, root = claims_world(
        tmp_path, {"entries": [{"id": "CVE-2026-0001", "status": "open"}]}, types=(score,)
    )
    base = claims_spec().model_dump(mode="python", by_alias=True)
    base["slots"]["compute"] = {"capability": "test.compute@1"}
    base["hard_caps"]["provider_calls"] = 2
    base["steps"].insert(-1, {"call": "computed", "slot": "compute", "input": {"value": 1}})
    base["steps"].insert(
        -1,
        {
            "project": "rows",
            "fields": {
                "rows": [
                    {
                        "key": "CVE-2026-0001",
                        "value": "$steps.computed.value",
                        "observation": "$steps.entries.header",
                    }
                ]
            },
        },
    )
    base["steps"][-1].update(
        for_each="$steps.rows.rows",
        subject={"kind": KIND, "key": {"cve_id": "$item.key"}},
        claims=[{"type": score.predicate, "value": "$item.value"}],
    )
    install(instance, procedure_spec(base=base, reference_text=True), authority="settle")
    return instance, root, score


def _grant(instance: Any, predicate: str, *, condition_reads_scope: bool = False) -> str:
    """A settle grant over one ClaimType, its condition reading the scope or not."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    line = parse_line_spec(tree[line_spec_path(LINE)], path=line_spec_path(LINE))
    ct = parse_claim_type(tree[claim_type_path(predicate)], path=claim_type_path(predicate))
    query = _condition(only_subject=None)
    query = query.model_copy(
        update={"entry": query.entry.model_copy(update={"subject_kinds": (KIND,)})}
    )
    if condition_reads_scope:
        query = query.model_copy(
            update={
                "projection": query.projection.model_copy(
                    update={
                        "fields": (
                            *query.projection.fields,
                            QueryProjectionField(
                                name="score",
                                value=QueryClaimValueRef(binding="advisory", predicate=predicate),
                            ),
                        )
                    }
                )
            }
        )
    mandate = procedure_mandate(
        GRANT,
        grants="settle",
        namespace=("claims",),
        line=ArtifactPin(
            role="line", target=line.identity, artifact_digest=line_spec_digest(line).tagged
        ),
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=ct.identity,
                    artifact_digest=claim_type_digest(ct).tagged,
                ),
                changes=scope_changes("create", "revise", basis=("measured",)),
            ),
        ),
        condition=MandateCondition(
            query=ArtifactPin(
                role="condition-query",
                target=query.identity,
                artifact_digest=query_definition_digest(query).tagged,
            ),
            binding_parameter="advisory_id",
            required_fields=("id",),
            fallback="propose",
        ),
    )
    accept_tree(
        instance,
        {
            query_definition_path(query.identity.name): render_query_definition(query),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="settle-grant",
    )
    return procedure_mandate_digest(mandate).tagged


def _accepted(instance: Any, path: str) -> bytes:
    return instance.tree_at(instance.accepted_coordinate().git_oid)[path]


def _move(instance: Any, build: Any) -> Any:
    install_builds(instance, build)
    moved = service_move_provider(
        instance,
        ProviderMoveRequest(provider="demo-provider", to=build.identity.name, dry_run=False),
        actor_id="owner",
        timestamp=MOVED_AT,
    )
    assert moved.status == "accepted", moved.detail
    return moved


def _carried(moved: Any) -> Any:
    """The settle grant's carry row (the Line's propose grant carries beside it)."""

    rows = {item.identity: item for item in moved.automation_carries}
    return rows[f"ProcedureMandate:{GRANT}"]


def test_an_observation_settle_grant_keeps_settle_across_a_pure_build_move(
    tmp_path: Path,
) -> None:
    instance, root, score = _world(tmp_path)
    _grant(instance, score.predicate)
    first = run_measured(instance, root, value=1)
    assert first.terminal_egress[0].settle_outcome == "settled"
    old_line = line_spec_digest(
        parse_line_spec(_accepted(instance, line_spec_path(LINE)), path=line_spec_path(LINE))
    ).tagged

    moved = _move(instance, operation_build(build="d"))
    carried = _carried(moved)
    assert carried.kind == "mandate"
    assert (carried.old_grant, carried.new_grant) == ("settle", "settle")
    assert carried.retained_scope == (score.predicate,)
    assert carried.carries_line_enablement and not carried.loses_settle
    assert carried.demotion is None

    path = procedure_mandate_path(GRANT)
    mandate = parse_procedure_mandate(_accepted(instance, path), path=path)
    line = parse_line_spec(_accepted(instance, line_spec_path(LINE)), path=line_spec_path(LINE))
    assert mandate.grants == "settle"
    assert mandate.line.artifact_digest == line_spec_digest(line).tagged
    assert line.lifecycle.predecessor_digest == old_line
    # The successor Line keeps settling under the carried grant.
    again = run_measured(instance, root, minute=2, value=2)
    assert again.status == "succeeded", again.terminal
    assert again.terminal_egress[0].settle_outcome == "settled"
    assert (
        again.terminal_egress[0].procedure_mandate_digest
        == procedure_mandate_digest(mandate).tagged
    )


@pytest.mark.parametrize(
    ("case", "demotion"),
    [
        ("judgment", "judgment_claim_type"),
        ("coupled", "condition_coupling"),
    ],
)
def test_a_grant_the_ruling_does_not_cover_demotes_and_says_why(
    tmp_path: Path, case: str, demotion: str
) -> None:
    instance, root, score = _world(tmp_path, observation_only=case != "judgment")
    _grant(instance, score.predicate, condition_reads_scope=case == "coupled")
    moved = _move(instance, operation_build(build="d"))
    carried = _carried(moved)
    assert (carried.old_grant, carried.new_grant) == ("settle", "propose")
    assert carried.loses_settle and carried.demotion == demotion
    assert not carried.carries_line_enablement
    path = procedure_mandate_path(GRANT)
    assert parse_procedure_mandate(_accepted(instance, path), path=path).grants == "propose"


def test_a_revision_73_instance_demotes_every_carried_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.compiler.compiler import COMPILER_REVISION_73

    with monkeypatch.context() as patch:
        patch.setattr(
            "cruxible_core.runtime.instance.current_compiler_coordinate",
            lambda: COMPILER_REVISION_73,
        )
        instance, _root, score = _world(tmp_path)
    assert instance.accepted_coordinate().compiler == COMPILER_REVISION_73
    _grant(instance, score.predicate)
    moved = _move(instance, operation_build(build="d"))
    carried = _carried(moved)
    assert carried.new_grant == "propose" and carried.demotion == "compiler_revision"


def test_the_law_refuses_a_settle_grant_carried_over_a_line_that_changed_more(
    tmp_path: Path,
) -> None:
    """A hand-built carry that keeps settle while the Line's budgets also change refuses."""

    from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest

    instance, _root, score = _world(tmp_path)
    _grant(instance, score.predicate)
    head = instance.accepted_coordinate()
    tree = dict(instance.tree_at(head.git_oid))
    line_path, mandate_path = line_spec_path(LINE), procedure_mandate_path(GRANT)
    line = parse_line_spec(tree[line_path], path=line_path)
    successor = line.model_copy(
        update={
            "budgets": line.budgets.model_copy(
                update={"max_provider_calls": line.budgets.max_provider_calls - 1}
            ),
            "lifecycle": line.lifecycle.model_copy(
                update={"predecessor_digest": line_spec_digest(line).tagged}
            ),
        }
    )
    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedures.line_specs import render_line_spec

    mandate = parse_procedure_mandate(tree[mandate_path], path=mandate_path)
    carried = mandate.model_copy(
        update={
            "line": mandate.line.model_copy(
                update={"artifact_digest": line_spec_digest(successor).tagged}
            ),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(mandate).tagged
            ),
        }
    )
    tree[line_path] = render_line_spec(successor)
    tree[mandate_path] = render_procedure_mandate(carried)
    result = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/carry-settle", proposed_base_oid=head.git_oid
        ),
        candidate_tree=tree,
        timestamp=MOVED_AT,
    )
    assert result.candidate is None
    codes = {item.code for item in result.evaluation.diagnostics}
    assert "cruxible.procedure_mandate.carried_grant_must_propose" in codes
    assert any("line changed" in item.message for item in result.evaluation.diagnostics)


# --- per-slot non-widening, judged directly --------------------------------------


def _lines(old_build: ProviderV2, new_build: ProviderV2) -> tuple[Any, Any, dict, dict]:
    from cruxible_client.contracts.provider_operations import LineBinding
    from cruxible_client.contracts.providers import provider_digest, provider_path, render_provider
    from tests.support.lines import line_spec

    def pin(build: ProviderV2) -> ArtifactPin:
        return ArtifactPin(
            role="provider", target=build.identity, artifact_digest=provider_digest(build).tagged
        )

    old = line_spec().model_copy(
        update={"bindings": {"compute": LineBinding(build=pin(old_build), operation="compute")}}
    )
    new = old.model_copy(
        update={"bindings": {"compute": LineBinding(build=pin(new_build), operation="compute")}}
    )
    before = {provider_path(old_build.identity.name): render_provider(old_build)}
    after = {**before, provider_path(new_build.identity.name): render_provider(new_build)}
    return old, new, before, after


@pytest.mark.parametrize(
    ("change", "demotion"),
    [
        ("same", None),
        ("effect", "effect_widened"),
        ("endpoints", "endpoints_widened"),
        ("fewer_endpoints", None),
        ("may_truncate", "may_truncate_widened"),
        ("capability", "capability_changed"),
    ],
)
def test_a_moved_slot_must_not_widen(change: str, demotion: str | None) -> None:
    from tests.support.operation_world import operation

    endpoints: tuple[str, ...] = ("api.example.com",)
    served = operation()
    if change == "effect":
        served = operation(effect="external_read", serves=(operation().serves[0],))
    elif change == "endpoints":
        endpoints = ("api.example.com", "other.example.com")
    elif change == "fewer_endpoints":
        endpoints = ()
    elif change == "may_truncate":
        served = served.model_copy(update={"may_truncate": True})
    elif change == "capability":
        served = operation(serves=("test.other",))
    old = operation_build(build="a", endpoints=("api.example.com",))
    new = operation_build(build="d", operations=(served,), endpoints=endpoints)
    old_line, new_line, before, after = _lines(old, new)
    assert build_move_refusal(old_line, new_line, before=before, after=after) == demotion


def test_a_line_that_changes_more_than_its_bindings_is_not_a_build_move() -> None:
    old, new = operation_build(build="a"), operation_build(build="d")
    old_line, new_line, before, after = _lines(old, new)
    changed = new_line.model_copy(update={"parameters": {"value": 2}})
    assert build_move_refusal(old_line, changed, before=before, after=after) == "line_changed"
    assert build_move_refusal(old_line, old_line, before=before, after=before) == "line_changed"


def test_a_grant_that_creates_subjects_demotes() -> None:
    from cruxible_core.providers.observation_carry import scope_refusal

    grant = procedure_mandate(
        "creating",
        grants="settle",
        scope=(
            MandateClaimScope(
                claim_type=ArtifactPin(
                    role="claim-type",
                    target=_score(True).identity,
                    artifact_digest=claim_type_digest(_score(True)).tagged,
                ),
                changes=scope_changes("create"),
            ),
        ),
        subject_creation=(KIND,),
    )
    assert scope_refusal(grant, tree={}, neighbors=lambda _identity: ()) == "subject_creation"


# --- the Line's enablement follows a kept grant (ruling 2) -------------------------


@pytest.mark.parametrize("observation_only", [True, False])
def test_an_enabled_line_stays_enabled_exactly_when_its_grant_keeps_settle(
    tmp_path: Path, observation_only: bool
) -> None:
    import json
    from datetime import UTC, datetime, timedelta

    from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
    from cruxible_client.contracts.procedures.line_specs import line_identity_digest
    from cruxible_core.service.procedures.line_dispatch import (
        LineDispatchStore,
        active_line_arm,
        service_enable_line,
        service_match_listening_lines,
    )
    from tests.test_procedures.test_line_arming import _actor

    instance, _root, score = _world(tmp_path, observation_only=observation_only)
    _grant(instance, score.predicate)
    line = parse_line_spec(_accepted(instance, line_spec_path(LINE)), path=line_spec_path(LINE))
    now = datetime.now(UTC)
    enabled = service_enable_line(
        instance,
        LINE,
        principal=LineEnablementPrincipal(kind="local_operator", label="operator"),
        actor=_actor(instance),
        now=now,
        daemon_id="daemon",
    )
    assert enabled.state == "enabled"
    line_id = line_identity_digest(line.identity)
    armed = active_line_arm(instance, line_id)
    assert armed is not None

    moved = _move(instance, operation_build(build="d"))
    assert _carried(moved).carries_line_enablement is observation_only
    successor = line_spec_digest(
        parse_line_spec(_accepted(instance, line_spec_path(LINE)), path=line_spec_path(LINE))
    ).tagged
    service_match_listening_lines(
        instance, actor=_actor(instance), now=now + timedelta(seconds=1), daemon_id="daemon"
    )
    arm = active_line_arm(instance, line_id)
    if observation_only:
        assert arm is not None and arm["session_id"] == armed["session_id"]
        assert arm["line_artifact_digest"] == successor
        assert arm["armed_by"] == armed["armed_by"]
        assert arm.get("trigger_pins") == armed.get("trigger_pins")
        assert arm["carried_from"] == [armed["line_artifact_digest"]]
    else:
        assert arm is None
        with LineDispatchStore(instance).locked() as conn:
            (payload,) = conn.execute(
                "SELECT payload FROM sessions WHERE session_id=?", (armed["session_id"],)
            ).fetchone()
        assert json.loads(payload)["stop_reason"] == "line_changed"


@pytest.mark.parametrize("observation_only", [True, False])
def test_review_says_whether_a_carried_grant_kept_settle_or_why_it_demoted(
    tmp_path: Path, observation_only: bool
) -> None:
    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_client.contracts.text_display import render_review_summary
    from cruxible_core.service.proposals.review import service_review_playbill_proposal

    instance, _root, score = _world(tmp_path, observation_only=observation_only)
    _grant(instance, score.predicate)
    build = operation_build(build="d")
    install_builds(instance, build)
    moved = service_move_provider(
        instance,
        ProviderMoveRequest(provider="demo-provider", to=build.identity.name, dry_run=False),
        actor_id="owner",
        timestamp=MOVED_AT,
        may_activate=False,
    )
    assert moved.proposal_id is not None, moved
    review = service_review_playbill_proposal(
        instance,
        proposal_id=moved.proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    (grant,) = [
        item for item in review.definitions if item.name == GRANT and item.detail is not None
    ]
    text = render_review_summary(review)
    if observation_only:
        assert grant.detail.carry == (
            f"retains observation settle on {score.predicate}; retains Line enablement"
        )
    else:
        assert grant.detail.carry == "settle -> propose: judgment claim type"
    assert grant.detail.carry in text
