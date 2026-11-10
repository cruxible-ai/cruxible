"""D-R42: an attestation cites write's evidence options, made into Captures first.

``because``, ``self``, a section as ``own_words``, observed file or section
evidence, an existing ``capture`` and ``contract`` evidence all reach the
signed statement as cited Captures through ``observe_evidence`` and the daemon's
evidence step; the attestation stays one signed append (no proposal, no
generation). Unobserved workspace evidence and a source with no accepted
contract refuse before anything is signed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.attestations import (
    LocalEd25519ClaimAttestationSigner,
    attest_claim,
)
from cruxible_client.contracts.attestation_evidence import ClaimAttestationEvidenceRequest
from cruxible_client.contracts.write import (
    CaptureEvidence,
    ContractEvidence,
    FileEvidence,
    SectionEvidence,
    SelfEvidence,
    WriteRequest,
)
from cruxible_core.evidence.attestation_verification import ClaimAttestationRefusal
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.evidence.claim_attestations import (
    service_prepare_claim_attestation_evidence,
)
from tests.core_support._write_support import (
    KIND,
    REPORTS,
    caller,
    cited_captures,
    report_evidence,
    seed_write_surface,
)
from tests.test_client._attestation_support import ServiceAttestationClient
from tests.test_scale._counters import counting, trace_sqlite

WHEN = datetime(2026, 9, 29, 13, tzinfo=UTC)


@pytest.fixture(autouse=True)
def traced_sqlite(tmp_path: Path):  # type: ignore[no-untyped-def]
    with trace_sqlite(tmp_path):
        yield


def _world(root: Path, *, extra: int = 0) -> tuple[Any, str, Any, Path]:
    instance, owner = seed_write_surface(root)
    workspace = root / "workspace"
    written = service_playbill_write(
        instance,
        request=WriteRequest.model_validate(
            {
                "because": "Measured from the report.",
                "changes": [
                    {
                        "op": "set",
                        "subject": f"{KIND}/wi-1",
                        "field": "measured",
                        "value": 3,
                        "evidence": report_evidence(workspace, "three"),
                    },
                    *(
                        {
                            "op": "set",
                            "subject": f"{KIND}/n-{index}",
                            "field": "status",
                            "value": "ready",
                        }
                        for index in range(extra)
                    ),
                ],
            }
        ),
        caller=caller(),
    )
    assert written.status == "accepted", written
    claim = next(change.claim for change in written.changes if change.field.endswith("measured"))
    signer = LocalEd25519ClaimAttestationSigner.open(
        signer=owner.principal.principal_id,
        signing_key_id=owner.principal.public_key_digest,
        private_key_path=owner.private_key_path,
        expected_public_key=owner.principal.public_key,
        forbidden_roots=(instance.root,),
    )
    return instance, claim, signer, workspace


def _attest(instance: Any, claim: str, signer: Any, workspace: Path, **evidence: Any) -> Any:
    client = ServiceAttestationClient(instance, actor_id="owner", state_dir=workspace / "state")
    return attest_claim(
        client,
        instance.descriptor.instance_id,
        claim_id=claim,
        stance="support",
        signer=signer,
        attested_at=WHEN,
        workspace=workspace,
        **evidence,
    )


def _cited(instance: Any) -> tuple[str, tuple[str, ...]]:
    _event, payload = instance.claim_attestation_evidence_store().events()[-1]
    statement = payload.attestation.statement
    return statement.attestation_basis, statement.cited_capture_digests


def _contract_of(instance: Any, digest: str) -> str:
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_core.storage.cas import BodyAccessContext

    envelope = parse_capture_envelope(
        instance.body_store().read(
            digest, access=BodyAccessContext(principal_id="t", can_read_body=True)
        )
    )
    return envelope.capture_contract_digest


@pytest.mark.parametrize(
    "option",
    ["because", "self", "own_words", "file", "section", "capture", "contract"],
)
def test_every_write_evidence_option_reaches_the_signed_statement(
    tmp_path: Path, option: str
) -> None:
    from cruxible_client.contracts.captures import (
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
        builtin_contract_for_write,
        capture_contract_digest,
    )

    instance, claim, signer, workspace = _world(tmp_path)
    (workspace / "reports.md").write_text(
        "# Reports\n\nthree\n\n## Review\n\nLooked again.\n", encoding="utf-8"
    )
    held = sorted(cited_captures(instance, claim))
    evidence: dict[str, Any] = {
        "because": {"because": "I re-read the report."},
        "self": {"evidence": SelfEvidence(self="The report still says three.")},
        "own_words": {
            "evidence": SectionEvidence(
                file="reports.md", section=("Reports", "Review"), own_words=True
            )
        },
        "file": {"evidence": FileEvidence(file="reports.md#Looked again.")},
        "section": {"evidence": SectionEvidence(file="reports.md", section=("Reports", "Review"))},
        "capture": {"evidence": CaptureEvidence(capture=held[0])},
        "contract": {"evidence": ContractEvidence(contract=REPORTS.identity.name)},
    }[option]
    generation = instance.accepted_coordinate()
    proposals = len(instance.proposal_evidence().list_admissions())
    _attest(instance, claim, signer, workspace, **evidence)
    basis, cited = _cited(instance)
    assert basis == "new_capture" and len(cited) == 1
    self_source = capture_contract_digest(
        builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, None)
    ).tagged
    contract = _contract_of(instance, cited[0])
    if option in ("because", "self", "own_words"):
        assert contract == self_source
    elif option in ("file", "section"):
        assert contract == capture_contract_digest(REPORTS).tagged
    else:
        assert cited[0] in held
    # A signed append: nothing is proposed or accepted.
    assert instance.accepted_coordinate() == generation
    assert len(instance.proposal_evidence().list_admissions()) == proposals


def test_no_evidence_attests_the_claims_own_citations(tmp_path: Path) -> None:
    instance, claim, signer, workspace = _world(tmp_path)
    _attest(instance, claim, signer, workspace)
    basis, cited = _cited(instance)
    assert basis == "examined_existing"
    assert set(cited) <= cited_captures(instance, claim)


def test_a_pinned_referent_survives_evidence_prep_then_refuses_once_stale(
    tmp_path: Path,
) -> None:
    """F1: evidence preparation must not silently move a pinned referent.

    A caller who names an exact Claim revision (e.g. from a ClaimRef read
    earlier) keeps that revision through evidence preparation and signing,
    whether or not evidence is supplied. Once the Claim is revised, evidence
    can only be prepared at the new head, so attesting under the old pinned
    referent would cite evidence made against a different revision than the
    one the statement names: the client refuses before signing instead.
    """

    from cruxible_client.authoring.attestations import ClaimAttestationReferentStaleError
    from cruxible_client.contracts.projection import AcceptedCoordinate

    instance, claim, signer, workspace = _world(tmp_path)
    pinned = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    # Without evidence, a pinned referent is signed as named.
    _attest(instance, claim, signer, workspace, referent_coordinate=pinned)
    statement = instance.claim_attestation_evidence_store().events()[-1][1].attestation.statement
    assert statement.referent_coordinate == pinned

    # With evidence, while the pinned referent is still current, it must
    # also still be signed as named rather than overwritten by the evidence
    # head (the two happen to be the same coordinate here).
    _attest(instance, claim, signer, workspace, referent_coordinate=pinned, because="Still three.")
    statement = instance.claim_attestation_evidence_store().events()[-1][1].attestation.statement
    assert statement.referent_coordinate == pinned

    # Revise the same Claim: same identity, new content, a new head.
    written = service_playbill_write(
        instance,
        request=WriteRequest.model_validate(
            {
                "because": "Re-measured from the report.",
                "changes": [
                    {
                        "op": "set",
                        "subject": f"{KIND}/wi-1",
                        "field": "measured",
                        "value": 4,
                        "evidence": report_evidence(workspace, "four"),
                    }
                ],
            }
        ),
        caller=caller(),
    )
    assert written.status == "accepted", written
    revised = next(change.claim for change in written.changes if change.field.endswith("measured"))
    assert revised == claim  # same Claim identity, revised in place

    # Without evidence, the now-stale pinned referent is still honored: no
    # evidence step touches the head, so there's nothing to contradict it.
    _attest(instance, claim, signer, workspace, referent_coordinate=pinned)
    statement = instance.claim_attestation_evidence_store().events()[-1][1].attestation.statement
    assert statement.referent_coordinate == pinned
    events_before_refusal = len(instance.claim_attestation_evidence_store().events())

    # With evidence, it can only be prepared at the new head, where the
    # pinned revision is no longer current: refuse before signing.
    with pytest.raises(ClaimAttestationReferentStaleError) as refused:
        _attest(
            instance, claim, signer, workspace, referent_coordinate=pinned, because="Still three."
        )
    assert refused.value.error_code == "cruxible.claim_attestation.referent_not_current"
    assert len(instance.claim_attestation_evidence_store().events()) == events_before_refusal


def test_unobserved_workspace_evidence_refuses(tmp_path: Path) -> None:
    instance, claim, _signer, _workspace = _world(tmp_path)
    for evidence in (
        FileEvidence(file="reports.md#three"),
        SectionEvidence(file="reports.md", section=("Reports",)),
    ):
        with pytest.raises(ClaimAttestationRefusal) as refused:
            service_prepare_claim_attestation_evidence(
                instance,
                request=ClaimAttestationEvidenceRequest(claim_id=claim, evidence=evidence),
                actor_id="owner",
            )
        assert refused.value.error_code == "cruxible.claim_attestation.evidence_unobserved"
    assert instance.claim_attestation_evidence_store().events() == ()


def test_a_source_with_no_accepted_contract_refuses(tmp_path: Path) -> None:
    from cruxible_client.authoring.write_evidence import observe_evidence

    instance, claim, _signer, workspace = _world(tmp_path)
    catalog = workspace / ".cruxible" / "files.yaml"
    catalog.write_text(
        catalog.read_text(encoding="utf-8")
        + "  - name: repo.elsewhere\n    locator: elsewhere.md\n",
        encoding="utf-8",
    )
    (workspace / "elsewhere.md").write_text("# Else\n\nunique words\n", encoding="utf-8")
    observed = observe_evidence(FileEvidence(file="elsewhere.md#unique words"), workspace=workspace)
    with pytest.raises(ClaimAttestationRefusal) as refused:
        service_prepare_claim_attestation_evidence(
            instance,
            request=ClaimAttestationEvidenceRequest(claim_id=claim, evidence=observed),
            actor_id="owner",
        )
    assert refused.value.error_code == "cruxible.claim_attestation.source_contract_unaccepted"


_REFUSED_SELECTIONS = {
    # The anchor quotes credential material the source rules never admit.
    "secret_anchor": ("Rotated Bearer abc123token today", "Bearer abc123token", "secrets"),
    # The selection is over the source contract's byte budget.
    "oversize": ("x" * (1024 * 1024 + 10), "x" * (1024 * 1024 + 10), "byte budget"),
}


def _refused_selection(workspace: Path, case: str) -> tuple[FileEvidence, str]:
    from cruxible_client.authoring.write_evidence import observe_evidence

    line, anchor, detail = _REFUSED_SELECTIONS[case]
    reports = workspace / "reports.md"
    reports.write_text(reports.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")
    observed = observe_evidence(FileEvidence(file=f"reports.md#{anchor}"), workspace=workspace)
    assert isinstance(observed, FileEvidence)
    return observed, detail


@pytest.mark.parametrize("case", sorted(_REFUSED_SELECTIONS))
def test_a_selection_the_source_rules_refuse_is_a_typed_refusal(tmp_path: Path, case: str) -> None:
    """The capture rules' refusals reach the attester typed, as write's do."""

    instance, claim, _signer, workspace = _world(tmp_path)
    observed, detail = _refused_selection(workspace, case)
    events = instance.claim_attestation_evidence_store().events()
    with pytest.raises(ClaimAttestationRefusal) as refused:
        service_prepare_claim_attestation_evidence(
            instance,
            request=ClaimAttestationEvidenceRequest(claim_id=claim, evidence=observed),
            actor_id="owner",
        )
    assert refused.value.error_code == "cruxible.claim_attestation.working_selection_refused"
    assert "evidence.observation.selector" in str(refused.value)
    assert detail in str(refused.value)
    assert instance.claim_attestation_evidence_store().events() == events


@pytest.mark.parametrize("case", sorted(_REFUSED_SELECTIONS))
def test_the_route_answers_a_refused_selection_with_a_coded_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from fastapi.testclient import TestClient

    from cruxible_core.runtime import playbill_api
    from cruxible_core.runtime.permissions import reset_permissions
    from cruxible_core.runtime.playbill_manager import get_playbill_manager
    from cruxible_core.server.app import create_app
    from cruxible_core.server.registry import get_registry, reset_registry

    world = tmp_path / "world"
    world.mkdir()
    instance, claim, _signer, workspace = _world(world)
    observed, _detail = _refused_selection(workspace, case)
    events = instance.claim_attestation_evidence_store().events()
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    try:
        get_registry().create_governed_instance_with_id(instance.descriptor.instance_id)
        get_playbill_manager().register(instance.descriptor.instance_id, instance)
        monkeypatch.setattr(playbill_api, "_actor_id", lambda _instance_id: "owner")
        with TestClient(create_app(), raise_server_exceptions=False) as client:
            response = client.post(
                f"/api/v1/{instance.descriptor.instance_id}/claim-attestations/evidence",
                json=ClaimAttestationEvidenceRequest(claim_id=claim, evidence=observed).model_dump(
                    mode="json"
                ),
            )
    finally:
        get_playbill_manager().clear()
        reset_registry()
        reset_permissions()
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "cruxible.claim_attestation.working_selection_refused"
    assert instance.claim_attestation_evidence_store().events() == events


def test_the_evidence_step_and_append_are_flat_in_unrelated_claims(tmp_path: Path) -> None:
    counts: dict[int, Any] = {}
    wholes: dict[int, Any] = {}
    for size in (2, 20):
        root = tmp_path / str(size)
        root.mkdir()
        instance, claim, signer, workspace = _world(root, extra=size)
        _attest(instance, claim, signer, workspace, because="warm")
        with counting(root) as window:
            service_prepare_claim_attestation_evidence(
                instance,
                request=ClaimAttestationEvidenceRequest(claim_id=claim, because="Again."),
                actor_id="owner",
            )
        assert not window.unexplained, window.unexplained
        counts[size] = window.counts
        with counting(root) as whole:
            _attest(instance, claim, signer, workspace, because="Re-read the report.")
        assert not whole.unexplained, whole.unexplained
        wholes[size] = whole.counts
    print("D-R42 evidence step", {size: dict(c) for size, c in counts.items()})
    print("D-R42 whole attest", {size: dict(c) for size, c in wholes.items()})
    for measured in (counts, wholes):
        small, large = measured[2], measured[20]
        assert large["sql_statements"] == small["sql_statements"], (small, large)
        assert large["claim_parses"] == small["claim_parses"], (small, large)
        assert large["git_blob_reads"] == small["git_blob_reads"], (small, large)
        assert large["sql_vm_steps"] <= small["sql_vm_steps"] * 1.10 + 200, (small, large)
