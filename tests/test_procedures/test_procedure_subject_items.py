"""A `propose_change_set` terminal creates Subjects by kind and key, never by id (identity D1).

A Subject item names a declared kind and its key values; core assigns the id. A
key a live Subject already holds names that Subject, and the item creates
nothing (reported ``reused``). A Claim item of the same terminal names a Subject
the run knows only by its key with a ``SubjectKeyReference`` (v3 item): it
resolves to the Subject the terminal's own Subject item lands on, else through
the common resolver over the base (exact live id, alias, key, exact retired
id), and refuses when none or several hold it. Every reference is checked again
over the final candidate; one that resolves otherwise there refuses the terminal
(``cruxible.procedure.identity_reference_unstable``).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

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


# -- E-b delta review F-002: one terminal's key changes are one overlay -------------------


def _second_terminal(instance, owner, templates: tuple[object, ...]):  # type: ignore[no-untyped-def]
    """A second accepted Procedure, Line and mandate whose terminal carries ``templates``."""

    from cruxible_client.contracts.artifacts import ArtifactLifecycle
    from cruxible_client.contracts.procedure_mandates import (
        parse_procedure_mandate,
        procedure_mandate_digest,
    )
    from cruxible_client.contracts.procedures.artifacts import parse_procedure, procedure_path

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    path = procedure_path(fixtures.PROCEDURE_NAME)
    line = delivery._revising_line(
        instance, owner, parse_procedure(tree[path], path=path), templates
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    mandate_path = procedure_mandate_path("advisory-revise-mandate")
    mandate = parse_procedure_mandate(tree[mandate_path], path=mandate_path)
    reaching = mandate.model_copy(
        update={
            "namespace": (
                "capture-contracts/playbill.coordinator-self-source-v1.json",
                "claims",
                f"subjects/{KIND}",
            ),
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_mandate_digest(mandate).tagged
            ),
        }
    )
    fixtures._accept_more(
        instance, owner, {mandate_path: render_procedure_mandate(reaching)}, name="reach-packages"
    )
    return line


def _key_move(instance, subject: str, value: str) -> dict[str, object]:  # type: ignore[no-untyped-def]
    """A v2 Claim item revising ``subject``'s slug key Claim to ``value``."""

    from cruxible_client.contracts.semantic import SemanticAddress

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    (key,) = (
        parse_claim(tree[path], path=path)
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == SLUG
        and parse_claim(tree[path], path=path).statement.subject.artifact_path == subject
    )
    return {
        "tag": "playbill-procedure-claim-proposal-item-v2",
        "statement": {
            "tag": "playbill-authoring-claim-statement-v1",
            "subject": SemanticAddress.whole_artifact(subject).model_dump(mode="json"),
            "predicate": SLUG,
            "object": {"kind": "literal", "value": value},
            "role": "normative",
        },
        "rationale": "The old Subject now has another key.",
        "revises": key.identity.name,
        "source": _self_source(value),
    }


def _unstable(instance, root, line) -> Any:  # type: ignore[no-untyped-def]
    """Run ``line`` and return its refusal, asserting it is the unstable-reference one."""

    base = instance.accepted_coordinate()
    refusal = delivery._refusal(delivery.run_line(instance, root, line))
    assert refusal.code == "proposal_lowering_refused", refusal
    assert refusal.details["code"] == "cruxible.procedure.identity_reference_unstable", refusal
    assert "separate runs or terminals" in refusal.message
    assert instance.accepted_coordinate() == base  # nothing proposed, nothing accepted
    return refusal


def _packages(tmp_path: Path, *slugs: str):  # type: ignore[no-untyped-def]
    """A world whose first run accepted one package per slug."""

    instance, owner, root, line = subject_world(
        tmp_path, tuple(subject_item(slug) for slug in slugs)
    )
    first = _delivered(instance, delivery.run_line(instance, root, line))
    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first.proposal_id)
    )
    return instance, owner, root


def _keyed(item: dict[str, object], slug: str) -> dict[str, object]:
    """``item`` as a v3 Claim item naming its Subject by the slug ``slug``."""

    statement = dict(item["statement"])  # type: ignore[arg-type]
    statement["tag"] = "playbill-procedure-claim-statement-v1"
    statement["subject"] = {"subject_kind": KIND, "key": {"slug": slug}}
    return {**item, "tag": "playbill-procedure-claim-proposal-item-v3", "statement": statement}


def test_a_reference_that_a_key_move_and_recreation_would_redirect_refuses(
    tmp_path: Path,
) -> None:
    """Review probe ``procedure_overlay2`` under the delta-2 rule.

    The terminal moves ``click``'s key to ``renamed``, creates a Subject keyed
    ``click`` (core names it ``click-<hex>``), and states a Claim naming key
    ``click``. Frozen, that reference names the new Subject item; over the final
    state ``click`` is the id of the old Subject. The results differ, so the
    terminal refuses instead of choosing either.
    """

    instance, owner, root = _packages(tmp_path, "Click")
    old = f"subjects/{KIND}/click.json"
    revising = _second_terminal(
        instance, owner, (_key_move(instance, old, "renamed"), subject_item(), keyed_claim_item())
    )

    refusal = _unstable(instance, root, revising)

    (changed,) = refusal.details["references"]
    assert changed["final"] == old and str(changed["frozen"]).startswith(f"subjects/{KIND}/click-")


def test_a_frozen_key_missing_the_terminal_would_supply_refuses_as_unstable(
    tmp_path: Path,
) -> None:
    """A key no Subject holds at the base, given to flask by the terminal itself."""

    instance, owner, root = _packages(tmp_path, "Click", "Flask")
    flask = f"subjects/{KIND}/flask.json"
    revising = _second_terminal(
        instance, owner, (_key_move(instance, flask, "requests"), keyed_claim_item("requests"))
    )

    refusal = _unstable(instance, root, revising)

    (changed,) = refusal.details["references"]
    assert changed == {
        "reference": {"subject_kind": KIND, "key": {"slug": "requests"}},
        "frozen": "key_missing",
        "final": flask,
    }


def test_a_keyed_alias_owner_that_redirects_a_sibling_reference_refuses(
    tmp_path: Path,
) -> None:
    """Review probe ``extra_delta2`` (keyed alias): never accept the Claim about other-name.

    One item (its owner named by key ``click``) gives click the alias ``other
    name``; another names the Subject keyed ``other name``. Frozen at the base,
    that is other-name; over the final state the alias names click first.
    """

    instance, owner, root = _packages(tmp_path, "Click", "Other Name")
    alias = {
        "tag": "playbill-procedure-claim-proposal-item-v3",
        "statement": {
            "tag": "playbill-procedure-claim-statement-v1",
            "subject": {"subject_kind": KIND, "key": {"slug": "click"}},
            "predicate": "semantic.alias",
            "object": {"kind": "literal", "value": "other name"},
            "role": "normative",
        },
        "rationale": "Another name for click",
        "source": _self_source("also known as other name"),
    }
    revising = _second_terminal(instance, owner, (alias, keyed_claim_item("other name")))

    refusal = _unstable(instance, root, revising)

    (changed,) = refusal.details["references"]
    assert changed["frozen"] == f"subjects/{KIND}/other-name.json"
    assert changed["final"] == f"subjects/{KIND}/click.json"


def test_a_keyed_owner_moving_its_key_then_referenced_by_it_refuses(tmp_path: Path) -> None:
    """Review probe ``extra_delta2`` (keyed key move): never a false ``key_missing``."""

    instance, owner, root = _packages(tmp_path, "Click")
    move = _keyed(_key_move(instance, f"subjects/{KIND}/click.json", "requests"), "click")
    revising = _second_terminal(instance, owner, (move, keyed_claim_item("requests")))

    refusal = _unstable(instance, root, revising)

    finals = {str(item["final"]) for item in refusal.details["references"]}
    assert f"subjects/{KIND}/click.json" in finals


def test_a_stable_terminal_still_creates_and_names_its_subject(tmp_path: Path) -> None:
    """References that resolve alike before and after lowering are delivered as before."""

    instance, owner, root = _packages(tmp_path, "Click")
    flask = f"subjects/{KIND}/flask.json"
    revising = _second_terminal(
        instance, owner, (subject_item("Flask"), keyed_claim_item("flask"), keyed_claim_item())
    )

    egress = _delivered(instance, delivery.run_line(instance, root, revising))

    accept_proposal(
        instance, owner, service_inspect_playbill_proposal(instance, proposal_id=egress.proposal_id)
    )
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    severity = sorted(
        parse_claim(tree[path], path=path).statement.subject.artifact_path
        for path in tree
        if path.startswith("claims/")
        and parse_claim(tree[path], path=path).statement.predicate == SEVERITY
    )
    assert severity == [f"subjects/{KIND}/click.json", flask]


def test_a_procedure_delivery_records_each_creations_actual_outcome(tmp_path: Path) -> None:
    """The operation's creation record holds the created first holder it assigned (F-001)."""

    from cruxible_core.authoring.assignment_records import SubjectAssignmentRecords
    from cruxible_core.indexes.subject_identity import RecordedCreation

    instance, _owner, root, line = subject_world(tmp_path, (subject_item(),))
    _delivered(instance, delivery.run_line(instance, root, line))
    records = SubjectAssignmentRecords(instance.root / instance.descriptor.storage.exhaust)
    entries = {
        entry
        for path in records.directory.glob("AIT-*.json")
        for record in (records.record(path.stem),)
        if record is not None
        for entry in record.recorded()
        if entry.kind == KIND
    }
    (entry,) = entries
    assert entry == RecordedCreation(
        member=entry.member,
        kind=KIND,
        key=entry.key,
        rule="first_holder",
        outcome="created",
        subject_id="click",
    )


# -- E-b delta-3 review F-001: a qualified alias is an alias in the stability check ----


def _qualified_alias(owner_slug: str, value: str) -> dict[str, object]:
    """A v3 item (owner named by key) giving ``owner_slug`` the qualified alias ``value``."""

    return {
        "tag": "playbill-procedure-claim-proposal-item-v3",
        "statement": {
            "tag": "playbill-procedure-claim-statement-v1",
            "subject": {"subject_kind": KIND, "key": {"slug": owner_slug}},
            "predicate": "semantic.alias",
            "qualifier": "local-name",
            "object": {"kind": "literal", "value": value},
            "role": "normative",
        },
        "rationale": f"Another name for {owner_slug}",
        "source": _self_source(f"also known as {value}"),
    }


def test_a_qualified_alias_redirecting_a_sibling_reference_refuses(tmp_path: Path) -> None:
    """Review probe ``qualifier_delta3``: the qualified alias counts exactly as accepted state
    indexes it, so the reference to ``other name`` is unstable and nothing is accepted."""

    instance, owner, root = _packages(tmp_path, "Click", "Other Name")
    revising = _second_terminal(
        instance, owner, (_qualified_alias("click", "other name"), keyed_claim_item("other name"))
    )

    refusal = _unstable(instance, root, revising)

    (changed,) = refusal.details["references"]
    assert changed["frozen"] == f"subjects/{KIND}/other-name.json"
    assert changed["final"] == f"subjects/{KIND}/click.json"


def test_a_qualified_alias_terminal_refuses_before_its_door_so_recovery_has_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review probe ``qualifier_recovery_delta3``: no proposal exists to recover."""

    from datetime import timedelta

    from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress

    instance, owner, root = _packages(tmp_path, "Click", "Other Name")
    revising = _second_terminal(
        instance, owner, (_qualified_alias("click", "other name"), keyed_claim_item("other name"))
    )
    base = instance.accepted_coordinate()
    with monkeypatch.context() as crash:
        delivery._crash_after(crash, after_submit=False)
        refusal = delivery._refusal(delivery.run_line(instance, root, revising))
    assert refusal.details["code"] == "cruxible.procedure.identity_reference_unstable"
    recovered = service_recover_proposal_egress(
        instance, recorded_at=fixtures.NOW + timedelta(minutes=1)
    )
    assert not recovered
    assert instance.accepted_coordinate() == base


def test_settle_preparation_refuses_the_same_unstable_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Propose and settle share one preparation: the settle form of the terminal refuses too."""

    from cruxible_core.procedures import proposal_delivery
    from cruxible_core.procedures.terminal_services import ProposalDeliveryRefused

    instance, owner, root = _packages(tmp_path, "Click", "Other Name")
    revising = _second_terminal(
        instance, owner, (_qualified_alias("click", "other name"), keyed_claim_item("other name"))
    )
    seen: list[dict[str, Any]] = []
    prepare = proposal_delivery.ProposalTerminalEgressSink.prepare_terminal_egress

    def recorded(self, **arguments):  # type: ignore[no-untyped-def]
        seen.append(arguments)
        return prepare(self, **arguments)

    monkeypatch.setattr(
        proposal_delivery.ProposalTerminalEgressSink, "prepare_terminal_egress", recorded
    )
    _unstable(instance, root, revising)
    (arguments,) = seen
    # The preparation reads the request's kind only to pick a mandate, after
    # the stability check: the same request as a settle terminal refuses alike.
    settle = arguments["request"].model_copy(update={"kind": "settle_change_set"})
    sink = proposal_delivery.ProposalTerminalEgressSink(instance=instance, accepted_mandates={})
    with pytest.raises(ProposalDeliveryRefused) as refused:
        prepare(sink, **{**arguments, "request": settle})
    assert refused.value.details["code"] == "cruxible.procedure.identity_reference_unstable"
