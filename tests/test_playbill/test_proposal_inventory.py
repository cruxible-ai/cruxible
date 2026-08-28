"""Operational proposal inventory and writer identity reads."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_core.playbill.service.documents import service_propose_playbill_document
from cruxible_core.playbill.service.query_definitions import (
    service_propose_playbill_query_definition,
)
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import PermissionMode
from cruxible_core.server.auth import ResolvedAuthContext
from cruxible_core.service.playbill_proposals import (
    service_list_playbill_proposals,
    service_playbill_whoami,
)
from tests.test_playbill._claim_authoring_support import service_propose_playbill_claim
from tests.test_playbill._knowledge_loop_support import (
    TIMESTAMP,
    activate,
    authoring,
    seed_claims,
    work_item_query,
)


def test_proposal_inventory_reduces_open_accepted_refused_and_stale(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    stale = service_propose_playbill_query_definition(
        instance,
        query=work_item_query("stale-query"),
        actor_id="owner",
        proposal_name="stale-query",
        timestamp=TIMESTAMP,
    )
    accepted = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-44", "ready", with_claim_type=False),
        actor_id="owner",
        proposal_name="advance-head",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, accepted)
    refused = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:missing-body",
            document_kind="design",
            title="Missing body",
            media_type="text/markdown",
            body_digest="sha256:" + "f" * 64,
            authority=DocumentAuthority(
                required_tier="graph_write",
            ),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="refused",
        timestamp=TIMESTAMP,
    )
    assert refused.proposal.evaluation.verdict == "refused"
    opened = service_propose_playbill_query_definition(
        instance,
        query=work_item_query("open-query"),
        actor_id="owner",
        proposal_name="open-query",
        timestamp=TIMESTAMP,
    )

    result = service_list_playbill_proposals(instance)
    by_id = {item.proposal_id: item for item in result.entries}
    assert by_id[stale.proposal.admission.proposal_id].terminal_reason == "stale"
    assert by_id[accepted.proposal.proposal.admission.proposal_id].terminal_reason == "accepted"
    assert by_id[refused.proposal.admission.proposal_id].terminal_reason == "refused"
    assert by_id[opened.proposal.admission.proposal_id].status == "open"
    assert by_id[opened.proposal.admission.proposal_id].terminal_reason is None
    assert result.entries == tuple(
        sorted(
            result.entries,
            key=lambda item: (item.admitted_at.encode("utf-8"), item.proposal_id.encode()),
        )
    )
    assert service_list_playbill_proposals(instance, status="open").entries == (
        by_id[opened.proposal.admission.proposal_id],
    )


def test_whoami_binds_transport_identity_to_current_principal_registry(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)

    active = service_playbill_whoami(
        instance,
        actor_id="owner",
        credential_label="owner",
        actor_id_source="runtime_credential_label",
        permission_mode=PermissionMode.GOVERNED_WRITE,
    )
    absent = service_playbill_whoami(
        instance,
        actor_id="local-operator",
        credential_label="local-operator",
        actor_id_source="local_operator",
        permission_mode=PermissionMode.ADMIN,
    )

    assert active.actor_id == active.credential_label == "owner"
    assert active.principal_registration_status == "active"
    assert active.credential_permission_mode == "governed_write"
    assert active.active_principal_ids == tuple(
        sorted(active.active_principal_ids, key=lambda item: item.encode("utf-8"))
    )
    assert absent.principal_registration_status == "absent"


def test_runtime_whoami_uses_the_runtime_credential_label_as_actor_id(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    instance, _owner = seed_claims(tmp_path)

    class Manager:
        def get(self, instance_id: str):  # type: ignore[no-untyped-def]
            assert instance_id == instance.descriptor.instance_id
            return instance

    monkeypatch.setattr(playbill_api, "get_playbill_manager", lambda: Manager())
    monkeypatch.setattr(playbill_api, "check_permission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(playbill_api, "get_current_mode", lambda: PermissionMode.READ_ONLY)
    monkeypatch.setattr(
        playbill_api,
        "get_current_auth_context",
        lambda: ResolvedAuthContext(
            principal_id="cred_opaque",
            principal_label="owner",
            credential_type="runtime_credential",
            instance_scope=instance.descriptor.instance_id,
            role="read_only",
            effective_permission_mode=PermissionMode.READ_ONLY,
        ),
    )

    result = playbill_api.playbill_whoami(instance.descriptor.instance_id)

    assert result.actor_id == result.credential_label == "owner"
    assert result.actor_id_source == "runtime_credential_label"
    assert result.credential_permission_mode == "read_only"
