"""Kits: export owned definitions as a release, import it as one governed change set."""

from __future__ import annotations

import base64
import json
import typing
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from cruxible_client import Cruxible
from cruxible_client.authoring.sdk import ChangeSetDraft
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
    ArtifactRef,
)
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.authoring.inputs import QueryDefinitionInput
from cruxible_client.contracts.authoring.models import ClaimTypeSuccessionDependent
from cruxible_client.contracts.captures import (
    CaptureContract,
    capture_contract_digest,
    parse_capture_contract,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest, parse_claim_type
from cruxible_client.contracts.claims import ClaimArtifact, parse_claim
from cruxible_client.contracts.documents import DocumentLifecycle, DocumentShell
from cruxible_client.contracts.kits import (
    InstalledKit,
    KitAddRequest,
    KitArtifact,
    KitArtifactBytes,
    KitBuildRequest,
    KitBundle,
    KitChangeResult,
    KitRemoveRequest,
    KitStatus,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.query.definitions import (
    QueryDefinition,
    QueryDefinitionSpec,
    QueryEvaluationPolicy,
    parse_query_definition,
)
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimValueRef,
    QueryEntry,
    QueryProjection,
    QueryProjectionField,
)
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
from tests.core_support._subject_kind_support import declare_by_slug
from tests.core_support._support import build_inputs, restamp_state_root
from tests.core_support._world_templates import TEMPLATES, copy_template
from tests.support.claim_types import claim_type as build_claim_type
from tests.support.claim_types import claim_type_changes

SEATS = "acme.account.seats"
PLAN = "acme.account.plan"
OWNER = "acme.account.owner"
UNOWNED = "other.thing.flag"


def _installed(status: KitStatus) -> tuple[InstalledKit, ...]:
    """The kits these tests add: every instance also holds creation's base kit."""

    return tuple(kit for kit in status.kits if kit.kit_id != "base")


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
        self.pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)

    def tree(self) -> dict[str, bytes]:
        instance = get_playbill_manager().get(self.instance_id)
        return dict(instance.immutable_tree_at(instance.accepted_coordinate().git_oid))

    def claim_type(self, predicate: str) -> ClaimType:
        path = _path(predicate)
        return parse_claim_type(self.tree()[path], path=path)

    def activate(self, proposal_id: str) -> None:
        base = f"/api/v1/{self.instance_id}/proposals/{proposal_id}"
        activated = self.http.post(f"{base}/activate")
        assert activated.status_code == 200, activated.text
        assert activated.json()["status"] == "accepted", activated.text

    def settle(self, result: KitChangeResult) -> None:
        if result.status == "accepted":
            # The policy required no approval, so the change landed at once.
            return
        assert result.status == "proposed" and result.proposal_id is not None, result
        if result.approval_required:
            self.approve(result.proposal_id)
        else:
            self.activate(result.proposal_id)

    def approve(self, proposal_id: str) -> None:
        base = f"/api/v1/{self.instance_id}/proposals/{proposal_id}"
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
        # The approval that completes the candidate activates it.
        assert approved.json()["activation"]["status"] == "accepted", approved.text

    def author(
        self,
        *definitions: ClaimType,
        successions: tuple[ClaimType, ...] = (),
        dependents: tuple[ClaimTypeSuccessionDependent, ...] = (),
    ) -> None:
        draft = self.pb.changes(rationale="Shape the account vocabulary.")
        for definition in definitions:
            draft.claim_type(definition)
        for successor in successions:
            draft.succeed_claim_type(successor, dependents=dependents)
        intent = draft.prepare()
        assert not intent.refused, intent.diagnostics
        submitted = intent.submit(accept="never")
        assert submitted._candidate_status is not None
        proposal_id = submitted._candidate_status.proposal_id
        assert proposal_id is not None
        self.approve(proposal_id)
        self.pb.refresh()

    def succeed(
        self,
        predicate: str,
        schema: dict[str, object],
        dependents: tuple[ClaimTypeSuccessionDependent, ...] = (),
    ) -> None:
        current = self.claim_type(predicate)
        successor = current.model_copy(
            update=claim_type_changes(
                value_type=schema,
                lifecycle=ArtifactLifecycle(predecessor_digest=claim_type_digest(current).tagged),
            )
        )
        self.author(successions=(successor,), dependents=dependents)

    def build(
        self, version: str, owns: tuple[str, ...] = ("acme.",), kit_id: str = "acme"
    ) -> KitBundle:
        return playbill_api.playbill_kit_build(
            self.instance_id,
            KitBuildRequest(kit_id=kit_id, version=version, owns=owns),
        ).bundle

    def add(self, bundle: KitBundle, **options: object) -> KitChangeResult:
        before = self.tree()
        result = playbill_api.playbill_kit_add(
            self.instance_id,
            KitAddRequest(bundle=bundle, source="test", dry_run=False, **options),  # type: ignore[arg-type]
        )
        if result.status == "proposed":
            # Proposing lands nothing; the ordinary activation does.
            assert self.tree() == before
            self.settle(result)
        return result


def _kit_preview(world, request):
    return playbill_api.playbill_kit_add(world.instance_id, request)


def _claim_type(predicate: str, schema: dict[str, object]) -> ClaimType:
    kind = predicate.rpartition(".")[0]
    return build_claim_type(predicate=predicate, value_type=schema, allowed_subject_kinds=(kind,))


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    (workspace / ".cruxible").mkdir(parents=True)
    (workspace / ".cruxible" / "files.yaml").write_text(
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


def _fresh_open_worlds(
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
                f"/api/v1/{instance_id}/init",
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


def _build_worlds(root: Path, independent: bool) -> tuple[tuple[str, Path, Path], ...]:
    """Open the publisher/consumer pair under ``root`` as the fixture does, then stop."""

    with pytest.MonkeyPatch.context() as build:
        opened = _fresh_open_worlds(root, build, independent=independent)
        pair = next(opened)
        for _ in opened:
            pass
    return tuple(
        (
            world.instance_id,
            world.reviewer_key.relative_to(root),
            Path(world.pb._workspace).relative_to(root),
        )
        for world in pair
    )


def _open_worlds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, independent: bool
) -> Iterator[tuple[_World, _World]]:
    """The pair, copied from this process's template pair when one applies."""

    template = TEMPLATES.template(
        ("kit_worlds", independent, build_inputs()), lambda root: _build_worlds(root, independent)
    )
    copied = None if template is None else copy_template(template, tmp_path)
    if template is None or copied is None:
        yield from _fresh_open_worlds(tmp_path, monkeypatch, independent=independent)
        return
    TEMPLATES.copies += 1
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    reset_permissions()
    reset_registry()
    reset_runtime_credential_store()
    get_playbill_manager().clear()
    restamp_state_root(tmp_path / "server-state")
    with TestClient(create_app()) as client:
        publisher, consumer = (
            _World(client, instance_id, tmp_path / reviewer_key, tmp_path / workspace)
            for instance_id, reviewer_key, workspace in template.value
        )
        yield publisher, consumer
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
    # No approval is required here, so the install lands at once.
    assert added.status == "accepted"
    assert added.transition == "install"
    assert added.provenance is not None
    assert added.provenance.instance_id == publisher.instance_id
    assert {item.action for item in added.plan} == {"add"}
    tree = consumer.tree()
    for path, content in release.contents().items():
        assert tree[path] == content
    status = playbill_api.playbill_kit_status(consumer.instance_id)
    assert [(kit.kit_id, kit.version, kit.drifted) for kit in _installed(status)] == [
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
    assert seats.value.kind == "int" and seats.value.min == 1
    assert consumer.tree()[_path(OWNER)] == second.contents()[_path(OWNER)]
    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id))[0].drifted == ()


def test_a_later_release_installs_fresh_on_its_own(worlds: tuple[_World, _World]) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    publisher.build("1.0.0")
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    later = publisher.build("1.1.0")

    added = consumer.add(later)

    assert [(item.path, item.action) for item in added.plan] == [(_path(SEATS), "add")]
    assert consumer.tree()[_path(SEATS)] == later.contents()[_path(SEATS)]


def _create_by_slug(world: _World, draft: ChangeSetDraft, kind: str, slug: str) -> typing.Any:
    """Declare ``kind`` keyed by its slug and create ``kind/<slug>`` in ``draft``.

    Returns the creation's pending reference, which core resolves to the path it
    assigns (``kind/<slug>``).
    """

    declare_by_slug(draft, kind)
    created = draft.subject(world.pb.new_subject(kind, key=slug))
    assert created is not None and created.creation is not None
    return created


def _claim_paths(world: _World, predicate: str) -> list[str]:
    """The world's Claim paths about ``predicate`` (each Subject's key Claim aside)."""

    tree = world.tree()
    return [
        path
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == predicate
    ]


@pytest.mark.parametrize(
    ("predicate", "schema", "value", "successor_schema"),
    [
        (SEATS, {"type": "integer"}, 50, {"type": "integer", "minimum": 0}),
        (
            "acme.account.status",
            {"type": "string"},
            "ready",
            {"type": "string", "enum": ["ready", "done"]},
        ),
    ],
)
def test_changing_a_claim_type_carries_its_live_claims_as_a_succession_would(
    worlds: tuple[_World, _World],
    predicate: str,
    schema: dict[str, object],
    value: object,
    successor_schema: dict[str, object],
) -> None:
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.floor.floor import service_export_playbill_floor
    from cruxible_core.storage.cas import BodyAccessContext

    publisher, consumer = worlds
    publisher.author(_claim_type(predicate, schema))
    consumer.add(publisher.build("1.0.0"))
    draft = consumer.pb.changes(rationale="Record Acme's account value.")
    created = _create_by_slug(consumer, draft, "acme.account", "acme")
    draft.claim(
        subject=created,
        predicate=predicate,
        value=value,
        role="observation",
        rationale="The source records this account value.",
        supported_by=None,
        copied_from=None,
        self_source=f"{predicate}: {value}\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    consumer.approve(submitted._candidate_status.proposal_id)
    claim_paths = _claim_paths(consumer, predicate)
    assert len(claim_paths) == 1
    before = parse_claim(consumer.tree()[claim_paths[0]], path=claim_paths[0])

    publisher.succeed(predicate, successor_schema)
    upgraded = consumer.add(publisher.build("1.1.0"))

    # Dependents are counted, not listed, and carried to the replacement.
    (seats,) = (item for item in upgraded.plan if item.path == _path(predicate))
    assert (seats.action, seats.dependent_count) == ("replace", 1)
    assert claim_paths[0] not in {item.path for item in upgraded.plan}
    carried = consumer.tree()[claim_paths[0]]
    assert claim_type_digest(consumer.claim_type(predicate)).tagged.encode("ascii") in carried

    # A carry keeps the Claim's backing, but this revision's reason is the kit
    # change's, resolved by the same accepting admission for get and the floor.
    instance = get_playbill_manager().get(consumer.instance_id)
    claim = parse_claim(carried, path=claim_paths[0])
    assert claim.statement.object == before.statement.object
    assert claim.backing == before.backing
    evidence = service_playbill_get(
        instance,
        request=GetRequest(ref=claim.identity.qualified, detail="evidence"),
        access=BodyAccessContext(principal_id="operator", can_read_body=True),
    ).evidence
    assert evidence is not None
    assert evidence.rationale == "Upgrade kit acme from 1.0.0 to 1.1.0."
    files = service_export_playbill_floor(instance)
    with instance.accepted_history_reader() as history:
        change = json.loads(files[f"changes/{history.sequence:020d}.json"])
    assert change["rationale"] == [evidence.rationale]


def test_a_kit_upgrade_carries_a_replace_claim_types_claims_with_their_backing(
    worlds: tuple[_World, _World],
) -> None:
    """The distro's rule b: a carry is not a revision, whatever the ClaimType says."""

    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.policies import ClaimEvidenceAdmissionPolicy

    publisher, consumer = worlds
    replacing = _claim_type(SEATS, {"type": "integer"}).model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(),
            "evidence_requirement": "self",
            "revision_evidence": "replace",
            "default_role": "observation",
        }
    )
    publisher.author(ClaimType.model_validate(replacing.model_dump(mode="python")))
    consumer.add(publisher.build("1.0.0"))
    draft = consumer.pb.changes(rationale="Record Acme's seats.")
    created = _create_by_slug(consumer, draft, "acme.account", "acme")
    draft.claim(
        subject=created,
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
    submitted = intent.submit(accept="never")
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    consumer.approve(submitted._candidate_status.proposal_id)
    (claim_path,) = _claim_paths(consumer, SEATS)
    before = parse_claim(consumer.tree()[claim_path], path=claim_path)

    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    upgraded = consumer.add(publisher.build("1.1.0"))

    (seats,) = (item for item in upgraded.plan if item.path == _path(SEATS))
    assert seats.dependent_count == 1
    carried = parse_claim(consumer.tree()[claim_path], path=claim_path)
    assert (
        carried.statement.claim_type_digest == claim_type_digest(consumer.claim_type(SEATS)).tagged
    )
    assert canonical_bytes(carried.backing.model_dump(mode="json")) == canonical_bytes(
        before.backing.model_dump(mode="json")
    )


def test_a_local_edit_takes_the_release_by_default_and_says_it_overwrites_your_edit(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    consumer.succeed(PLAN, {"type": "string", "minLength": 1})
    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id))[0].drifted == (
        _path(PLAN),
    )

    publisher.succeed(PLAN, {"type": "string", "maxLength": 64})
    upgraded = consumer.add(publisher.build("1.1.0"))

    assert upgraded.status == "accepted"
    assert upgraded.transition == "upgrade" and upgraded.installed_version == "1.0.0"
    assert [(item.path, item.action, item.consequence) for item in upgraded.plan] == [
        (_path(PLAN), "replace", "overwrites_your_edit"),
        (_path(SEATS), "unchanged", None),
    ]
    assert consumer.claim_type(PLAN).value.max_len == 64
    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id))[0].drifted == ()


def test_keeping_a_local_edit_records_it_so_the_next_upgrade_does_not_ask_again(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    consumer.succeed(PLAN, {"type": "string", "minLength": 1})
    publisher.succeed(PLAN, {"type": "string", "maxLength": 64})
    release = publisher.build("1.1.0")

    kept = consumer.add(release, keep_local_edits=True)

    assert [(item.path, item.action, item.consequence) for item in kept.plan] == [
        (_path(PLAN), "keep", "overwrites_your_edit"),
        (_path(SEATS), "unchanged", None),
    ]
    assert consumer.claim_type(PLAN).value.min_len == 1
    (installed,) = _installed(playbill_api.playbill_kit_status(consumer.instance_id))
    assert installed.drifted == ()
    assert [(item.identity, item.consequence) for item in installed.kept] == [
        (f"ClaimType:{PLAN}", "overwrites_your_edit")
    ]
    # A later release that leaves that definition as it was does not ask again.
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    later = consumer.add(publisher.build("1.2.0"))
    assert {(item.path, item.action) for item in later.plan} == {
        (_path(PLAN), "keep"),
        (_path(SEATS), "replace"),
    }
    assert consumer.claim_type(PLAN).value.min_len == 1


def test_reverting_a_local_edit_is_no_longer_an_edit(worlds: tuple[_World, _World]) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0"))
    consumer.succeed(SEATS, {"type": "integer", "minimum": 5})
    consumer.succeed(SEATS, {"type": "integer"})

    # A revert is a new successor with a new digest; content says it is unedited.
    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id))[0].drifted == ()
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    upgraded = consumer.add(publisher.build("1.1.0"))
    assert [(item.action, item.consequence) for item in upgraded.plan] == [("replace", None)]


def test_a_downgrade_needs_allow_downgrade(worlds: tuple[_World, _World]) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    older = publisher.build("1.0.0")
    publisher.succeed(SEATS, {"type": "integer", "minimum": 0})
    consumer.add(publisher.build("1.1.0"))

    refused = consumer.add(older)
    assert (refused.status, refused.transition) == ("blocked", "downgrade")
    assert "older than installed 1.1.0" in (refused.detail or "")

    downgraded = consumer.add(older, allow_downgrade=True)
    assert (downgraded.status, downgraded.transition) == ("accepted", "downgrade")
    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id))[0].version == "1.0.0"


def test_a_dropped_definition_retires_unless_kept_or_depended_on(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    _author_query(consumer, _query("local.accounts", (SEATS,)))
    publisher.author(_claim_type(OWNER, {"type": "string"}))
    full = publisher.build("2.0.0")
    # The 2.0.0 release drops SEATS and PLAN and keeps OWNER.
    kept_paths = {_path(OWNER)}
    dropped = KitBundle(
        manifest=full.manifest.model_copy(
            update={
                "artifacts": tuple(
                    item for item in full.manifest.artifacts if item.path in kept_paths
                )
            }
        ),
        artifacts=tuple(item for item in full.artifacts if item.path in kept_paths),
    )

    preview = playbill_api.playbill_kit_add(
        consumer.instance_id, KitAddRequest(bundle=dropped, source="test")
    )
    by_path = {item.path: item for item in preview.plan}
    # Nothing depends on PLAN: it retires. A local query depends on SEATS: kept.
    assert (by_path[_path(PLAN)].action, by_path[_path(PLAN)].consequence) == (
        "retire",
        "release_dropped",
    )
    assert (by_path[_path(SEATS)].action, by_path[_path(SEATS)].dependent_count) == ("keep", 1)

    retired = consumer.add(dropped, retire_dependents=(f"ClaimType:{SEATS}",))
    assert {(item.path, item.action) for item in retired.plan} >= {
        (_path(SEATS), "retire"),
        (_path(PLAN), "retire"),
        (_path(OWNER), "add"),
    }
    assert consumer.claim_type(SEATS).lifecycle.state == "retired"
    query = parse_query_definition(
        consumer.tree()["query-definitions/local.accounts.json"],
        path="query-definitions/local.accounts.json",
    )
    assert query.lifecycle.state == "retired"


def _without(bundle: KitBundle, *predicates: str) -> KitBundle:
    dropped = {_path(predicate) for predicate in predicates}
    return KitBundle(
        manifest=bundle.manifest.model_copy(
            update={
                "artifacts": tuple(
                    item for item in bundle.manifest.artifacts if item.path not in dropped
                )
            }
        ),
        artifacts=tuple(item for item in bundle.artifacts if item.path not in dropped),
    )


def _query_state(world: _World, name: str) -> str:
    path = f"query-definitions/{name}.json"
    return parse_query_definition(world.tree()[path], path=path).lifecycle.state


def test_retiring_dependents_follows_the_dropped_definitions_whole_closure(
    worlds: tuple[_World, _World],
) -> None:
    """F-001: a dependent also reached through a replaced definition still retires."""

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    # Discovered first through PLAN (replaced), but it also pins SEATS (dropped).
    _author_query(consumer, _query("local.accounts", (PLAN, SEATS)))
    publisher.succeed(PLAN, {"type": "string", "minLength": 1})
    release = _without(publisher.build("2.0.0"), SEATS)

    result = consumer.add(release, retire_dependents=(f"ClaimType:{SEATS}",))

    assert result.status == "accepted", result.detail
    by_identity = {item.identity: item for item in result.plan}
    assert by_identity[f"ClaimType:{SEATS}"].action == "retire"
    assert by_identity[f"ClaimType:{SEATS}"].dependent_count == 1
    # The query names PLAN, so replacing PLAN leaves it nothing to restate.
    assert by_identity[f"ClaimType:{PLAN}"].dependent_count == 0
    assert consumer.claim_type(SEATS).lifecycle.state == "retired"
    assert _query_state(consumer, "local.accounts") == "retired"


def test_retiring_dependents_reaches_transitive_dependents(
    worlds: tuple[_World, _World],
) -> None:
    """F-001: a dependent of a dependent retires with the dropped root."""

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    # SEATS <- a local type pinning it <- a local query reading that type: the
    # query reaches SEATS only through the type.
    tier = _claim_type("acme.account.tier", {"type": "string"}).model_copy(
        update={
            "pins": (
                ArtifactPin(
                    role="reads",
                    target=ArtifactIdentity(kind="ClaimType", name=SEATS),
                    artifact_digest=claim_type_digest(consumer.claim_type(SEATS)).tagged,
                ),
            )
        }
    )
    consumer.author(tier)
    _author_query(consumer, _query("local.tiers", ("acme.account.tier",)))
    release = _without(publisher.build("2.0.0"), SEATS)

    preview = playbill_api.playbill_kit_add(
        consumer.instance_id, KitAddRequest(bundle=release, source="test")
    )
    seats = next(item for item in preview.plan if item.path == _path(SEATS))
    assert (seats.action, seats.dependent_count) == ("keep", 2)

    retired = consumer.add(release, retire_dependents=(f"ClaimType:{SEATS}",))

    assert retired.status == "accepted", retired.detail
    assert consumer.claim_type("acme.account.tier").lifecycle.state == "retired"
    assert _query_state(consumer, "local.tiers") == "retired"


def test_a_kept_dropped_definition_stays_the_kits_until_retired_later(
    worlds: tuple[_World, _World],
) -> None:
    """F-002: keeping a dropped definition on one install, retiring it on the next."""

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    _author_query(consumer, _query("local.accounts", (SEATS,)))
    release = _without(publisher.build("2.0.0"), SEATS)

    kept = consumer.add(release)
    assert any(item.path == _path(SEATS) and item.action == "keep" for item in kept.plan)
    status = _installed(playbill_api.playbill_kit_status(consumer.instance_id))[0]
    assert status.version == "2.0.0"
    assert any(item.path == _path(SEATS) for item in status.kept)
    # Still the kit's: removing the kit would retire it.
    removal = playbill_api.playbill_kit_remove(
        consumer.instance_id, KitRemoveRequest(kit_id="acme")
    )
    assert (_path(SEATS), "retire") in {(item.path, item.action) for item in removal.plan}

    # The same release again: the retained record keeps the decision.
    again = consumer.add(release)
    assert again.status == "unchanged"

    retired = consumer.add(release, retire_dependents=(f"ClaimType:{SEATS}",))

    assert retired.status == "accepted", retired.detail
    assert consumer.claim_type(SEATS).lifecycle.state == "retired"
    assert _query_state(consumer, "local.accounts") == "retired"
    status = _installed(playbill_api.playbill_kit_status(consumer.instance_id))[0]
    assert not any(item.path == _path(SEATS) for item in status.kept)


def test_a_kept_definition_stays_held_when_a_release_narrows_its_prefix(
    worlds: tuple[_World, _World],
) -> None:
    """F-002: a kit holds what its receipt records, not only what its prefixes name."""

    publisher, consumer = worlds
    flag = "acme.other.flag"
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(flag, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))
    narrowed = _without(publisher.build("2.0.0"), flag)
    narrowed = narrowed.model_copy(
        update={"manifest": narrowed.manifest.model_copy(update={"owns": ("acme.account.",)})}
    )
    kept = consumer.add(narrowed, keep=(f"ClaimType:{flag}",))
    assert kept.status == "accepted", kept.detail

    # Another kit taking that definition is refused, however its prefix reads.
    publisher.succeed(flag, {"type": "string", "minLength": 1})
    competing = consumer.add(publisher.build("1.0.0", kit_id="beta", owns=("acme.other.",)))
    assert competing.status == "blocked", competing
    assert f"ClaimType:{flag} is held by kit acme" in (competing.detail or "")
    (refusal,) = competing.refusals
    assert refusal.code == "cruxible.kit.ownership_conflict"
    assert refusal.our_prefixes == ("acme.other.",)
    assert refusal.owning_prefix is None
    assert refusal.identity == f"ClaimType:{flag}"
    assert refusal.owning_kit_id == "acme"
    assert "cruxible kit remove acme" in refusal.repair
    assert consumer.claim_type(flag).value.max_len == 256

    # The holder still removes it, as an unedited definition it installed.
    removal = playbill_api.playbill_kit_remove(
        consumer.instance_id, KitRemoveRequest(kit_id="acme", dry_run=False)
    )
    assert removal.status in {"accepted", "proposed"}, removal.detail
    if removal.status == "proposed":
        consumer.settle(removal)
    assert (_path(flag), "retire") in {(item.path, item.action) for item in removal.plan}
    assert consumer.claim_type(flag).lifecycle.state == "retired"


def _identity_rule_type(predicate: str, contract: CaptureContract) -> ClaimType:
    return _claim_type(predicate, {"type": "string"}).model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="orders",
                        claim_roles=("observation",),
                        capture_contracts=(
                            ArtifactRef(role="capture-contract", target=contract.identity),
                        ),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            ),
        }
    )


@pytest.mark.parametrize("rule", ["identity", "exact_digest"])
def test_a_dropped_contract_an_evidence_rule_needs_is_kept_or_retired_with_it(
    worlds: tuple[_World, _World], rule: str
) -> None:
    """F-006: evidence-rule consumers are dependents, though no pin names the contract."""

    publisher, consumer = worlds
    contract = _contract("acme.orders-v1")
    _author_contract(publisher, contract)
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0"))
    local = "local.account.status"
    consumer.author(
        _identity_rule_type(local, contract)
        if rule == "identity"
        else _pinning_type(local, contract)
    )
    full = publisher.build("2.0.0")
    release = KitBundle(
        manifest=full.manifest.model_copy(
            update={
                "artifacts": tuple(
                    item for item in full.manifest.artifacts if item.path != CONTRACT_PATH
                )
            }
        ),
        artifacts=tuple(item for item in full.artifacts if item.path != CONTRACT_PATH),
    )
    installed_contract = consumer.tree()[CONTRACT_PATH]

    # Explicit keep, previewed: kept with no question asked.
    explicit = playbill_api.playbill_kit_add(
        consumer.instance_id,
        KitAddRequest(bundle=release, source="test", keep=("CaptureContract:acme.orders-v1",)),
    )
    planned = next(item for item in explicit.plan if item.path == CONTRACT_PATH)
    assert (planned.action, planned.detail) == ("keep", None)

    # By default the contract a live type's evidence rule needs is kept.
    default = consumer.add(release)
    assert default.status == "accepted", default.detail
    planned = next(item for item in default.plan if item.path == CONTRACT_PATH)
    assert (planned.action, planned.dependent_count) == ("keep", 1)
    assert "retire_dependents" in (planned.detail or "")
    assert consumer.tree()[CONTRACT_PATH] == installed_contract

    # Retiring it on request retires the type that needs it, in one change.
    retired = consumer.add(release, retire_dependents=("CaptureContract:acme.orders-v1",))
    assert retired.status == "accepted", retired.detail
    planned = next(item for item in retired.plan if item.path == CONTRACT_PATH)
    assert (planned.action, planned.dependent_count) == ("retire", 1)
    assert consumer.claim_type(local).lifecycle.state == "retired"
    assert (
        parse_capture_contract(consumer.tree()[CONTRACT_PATH], path=CONTRACT_PATH).lifecycle.state
        == "retired"
    )


@pytest.mark.parametrize("owns", [("acme.account.",), ("acme.",)])
@pytest.mark.parametrize("dry_run", [True, False])
def test_overlapping_ownership_is_refused(
    worlds: tuple[_World, _World], owns: tuple[str, ...], dry_run: bool
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))

    before = consumer.tree()
    response = consumer.http.post(
        f"/api/v1/{consumer.instance_id}/kits",
        json=KitAddRequest(
            bundle=publisher.build("1.0.0", owns=owns, kit_id="acme-extra"),
            dry_run=dry_run,
            keep=(f"ClaimType:{SEATS}",),
        ).model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    overlapping = KitChangeResult.model_validate(response.json())
    assert overlapping.status == ("would_block" if dry_run else "blocked")
    assert "owned by kit acme" in (overlapping.detail or "")
    (refusal,) = overlapping.refusals
    assert refusal.code == "cruxible.kit.ownership_conflict"
    assert refusal.our_prefixes == owns
    assert refusal.owning_prefix == "acme."
    assert refusal.identity is None
    assert refusal.owning_kit_id == "acme"
    assert "cruxible kit remove acme" in refusal.repair
    assert "release that does not own the overlapping prefix" in refusal.repair
    assert consumer.tree() == before


def test_a_consumers_own_definition_is_a_takeover_that_keep_preserves(
    worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.author(_claim_type(SEATS, {"type": "integer", "minimum": 1}))
    bundle = publisher.build("1.0.0")
    preview = playbill_api.playbill_kit_add(consumer.instance_id, KitAddRequest(bundle=bundle))
    assert preview.status == "would_propose", preview.detail
    assert preview.refusals == ()
    (item,) = preview.plan
    assert (item.action, item.consequence) == ("replace", "takes_over_outside_definition")

    kept = consumer.add(bundle, keep=(f"ClaimType:{SEATS}",))
    assert kept.refusals == ()
    assert kept.plan[0].action == "keep"
    assert kept.plan[0].consequence == "takes_over_outside_definition"
    assert (
        consumer.claim_type(SEATS).value.kind == "int" and consumer.claim_type(SEATS).value.min == 1
    )


@pytest.mark.parametrize("dry_run", [True, False])
def test_reusing_an_installed_kit_id_with_disjoint_prefixes_is_refused(
    worlds: tuple[_World, _World], dry_run: bool
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0"))
    publisher.author(_claim_type("beta.account.flag", {"type": "string"}))
    unrelated = publisher.build("2.0.0", owns=("beta.",))
    before = consumer.tree()
    response = consumer.http.post(
        f"/api/v1/{consumer.instance_id}/kits",
        json=KitAddRequest(bundle=unrelated, dry_run=dry_run).model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    result = KitChangeResult.model_validate(response.json())
    assert result.status == ("would_block" if dry_run else "blocked")
    (refusal,) = result.refusals
    assert refusal.code == "cruxible.kit.id_reused"
    assert refusal.owning_kit_id == "acme"
    assert refusal.our_prefixes == ("beta.",)
    assert refusal.installed_prefixes == ("acme.",)
    assert "cruxible kit remove acme" in refusal.repair
    assert consumer.tree() == before
    assert result.plan == ()

    # Removing the installed kit is the exit, not a permanent reservation of its id.
    removal = playbill_api.playbill_kit_remove(
        consumer.instance_id, KitRemoveRequest(kit_id="acme", dry_run=False)
    )
    consumer.settle(removal)
    preview = playbill_api.playbill_kit_add(consumer.instance_id, KitAddRequest(bundle=unrelated))
    assert preview.status == "would_propose", preview.detail
    assert preview.refusals == ()


@pytest.mark.parametrize(
    ("installed_owns", "release_owns"),
    [
        (("acme.",), ("acme.",)),
        (("acme.",), ("acme.", "acme.account.")),
        (("acme.",), ("acme.account.",)),
        (("acme.account.",), ("acme.",)),
    ],
)
def test_a_genuine_upgrade_with_related_prefixes_still_plans_normally(
    worlds: tuple[_World, _World],
    installed_owns: tuple[str, ...],
    release_owns: tuple[str, ...],
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0", owns=installed_owns))
    publisher.succeed(SEATS, {"type": "integer", "minimum": 1})
    result = playbill_api.playbill_kit_add(
        consumer.instance_id,
        KitAddRequest(bundle=publisher.build("2.0.0", owns=release_owns)),
    )
    assert result.status == "would_propose", result.detail
    assert result.transition == "upgrade"
    assert result.refusals == ()
    assert (result.plan[0].identity, result.plan[0].action) == (f"ClaimType:{SEATS}", "replace")


def test_removing_a_kit_retires_what_it_installed(worlds: tuple[_World, _World]) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    consumer.add(publisher.build("1.0.0"))

    removed = playbill_api.playbill_kit_remove(
        consumer.instance_id, KitRemoveRequest(kit_id="acme", dry_run=False)
    )
    consumer.settle(removed)

    assert {item.action for item in removed.plan} == {"retire"}
    assert consumer.claim_type(SEATS).lifecycle.state == "retired"
    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id)) == ()


def test_removing_a_kit_that_is_not_installed_is_refused_naming_the_installed_ones(
    worlds: tuple[_World, _World],
) -> None:
    from cruxible_core.errors import RequestRefusedError

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0"))

    with pytest.raises(RequestRefusedError) as refused:
        playbill_api.playbill_kit_remove(consumer.instance_id, KitRemoveRequest(kit_id="nokit"))

    assert refused.value.error_code == "cruxible.kit.not_installed"
    assert "installed: acme" in str(refused.value)


def test_a_kit_needs_the_approval_the_consumer_policy_requires(
    strict_worlds: tuple[_World, _World],
) -> None:
    publisher, consumer = strict_worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    proposed = playbill_api.playbill_kit_add(
        consumer.instance_id,
        KitAddRequest(bundle=publisher.build("1.0.0"), source="test", dry_run=False),
    )

    assert proposed.status == "proposed" and proposed.approval_required
    assert proposed.proposal_id is not None
    refused = consumer.http.post(
        f"/api/v1/{consumer.instance_id}/proposals/{proposed.proposal_id}/activate"
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

    built = transport.build_kit(
        publisher.instance_id,
        KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",)),
    )
    directory = tmp_path / "acme-1.0.0"
    write_kit_directory(built.bundle, directory)
    assert (directory / "cruxible-kit.json").is_file()
    assert (directory / "artifacts" / _path(SEATS)).read_bytes() == built.bundle.contents()[
        _path(SEATS)
    ]
    read_back = read_kit_directory(directory)
    assert read_back == built.bundle

    proposed = transport.add_kit(
        consumer.instance_id,
        KitAddRequest(bundle=read_back, source=directory.name, dry_run=False),
    )
    consumer.settle(proposed)
    status = transport.kit_status(consumer.instance_id)
    assert [(kit.kit_id, kit.source) for kit in _installed(status)] == [("acme", "acme-1.0.0")]

    (directory / "artifacts" / "claim-types" / "stray.json").write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="does not match its manifest"):
        read_kit_directory(directory)


def test_an_unpinned_kit_add_whose_head_moves_after_evaluation_refuses_typed_and_retries(
    worlds: tuple[_World, _World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent acceptance between evaluation and publication of a commit with no
    ``at``: the add refuses ``state_moved`` with the add itself as its repair, publishes
    nothing, and the caller's second add lands."""

    from cruxible_client.contracts.repairs import RepairOperation
    from cruxible_core.proposals.proposals import ProposalService

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    release = publisher.build("1.0.0")
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = consumer.http  # type: ignore[assignment]
    original = ProposalService._evaluate_admission
    moved: list[bool] = []

    def evaluate_then_move(self: ProposalService, **kwargs: typing.Any) -> typing.Any:
        evaluated = original(self, **kwargs)
        if not moved and "/kit-acme-" in kwargs["request"].target_ref:
            moved.append(True)
            consumer.author(_claim_type(UNOWNED, {"type": "boolean"}))
        return evaluated

    monkeypatch.setattr(ProposalService, "_evaluate_admission", evaluate_then_move)
    with pytest.raises(Exception) as refused:
        transport.add_kit(
            consumer.instance_id, KitAddRequest(bundle=release, source="test", dry_run=False)
        )

    assert moved == [True]
    assert getattr(refused.value, "error_code", None) == "cruxible.preview.state_moved"
    assert getattr(refused.value, "repair", None) == RepairOperation(
        operation="cruxible.kit.add", arguments={"dry_run": False}
    )
    proposals = consumer.http.get(f"/api/v1/{consumer.instance_id}/proposals").json()
    assert not [entry for entry in proposals["entries"] if "/kit-acme-" in entry["target_ref"]]
    assert _path(SEATS) not in consumer.tree()
    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id)) == ()
    # The caller runs it again, at the moved head.
    assert consumer.add(release).status == "accepted"
    assert _path(SEATS) in consumer.tree()


def test_a_build_with_nothing_owned_is_refused(worlds: tuple[_World, _World]) -> None:
    publisher, _consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))

    with pytest.raises(DataValidationError, match="no live definitions"):
        publisher.build("1.0.0", owns=("nothing.",))


def _contract(name: str, *, max_rows: int = 4, predecessor: str | None = None) -> CaptureContract:
    from cruxible_client.contracts.capture_contract_inputs import (
        CaptureContractInputRecord,
        lower_capture_contract_input,
    )

    base = lower_capture_contract_input(
        CaptureContractInputRecord(
            name=name, shape="database_snapshot", replay="exact", max_bytes=4096
        ),
        named=True,
    )
    return base.model_copy(
        update={
            "selection_budget": base.selection_budget.model_copy(update={"max_rows": max_rows}),
            "lifecycle": ArtifactLifecycle(predecessor_digest=predecessor),
        }
    )


def _pinning_type(predicate: str, contract: CaptureContract) -> ClaimType:
    return _claim_type(predicate, {"type": "string"}).model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicy(
                rules=(
                    ClaimEvidenceAdmissionRule(
                        rule_id="orders",
                        claim_roles=("observation",),
                        capture_contracts=(contract.identity.name,),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )


def _author_contract(world: _World, contract: CaptureContract) -> None:
    draft = world.pb.changes(rationale="Define the orders capture.")
    draft.capture_contract(contract)
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
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
    # The rule names the contract by identity, so the release moves no digest in it.
    (rule,) = status.evidence_admission_policy.rules
    assert [item.target for item in rule.capture_contracts] == [revised.identity]
    zlimit = parse_claim_type(
        release.contents()["claim-types/acme.orders/zlimit.json"],
        path="claim-types/acme.orders/zlimit.json",
    )
    assert zlimit.value.members == (local_digest,)
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
        KitBuildRequest(kit_id="beta", version="1.0.0", owns=("beta.",)),
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
        else KitArtifact(
            path=CONTRACT_PATH, artifact_digest=capture_contract_digest(revised).tagged
        )
        for item in beta.manifest.artifacts
    )
    forged = KitBundle(
        manifest=beta.manifest.model_copy(update={"version": "1.1.0", "artifacts": artifacts}),
        artifacts=tuple(
            item if item.path != CONTRACT_PATH else KitArtifactBytes.of(CONTRACT_PATH, content)
            for item in beta.artifacts
        ),
    )
    refused = consumer.add(forged)
    assert refused.status == "blocked"
    assert "owned by kit acme" in (refused.detail or "")
    (refusal,) = refused.refusals
    assert refusal.code == "cruxible.kit.ownership_conflict"
    assert refusal.our_prefixes == ("beta.",)
    assert refusal.owning_prefix == "acme."
    assert refusal.identity == "CaptureContract:acme.orders-v1"
    assert refusal.owning_kit_id == "acme"
    assert "cruxible kit remove acme" in refusal.repair

    removed = playbill_api.playbill_kit_remove(
        consumer.instance_id, KitRemoveRequest(kit_id="beta")
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
    # The rule names the contract by identity, which the consumer's own version holds.
    (rule,) = status.evidence_admission_policy.rules
    assert [item.target for item in rule.capture_contracts] == [held.identity]


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
        consumer.instance_id, shell=shell, proposal_name="kit-notes", accept="never"
    )
    consumer.activate(proposed.proposal["admission"]["proposal_id"])

    assert _installed(playbill_api.playbill_kit_status(consumer.instance_id)) == ()
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    consumer.add(publisher.build("1.0.0"))
    assert [
        kit.kit_id for kit in _installed(playbill_api.playbill_kit_status(consumer.instance_id))
    ] == ["acme"]


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
        KitArtifact(path=path, artifact_digest="sha256:" + "a" * 64)
    with pytest.raises(ValueError):
        KitArtifactBytes.of(path, b"{}\n")


def _query(name: str, predicates: tuple[str, ...]) -> QueryDefinitionInput:
    return QueryDefinitionInput(
        kind="query_definition",
        query_definition=QueryDefinitionSpec(
            identity=ArtifactIdentity(kind="QueryDefinition", name=name),
            description="Accounts with their seats and plan.",
            entry=QueryEntry(binding="item", subject_kinds=("acme.account",)),
            result_binding="item",
            result_shape="subject",
            result_cardinality="many",
            dedupe="subject",
            projection=QueryProjection(
                fields=tuple(
                    QueryProjectionField(
                        name=predicate.rpartition(".")[2],
                        value=QueryClaimValueRef(binding="item", predicate=predicate),
                    )
                    for predicate in predicates
                )
            ),
            evaluation_policy=QueryEvaluationPolicy(
                visible_verdicts=("supported",),
                visible_currency=("current",),
                conflict_behavior="surface_conflicts",
            ),
            default_budgets=QueryBudgets(max_results=100, max_traversal_depth=0),
            maximum_budgets=QueryBudgets(max_results=1000, max_traversal_depth=0),
        ),
    )


def _author_query(world: _World, definition: QueryDefinitionInput) -> None:
    draft = world.pb.changes(rationale="Read accounts.")
    draft.query_definition(definition)
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    world.approve(submitted._candidate_status.proposal_id)
    world.pb.refresh()


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
        ("capture-contracts/", CaptureContract),
        ("claim-types/", ClaimType),
        ("query-definitions/", QueryDefinition),
    ],
)
def test_the_reference_table_names_every_digest_field_of_each_kit_family(
    prefix: str, model: type[BaseModel]
) -> None:
    table = {tuple(s for s in steps if not s.startswith("*")) for steps in REFERENCE_FIELDS[prefix]}
    if prefix == "capture-contracts/":
        from cruxible_client.contracts.captures import CaptureContractV1

        assert table == _digest_fields(model) | _digest_fields(CaptureContractV1)
    else:
        assert table == _digest_fields(model)


@pytest.mark.parametrize(
    ("prefix", "model"), [("claims/", ClaimArtifact), ("documents/", DocumentShell)]
)
def test_state_reference_fields_are_real_digest_fields(prefix: str, model: type[BaseModel]) -> None:
    # Claims and Documents also hold content digests, which are never references.
    table = {tuple(s for s in steps if not s.startswith("*")) for steps in REFERENCE_FIELDS[prefix]}
    assert table <= _digest_fields(model)


# test_policy_and_interface_references_are_their_pins_alone was deleted:
# "source-acquisition-policies/" and "provider-interfaces/" are no longer
# REFERENCE_FIELDS prefixes (SourceAcquisitionPolicy and ProviderInterface
# are removed nouns, cut clean per ruling C13; acquisition is now inline
# data on a Procedure/Line, never a separately pinned artifact).


def test_a_kit_install_and_removal_preview_by_default_and_write_nothing(
    worlds: tuple[_World, _World], tmp_path: Path
) -> None:
    """R12: a kit change is derived across many artifacts, so it previews first."""

    from cruxible_core.errors import ChangeRefusedError
    from tests.support.store_snapshot import assert_writes_nothing

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}), _claim_type(PLAN, {"type": "string"}))
    release = publisher.build("1.0.0")
    get_playbill_manager().consumer_runner.close()

    def settle_background() -> None:
        # Advisory ref refreshes from earlier writes finish before comparing.
        for world in (publisher, consumer):
            get_playbill_manager().get(world.instance_id).settled_workspace_advertisement()
        consumer.tree()

    preview = assert_writes_nothing(
        [tmp_path],
        lambda: playbill_api.playbill_kit_add(
            consumer.instance_id, KitAddRequest(bundle=release, source="test")
        ),
        warm=settle_background,
    )

    assert preview.status == "would_propose" and preview.proposal_id is None
    assert preview.coordinate is not None
    assert {item.action for item in preview.plan} == {"add"}
    committed = playbill_api.playbill_kit_add(
        consumer.instance_id,
        KitAddRequest(bundle=release, source="test", dry_run=False, at=preview.coordinate.git_oid),
    )
    consumer.settle(committed)
    # The head moved under the preview's coordinate: a commit pinned to it refuses.
    with pytest.raises(ChangeRefusedError) as moved:
        playbill_api.playbill_kit_add(
            consumer.instance_id,
            KitAddRequest(
                bundle=release, source="test", dry_run=False, at=preview.coordinate.git_oid
            ),
        )
    assert moved.value.error_code == "cruxible.preview.state_moved"

    removal = assert_writes_nothing(
        [tmp_path],
        lambda: playbill_api.playbill_kit_remove(
            consumer.instance_id, KitRemoveRequest(kit_id="acme")
        ),
        warm=settle_background,
    )
    assert removal.status == "would_propose" and removal.proposal_id is None
    assert {item.action for item in removal.plan} == {"retire"}
    assert [
        kit.kit_id for kit in _installed(playbill_api.playbill_kit_status(consumer.instance_id))
    ] == ["acme"]


def test_a_kit_carries_its_subject_kinds_and_their_keys(
    worlds: tuple[_World, _World],
) -> None:
    """Kits carry SubjectKinds like other definitions (review: kit gap).

    A kit's kind arrives declared with its key ClaimType, so a consumer creates
    its Subjects by key at once; a kit upgrade succeeds the declaration, and the
    kit's status reads it like any other owned artifact.
    """

    from cruxible_client.contracts.subject_kinds import parse_subject_kind, subject_kind_path
    from tests.core_support._subject_kind_support import bare_slug_claim_type

    publisher, consumer = worlds
    draft = publisher.pb.changes(rationale="Name accounts by their slug.")
    draft.claim_type(bare_slug_claim_type("acme.account"))
    draft.subject_kind(name="acme.account", keys=("acme.account.slug",), uniqueness="unique")
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    publisher.approve(submitted._candidate_status.proposal_id)
    publisher.pb.refresh()
    bundle = publisher.build("1.0.0")
    assert subject_kind_path("acme.account") in {item.path for item in bundle.manifest.artifacts}

    consumer.add(bundle)
    path = subject_kind_path("acme.account")
    declared = parse_subject_kind(consumer.tree()[path], path=path)
    assert declared.keys == ("acme.account.slug",) and declared.unique
    creating = consumer.pb.changes(rationale="Acme is an account.")
    creating.subject(consumer.pb.new_subject("acme.account", key="Acme Corp"))
    created = creating.prepare()
    assert not created.refused, created.diagnostics
    landed = created.submit(accept="never")
    assert landed._candidate_status is not None and landed._candidate_status.proposal_id
    consumer.approve(landed._candidate_status.proposal_id)
    assert "subjects/acme.account/acme-corp.json" in consumer.tree()

    # The publisher makes the kind a hint; the upgrade carries the successor.
    succeed = publisher.pb.changes(rationale="Two accounts may share a slug.")
    succeed.subject_kind(name="acme.account", keys=("acme.account.slug",), uniqueness="hint")
    succeeded = succeed.prepare()
    assert not succeeded.refused, succeeded.diagnostics
    sent = succeeded.submit(accept="never")
    assert sent._candidate_status is not None and sent._candidate_status.proposal_id
    publisher.approve(sent._candidate_status.proposal_id)
    publisher.pb.refresh()
    upgraded = consumer.add(publisher.build("1.1.0"))
    assert upgraded.status in {"accepted", "proposed"}, upgraded
    declared = parse_subject_kind(consumer.tree()[path], path=path)
    assert not declared.unique
    (status,) = _installed(playbill_api.playbill_kit_status(consumer.instance_id))
    assert status.drifted == ()


def test_orient_and_get_name_the_kit_that_owns_each_claim_type_and_subject_kind(
    worlds: tuple[_World, _World],
) -> None:
    """The owning kit is its receipt's: a consumer's own definitions name none."""

    from cruxible_client.contracts.get_reads import GetRequest
    from tests.core_support._subject_kind_support import bare_slug_claim_type

    publisher, consumer = worlds
    draft = publisher.pb.changes(rationale="Name accounts by their slug.")
    draft.claim_type(bare_slug_claim_type("acme.account"))
    draft.claim_type(_claim_type(SEATS, {"type": "integer"}))
    draft.subject_kind(name="acme.account", keys=("acme.account.slug",), uniqueness="unique")
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit(accept="never")
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    publisher.approve(submitted._candidate_status.proposal_id)
    publisher.pb.refresh()
    consumer.add(publisher.build("1.0.0"))
    consumer.author(_claim_type(UNOWNED, {"type": "boolean"}))

    claim_types = playbill_api.playbill_orient(consumer.instance_id, section="claim_types")
    owners = {row.predicate: row.kit for row in claim_types.claim_types or ()}
    assert owners["acme.account.slug"] == "acme"
    assert owners[SEATS] == "acme"
    assert owners[UNOWNED] is None
    unowned = next(row for row in claim_types.claim_types or () if row.predicate == UNOWNED)
    assert "kit" not in unowned.model_dump(mode="json")
    kinds = playbill_api.playbill_orient(consumer.instance_id, section="subject_kinds")
    assert {row.kind: row.kit for row in kinds.subject_kinds or ()}["acme.account"] == "acme"
    # query names the same owners: ClaimType rows under select, SubjectKind rows always.
    from cruxible_client.contracts.compact_query import QueryRequest

    def listed(kind: str, **fields: typing.Any) -> dict[str, typing.Any]:
        rows = playbill_api.playbill_query(
            consumer.instance_id, request=QueryRequest(kind=kind, **fields)
        ).rows
        return {str(row.get("predicate", row.get("kind"))): row.get("kit") for row in rows}

    queried = listed("ClaimType", select=("predicate", "kit"))
    assert (queried[SEATS], queried[UNOWNED]) == ("acme", None)
    assert listed("SubjectKind")["acme.account"] == "acme"
    owned = listed("ClaimType", where=({"field": "kit", "eq": "acme"},), select=("predicate",))
    assert SEATS in owned and UNOWNED not in owned

    def card(ref: str) -> typing.Any:
        return playbill_api.playbill_get(consumer.instance_id, request=GetRequest(ref=ref)).card

    assert card(f"ClaimType:{SEATS}").kit == "acme"
    assert card(f"ClaimType:{UNOWNED}").kit is None
    assert card("SubjectKind:acme.account").kit == "acme"

    # Removing the kit retires what it installed; nothing names it any more.
    consumer.settle(
        playbill_api.playbill_kit_remove(
            consumer.instance_id, KitRemoveRequest(kit_id="acme", dry_run=False)
        )
    )
    assert card(f"ClaimType:{SEATS}").kit is None


def test_a_release_with_inert_files_installs_as_the_same_release(
    worlds: tuple[_World, _World], tmp_path: Path
) -> None:
    from cruxible_client.contracts.kits import KitFileBytes, kit_file_entry

    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = publisher.http  # type: ignore[assignment]
    built = transport.build_kit(
        publisher.instance_id,
        KitBuildRequest(kit_id="acme", version="1.0.0", owns=("acme.",)),
    )
    readme = b"# acme\n"
    documented = built.bundle.model_copy(
        update={
            "manifest": built.bundle.manifest.model_copy(
                update={"files": (kit_file_entry("README.md", readme),)}
            ),
            "files": (KitFileBytes.of("README.md", readme),),
        }
    )
    directory = tmp_path / "acme-1.0.0"
    write_kit_directory(documented, directory)
    read_back = read_kit_directory(directory)
    assert read_back.file_contents() == {"README.md": readme}
    assert read_back.manifest.content_digest == built.bundle.manifest.content_digest

    proposed = transport.add_kit(
        consumer.instance_id,
        KitAddRequest(bundle=read_back, source=directory.name, dry_run=False),
    )
    consumer.settle(proposed)
    (installed,) = _installed(transport.kit_status(consumer.instance_id))
    assert installed.content_digest == built.bundle.manifest.content_digest


# test_a_kit_carries_a_named_policy_without_pinning_its_versions was deleted:
# the named-reference form it tested (Procedure.acquisition_policy = <name>,
# no pin) is gone along with SourceAcquisitionPolicy -- acquisition is now
# inline LineAcquisitionRule data on the Procedure/Line itself (ruling C12).


# -- the kit's automation template (D-R48) ---------------------------------------------


def _template(*members: dict[str, object]) -> str:
    return json.dumps({"kind": "change_set", "members": list(members)}, indent=1) + "\n"


_START = {"kind": "runbook", "name": "acme.start", "steps": [{"action": "evidence.sweep"}]}
_START_DAILY = {
    "kind": "trigger",
    "name": "acme.start-daily",
    "runbook_name": "acme.start",
    "schedule": {"kind": "cron", "expression": "0 6 * * *"},
}


def _build_with_template(world: _World, content: str, path: str = "automation.json") -> KitBundle:
    from cruxible_client.contracts.kits import KitAutomationTemplate

    return playbill_api.playbill_kit_build(
        world.instance_id,
        KitBuildRequest(
            kit_id="acme",
            version="1.0.0",
            owns=("acme.",),
            automation_template=KitAutomationTemplate(path=path, content=content),
        ),
    ).bundle


def test_a_checked_automation_template_is_named_and_carried_never_installed(
    worlds: tuple[_World, _World], tmp_path: Path
) -> None:
    publisher, consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    plain = publisher.build("1.0.0")
    head = get_playbill_manager().get(publisher.instance_id).accepted_coordinate()
    content = _template(_START, _START_DAILY)

    bundle = _build_with_template(publisher, content)

    # The check is a preview: nothing landed or waits on the publisher.
    instance = get_playbill_manager().get(publisher.instance_id)
    assert instance.accepted_coordinate() == head
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.proposals.proposals import AuthenticatedActor

    pending = AuthoringIntentCoordinator.for_instance(instance).list_pending(
        actor=AuthenticatedActor(actor_id="operator")
    )
    assert pending.intents == ()
    manifest = bundle.manifest
    assert manifest.automation_template == "automation.json"
    assert [(item.path, item.role) for item in manifest.files] == [("automation.json", "reference")]
    assert bundle.file_contents() == {"automation.json": content.encode()}
    # An inert file: the release identity is the template-free build's.
    assert manifest.content_digest == plain.manifest.content_digest
    assert bundle.contents() == plain.contents()

    directory = tmp_path / "acme-1.0.0"
    write_kit_directory(bundle, directory)
    assert (directory / "automation.json").read_text() == content
    read_back = read_kit_directory(directory)
    assert read_back.manifest.automation_template == "automation.json"

    preview = _kit_preview(consumer, KitAddRequest(bundle=read_back, source="test"))
    assert preview.automation_template == "automation.json"
    installed = consumer.add(read_back)
    assert installed.automation_template == "automation.json"
    paths = set(consumer.tree())
    assert "runbooks/acme.start.json" not in paths
    assert not any(
        path.startswith(("triggers/", "lines/", "runbooks/")) and "acme." in path for path in paths
    )
    assert not any("acme.start" in item.path for item in installed.plan)


@pytest.mark.parametrize(
    ("members", "first"),
    [
        # Drift: the start Runbook names a Line the kit's instance does not hold.
        (
            ({"kind": "runbook", "name": "acme.start", "steps": [{"line": "acme.gone"}]},),
            "cruxible.authoring.runbook_line_missing",
        ),
        # A shape the current core no longer accepts.
        (
            (
                {
                    "kind": "procedure_mandate",
                    "name": "acme.old",
                    "procedure_name": "acme.triage",
                    "grants": "propose",
                    "namespace": ["claims"],
                    "valid_from": "NOW",
                    "expires_at": "EXPIRES",
                },
            ),
            "cruxible.authoring.input_invalid",
        ),
    ],
)
def test_a_template_that_does_not_author_refuses_the_build_naming_it(
    worlds: tuple[_World, _World], members: tuple[dict[str, object], ...], first: str
) -> None:
    from cruxible_core.errors import RequestRefusedError

    publisher, _consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    head = get_playbill_manager().get(publisher.instance_id).accepted_coordinate()

    with pytest.raises(RequestRefusedError) as refused:
        _build_with_template(publisher, _template(*members), path="automation/start.json")

    assert refused.value.error_code == "cruxible.kit.automation_template_invalid"
    assert "automation/start.json" in str(refused.value)
    assert first in str(refused.value)
    assert get_playbill_manager().get(publisher.instance_id).accepted_coordinate() == head


def _author_as(instance: typing.Any, payload: typing.Any, *, actor_id: str, name: str) -> None:
    """Lower one authoring input at the head and accept what it changes, as ``actor_id``."""

    from cruxible_client.contracts.authoring.inputs import lower_authoring_input
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.lowering import lower_authoring
    from cruxible_core.proposals.proposals import AuthenticatedActor
    from tests.core_support._identity_support import accept_tree

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    intent = coordinator.create(
        actor=AuthenticatedActor(actor_id=actor_id),
        payload=lower_authoring_input(payload),
        canonical_timestamp="2026-11-07T11:00:00.000000Z",
    ).intent
    changed = dict(lower_authoring(instance, intent=intent, actor_id=actor_id).changed_members)
    if changed:
        accept_tree(instance, changed, name=name, actor_id=actor_id)


def test_a_template_naming_a_publisher_local_line_the_kit_does_not_export_refuses(
    worlds: tuple[_World, _World],
) -> None:
    """D-R48 fix round F3: the check validates the exported closure, never the publisher's
    other local definitions.

    ``triage-hourly`` is a real, live Line on the publisher -- just not one the kit
    exports (Lines are never kit content). A template whose Runbook names it must
    refuse exactly as a fresh adopter's build would, not pass because the
    publisher happens to hold a Line of that name for its own operational use.
    """

    from cruxible_client.contracts.authoring.inputs import LineInput, ProcedureInput
    from cruxible_core.errors import RequestRefusedError
    from tests.core_support._identity_support import accept_tree
    from tests.support.operation_world import (
        build_files,
        contract_files,
        operation_build,
        slotted_spec,
    )

    publisher, _consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    instance = get_playbill_manager().get(publisher.instance_id)
    accept_tree(
        instance,
        {**contract_files(), **build_files(operation_build())},
        name="local-operations",
        actor_id="operator",
    )
    _author_as(
        instance,
        ProcedureInput(kind="procedure", spec=slotted_spec(name="acme.compute")),
        actor_id="operator",
        name="local-procedure",
    )
    _author_as(
        instance,
        LineInput.model_validate(
            {
                "kind": "line",
                "name": "triage-hourly",
                "procedure_name": "acme.compute",
                "bindings": {},
                "acquisition": {},
                "findings": {},
                "parameters": {"value": 1},
                "max_authority": "observe",
                "occurrence_epoch": 1,
            }
        ),
        actor_id="operator",
        name="local-line",
    )
    head = instance.accepted_coordinate()

    content = _template(
        {"kind": "runbook", "name": "acme.start", "steps": [{"line": "triage-hourly"}]}
    )
    with pytest.raises(RequestRefusedError) as refused:
        _build_with_template(publisher, content)

    assert refused.value.error_code == "cruxible.kit.automation_template_invalid"
    assert "cruxible.authoring.runbook_line_missing" in str(refused.value)
    assert get_playbill_manager().get(publisher.instance_id).accepted_coordinate() == head
    assert get_playbill_manager().get(publisher.instance_id).accepted_coordinate() == head


def _bundled_local_call_provider(
    tmp_path: Path,
    instance: typing.Any,
    checkout: typing.Any,
    *,
    name: str = "kit-call",
    refusal: str | None = None,
) -> typing.Any:
    """A real wheel for ``name``'s ``local.increment`` operation, staged for ``KitBuildRequest``.

    ``refusal`` (a ``name@N`` Contract ref), when given, is declared on every
    operation the package registers (D-R48 review fix round 3, F1): the one
    kind of Contract a bundled operation pins that nothing else in the owned
    tree ever pins (only the Provider's own registration does), so a kit's
    export must sweep it on its own.
    """

    import hashlib

    from cruxible_client.contracts.kits import KitBuildProvider
    from cruxible_client.contracts.provider_installation import ProviderWheelObject
    from tests.support.provider_installation import build_local_call

    wheel, lock = build_local_call(tmp_path, checkout.repository, name=name, refusal=refusal)
    runtime = next(checkout.wheels.glob("cruxible_provider_runtime-*.whl"))

    def staged_digest(path: Path) -> str:
        content = path.read_bytes()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        instance.body_store().store(content)
        return digest

    def staged_wheel(path: Path) -> ProviderWheelObject:
        return ProviderWheelObject(filename=path.name, digest=staged_digest(path))

    wheel_object = staged_wheel(wheel)
    lock_digest = staged_digest(lock)
    runtime_object = staged_wheel(runtime)
    return KitBuildProvider(
        wheel=wheel_object,
        lock_digest=lock_digest,
        dependencies=(runtime_object,),
    )


def test_a_template_binding_a_bundled_provider_operation_builds(
    worlds: tuple[_World, _World], tmp_path: Path, provider_checkout: typing.Any
) -> None:
    """D-R48 review fix round 2, R2: the check seeds the bundle's own validated
    provider packages' operations, so a Line binding ``name#operation`` of a
    bundled build authors -- never a publisher-local build's.
    """

    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.kits import KitAutomationTemplate
    from cruxible_client.contracts.procedures.authored import CallStep
    from tests.core_support._identity_support import accept_tree
    from tests.support.procedures import procedure_limits, procedure_spec
    from tests.support.provider_installation import (
        LOCAL_CALL_SLOT,
        local_call_capability_files,
        local_call_contract_files,
    )

    publisher, _consumer = worlds
    instance = get_playbill_manager().get(publisher.instance_id)
    accept_tree(
        instance,
        {**local_call_contract_files(), **local_call_capability_files()},
        name="local-call-contract",
        actor_id="operator",
    )
    _author_as(
        instance,
        ProcedureInput(
            kind="procedure",
            spec=procedure_spec(
                procedure="acme.increment",
                params={"n": "int"},
                output={"n": "int"},
                slots={"increment": LOCAL_CALL_SLOT},
                steps=[
                    CallStep(
                        call="result",
                        slot="increment",
                        input={"n": {"$ref": "param", "path": ["n"]}},
                    )
                ],
                returns="result",
                hard_caps=procedure_limits(wall_clock="PT30S", provider_calls=1),
            ),
        ),
        actor_id="operator",
        name="acme-increment-procedure",
    )
    provider = _bundled_local_call_provider(tmp_path, instance, provider_checkout)
    content = _template(
        {
            "kind": "line",
            "name": "acme.increment-run",
            "procedure_name": "acme.increment",
            "bindings": {"increment": "kit-call#local.increment"},
            "acquisition": {},
            "findings": {},
            "parameters": {"n": 1},
            "max_authority": "observe",
            "occurrence_epoch": 1,
        }
    )
    head = instance.accepted_coordinate()

    bundle = playbill_api.playbill_kit_build(
        publisher.instance_id,
        KitBuildRequest(
            kit_id="acme",
            version="1.0.0",
            owns=("acme.",),
            providers=(provider,),
            automation_template=KitAutomationTemplate(path="automation.json", content=content),
        ),
    ).bundle

    assert bundle.manifest.automation_template == "automation.json"
    assert instance.accepted_coordinate() == head


@pytest.mark.parametrize("preaccept", [False, True])
def test_a_bundled_providers_refusal_contract_outside_owns_builds_and_installs(
    worlds: tuple[_World, _World], tmp_path: Path, provider_checkout: typing.Any, preaccept: bool
) -> None:
    """D-R48 review fix round 3, F1: ``build_kit`` sweeps a bundled package's
    complete operation Contract closure -- input, output AND refusal -- into
    the export, whatever namespace those Contracts live in, never only the
    ``owns`` prefix's own dependency closure.

    ``other.declined`` is outside ``acme.`` (the kit's only owned prefix,
    never widened here): nothing but the Provider's own registration pins
    it, so before this fix the kit would not export it at all and the build
    would refuse ``cruxible.kit.automation_template_invalid`` /
    ``cruxible.provider.operation_contract_unresolved``. The consumer can
    pre-accept these Contracts or let kit add govern them before installing
    the bundled provider.
    """

    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.procedures.authored import CallStep
    from cruxible_client.contracts.shapes import (
        Contract,
        contract_digest,
        contract_path,
        render_contract,
    )
    from tests.core_support._identity_support import accept_tree
    from tests.support.procedures import procedure_limits, procedure_spec
    from tests.support.provider_installation import (
        LOCAL_CALL_SLOT,
        OTHER_DECLINED_REF,
        local_call_capability_files,
        local_call_contract_files,
    )

    # A refusal Contract also pins a nested version with a predecessor:
    # all three must land in the prerequisite change, before registration.
    leaf1 = Contract.model_validate({"name": "other.detail", "version": 1, "fields": {}})
    leaf2 = Contract.model_validate(
        {
            "name": "other.detail",
            "version": 2,
            "predecessor_digest": contract_digest(leaf1).tagged,
            "fields": {},
        }
    )
    declined = Contract.model_validate(
        {
            "name": "other.declined",
            "version": 1,
            "fields": {
                "detail": {
                    "type": {
                        "kind": "contract",
                        "contract": leaf2.ref.model_copy(
                            update={"digest": contract_digest(leaf2).tagged}
                        ).model_dump(mode="json"),
                    }
                }
            },
        }
    )
    refusal_files = {
        contract_path(contract.name, contract.version): render_contract(contract)
        for contract in (leaf1, leaf2, declined)
    }
    publisher, consumer = worlds
    instance = get_playbill_manager().get(publisher.instance_id)
    accept_tree(
        instance,
        {**local_call_contract_files(), **local_call_capability_files()},
        name="local-call-contract",
        actor_id="operator",
    )
    accept_tree(
        instance,
        refusal_files,
        name="other-declined-contract",
        actor_id="operator",
    )
    _author_as(
        instance,
        ProcedureInput(
            kind="procedure",
            spec=procedure_spec(
                procedure="acme.increment",
                params={"n": "int"},
                output={"n": "int"},
                slots={"increment": LOCAL_CALL_SLOT},
                steps=[
                    CallStep(
                        call="result",
                        slot="increment",
                        input={"n": {"$ref": "param", "path": ["n"]}},
                    )
                ],
                returns="result",
                hard_caps=procedure_limits(wall_clock="PT30S", provider_calls=1),
            ),
        ),
        actor_id="operator",
        name="acme-increment-procedure",
    )
    provider = _bundled_local_call_provider(
        tmp_path, instance, provider_checkout, refusal=OTHER_DECLINED_REF
    )
    head = instance.accepted_coordinate()

    result = playbill_api.playbill_kit_build(
        publisher.instance_id,
        KitBuildRequest(
            kit_id="acme",
            version="1.0.0",
            owns=("acme.",),  # never widened to include "other."
            providers=(provider,),
        ),
    )
    bundle = result.bundle
    assert instance.accepted_coordinate() == head
    assert bundle.manifest.owns == ("acme.",)
    refusal_path = "contracts/other.declined/v1.json"
    assert refusal_files.items() <= bundle.contents().items()
    assert refusal_path in bundle.contents(), (
        "the bundled provider's refusal Contract must be swept into the export "
        "even though it is outside every owned prefix"
    )

    consumer_instance = get_playbill_manager().get(consumer.instance_id)
    if preaccept:
        accept_tree(
            consumer_instance,
            local_call_contract_files(),
            name="local-call-contract",
            actor_id="operator",
        )
        accept_tree(
            consumer_instance,
            refusal_files,
            name="other-declined-contract",
            actor_id="operator",
        )
    for item in bundle.provider_files:
        consumer_instance.body_store().store(item.content)
    staged = bundle.model_copy(update={"provider_files": ()})

    added = consumer.add(staged)
    assert added.status == "accepted", added

    tree = consumer.tree()
    assert refusal_files.items() <= tree.items()
    assert refusal_path in tree, (
        "the kit's own definitions must carry the refusal Contract to the "
        "installed tree, as a carried dependency (never owned, never widened "
        "into owns)"
    )
    assert any(path.startswith("providers/kit-call/") for path in tree)


def test_a_template_binding_a_publisher_local_provider_not_in_the_bundle_refuses(
    worlds: tuple[_World, _World], tmp_path: Path, provider_checkout: typing.Any
) -> None:
    """The same template, but ``kit-call`` is installed live on the publisher
    and NOT named in ``request.providers``: a fresh adopter's throwaway
    closure never seeds it (a Provider is never kit content, and the check
    reads only ``request.providers``, never the publisher's own installed
    builds), so the build refuses exactly as it would for them -- never
    because the publisher happens to run it locally.
    """

    from cruxible_client.contracts.authoring.inputs import ProcedureInput
    from cruxible_client.contracts.kits import KitAutomationTemplate
    from cruxible_client.contracts.procedures.authored import CallStep
    from cruxible_client.transport.http import CruxibleClient
    from cruxible_core.cli.provider_wheels import install_provider_wheel
    from cruxible_core.errors import RequestRefusedError
    from tests.core_support._identity_support import accept_tree
    from tests.support.procedures import procedure_limits, procedure_spec
    from tests.support.provider_installation import (
        LOCAL_CALL_SLOT,
        build_local_call,
        local_call_capability_files,
        local_call_contract_files,
    )

    publisher, _consumer = worlds
    instance = get_playbill_manager().get(publisher.instance_id)
    accept_tree(
        instance,
        {**local_call_contract_files(), **local_call_capability_files()},
        name="local-call-contract",
        actor_id="operator",
    )
    _author_as(
        instance,
        ProcedureInput(
            kind="procedure",
            spec=procedure_spec(
                procedure="acme.increment",
                params={"n": "int"},
                output={"n": "int"},
                slots={"increment": LOCAL_CALL_SLOT},
                steps=[
                    CallStep(
                        call="result",
                        slot="increment",
                        input={"n": {"$ref": "param", "path": ["n"]}},
                    )
                ],
                returns="result",
                hard_caps=procedure_limits(wall_clock="PT30S", provider_calls=1),
            ),
        ),
        actor_id="operator",
        name="acme-increment-procedure",
    )
    # A real, live install on the publisher -- not staged, not bundled, and
    # never passed to ``request.providers``. A check that fell back to the
    # publisher's own installed builds would wrongly resolve the binding.
    wheel, lock = build_local_call(tmp_path, provider_checkout.repository, name="kit-call")
    runtime = next(provider_checkout.wheels.glob("cruxible_provider_runtime-*.whl"))
    client = CruxibleClient(base_url="http://cruxible")
    client._client = publisher.http  # type: ignore[assignment]
    result = install_provider_wheel(
        client, publisher.instance_id, wheel=wheel, lock=lock, dependency_wheels=(runtime,)
    )
    assert result.registered, result
    content = _template(
        {
            "kind": "line",
            "name": "acme.increment-run",
            "procedure_name": "acme.increment",
            "bindings": {"increment": "kit-call#local.increment"},
            "acquisition": {},
            "findings": {},
            "parameters": {"n": 1},
            "max_authority": "observe",
            "occurrence_epoch": 1,
        }
    )
    head = instance.accepted_coordinate()

    with pytest.raises(RequestRefusedError) as refused:
        playbill_api.playbill_kit_build(
            publisher.instance_id,
            KitBuildRequest(
                kit_id="acme",
                version="1.0.0",
                owns=("acme.",),
                automation_template=KitAutomationTemplate(path="automation.json", content=content),
            ),
        )

    # Binding resolution refuses directly (``resolve_slot_bindings``), not
    # wrapped as ``cruxible.kit.automation_template_invalid`` -- the same code
    # a fresh adopter's real ``kit add`` would see for this same Line.
    assert refused.value.error_code == "cruxible.line.binding_unavailable"
    assert instance.accepted_coordinate() == head


def test_a_template_that_is_not_one_change_set_refuses_the_build(
    worlds: tuple[_World, _World],
) -> None:
    from cruxible_core.errors import RequestRefusedError

    publisher, _consumer = worlds
    publisher.author(_claim_type(SEATS, {"type": "integer"}))
    for content, said in (
        ("{not json", "not JSON"),
        (json.dumps(_START), "one change_set"),
    ):
        with pytest.raises(RequestRefusedError, match=said) as refused:
            _build_with_template(publisher, content)
        assert refused.value.error_code == "cruxible.kit.automation_template_invalid"


def test_a_manifest_names_only_a_template_it_lists_as_a_file() -> None:
    from cruxible_client.contracts.kits import KitManifest, kit_file_entry

    with pytest.raises(ValueError, match="not one of the kit's files"):
        KitManifest(
            kit_id="acme",
            version="1.0.0",
            owns=("acme.",),
            artifacts=(),
            automation_template="automation.json",
        )
    listed = KitManifest(
        kit_id="acme",
        version="1.0.0",
        owns=("acme.",),
        artifacts=(),
        files=(kit_file_entry("automation.json", b"{}"),),
        automation_template="automation.json",
    )
    bare = KitManifest(kit_id="acme", version="1.0.0", owns=("acme.",), artifacts=())
    assert listed.content_digest == bare.content_digest
    assert "automation_template" not in bare.model_dump(mode="json")
