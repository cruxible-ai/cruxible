"""A request's shared verification context answers exactly as fresh contexts would.

The context keeps finished Capture verifications and what they derived, so a
warm answer may only ever be the cold answer: anything that depends on which
bodies the store holds is re-checked against what the store holds now.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    ExistingCaptureCitationSource,
    authoring_member_identity,
)
from cruxible_client.contracts.captures import (
    InputReceiptSetManifest,
    build_derived_cas_capture,
    build_working_selection_capture,
    verify_capture,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.exhaust.producer_receipts import LocalProducerReceiptResolver
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._pc_c_support import (
    NOW,
    artifact_digest,
    body_store,
    capture_contract,
    digest,
    provider,
    provider_run,
)
from tests.test_authoring.test_authoring_existing_capture import _activate, _self_asserted
from tests.test_indexes.test_projection_window_evidence import (
    BLOCK_BODY,
    EVIDENCE_CODE,
    TIMESTAMP,
    _page,
    _selection_payload,
    _world,
)


def _whole_page_lands_mid_request(
    tmp_path: Path,
) -> tuple[Any, AuthoringIntentCoordinator, Any, str]:
    """Two members: a retained selection inside a block whose page is not held,
    then a member presenting the whole page for prose outside the block."""

    instance, _coordinator, actor = _world(tmp_path)
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    page = _page()
    inside = _selection_payload(
        page, anchor=BLOCK_BODY, citation_role="evidence", present_page=False
    )
    built = build_working_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id="CLM-" + "a" * 32,
        rationale="A retained selection of the source.",
        observed_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        source_id=inside.source.source_id,
        coordinate=inside.source.coordinate.model_dump(mode="json"),
        selector=inside.source.selector.model_dump(mode="json"),
        selected_content=inside.source.selected_content,
    )
    outside = _selection_payload(
        page, anchor=b"trailing prose", citation_role="evidence", present_page=True
    )
    second = ClaimAuthoringPayload(
        statement=outside.statement.model_copy(update={"qualifier": "zzz"}),
        rationale="Present the whole source for prose outside its block.",
        source=outside.source,
        citation_role="evidence",
        dependency_drafts=ClaimDependencyDrafts(),
    )
    first = ClaimAuthoringPayload(
        statement=inside.statement,
        rationale="Cite the retained selection.",
        source=ExistingCaptureCitationSource(capture_digest=built.capture_digest),
        citation_role="evidence",
        dependency_drafts=ClaimDependencyDrafts(),
    )
    # Order the members so the retained citation lowers before the page lands.
    for index in range(1000):
        first = first.model_copy(
            update={"statement": first.statement.model_copy(update={"qualifier": f"in-{index}"})}
        )
        if authoring_member_identity(first) < authoring_member_identity(second):
            break
    members = tuple(sorted((first, second), key=lambda m: authoring_member_identity(m).encode()))
    assert members[0] is first
    intent = coordinator.create(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=members, rationale="Two selections of one source"
        ),
        canonical_timestamp=TIMESTAMP,
    ).intent
    return instance, coordinator, actor, intent.intent_id


def _cold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every verification gets a fresh context: no request shares one."""

    from cruxible_core.runtime import instance as runtime_instance

    monkeypatch.setattr(runtime_instance, "requested_verification_context", lambda _key: None)
    monkeypatch.setattr(
        runtime_instance,
        "verification_request",
        lambda _key, open_context: nullcontext(open_context()),
    )


@pytest.mark.parametrize("warm", [True, False])
def test_a_page_landing_later_in_the_request_reopens_the_cited_window(
    tmp_path: Path, warm: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, coordinator, actor, intent_id = _whole_page_lands_mid_request(tmp_path)
    if not warm:
        _cold(monkeypatch)
    with instance.capture_verification_request():
        submitted = coordinator.submit(intent_id, actor=actor)
    # Warm or cold, the whole page now shows the first citation is in the block.
    assert submitted.status.proposal_id is None
    assert submitted.intent.last_preflight is not None
    assert EVIDENCE_CODE in [d.code for d in submitted.intent.last_preflight.frontier.diagnostics]
    # Nothing was accepted that a cold recovery could not reproduce.
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()


def _derived(tmp_path: Path):  # type: ignore[no-untyped-def]
    contract = _self_asserted(capture_contract(epistemic_grade="derived"))
    producer = provider(contract)
    store = body_store(tmp_path)
    built = build_derived_cas_capture(
        store=store,
        contract=contract,
        output_body=b'{"derived":true}',
        manifest=InputReceiptSetManifest(
            input_receipt_digests=(digest("receipt", "input"),),
            input_capture_digests=(digest("capture", "input"),),
            input_claim_artifact_digests=(artifact_digest("claim", "input"),),
        ),
        reducer_digest=artifact_digest("reducer", "release-v1"),
        run_coordinate=provider_run(producer),
        run_receipt_digest=digest("receipt", "derived"),
        producer=producer.identity,
        producer_binding_digest=digest("binding", "derived"),
        observed_at=NOW,
    )

    def context() -> LocalProducerReceiptResolver:
        return LocalProducerReceiptResolver(
            exhaust_root=tmp_path / "exhaust",
            instance_id="warm-cold",
            bodies=store,
            full_journal=False,
        )

    def verify(resolver: LocalProducerReceiptResolver) -> object:
        return verify_capture(
            built.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests={
                producer.identity.qualified: provider_run(producer).executable_digest
            },
            producer_receipt_resolver=resolver,
        )

    return store, built, context, verify


def _outcome(call: Callable[[], object]) -> str:
    try:
        call()
    except (CruxibleError, ValueError) as exc:
        return f"refused: {type(exc).__name__}: {exc}"
    return "verified"


@pytest.mark.parametrize("damage", ["erase", "corrupt"])
def test_a_derived_capture_needs_its_input_manifest_warm_as_cold(
    tmp_path: Path, damage: str
) -> None:
    store, built, context, verify = _derived(tmp_path)
    warm = context()
    assert _outcome(lambda: verify(warm)) == "verified"
    manifest = built.envelope.input_receipt_set_manifest_digest
    assert manifest is not None
    if damage == "erase":
        assert store.erase(manifest)
    else:
        path = store._path(manifest)
        path.chmod(0o600)
        path.write_bytes(b"not the manifest")
    cold = _outcome(lambda: verify(context()))
    assert cold.startswith("refused")
    assert _outcome(lambda: verify(warm)) == cold


def test_warm_and_cold_requests_give_identical_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same scenarios through one shared context and through fresh contexts."""

    outcomes: dict[bool, list[object]] = {}
    for warm in (True, False):
        if not warm:
            _cold(monkeypatch)
        root = tmp_path / ("warm" if warm else "cold")
        (root / "window").mkdir(parents=True)
        instance, coordinator, actor, intent_id = _whole_page_lands_mid_request(root / "window")
        with instance.capture_verification_request():
            submitted = coordinator.submit(intent_id, actor=actor)
            if submitted.status.proposal_id is not None:
                _activate(instance, submitted)
        assert submitted.intent.last_preflight is not None
        window = (
            submitted.status.state,
            tuple(d.code for d in submitted.intent.last_preflight.frontier.diagnostics),
        )
        (root / "derived").mkdir()
        store, built, context, verify = _derived(root / "derived")
        shared = context()
        derived = [_outcome(lambda: verify(shared if warm else context()))]
        manifest = built.envelope.input_receipt_set_manifest_digest
        assert manifest is not None
        store.erase(manifest)
        derived.append(_outcome(lambda: verify(shared if warm else context())))
        outcomes[warm] = [window, derived]
    assert outcomes[True] == outcomes[False]
