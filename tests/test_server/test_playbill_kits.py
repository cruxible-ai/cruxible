"""Kits: export owned definitions as a release, import it as one governed change set."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Playbill
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, parse_claim_type
from cruxible_client.contracts.kits import (
    KitBundleV1,
    PlaybillKitAddRequestV1,
    PlaybillKitBuildRequestV1,
    PlaybillKitChangeResultV1,
    PlaybillKitRemoveRequestV1,
)
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.kits import read_kit_directory, write_kit_directory
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.ledger.signing import LocalEd25519ApprovalSigner
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.credentials import reset_runtime_credential_store
from cruxible_core.server.registry import get_registry, reset_registry

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

    def author(self, *definitions: ClaimType, successions: tuple[ClaimType, ...] = ()) -> None:
        draft = self.pb.changes(rationale="Shape the account vocabulary.")
        for definition in definitions:
            draft.claim_type(definition)
        for successor in successions:
            draft.succeed_claim_type(successor)
        intent = draft.prepare()
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit()
        assert submitted._candidate_status is not None
        proposal_id = submitted._candidate_status.proposal_id
        assert proposal_id is not None
        self.approve(proposal_id)
        self.pb.refresh()

    def succeed(self, predicate: str, schema: dict[str, object]) -> None:
        current = self.claim_type(predicate)
        successor = current.model_copy(
            update={
                "literal_schema": schema,
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=claim_type_digest(current).tagged
                ),
            }
        )
        self.author(successions=(successor,))

    def build(
        self, version: str, previous: KitBundleV1 | None = None, owns: tuple[str, ...] = ("acme.",)
    ) -> KitBundleV1:
        return playbill_api.playbill_kit_build(
            self.instance_id,
            PlaybillKitBuildRequestV1(kit_id="acme", version=version, owns=owns, previous=previous),
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


def test_an_upgrade_names_the_previous_release_not_the_publishers_history(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    first = publisher.build("1.0.0")
    consumer.add(first)

    # Two revisions between releases; the release must descend from 1.0.0 directly.
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    publisher.succeed(SEATS, {"type": "integer", "minimum": 1})
    publisher.author(_claim_type(OWNER, {"type": "string"}))
    second = publisher.build("1.1.0", previous=first)

    assert second.manifest.previous is not None
    assert second.manifest.previous.content_digest == first.manifest.content_digest
    assert second.contents()[_path(PLAN)] == first.contents()[_path(PLAN)]
    seats = parse_claim_type(second.contents()[_path(SEATS)], path=_path(SEATS))
    assert seats.lifecycle.predecessor_digest == first.manifest.digests()[_path(SEATS)]

    upgraded = consumer.add(second)

    assert {(item.path, item.action) for item in upgraded.plan} == {
        (_path(OWNER), "add"),
        (_path(PLAN), "unchanged"),
        (_path(SEATS), "replace"),
    }
    tree = consumer.tree()
    for path, content in second.contents().items():
        assert tree[path] == content


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
    second = publisher.build("1.1.0", previous=first)
    blocked = consumer.add(second)

    assert blocked.status == "blocked"
    assert blocked.proposal_id is None
    assert [(item.path, item.action) for item in blocked.plan] == [
        (_path(PLAN), "conflict"),
        (_path(SEATS), "unchanged"),
    ]


def test_skipping_a_release_and_overlapping_ownership_are_refused(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    first = publisher.build("1.0.0")
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    second = publisher.build("1.1.0", previous=first)
    publisher.succeed(SEATS, {"type": "integer", "minimum": 1})
    third = publisher.build("1.2.0", previous=second)

    consumer.add(first)
    skipped = consumer.add(third)
    assert skipped.status == "blocked"
    assert "one release at a time" in (skipped.detail or "")

    other = playbill_api.playbill_kit_build(
        publisher.instance_id,
        PlaybillKitBuildRequestV1(kit_id="acme-extra", version="1.0.0", owns=("acme.account.",)),
    ).bundle
    overlapping = consumer.add(other)
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
