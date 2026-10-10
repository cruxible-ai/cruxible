"""A `propose_change_set` terminal creates Subjects by kind and key, never by id (identity D1).

A Subject item names a declared kind and its key values; core assigns the id. A
key a live Subject already holds names that Subject, and the item creates
nothing (reported ``reused``). A Claim item of the same terminal names a Subject
the run knows only by its key with a ``SubjectKeyReference`` (v3 item): it
resolves to the Subject the terminal's own Subject item lands on, else to the
one live holder of the key, and refuses when none or several hold it.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import capture_contract_digest
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path, render_claim_type
from cruxible_client.contracts.claims import parse_claim
from cruxible_client.contracts.procedure_mandates import (
    procedure_mandate_path,
    render_procedure_mandate,
)
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from cruxible_client.contracts.subjects import parse_subject
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.core_support._pc_c_support import capture_contract
from tests.core_support._subject_kind_support import bare_slug_claim_type, keyed_subject_kind
from tests.test_procedures import test_procedure_proposal_delivery as delivery
from tests.test_procedures import test_procedure_source_runs as fixtures

KIND = "security.package"
SLUG = f"{KIND}.slug"
SEVERITY = f"{KIND}.severity"


def _self_source(text: str) -> dict[str, str]:
    return {
        "tag": "playbill-self-source-body-v1",
        "content_base64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


def subject_item(slug: str = "Click", **overrides: Any) -> dict[str, object]:
    item: dict[str, object] = {
        "tag": "playbill-procedure-subject-proposal-item-v1",
        "subject_kind": KIND,
        "key": {"slug": slug},
        "rationale": "The advisory names this package.",
        "source": _self_source(f"package: {slug}\n"),
    }
    item.update(overrides)
    return item


def keyed_claim_item(slug: str = "click") -> dict[str, object]:
    return {
        "tag": "playbill-procedure-claim-proposal-item-v3",
        "statement": {
            "tag": "playbill-procedure-claim-statement-v1",
            "subject": {"subject_kind": KIND, "key": {"slug": slug}},
            "predicate": SEVERITY,
            "qualifier": None,
            "object": {"kind": "literal", "value": "$steps.result.severity"},
            "role": "observation",
            "effective_from": None,
            "effective_until": None,
        },
        "rationale": "Severity as read from the accepted advisory document.",
        "revises": None,
    }


def _severity_type() -> ClaimType:
    template = delivery._claim_type(capture_contract_digest(capture_contract()).tagged)
    return template.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=SEVERITY),
            "predicate": SEVERITY,
            "allowed_subject_kinds": (KIND,),
        }
    )


def subject_world(tmp_path: Path, templates: tuple[object, ...]):  # type: ignore[no-untyped-def]
    """The proposal world, with packages keyed by slug and a mandate reaching them."""

    instance, owner, root, line, procedure = delivery.proposal_world(
        tmp_path, templates=templates, mandate=None
    )
    slug = bare_slug_claim_type(KIND)
    severity = _severity_type()
    # The key Claims cite the item's own words: the first such Claim installs the
    # coordinator self-source CaptureContract, so the mandate reaches it too.
    mandate = fixtures._line_mandate(procedure).model_copy(
        update={
            "namespace": (
                "capture-contracts/playbill.coordinator-self-source-v1.json",
                "claims",
                f"subjects/{KIND}",
            )
        }
    )
    fixtures._accept_more(
        instance,
        owner,
        {
            claim_type_path(SLUG): render_claim_type(slug),  # type: ignore[arg-type]
            claim_type_path(SEVERITY): render_claim_type(severity),
            subject_kind_path(KIND): render_subject_kind(keyed_subject_kind(KIND, (SLUG,))),
            procedure_mandate_path(mandate.identity.name): render_procedure_mandate(mandate),
        },
        name="package-kind",
    )
    return instance, owner, root, line


def _delivered(instance, state):  # type: ignore[no-untyped-def]
    assert state.status == "succeeded", (state.terminal, state.terminal_egress)
    (egress,) = state.terminal_egress
    assert egress.verdict == "delivered", egress
    return egress


def test_a_subject_item_creates_by_key_and_a_claim_item_names_it_by_key(tmp_path: Path) -> None:
    instance, owner, root, line = subject_world(tmp_path, (subject_item(), keyed_claim_item()))

    egress = _delivered(instance, delivery.run_line(instance, root, line))

    created, claimed = egress.children
    # Core named the package from its canonical slug, never from the run.
    assert created.path == f"subjects/{KIND}/click.json"
    assert created.path in egress.target_paths and not created.unchanged
    assert claimed.path is not None and claimed.path in egress.target_paths
    assert any(path.startswith("claims/") for path in egress.target_paths)

    inspection = service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    accept_proposal(instance, owner, inspection)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert parse_subject(tree[created.path], path=created.path).subject_id == "click"
    claim = parse_claim(tree[claimed.path], path=claimed.path)
    assert claim.statement.subject.artifact_path == created.path
    keys = [
        parse_claim(tree[path], path=path)
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == SLUG
    ]
    assert [item.statement.object.model_dump(mode="json")["value"] for item in keys] == ["click"]


def test_a_key_a_live_subject_holds_reuses_it_and_creates_nothing(tmp_path: Path) -> None:
    instance, owner, root, line = subject_world(tmp_path, (subject_item(),))
    first = _delivered(instance, delivery.run_line(instance, root, line))
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first.proposal_id)
    )
    base = instance.accepted_coordinate()

    # The same key now names the live package: the item creates nothing.
    state = delivery.run_line(
        instance, root, line, at=fixtures.NOW.replace(minute=fixtures.NOW.minute + 1)
    )

    assert state.status == "succeeded", state.terminal
    (egress,) = state.terminal_egress
    (child,) = egress.children
    assert child.unchanged and child.path == f"subjects/{KIND}/click.json"
    assert egress.proposal_id is None
    assert instance.accepted_coordinate() == base


def test_a_key_reference_no_subject_holds_refuses_naming_the_creation(tmp_path: Path) -> None:
    instance, _owner, root, line = subject_world(tmp_path, (keyed_claim_item("requests"),))
    base = instance.accepted_coordinate()

    refusal = delivery._refusal(delivery.run_line(instance, root, line))

    assert refusal.code == "proposal_lowering_refused", refusal
    assert refusal.details["code"] == "cruxible.subject.key_missing"
    assert "Subject item" in refusal.message or "Subject item" in str(refusal.details)
    assert instance.accepted_coordinate() == base


def test_a_subject_item_of_an_undeclared_kind_refuses(tmp_path: Path) -> None:
    instance, _owner, root, line = subject_world(
        tmp_path, (subject_item(subject_kind="security.vendor", key={"slug": "acme"}),)
    )

    refusal = delivery._refusal(delivery.run_line(instance, root, line))

    assert refusal.code == "proposal_lowering_refused", refusal
    assert refusal.details["code"] == "cruxible.subject.kind_undeclared"


def test_create_new_on_a_unique_kind_refuses(tmp_path: Path) -> None:
    instance, _owner, root, line = subject_world(tmp_path, (subject_item(create_new=True),))

    refusal = delivery._refusal(delivery.run_line(instance, root, line))

    assert refusal.code == "proposal_lowering_refused", refusal
    assert refusal.details["code"] == "cruxible.subject.create_new_refused"
