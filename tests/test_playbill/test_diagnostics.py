"""PB-C typed-diagnostic authority and redaction-boundary tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.diagnostics import (
    CompilerDiagnostic,
    GovernedOperationReference,
    LocalDraftEdit,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress


def _subject(name: str) -> SemanticAddress:
    return SemanticAddress.whole_artifact(f"documents/{name}.yaml")


def test_diagnostic_code_and_related_subjects_survive_message_rewording() -> None:
    subject = _subject("design")
    related = (_subject("reference"),)
    first = CompilerDiagnostic(
        code="playbill.document.stale_predecessor",
        severity="error",
        message="The predecessor is stale.",
        subject=subject,
        related_subjects=related,
    )
    reworded = first.model_copy(update={"message": "Rebase onto the current predecessor."})
    assert reworded.code == first.code
    assert reworded.subject == first.subject
    assert reworded.related_subjects == first.related_subjects


def test_governed_operation_references_carry_no_authority_or_mutation_payload() -> None:
    reference = GovernedOperationReference(operation="propose", subject=_subject("design"))
    assert reference.operation == "propose"
    for forbidden in (
        {"credential": "secret"},
        {"approval": "yes"},
        {"authority": "admin"},
        {"mutation_payload": {"write": "main"}},
        {"transport": "git-receive-pack"},
    ):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            GovernedOperationReference.model_validate({"operation": "propose", **forbidden})


def test_local_edits_can_name_only_exact_unaccepted_draft_bytes() -> None:
    payload = {
        "draft_id": "draft-1",
        "content_digest": "sha256:" + "22" * 32,
        "start_byte": 0,
        "end_byte": 0,
        "replacement_text": "title",
    }
    assert LocalDraftEdit.model_validate(payload).draft_id == "draft-1"
    for forbidden in (
        {"path": "documents/accepted.yaml"},
        {"git_oid": "a" * 40},
        {"semantic_root": "sha256:" + "33" * 32},
        {"generation_root": "sha256:" + "44" * 32},
    ):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            LocalDraftEdit.model_validate({**payload, **forbidden})


def test_diagnostic_redaction_removes_protected_span_but_preserves_identity() -> None:
    diagnostic = CompilerDiagnostic(
        code="playbill.document.body_invalid",
        severity="error",
        message="Body bytes do not match the declared format.",
        subject=_subject("design"),
        span=ContentSpan(
            content_digest="sha256:" + "55" * 32,
            start_byte=0,
            end_byte=12,
        ),
    )
    redacted = diagnostic.without_protected_body_metadata()
    assert redacted.span is None
    assert redacted.code == diagnostic.code
    assert redacted.subject == diagnostic.subject
