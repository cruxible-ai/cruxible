"""Install a provider build and move Lines to it as ONE proposal (``install --move``).

``ProviderInstallRequest.move`` lays exactly the ``provider move --to <build>``
writes over the install's candidate: the new Provider, each Line successor on
it, and the Mandates carried with them. Compiler revision 74 judges the carried
grants from the parent tree (old side) and that candidate (new side), so an
observation-only settle grant across a pure, non-widening build keeps settle and
its enabled Line stays enabled -- one proposal, one approval. Package
preparation is not what these tests cover: it is replaced by the build a
preparation would register (``operation_build``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cruxible_client.contracts.procedure_mandates import (
    parse_procedure_mandate,
    procedure_mandate_digest,
    procedure_mandate_path,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_identity_digest,
    line_spec_digest,
    line_spec_path,
    parse_line_spec,
)
from cruxible_client.contracts.provider_installation import (
    ProviderInstallRequest,
    ProviderInstallResult,
    ProviderWheelObject,
)
from cruxible_client.contracts.providers import provider_path, render_provider
from cruxible_core.derived.derived_state import fork_tree
from cruxible_core.service.procedures import provider_installation as service
from tests.support.operation_world import operation_build
from tests.test_procedures.test_claims_fix import run_measured
from tests.test_procedures.test_observation_settle_carry import (
    GRANT,
    LINE,
    _accepted,
    _grant,
    _world,
)

pytestmark = pytest.mark.executor_only

ZERO = "sha256:" + "0" * 64
INSTALLED_AT = "2026-10-29T11:30:00.000000Z"


def _install(
    instance: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    build: Any,
    *,
    move: bool = True,
    only: tuple[str, ...] = (),
    dry_run: bool = False,
    actor_id: str = "owner",
) -> ProviderInstallResult:
    """``service_install_provider`` with its package preparation replaced by ``build``."""

    def definition_changes(instance: Any, *_prepared: Any) -> Any:
        oid = _prepared[-1]
        tree = instance.immutable_tree_at(oid)
        path = provider_path(build.identity.name)
        candidate = fork_tree(tree)
        if path in tree:
            return candidate, (), build
        candidate[path] = render_provider(build)
        return candidate, (path,), build

    nothing = SimpleNamespace(
        manifest=SimpleNamespace(implementations=()), runtime_requirements=(), operations=()
    )
    prepared = service._PreparedInstallation(
        document=nothing,  # type: ignore[arg-type]
        provider=SimpleNamespace(implementations=()),  # type: ignore[arg-type]
        configured=None,  # type: ignore[arg-type]
        deployment=None,
        rewrite_prepared=False,
    )
    monkeypatch.setattr(service, "_definition_changes", definition_changes)
    monkeypatch.setattr(service, "_prepare_installation", lambda *_args: prepared)
    monkeypatch.setattr(service, "_register_deployment", lambda *_args: None)
    monkeypatch.setattr(service, "_retained_registration", lambda _path: (nothing, None))
    operator = SimpleNamespace(
        config=SimpleNamespace(provider_repository=None, provider_index_urls=(), deployments=()),
        process_leases=object(),
        state_root=tmp_path,
    )
    request = ProviderInstallRequest(
        wheel=ProviderWheelObject(
            filename=f"demo_provider-1.0.0+{build.identity.name[-6:]}-py3-none-any.whl",
            digest=ZERO,
        ),
        lock_digest=ZERO,
        move=move,
        move_only=only,
        dry_run=dry_run,
    )
    return service.service_install_provider(
        instance,
        operator=operator,  # type: ignore[arg-type]
        request=request,
        actor_id=actor_id,
        timestamp=INSTALLED_AT,
    )


def _require_independent_approval(instance: Any) -> None:
    """As ``init --require-independent-approval``: the owner's own changes need a reviewer."""

    from cruxible_client.contracts.approval_policy import (
        APPROVAL_POLICY_PATH,
        ApprovalPolicy,
        render_approval_policy,
    )
    from tests.core_support._identity_support import accept_tree

    accept_tree(
        instance,
        {
            APPROVAL_POLICY_PATH: render_approval_policy(
                ApprovalPolicy(mode="independent_approval_required")
            )
        },
        name="require-review",
    )


def _carry(result: ProviderInstallResult, identity: str) -> Any:
    return {item.identity: item for item in result.automation_carries}[identity]


def _line_digest(instance: Any) -> str:
    path = line_spec_path(LINE)
    return line_spec_digest(parse_line_spec(_accepted(instance, path), path=path)).tagged


def _proposal_members(instance: Any, proposal_id: str) -> set[str]:
    from cruxible_client.contracts.cas_contracts import BodyAccessContext
    from cruxible_core.service.proposals.review import service_review_playbill_proposal

    review = service_review_playbill_proposal(
        instance,
        proposal_id=proposal_id,
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    return {f"{item.kind}:{item.name}:{item.action}" for item in review.definitions}


def test_install_with_move_is_one_proposal_and_one_approval_keeping_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under independent approval: the build, the Line successor and the kept grant
    land together after ONE reviewer approval, and the next run settles on the new
    build."""

    from tests.core_support._retirement_support import activate_proposal
    from tests.core_support._support import client_material

    instance, root, score = _world(tmp_path)
    _grant(instance, score.predicate)
    first = run_measured(instance, root, value=1)
    assert first.terminal_egress[0].settle_outcome == "settled"
    _require_independent_approval(instance)
    old_line = _line_digest(instance)
    build = operation_build(build="d")

    result = _install(instance, monkeypatch, tmp_path, build, actor_id="owner")
    assert result.status == "awaiting_approval", result
    assert not result.registered and result.proposal_id is not None
    assert result.build == build.identity.name
    (repin,) = result.moved
    assert (repin.identity, repin.action, repin.to_build) == (
        f"Line:{LINE}",
        "successor",
        build.identity.name,
    )
    carried = _carry(result, f"ProcedureMandate:{GRANT}")
    assert (carried.old_grant, carried.new_grant) == ("settle", "settle")
    assert carried.retained_scope == (score.predicate,) and carried.carries_line_enablement
    members = _proposal_members(instance, result.proposal_id)
    assert any(item.startswith(f"Provider:{build.identity.name}:") for item in members), members
    assert any(item.startswith(f"Line:{LINE}:") for item in members), members
    assert any(item.startswith(f"ProcedureMandate:{GRANT}:") for item in members), members

    # A retry before the approval finds the same pending proposal.
    again = _install(instance, monkeypatch, tmp_path, build, actor_id="owner")
    assert (again.proposal_id, again.candidate_digest) == (
        result.proposal_id,
        result.candidate_digest,
    )

    activate_proposal(instance, client_material(instance.root.parent, instance), result.proposal_id)
    line = parse_line_spec(_accepted(instance, line_spec_path(LINE)), path=line_spec_path(LINE))
    assert line.lifecycle.predecessor_digest == old_line
    assert line.bindings["compute"].build.target.name == build.identity.name
    mandate_path = procedure_mandate_path(GRANT)
    mandate = parse_procedure_mandate(_accepted(instance, mandate_path), path=mandate_path)
    assert mandate.grants == "settle"
    again = run_measured(instance, root, minute=2, value=2)
    assert again.status == "succeeded", again.terminal
    assert again.terminal_egress[0].settle_outcome == "settled"
    assert (
        again.terminal_egress[0].procedure_mandate_digest
        == procedure_mandate_digest(mandate).tagged
    )

    # Installed and moved: installing again changes nothing.
    done = _install(instance, monkeypatch, tmp_path, build, actor_id="owner")
    assert done.status == "ready" and done.registered and done.proposal_id is None
    assert done.moved == () and done.automation_carries == ()


@pytest.mark.parametrize("observation_only", [True, False])
def test_an_enabled_line_stays_enabled_across_install_with_move_exactly_when_settle_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation_only: bool
) -> None:
    from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
    from cruxible_core.service.procedures.line_dispatch import (
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

    result = _install(instance, monkeypatch, tmp_path, operation_build(build="d"))
    assert result.status == "ready" and result.registered, result
    carried = _carry(result, f"ProcedureMandate:{GRANT}")
    assert carried.carries_line_enablement is observation_only
    if not observation_only:
        assert carried.demotion == "judgment_claim_type"
    service_match_listening_lines(
        instance, actor=_actor(instance), now=now + timedelta(seconds=1), daemon_id="daemon"
    )
    arm = active_line_arm(instance, line_id)
    if observation_only:
        assert arm is not None and arm["session_id"] == armed["session_id"]
        assert arm["line_artifact_digest"] == _line_digest(instance)
    else:
        assert arm is None


def test_install_with_move_onto_a_widening_build_demotes_settle_in_the_same_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _root, score = _world(tmp_path)
    _grant(instance, score.predicate)
    _require_independent_approval(instance)
    widened = operation_build(build="d", endpoints=("api.example.com",))
    result = _install(instance, monkeypatch, tmp_path, widened, actor_id="owner")
    assert result.status == "awaiting_approval" and result.proposal_id is not None
    carried = _carry(result, f"ProcedureMandate:{GRANT}")
    assert (carried.old_grant, carried.new_grant) == ("settle", "propose")
    assert carried.loses_settle and carried.demotion == "endpoints_widened"
    assert not carried.carries_line_enablement
    assert [item.action for item in result.moved] == ["successor"]


def test_install_with_move_and_no_line_on_another_build_just_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _root, _score = _world(tmp_path)
    other = operation_build(provider_id="other-provider", build="e")
    result = _install(instance, monkeypatch, tmp_path, other)
    assert result.status == "ready" and result.registered, result
    assert result.moved == () and result.automation_carries == () and result.stops == ()
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert provider_path(other.identity.name) in tree
    assert "moved" not in result.model_dump(mode="json")


def test_install_with_move_only_names_lines_and_without_move_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _root, score = _world(tmp_path)
    _grant(instance, score.predicate)
    before = _line_digest(instance)
    skipped = _install(
        instance, monkeypatch, tmp_path, operation_build(build="d"), only=("Line:test.other",)
    )
    assert skipped.status == "ready" and skipped.moved == ()
    assert _line_digest(instance) == before
    plain = _install(instance, monkeypatch, tmp_path, operation_build(build="e"), move=False)
    assert plain.status == "ready" and plain.moved == ()
    assert _line_digest(instance) == before


def test_install_with_move_preview_evaluates_the_combined_change_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _root, score = _world(tmp_path)
    _grant(instance, score.predicate)
    head = instance.accepted_coordinate()
    build = operation_build(build="d")
    unprepared = _install(instance, monkeypatch, tmp_path, build, dry_run=True)
    assert unprepared.status == "would_install" and unprepared.moved == ()
    assert "registration" in unprepared.not_run
    directory = (
        instance.root
        / "exhaust"
        / "provider-installations"
        / unprepared.installation_id.removeprefix("sha256:")
    )
    directory.mkdir(parents=True)
    (directory / "prepared.json").write_text("{}")
    preview = _install(instance, monkeypatch, tmp_path, build, dry_run=True)
    assert preview.status == "would_install", preview
    assert preview.candidate_digest is not None and preview.detail
    assert [item.identity for item in preview.moved] == [f"Line:{LINE}"]
    assert _carry(preview, f"ProcedureMandate:{GRANT}").new_grant == "settle"
    assert instance.accepted_coordinate() == head


def test_move_is_not_part_of_the_installation_identity() -> None:
    """The preparation is the package's: asking to move Lines prepares nothing again."""

    wheel = ProviderWheelObject(filename="demo_provider-1.0.0-py3-none-any.whl", digest=ZERO)
    plain = ProviderInstallRequest(wheel=wheel, lock_digest=ZERO)
    moving = ProviderInstallRequest(
        wheel=wheel, lock_digest=ZERO, move=True, move_only=("Line:a",)
    )
    excluded = {"reverify", "dry_run", "at", "move", "move_only"}
    assert plain.model_dump(mode="json", exclude=excluded) == moving.model_dump(
        mode="json", exclude=excluded
    )
    with pytest.raises(ValueError, match="requires move"):
        ProviderInstallRequest(wheel=wheel, lock_digest=ZERO, move_only=("Line:a",))
    with pytest.raises(ValueError, match="sorted and unique"):
        ProviderInstallRequest(
            wheel=wheel, lock_digest=ZERO, move=True, move_only=("Line:b", "Line:a")
        )
