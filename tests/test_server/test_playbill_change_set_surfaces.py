"""One changeset, one intent identity, whichever surface authored it."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from cruxible_client import Playbill
from cruxible_client.authoring.examples import authoring_example
from cruxible_client.authoring.inputs import (
    ChangeSetInput,
    ClaimInput,
    ClaimRetirementInput,
    ClaimTypeInput,
    ClaimTypeSuccessionInput,
    LiteralObjectInput,
    SelfSourceInput,
    SubjectInput,
)
from cruxible_client.authoring.sdk import ChangeSetDraft, carry, re_author, rescind, retire
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.authoring.models import ClaimTypeSuccessionDependentV1
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.transport.http import CruxibleClient

PREDICATE = "project.work_item.parity"
SUBJECT_NAME = "project.work_item/parity-1"


def _shell() -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=SUBJECT_NAME),
        subject_kind="project.work_item",
        subject_id="parity-1",
        lifecycle=ArtifactLifecycle(),
    )


def _claim_type() -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=PREDICATE),
        predicate=PREDICATE,
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "string"},
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


def _payload_digest(intent: dict[str, object]) -> str:
    digest = intent.get("payload_digest")
    assert isinstance(digest, str)
    return digest


CLAIM_RATIONALE = "The writer observed the current parity value."
CLAIM_BODY = "parity: ready\n"


def _claim_input() -> ClaimInput:
    """The parity Claim as a CLI payload file or an MCP dict carries it."""

    return ClaimInput(
        kind="claim",
        subject=SUBJECT_NAME,
        predicate=PREDICATE,
        object=LiteralObjectInput(kind="literal", value="ready"),
        role="observation",
        rationale=CLAIM_RATIONALE,
        source=SelfSourceInput(kind="self_source", body=CLAIM_BODY),
    )


def _add_parity_claim(draft: ChangeSetDraft) -> None:
    """The same Claim as `_claim_input`, authored through the SDK builder."""

    draft.claim(
        subject=SUBJECT_NAME,
        predicate=PREDICATE,
        value="ready",
        role="observation",
        rationale=CLAIM_RATIONALE,
        supported_by=None,
        copied_from=None,
        self_source=CLAIM_BODY,
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )


def _claim_member(members: list[dict[str, object]]) -> dict[str, object]:
    return next(
        item for item in members if str(item["tag"]).startswith("playbill-claim-authoring-payload-")
    )


def _normalized_member(member: dict[str, object]) -> dict[str, object]:
    """Strip exactly the two fields the V1/V2 Claim payload split introduces."""

    if not str(member["tag"]).startswith("playbill-claim-authoring-payload-"):
        return member
    return {key: value for key, value in member.items() if key not in {"tag", "dependency_drafts"}}


def test_one_changeset_has_one_intent_identity_across_sdk_cli_and_mcp(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    """The same members author the same intent whichever surface sent them.

    The SDK builder, the tagless `change_set` input a CLI payload file carries,
    and the raw dict an MCP caller sends all lower onto exactly one changeset
    payload, so the intent identity is a property of the members and not of the
    surface that happened to carry them.
    """

    http, instance_id, _private_key_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    workspace = tmp_path / "parity-world"
    (workspace / ".playbill").mkdir(parents=True)
    (workspace / "corpus").mkdir()
    (workspace / "corpus" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (workspace / ".playbill" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: corpus.notes\n"
        "    locator: corpus/notes.md\n"
        "    document_id: notes\n"
        "    document_kind: note\n"
        "    title: Notes\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [Document:notes]\n",
        encoding="utf-8",
    )
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=workspace)

    tagless = ChangeSetInput(
        kind="change_set",
        members=(
            SubjectInput(kind="subject", subject=_shell()),
            ClaimTypeInput(kind="claim_type", claim_type=_claim_type()),
        ),
    )
    # A CLI payload file is read back off disk before it is sent.
    payload_file = tmp_path / "change-set.json"
    payload_file.write_text(json.dumps(tagless.model_dump(mode="json")), encoding="utf-8")
    cli_input = ChangeSetInput.model_validate(json.loads(payload_file.read_text(encoding="utf-8")))
    cli_intent = transport.create_playbill_authoring_input(
        instance_id,
        input=cli_input.model_dump(mode="json"),
    ).intent
    # An MCP caller sends the same shape as a raw dict.
    mcp_intent = transport.create_playbill_authoring_input(
        instance_id,
        input=json.loads(json.dumps(tagless.model_dump(mode="json"))),
    ).intent

    sdk_draft = pb.changes(rationale="Define the parity Subject and its ClaimType.")
    sdk_draft.subject(_shell())
    sdk_draft.claim_type(_claim_type())
    sdk_prepared = sdk_draft.prepare()
    assert not sdk_prepared.refused, sdk_prepared.diagnostics
    sdk_intent = sdk_prepared._raw

    digests = {
        _payload_digest(cli_intent),
        _payload_digest(mcp_intent),
        _payload_digest(sdk_intent),
    }
    assert len(digests) == 1
    identities = {
        cli_intent["semantic_identity"],
        mcp_intent["semantic_identity"],
        sdk_intent["semantic_identity"],
    }
    assert len(identities) == 1
    assert next(iter(identities)).startswith("ChangeSet:")

    # The same three surfaces again, this time carrying a Claim member. Claims
    # are where the surfaces could diverge and did not: a Claim member's
    # identity is its authored statement with the payload tag popped, so the set
    # identity is still a property of the members alone -- even though a tagless
    # Claim lowers to a V1 payload and an SDK Claim is always a V2. That
    # pre-existing version split is the ONE thing that separates the digests.
    with_claim = ChangeSetInput(
        kind="change_set",
        members=(*tagless.members, _claim_input()),
    )
    cli_claim_intent = transport.create_playbill_authoring_input(
        instance_id,
        input=ChangeSetInput.model_validate(
            json.loads(json.dumps(with_claim.model_dump(mode="json")))
        ).model_dump(mode="json"),
    ).intent
    mcp_claim_intent = transport.create_playbill_authoring_input(
        instance_id,
        input=json.loads(json.dumps(with_claim.model_dump(mode="json"))),
    ).intent

    sdk_claim_draft = pb.changes(rationale="Open the parity slot and state its first value.")
    sdk_claim_draft.subject(_shell())
    sdk_claim_draft.claim_type(_claim_type())
    _add_parity_claim(sdk_claim_draft)
    sdk_claim_prepared = sdk_claim_draft.prepare()
    assert not sdk_claim_prepared.refused, sdk_claim_prepared.diagnostics
    sdk_claim_intent = sdk_claim_prepared._raw

    claim_identities = {
        cli_claim_intent["semantic_identity"],
        mcp_claim_intent["semantic_identity"],
        sdk_claim_intent["semantic_identity"],
    }
    assert len(claim_identities) == 1
    assert next(iter(claim_identities)).startswith("ChangeSet:")
    assert claim_identities != identities

    # The tagless and MCP payloads are the same bytes; the SDK's differs, and
    # only in the Claim member's payload version.
    assert _payload_digest(cli_claim_intent) == _payload_digest(mcp_claim_intent)
    assert _payload_digest(sdk_claim_intent) != _payload_digest(cli_claim_intent)
    tagless_members = cli_claim_intent["payload"]["members"]
    sdk_members = sdk_claim_intent["payload"]["members"]
    assert [_normalized_member(item) for item in tagless_members] == [
        _normalized_member(item) for item in sdk_members
    ]
    versions = {
        _claim_member(tagless_members)["tag"],
        _claim_member(sdk_members)["tag"],
    }
    assert versions == {
        "playbill-claim-authoring-payload-v1",
        "playbill-claim-authoring-payload-v2",
    }


def test_the_shipped_change_set_example_round_trips_and_creates_one_intent(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """`--example change-set` is a mixed set an agent can send back unedited."""

    http, instance_id, _private_key_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]

    example = authoring_example("change-set")
    assert isinstance(example, ChangeSetInput)
    kinds = {member.kind for member in example.members}
    assert {"subject", "claim_type", "claim", "claim_retirement"} <= kinds
    assert any(isinstance(member, ClaimRetirementInput) for member in example.members)

    round_tripped = ChangeSetInput.model_validate(json.loads(example.model_dump_json()))
    assert round_tripped == example

    created = transport.create_playbill_authoring_input(
        instance_id,
        input=round_tripped.model_dump(mode="json"),
    ).intent
    assert created["semantic_identity"].startswith("ChangeSet:")
    assert len(created["payload"]["members"]) == len(example.members)


def test_the_sdk_builder_authors_a_mixed_changeset_that_preflights_clean(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    """One SDK program: a Subject, its ClaimType and a Claim that reads both."""

    http, instance_id, _private_key_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    workspace = tmp_path / "builder-world"
    (workspace / ".playbill").mkdir(parents=True)
    (workspace / "corpus").mkdir()
    (workspace / "corpus" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (workspace / ".playbill" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: corpus.notes\n"
        "    locator: corpus/notes.md\n"
        "    document_id: notes\n"
        "    document_kind: note\n"
        "    title: Notes\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [Document:notes]\n",
        encoding="utf-8",
    )
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=workspace)

    draft = pb.changes(rationale="Open the parity slot and state its first value.")
    draft.subject(_shell())
    draft.claim_type(_claim_type())
    draft.claim(
        subject=SUBJECT_NAME,
        predicate=PREDICATE,
        value="ready",
        role="observation",
        rationale="The writer observed the current parity value.",
        supported_by=None,
        copied_from=None,
        self_source="parity: ready\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        subject_definition=None,
        claim_type_definition=None,
    )
    intent = draft.prepare()

    assert not intent.refused, intent.diagnostics
    payload = intent._raw["payload"]
    assert payload["tag"] == "playbill-change-set-authoring-payload-v1"
    assert len(payload["members"]) == 3
    assert intent._raw["semantic_identity"].startswith("ChangeSet:")
    assert len(intent._raw["change_set_claim_identities"]) == 1

    submitted = intent.submit()
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None


def _succession_type() -> ClaimType:
    """The parity ClaimType, narrowed, naming the digest it succeeds."""

    current = _claim_type()
    return current.model_copy(
        update={
            "literal_schema": {"type": "string", "enum": ["ready"]},
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_type_digest(current).tagged,
            ),
        }
    )


def test_one_claim_type_succession_has_one_identity_across_sdk_cli_and_mcp(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    """Evolving vocabulary is one intent whichever surface authored it."""

    http, instance_id, _private_key_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    workspace = tmp_path / "succession-world"
    (workspace / ".playbill").mkdir(parents=True)
    (workspace / "corpus").mkdir()
    (workspace / "corpus" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (workspace / ".playbill" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: corpus.notes\n"
        "    locator: corpus/notes.md\n"
        "    document_id: notes\n"
        "    document_kind: note\n"
        "    title: Notes\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [Document:notes]\n",
        encoding="utf-8",
    )
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=workspace)

    successor = _succession_type()
    claim_id = "CLM-" + "a" * 32
    tagless = ChangeSetInput(
        kind="change_set",
        members=(
            ClaimTypeSuccessionInput(
                kind="claim_type_succession",
                successor=successor,
                dependents=(
                    ClaimTypeSuccessionDependentV1(
                        identity=ArtifactIdentity(kind="Claim", name=claim_id),
                        disposition="retire",
                        claim_retirement_reason="was-rescinded",
                    ),
                ),
            ),
            SubjectInput(kind="subject", subject=_shell()),
        ),
    )
    payload_file = tmp_path / "succession.json"
    payload_file.write_text(json.dumps(tagless.model_dump(mode="json")), encoding="utf-8")
    cli_input = ChangeSetInput.model_validate(json.loads(payload_file.read_text(encoding="utf-8")))
    cli_intent = transport.create_playbill_authoring_input(
        instance_id,
        input=cli_input.model_dump(mode="json"),
    ).intent
    mcp_intent = transport.create_playbill_authoring_input(
        instance_id,
        input=json.loads(json.dumps(tagless.model_dump(mode="json"))),
    ).intent

    draft = pb.changes(rationale="Narrow the parity vocabulary and settle its closure.")
    draft.subject(_shell())
    draft.succeed_claim_type(successor, dependents=[rescind(claim_id)])
    sdk_intent = draft._compiled()

    identities = {
        cli_intent["semantic_identity"],
        mcp_intent["semantic_identity"],
    }
    assert len(identities) == 1
    assert next(iter(identities)).startswith("ChangeSet:")
    assert _payload_digest(cli_intent) == _payload_digest(mcp_intent)
    assert sdk_intent.payload.model_dump(mode="json") == cli_intent["payload"]
    assert any(
        str(member["tag"]) == "playbill-claim-type-succession-authoring-payload-v1"
        for member in cli_intent["payload"]["members"]
    )


def test_the_succession_disposition_helpers_spell_one_vocabulary() -> None:
    """`carry`, `rescind`, `retire` and `re_author` are the standalone words."""

    claim_id = "CLM-" + "b" * 32
    assert carry(claim_id).disposition == "successor"
    assert carry(f"Claim:{claim_id}").identity.name == claim_id
    rescinded = rescind(claim_id)
    assert (rescinded.disposition, rescinded.claim_retirement_reason) == (
        "retire",
        "was-rescinded",
    )
    retired = retire(claim_id, reason="was-wrong")
    assert (retired.disposition, retired.claim_retirement_reason) == ("retire", "was-wrong")
    said_again = re_author(claim_id)
    assert said_again.disposition == "re_author"
    assert said_again.successor_claim_id == claim_id
    # `with_` is only ever an explicit spelling of what `claim` already says.
    assert re_author(claim_id, with_=f"Claim:{claim_id}") == said_again


def test_the_shipped_succession_example_round_trips_and_creates_one_intent(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """`--example claim-type-succession` is a set an agent can send back unedited."""

    http, instance_id, _private_key_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]

    example = authoring_example("claim-type-succession")
    assert isinstance(example, ChangeSetInput)
    assert {member.kind for member in example.members} == {"claim_type_succession", "claim"}

    round_tripped = ChangeSetInput.model_validate(json.loads(example.model_dump_json()))
    assert round_tripped == example

    created = transport.create_playbill_authoring_input(
        instance_id,
        input=round_tripped.model_dump(mode="json"),
    ).intent
    assert created["semantic_identity"].startswith("ChangeSet:")
    assert len(created["payload"]["members"]) == len(example.members)
