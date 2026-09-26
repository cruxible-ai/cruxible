"""Kits: export owned definitions as a release, import it as one governed change set."""

from __future__ import annotations

import base64
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cruxible_client import Playbill
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
from cruxible_client.contracts.authoring.models import ClaimTypeSuccessionDependentV1
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    capture_contract_digest,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, parse_claim_type
from cruxible_client.contracts.claims import ClaimArtifactV3, parse_claim
from cruxible_client.contracts.documents import DocumentLifecycle, DocumentShell
from cruxible_client.contracts.kits import (
    KitArtifactBytesV1,
    KitArtifactV1,
    KitBundleV1,
    PlaybillKitAddRequestV1,
    PlaybillKitBuildRequestV1,
    PlaybillKitChangeResultV1,
    PlaybillKitRemoveRequestV1,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinitionSpecV1,
    QueryDefinitionV1,
    QueryEvaluationPolicyV1,
    parse_query_definition,
    query_definition_digest,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimValueRefV1,
    QueryEntryV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
)
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.kits import read_kit_directory, write_kit_directory
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.claims.artifact_references import REFERENCE_FIELDS
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._pc_c_support import capture_contract

SEATS = "acme.account.seats"
PLAN = "acme.account.plan"
OWNER = "acme.account.owner"
UNOWNED = "other.thing.flag"


def _path(predicate: str) -> str:
    kind, _, name = predicate.rpartition(".")
    return f"claim-types/{kind}/{name}.json"


class _World:
    def __init__(self, http: TestClient, instance_id: str, reviewer_key: Path, workspace: Path):
        self.http = http
        self.instance_id = instance_id
        self.reviewer_key = reviewer_key
        transport = CruxibleClient(base_url="http://cruxible")
        transport._client = http  # type: ignore[assignment]
        self.pb = Playbill._from_client(transport, instance_id=instance_id, workspace=workspace)

    def tree(self) -> dict[str, bytes]:
        instance = get_playbill_manager().get(self.instance_id)
        return dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))

    def claim_type(self, predicate: str) -> ClaimType:
        path = _path(predicate)
        return parse_claim_type(self.tree()[path], path=path)

    def activate(self, proposal_id: str) -> None:
        base = f"/api/v1/{self.instance_id}/playbill/proposals/{proposal_id}"
        activated = self.http.post(f"{base}/activate")
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "accepted", activated.text

    def settle(self, result: PlaybillKitChangeResultV1) -> None:
        assert result.status == "proposed" and result.proposal_id is not None, result
        if result.approval_required:
            self.approve(result.proposal_id)
        else:
            self.activate(result.proposal_id)

    def approve(self, proposal_id: str) -> None:
        base = f"/api/v1/{self.instance_id}/playbill/proposals/{proposal_id}"
        challenge = self.http.post(f"{base}/approval-challenge", json={"signer_id": "reviewer"})
        assert challenge.status_code == 200, challenge.text
        body = challenge.json()
        signer = LocalEd25519ApprovalSigner.open(
            signer_id="reviewer",
            private_key_path=self.reviewer_key,
            expected_public_key=body["signer_principal"]["public_key"],
            forbidden_roots=(),
        )
        attestation = signer.sign(ApprovalStatement.model_validate(body["statement"]))
        approved = self.http.post(
            f"{base}/approvals", json={"attestation": attestation.model_dump(mode="json")}
        )
        assert approved.status_code == 200, approved.text
        activated = self.http.post(f"{base}/activate")
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "accepted", activated.text

    def author(
        self,
        *definitions: ClaimType,
        successions: tuple[ClaimType, ...] = (),
        dependents: tuple[ClaimTypeSuccessionDependentV1, ...] = (),
    ) -> None:
        draft = self.pb.changes(rationale="Shape the account vocabulary.")
        for definition in definitions:
            draft.claim_type(definition)
        for successor in successions:
            draft.succeed_claim_type(successor, dependents=dependents)
        intent = draft.prepare()
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit()
        assert submitted._candidate_status is not None
        proposal_id = submitted._candidate_status.proposal_id
        assert proposal_id is not None
        self.approve(proposal_id)
        self.pb.refresh()

    def succeed(
        self,
        predicate: str,
        schema: dict[str, object],
        dependents: tuple[ClaimTypeSuccessionDependentV1, ...] = (),
    ) -> None:
        current = self.claim_type(predicate)
        successor = current.model_copy(
            update={
                "literal_schema": schema,
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=claim_type_digest(current).tagged
                ),
            }
        )
        self.author(successions=(successor,), dependents=dependents)

    def build(
        self, version: str, owns: tuple[str, ...] = ("acme.",), kit_id: str = "acme"
    ) -> KitBundleV1:
        return playbill_api.playbill_kit_build(
            self.instance_id,
            PlaybillKitBuildRequestV1(kit_id=kit_id, version=version, owns=owns),
        ).bundle

    def add(self, bundle: KitBundleV1) -> PlaybillKitChangeResultV1:
        before = self.tree()
        result = playbill_api.playbill_kit_add(
            self.instance_id, PlaybillKitAddRequestV1(bundle=bundle, source="test")
        )
        if result.status == "proposed":
            # Proposing lands nothing; the ordinary activation does.
            assert self.tree() == before
            self.settle(result)
        return result


def _claim_type(predicate: str, schema: dict[str, object]) -> ClaimType:
    kind = predicate.rpartition(".")[0]
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=predicate),
        predicate=predicate,
        allowed_subject_kinds=(kind,),
        object_kind="literal",
        literal_schema=schema,
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    (workspace / ".playbill").mkdir(parents=True)
    (workspace / ".playbill" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\ncatalog_kind: portable\nentries: []\n",
        encoding="utf-8",
    )
    return workspace


@pytest.fixture
def worlds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_World, _World]]:
    """A publisher instance and a consumer instance on one daemon."""

    yield from _open_worlds(tmp_path, monkeypatch, independent=False)


@pytest.fixture
def strict_worlds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[_World, _World]]:
    """The same pair, with a consumer that requires independent approval."""

    yield from _open_worlds(tmp_path, monkeypatch, independent=True)


def _open_worlds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, independent: bool
) -> Iterator[tuple[_World, _World]]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    with TestClient(create_app()) as client:
        opened = []
        for name in ("publisher", "consumer"):
            registered = get_registry().create_governed_instance_with_id(f"inst_kit_{name}")
            managed = Path(registered.record.location)
            keys = [
                generate_client_principal_key(
                    tmp_path / f"{name}-{principal}-custody",
                    principal_id=principal,
                    kind="ordinary",
                    forbidden_roots=(managed,),
                )
                for principal in ("operator", "reviewer")
            ]
            instance_id = registered.record.instance_id
            initialized = client.post(
                f"/api/v1/{instance_id}/playbill/init",
                json={
                    "require_independent_approval": independent and name == "consumer",
                    "principals": [key.principal.model_dump(mode="json") for key in keys],
                },
            )
            assert initialized.status_code == 200, initialized.text
            opened.append(
                _World(client, instance_id, keys[1].private_key_path, _workspace(tmp_path / name))
            )
        yield opened[0], opened[1]
    get_playbill_manager().clear()
    reset_runtime_credential_store()
    reset_registry()
    reset_permissions()


def test_a_release_installs_byte_identical_and_reinstalling_it_changes_nothing(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(
        _claim_type(SEATS, {"type": "integer"}),
        _claim_type(PLAN, {"type": "string"}),
        _claim_type(UNOWNED, {"type": "boolean"}),
    )
    # History before the first release must not leak into it.
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})

    release = publisher.build("1.0.0")

    assert [item.path for item in release.manifest.artifacts] == [_path(PLAN), _path(SEATS)]
    assert all(b'"predecessor_digest": null' in content for content in release.contents().values())
    added = consumer.add(release)
    assert added.status == "proposed"
    assert {item.action for item in added.plan} == {"add"}
    tree = consumer.tree()
    for path, content in release.contents().items():
        assert tree[path] == content
    status = playbill_api.playbill_kit_status(consumer.instance_id)
    assert [(kit.kit_id, kit.version, kit.drifted) for kit in status.kits] == [
        ("acme", "1.0.0", ())
    ]
    assert consumer.add(release).status == "unchanged"


def test_an_upgrade_is_the_consumers_diff_against_a_self_contained_release(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    first = publisher.build("1.0.0")
    consumer.add(first)
    installed_seats = claim_type_digest(consumer.claim_type(SEATS)).tagged

    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    publisher.succeed(SEATS, {"type": "integer", "minimum": 1})
    publisher.author(_claim_type(OWNER, {"type": "string"}))
    second = publisher.build("1.1.0")

    # The release carries no history of its own; the consumer supplies it.
    assert all(b'"predecessor_digest": null' in content for content in second.contents().values())
    assert second.contents()[_path(PLAN)] == first.contents()[_path(PLAN)]
    upgraded = consumer.add(second)

    assert {(item.path, item.action) for item in upgraded.plan} == {
        (_path(OWNER), "add"),
        (_path(PLAN), "unchanged"),
        (_path(SEATS), "replace"),
    }
    seats = consumer.claim_type(SEATS)
    assert seats.lifecycle.predecessor_digest == installed_seats
    assert seats.literal_schema == {"type": "integer", "minimum": 1}
    assert consumer.tree()[_path(OWNER)] == second.contents()[_path(OWNER)]
    assert playbill_api.playbill_kit_status(consumer.instance_id).kits[0].drifted == ()


def test_a_later_release_installs_fresh_on_its_own(worlds: tuple[_World, _World]) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    publisher.build("1.0.0")
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    later = publisher.build("1.1.0")

    added = consumer.add(later)

    assert [(item.path, item.action) for item in added.plan] == [(_path(SEATS), "add")]
    assert consumer.tree()[_path(SEATS)] == later.contents()[_path(SEATS)]


def test_changing_a_claim_type_carries_its_live_claims_as_a_succession_would(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0"))
    draft = consumer.pb.changes(rationale="Record Acme's seats.")
    draft.subject(
        SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name="acme.account/acme"),
            subject_kind="acme.account",
            subject_id="acme",
            lifecycle=ArtifactLifecycle(),
        )
    )
    draft.claim(
        subject="acme.account/acme",
        predicate=SEATS,
        value=50,
        role="observation",
        rationale="The contract says 50 seats.",
        supported_by=None,
        copied_from=None,
        self_source="seats: 50\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    consumer.approve(submitted._candidate_status.proposal_id)
    claim_paths = [path for path in consumer.tree() if path.startswith("claims/")]
    assert len(claim_paths) == 1

    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    upgraded = consumer.add(publisher.build("1.1.0"))

    assert (claim_paths[0], "carry") in {(item.path, item.action) for item in upgraded.plan}
    carried = consumer.tree()[claim_paths[0]]
    assert claim_type_digest(consumer.claim_type(SEATS)).tagged.encode("ascii") in carried


def test_a_local_edit_blocks_the_upgrade_of_that_path_and_is_reported(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    first = publisher.build("1.0.0")
    consumer.add(first)
    consumer.succeed(PLAN, {"type": "string", "minLength": 1})
    assert playbill_api.playbill_kit_status(consumer.instance_id).kits[0].drifted == (_path(PLAN),)

    publisher.succeed(PLAN, {"type": "string", "maxLength": 64})
    second = publisher.build("1.1.0")
    blocked = consumer.add(second)

    assert blocked.status == "blocked"
    assert blocked.proposal_id is None
    assert [(item.path, item.action) for item in blocked.plan] == [
        (_path(PLAN), "conflict"),
        (_path(SEATS), "unchanged"),
    ]


def test_overlapping_ownership_is_refused(worlds: tuple[_World, _World]) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))

    overlapping = consumer.add(
        publisher.build("1.0.0", owns=("acme.account.",), kit_id="acme-extra")
    )

    assert overlapping.status == "blocked"
    assert "owned by kit acme" in (overlapping.detail or "")


def test_removing_a_kit_retires_what_it_installed(worlds: tuple[_World, _World]) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))

    removed = playbill_api.playbill_kit_remove(
        consumer.instance_id, PlaybillKitRemoveRequestV1(kit_id="acme")
    )
    consumer.settle(removed)

    assert {item.action for item in removed.plan} == {"retire"}
    assert consumer.claim_type(SEATS).lifecycle.state == "retired"
    assert playbill_api.playbill_kit_status(consumer.instance_id).kits == ()


def test_a_kit_needs_the_approval_the_consumer_policy_requires(
    strict_worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = strict_worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    proposed = playbill_api.playbill_kit_add(
        consumer.instance_id,
        PlaybillKitAddRequestV1(bundle=publisher.build("1.0.0"), source="test"),
    )

    assert proposed.status == "proposed" and proposed.approval_required
    assert proposed.proposal_id is not None
    refused = consumer.http.post(
        f"/api/v1/{consumer.instance_id}/playbill/proposals/{proposed.proposal_id}/activate"
    )
    assert refused.status_code != 200 or refused.json()["status"] != "accepted"
    consumer.settle(proposed)
    assert _path(SEATS) in consumer.tree()


def test_a_release_travels_as_a_directory_through_the_http_client(
    worlds: tuple[_World, _World], tmp_path: Path
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = publisher.http  # type: ignore[assignment]

    built = transport.build_playbill_kit(
        publisher.instance_id,
        PlaybillKitBuildRequestV1(kit_id="acme", version="1.0.0", owns=("acme.",)),
    )
    directory = tmp_path / "acme-1.0.0"
    write_kit_directory(built.bundle, directory)
    assert (directory / "cruxible-kit.json").is_file()
    assert (directory / "artifacts" / _path(SEATS)).read_bytes() == built.bundle.contents()[
        _path(SEATS)
    ]
    read_back = read_kit_directory(directory)
    assert read_back == built.bundle

    proposed = transport.add_playbill_kit(
        consumer.instance_id, PlaybillKitAddRequestV1(bundle=read_back, source=directory.name)
    )
    consumer.settle(proposed)
    status = transport.playbill_kit_status(consumer.instance_id)
    assert [(kit.kit_id, kit.source) for kit in status.kits] == [("acme", "acme-1.0.0")]

    (directory / "artifacts" / "claim-types" / "stray.json").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="does not match its manifest"):
        read_kit_directory(directory)


def test_a_build_with_nothing_owned_is_refused(worlds: tuple[_World, _World]) -> None:
    publisher, _consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))

    with pytest.raises(DataValidationError, match="no live definitions"):
        publisher.build("1.0.0", owns=("nothing.",))


def _contract(name: str, *, max_rows: int = 4, predecessor: str | None = None) -> CaptureContractV1:
    base = capture_contract(name=name)
    return base.model_copy(
        update={
            "selection_budget": base.selection_budget.model_copy(update={"max_rows": max_rows}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor),
        }
    )


def _pinning_type(predicate: str, contract: CaptureContractV1) -> ClaimType:
    return _claim_type(predicate, {"type": "string"}).model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="orders",
                        claim_roles=("observation",),
                        capture_contract_digests=(capture_contract_digest(contract).tagged,),
                        evidence_kinds=("database_record",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def _author_contract(world: _World, contract: CaptureContractV1) -> None:
    draft = world.pb.changes(rationale="Define the orders capture.")
    draft.capture_contract(contract)
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    proposal_id = submitted._candidate_status.proposal_id
    assert proposal_id is not None
    world.approve(proposal_id)
    world.pb.refresh()


CONTRACT_PATH = "capture-contracts/acme.orders-v1.json"


def test_release_lineage_moves_owned_pins_and_leaves_literal_values_alone(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    first = _contract("acme.orders-v1")
    _author_contract(publisher, first)
    revised = _contract(
        "acme.orders-v1", max_rows=5, predecessor=capture_contract_digest(first).tagged
    )
    _author_contract(publisher, revised)
    local_digest = capture_contract_digest(revised).tagged
    publisher.author(
        _pinning_type("acme.orders.status", revised),
        # A literal value that happens to equal the contract digest is data, not a pin.
        _claim_type("acme.orders.zlimit", {"type": "string", "const": local_digest}),
    )

    release = publisher.build("1.0.0")

    released = release.manifest.digests()[CONTRACT_PATH]
    assert released != local_digest
    status = parse_claim_type(
        release.contents()["claim-types/acme.orders/status.json"],
        path="claim-types/acme.orders/status.json",
    )
    assert status.evidence_admission_policy.rules[0].capture_contract_digests == (released,)
    zlimit = parse_claim_type(
        release.contents()["claim-types/acme.orders/zlimit.json"],
        path="claim-types/acme.orders/zlimit.json",
    )
    assert zlimit.literal_schema == {"type": "string", "const": local_digest}
    consumer.add(release)
    for path, content in release.contents().items():
        assert consumer.tree()[path] == content


def test_a_kit_never_replaces_or_retires_a_definition_it_only_carries(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    contract = _contract("acme.orders-v1")
    _author_contract(publisher, contract)
    publisher.author(_pinning_type("beta.orders.status", contract))
    acme = publisher.build("1.0.0")
    beta = playbill_api.playbill_kit_build(
        publisher.instance_id,
        PlaybillKitBuildRequestV1(kit_id="beta", version="1.0.0", owns=("beta.",)),
    ).bundle
    assert CONTRACT_PATH in beta.manifest.digests()

    consumer.add(acme)
    added = consumer.add(beta)
    assert (CONTRACT_PATH, "unchanged") in {(item.path, item.action) for item in added.plan}

    # A release of beta that carries a different acme contract is refused for that
    # path rather than replacing the definition acme owns.
    revised = _contract("acme.orders-v1", max_rows=9)
    content = render_capture_contract(revised)
    artifacts = tuple(
        item
        if item.path != CONTRACT_PATH
        else KitArtifactV1(
            path=CONTRACT_PATH, artifact_digest=capture_contract_digest(revised).tagged
        )
        for item in beta.manifest.artifacts
    )
    forged = KitBundleV1(
        manifest=beta.manifest.model_copy(update={"version": "1.1.0", "artifacts": artifacts}),
        artifacts=tuple(
            item if item.path != CONTRACT_PATH else KitArtifactBytesV1.of(CONTRACT_PATH, content)
            for item in beta.artifacts
        ),
    )
    refused = consumer.add(forged)
    assert refused.status == "blocked"
    assert (CONTRACT_PATH, "conflict") in {(item.path, item.action) for item in refused.plan}

    removed = playbill_api.playbill_kit_remove(
        consumer.instance_id, PlaybillKitRemoveRequestV1(kit_id="beta")
    )
    assert {item.path for item in removed.plan} == {"claim-types/beta.orders/status.json"}


def test_a_carried_definition_matches_by_content_and_pins_move_to_the_consumers_digest(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    first = _contract("acme.orders-v1")
    _author_contract(publisher, first)
    consumer.add(publisher.build("1.0.0"))
    revised = _contract(
        "acme.orders-v1", max_rows=5, predecessor=capture_contract_digest(first).tagged
    )
    _author_contract(publisher, revised)
    consumer.add(publisher.build("1.1.0"))
    consumer_contract = consumer.tree()[CONTRACT_PATH]
    publisher.author(_pinning_type("beta.orders.status", revised))

    beta = publisher.build("1.0.0", owns=("beta.",), kit_id="beta")
    added = consumer.add(beta)

    # The consumer's contract has its own history, so its digest differs from the
    # release's snapshot; content matches, and beta's pin moves to what it holds.
    assert beta.contents()[CONTRACT_PATH] != consumer_contract
    assert (CONTRACT_PATH, "unchanged") in {(item.path, item.action) for item in added.plan}
    status = consumer.claim_type("beta.orders.status")
    held = parse_capture_contract(consumer_contract, path=CONTRACT_PATH)
    assert status.evidence_admission_policy.rules[0].capture_contract_digests == (
        capture_contract_digest(held).tagged,
    )


def test_an_ordinary_document_named_like_a_receipt_does_not_break_kits(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    stored = playbill_api.playbill_store_body(
        consumer.instance_id, content_base64=base64.b64encode(b"# notes\n").decode("ascii")
    )
    shell = DocumentShell(
        identity="document:kit-notes",
        document_kind="note",
        title="Notes",
        media_type="text/markdown",
        body_digest=stored.digest,
        governance_scope=("notes",),
        lifecycle=DocumentLifecycle(revision=1),
    )
    proposed = playbill_api.playbill_propose_document(
        consumer.instance_id, shell=shell, proposal_name="kit-notes"
    )
    consumer.activate(proposed.proposal["admission"]["proposal_id"])

    assert playbill_api.playbill_kit_status(consumer.instance_id).kits == ()
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0"))
    assert [kit.kit_id for kit in playbill_api.playbill_kit_status(consumer.instance_id).kits] == [
        "acme"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "claim-types/../../victim.json",
        "claim-types/acme/../../../victim.json",
        "claim-types//acme.json",
        "claim-types/./acme.json",
        "claim-types\\acme.json",
        "governance/approval-policy.json",
    ],
)
def test_a_kit_path_must_be_a_canonical_path_inside_a_kit_family(path: str) -> None:
    with pytest.raises(ValueError):
        KitArtifactV1(path=path, artifact_digest="sha256:" + "a" * 64)
    with pytest.raises(ValueError):
        KitArtifactBytesV1.of(path, b"{}\n")


def _query(name: str, predicates: tuple[str, ...]) -> QueryDefinitionInput:
    return QueryDefinitionInput(
        kind="query_definition",
        query_definition=QueryDefinitionSpecV1(
            identity=ArtifactIdentity(kind="QueryDefinition", name=name),
            description="Accounts with their seats and plan.",
            entry=QueryEntryV1(binding="item", subject_kinds=("acme.account",)),
            result_binding="item",
            result_shape="subject",
            result_cardinality="many",
            dedupe="subject",
            projection=QueryProjectionV1(
                fields=tuple(
                    QueryProjectionFieldV1(
                        name=predicate.rpartition(".")[2],
                        value=QueryClaimValueRefV1(binding="item", predicate=predicate),
                    )
                    for predicate in predicates
                )
            ),
            evaluation_policy=QueryEvaluationPolicyV1(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgetsV1(max_results=100, max_traversal_depth=0),
            maximum_budgets=QueryBudgetsV1(max_results=1000, max_traversal_depth=0),
        ),
    )


def _author_query(world: _World, definition: QueryDefinitionInput) -> None:
    draft = world.pb.changes(rationale="Read accounts.")
    draft.query_definition(definition)
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()


def test_a_local_definition_pinning_two_changed_types_takes_one_successor(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    _author_query(consumer, _query("local.accounts", (PLAN, SEATS)))
    query_path = "query-definitions/local.accounts.json"
    accepted_query = consumer.tree()[query_path]

    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    publisher.succeed(PLAN, {"type": "string", "minLength": 1})
    upgraded = consumer.add(publisher.build("1.1.0"))

    assert (query_path, "carry") in {(item.path, item.action) for item in upgraded.plan}
    query = parse_query_definition(consumer.tree()[query_path], path=query_path)
    accepted = parse_query_definition(accepted_query, path=query_path)
    assert query.lifecycle.predecessor_digest == query_definition_digest(accepted).tagged
    pinned = {pin.artifact_digest for pin in query.pins}
    assert claim_type_digest(consumer.claim_type(SEATS)).tagged in pinned
    assert claim_type_digest(consumer.claim_type(PLAN)).tagged in pinned


def test_a_kit_dependent_lands_as_the_kit_wrote_it_when_its_type_changes(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    _author_query(publisher, _query("acme.accounts", (SEATS,)))
    consumer.add(publisher.build("1.0.0"))
    query_path = "query-definitions/acme.accounts.json"
    installed_query = consumer.tree()[query_path]

    publisher.succeed(
        SEATS,
        {"type": "integer", "minimum": 0},
        dependents=(
            ClaimTypeSuccessionDependentV1(
                identity=ArtifactIdentity(kind="QueryDefinition", name="acme.accounts"),
                disposition="successor",
            ),
        ),
    )
    upgraded = consumer.add(publisher.build("1.1.0"))

    actions = {(item.path, item.action) for item in upgraded.plan}
    assert (query_path, "replace") in actions
    assert (query_path, "carry") not in actions
    query = parse_query_definition(consumer.tree()[query_path], path=query_path)
    installed = parse_query_definition(installed_query, path=query_path)
    assert query.lifecycle.predecessor_digest == query_definition_digest(installed).tagged
    assert {pin.artifact_digest for pin in query.pins} == {
        claim_type_digest(consumer.claim_type(SEATS)).tagged
    }


def _digest_fields(
    model: type[BaseModel],
    prefix: tuple[str, ...] = (),
    seen: frozenset[type[BaseModel]] = frozenset(),
) -> set[tuple[str, ...]]:
    """Every *_digest field path; a model already on the path is not re-entered."""

    found: set[tuple[str, ...]] = set()
    if model in seen:
        return found
    for name, field in model.model_fields.items():
        path = (*prefix, name)
        if name.endswith(("_digest", "_digests")) and name != "predecessor_digest":
            found.add(path)
        stack = [field.annotation]
        while stack:
            annotation = stack.pop()
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                found |= _digest_fields(annotation, path, seen | {model})
            stack.extend(typing.get_args(annotation))
    return found


@pytest.mark.parametrize(
    ("prefix", "model"),
    [
        ("capture-contracts/", CaptureContractV1),
        ("claim-types/", ClaimType),
        ("query-definitions/", QueryDefinitionV1),
    ],
)
def test_the_reference_table_names_every_digest_field_of_each_kit_family(
    prefix: str, model: type[BaseModel]
) -> None:
    table = {tuple(step for step in steps if step != "*") for steps in REFERENCE_FIELDS[prefix]}
    assert table == _digest_fields(model)


@pytest.mark.parametrize(
    ("prefix", "model"), [("claims/", ClaimArtifactV3), ("documents/", DocumentShell)]
)
def test_state_reference_fields_are_real_digest_fields(prefix: str, model: type[BaseModel]) -> None:
    # Claims and Documents also hold content digests, which are never references.
    table = {tuple(step for step in steps if step != "*") for steps in REFERENCE_FIELDS[prefix]}
    assert table <= _digest_fields(model)


def test_a_local_type_pinning_a_changed_contract_keeps_its_literals_and_its_claims_follow(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    contract = _contract("acme.orders-v1")
    _author_contract(publisher, contract)
    consumer.add(publisher.build("1.0.0"))
    held = parse_capture_contract(consumer.tree()[CONTRACT_PATH], path=CONTRACT_PATH)
    held_digest = capture_contract_digest(held).tagged
    local = _claim_type("local.order.source", {"type": "string", "const": held_digest})
    local = local.model_copy(
        update={
            "pins": (
                ArtifactPin(
                    role="capture-contract",
                    target=ArtifactIdentity(kind="CaptureContract", name="acme.orders-v1"),
                    artifact_digest=held_digest,
                ),
            )
        }
    )
    consumer.author(local)
    draft = consumer.pb.changes(rationale="Record where one order came from.")
    draft.subject(
        SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name="local.order/one"),
            subject_kind="local.order",
            subject_id="one",
            lifecycle=ArtifactLifecycle(),
        )
    )
    draft.claim(
        subject="local.order/one",
        predicate="local.order.source",
        value=held_digest,
        role="observation",
        rationale="The order names its contract.",
        supported_by=None,
        copied_from=None,
        self_source=f"source: {held_digest}\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    consumer.approve(submitted._candidate_status.proposal_id)
    claim_path = next(path for path in consumer.tree() if path.startswith("claims/"))
    claim_id = parse_claim(consumer.tree()[claim_path], path=claim_path).identity

    _author_contract(
        publisher,
        _contract(
            "acme.orders-v1", max_rows=5, predecessor=capture_contract_digest(contract).tagged
        ),
    )
    result = playbill_api.playbill_kit_add(
        consumer.instance_id,
        PlaybillKitAddRequestV1(
            bundle=publisher.build("1.1.0"),
            source="test",
            dependents=(
                ClaimTypeSuccessionDependentV1(
                    identity=claim_id,
                    disposition="retire",
                    claim_retirement_reason="was-rescinded",
                ),
            ),
        ),
    )
    consumer.settle(result)

    successor = consumer.claim_type("local.order.source")
    new_contract = parse_capture_contract(consumer.tree()[CONTRACT_PATH], path=CONTRACT_PATH)
    assert successor.pins[0].artifact_digest == capture_contract_digest(new_contract).tagged
    # The constant is a value that happened to equal the old digest; it stays.
    assert successor.literal_schema == {"type": "string", "const": held_digest}
    retired = parse_claim(consumer.tree()[claim_path], path=claim_path)
    assert retired.lifecycle.state == "retired"
    assert retired.statement.claim_type_digest == claim_type_digest(successor).tagged
    assert retired.statement.object.value == held_digest
