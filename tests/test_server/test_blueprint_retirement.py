"""Compiler 36 permits retiring a kit's retained Blueprint, without keeping it."""

from cruxible_client.contracts.procedures.blueprints import (
    blueprint_digest,
    parse_blueprint,
    render_blueprint,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_core.compiler.compiler import LIGHT_ARTIFACTS_COMPILER
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_inspect_playbill_proposal,
    service_propose_compiler_upgrade,
    service_submit_playbill_approval,
)
from tests.support.core_provider_world import AT as TIMESTAMP
from tests.test_ledger.test_activation import _sign
from tests.test_server.test_kit_providers import (
    BLUEPRINT,
    _add,
    _author,
    _build,
    _install,
    _package,
    _tree,
    _worlds,
)


def test_kit_release_dropping_installed_blueprint_retires_it_on_36_without_keep(
    tmp_path, monkeypatch, provider_checkout
):
    opened = _worlds(tmp_path, monkeypatch, historical=True)
    publisher, consumer = next(opened)
    package = _package(tmp_path / "kit-source", provider_checkout, name="kit-call")
    _install(publisher, package, control_domain="publisher")
    _author(publisher, procedure=False)
    first = _build(publisher, package)
    path = f"blueprints/{BLUEPRINT}.json"
    instance, owner = consumer.instance, consumer.owner

    def accept(proposal_id):
        inspected = service_inspect_playbill_proposal(instance, proposal_id=proposal_id)
        candidate = inspected.proposal.candidate
        assert candidate is not None, inspected.proposal.evaluation.diagnostics
        signed = _sign(owner, candidate.candidate_digest, candidate.candidate.parent_semantic_root)
        service_submit_playbill_approval(
            instance,
            proposal_id=proposal_id,
            attestation=signed.attestation,
            authenticated_submitter="owner",
        )
        receipt = service_activate_playbill_proposal(
            instance, proposal_id=proposal_id, activated_by="owner"
        )
        assert receipt.status == "accepted", receipt

    def install(bundle):
        result = _add(consumer, bundle, dry_run=False)
        assert result.status in {"proposed", "accepted"}, result
        if result.status == "proposed":
            accept(result.proposal_id)
        return result

    install(first)
    upgrade = service_propose_compiler_upgrade(
        instance, target=LIGHT_ARTIFACTS_COMPILER, actor_id="owner", timestamp=TIMESTAMP
    )
    accept(upgrade.proposal.admission.proposal_id)
    assert instance.accepted_coordinate().compiler == LIGHT_ARTIFACTS_COMPILER
    previous = parse_blueprint(
        instance.blob_at(instance.accepted_coordinate().git_oid, path), path=path
    )
    from cruxible_core.proposals.proposals import evaluate_proposal_tree
    from cruxible_core.service.kits import _retirement

    parent = instance.tree_at(instance.accepted_coordinate().git_oid)
    for by_name in (False, True):
        content = _retirement(path, parent[path], by_name=by_name)
        result = evaluate_proposal_tree(
            base_tree=parent,
            current_tree=parent,
            proposed_tree={**parent, path: content},
            current=instance.accepted_coordinate(),
            bodies=instance.body_store(),
            timestamp=TIMESTAMP,
            rebased=False,
            actor_id="owner",
        )
        assert result.diagnostics == ()
        assert result.candidate is not None
        retired = parse_blueprint(content, path=path)
        definition = retired.definition.model_copy(update={"description": "edited on retirement"})
        edited = retired.model_copy(
            update={
                "definition": definition,
                "definition_digest": compute_procedure_definition_digest(definition).tagged,
            }
        )
        refused = evaluate_proposal_tree(
            base_tree=parent,
            current_tree=parent,
            proposed_tree={**parent, path: render_blueprint(edited)},
            current=instance.accepted_coordinate(),
            bodies=instance.body_store(),
            timestamp=TIMESTAMP,
            rebased=False,
            actor_id="owner",
        )
        assert refused.candidate is None
        assert any("author a Procedure with slots" in d.message for d in refused.diagnostics)
    # The publisher's next release no longer uses the interface: its retired
    # Blueprint must not pull that interface or its package into the release.
    publisher.accept_members({path: _retirement(path, _tree(publisher)[path])}, timestamp=TIMESTAMP)
    second = _build(publisher, version="1.1.0")
    assert path not in second.contents()
    assert not any(p.startswith("provider-interfaces/") for p in second.contents())
    assert second.manifest.providers == ()
    result = install(second)
    assert result.transition == "upgrade"
    assert [(item.identity, item.action) for item in result.plan if item.path == path] == [
        (f"Blueprint:{BLUEPRINT}", "retire")
    ]
    retired = parse_blueprint(
        instance.blob_at(instance.accepted_coordinate().git_oid, path), path=path
    )
    assert retired.lifecycle.state == "retired"
    assert retired.lifecycle.predecessor_digest == blueprint_digest(previous).tagged
    from cruxible_client.contracts.procedures.names import name_procedure

    assert retired.definition == name_procedure(previous).definition
    assert retired.pins == name_procedure(previous).pins
    for _ in opened:
        pass
