"""The reviewer's view of a proposal: value changes in the write outcome's form, definitions.

Built once per candidate with the summary projection, from the members the cold
build already reads (O(changed members)). A page reads only its own rows; the
text a shown change cites is read then, one bounded body read per cited Capture
body (``CitedText``), never at build time.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast

from pydantic import BaseModel

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import ClaimType, claim_type_path
from cruxible_client.contracts.documents import DocumentShell
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.notes import Note
from cruxible_client.contracts.operational_reads import GetCaptureErasure, MandateScope
from cruxible_client.contracts.primitives import canonical_json
from cruxible_client.contracts.procedure_mandates import ProcedureMandate
from cruxible_client.contracts.procedures.line_specs import LineSpec
from cruxible_client.contracts.proposal_summary import (
    REVIEW_CITATIONS_MAX,
    REVIEW_CITED_TEXT_MAX_CHARS,
    ReviewChange,
    ReviewCitation,
    ReviewClaimTypeDefinition,
    ReviewDefinition,
    ReviewDefinitionDetail,
    ReviewDocumentDefinition,
    ReviewFieldChange,
    ReviewLineBinding,
    ReviewLineDefinition,
    ReviewLink,
    ReviewMandateDefinition,
    ReviewNoteDefinition,
    ReviewRunbookDefinition,
    ReviewSummaryValue,
    ReviewTriggerDefinition,
)
from cruxible_client.contracts.public_names import public_name
from cruxible_client.contracts.read_values import summary_value, whole_value_read
from cruxible_client.contracts.runbooks import Runbook
from cruxible_client.contracts.shapes import coerce_field_type, render_type
from cruxible_client.contracts.text_display import short_provider_build
from cruxible_client.contracts.triggers import ActionTarget, LineTarget, RunbookTarget, Trigger
from cruxible_client.contracts.write import ChangeOutcome, capture_handle
from cruxible_core.indexes.subject_identity import subject_kind_of
from cruxible_core.service.discovery.field_names import is_reserved_field_name

_ACTION: dict[str, Literal["created", "revised", "retired"]] = {
    "create": "created",
    "replace": "revised",
    "retire": "retired",
    "delete": "retired",
}
#: Definitions with a text block come first, in this order; the rest by kind.
_DETAIL_ORDER = {"line": 0, "procedure-mandate": 1, "runbook": 2, "trigger": 3}
_SERVICE_ACCESS = BodyAccessContext(principal_id="playbill-review-view", can_read_body=True)


def _value(value: Any, limit: int = 1024) -> ReviewSummaryValue:
    text = canonical_json(value)
    if len(text.encode("utf-8")) <= limit:
        return ReviewSummaryValue(value=value)
    return ReviewSummaryValue(
        value=text.encode("utf-8")[: limit // 2].decode("utf-8", "ignore"), truncated=True
    )


# -- value changes -----------------------------------------------------------------


def _subject_ref(path: str) -> tuple[str, str]:
    """``(kind, kind/id)`` of a Claim's subject path; a non-Subject path stays a path."""

    if path.startswith("subjects/"):
        kind, subject_id = subject_kind_of(path)
        return kind, f"{kind}/{subject_id}"
    if path.startswith("claim-types/"):
        return "semantic.claim_type", path.removesuffix(".json")
    return path.split("/", 1)[0], path.removesuffix(".json")


def _field(predicate: str, kind: str) -> str:
    """The short field name ``get`` shows: the predicate without ``kind.``.

    Every predicate holds a dot, so a dot-free short name never names another
    predicate; anything else keeps the full predicate.
    """

    short = predicate.removeprefix(f"{kind}.")
    if short != predicate and "." not in short and not is_reserved_field_name(short):
        return short
    return predicate


def _object_value(statement: Mapping[str, Any]) -> object:
    item = statement.get("object")
    if isinstance(item, Mapping):
        if item.get("kind") == "literal":
            return item.get("value")
        address = item.get("address")
        if item.get("kind") == "subject" and isinstance(address, Mapping):
            return _subject_ref(str(address.get("artifact_path", "")))[1]
    return item


def _claim_id(path: str) -> str:
    return path.rsplit("/", 1)[-1].removesuffix(".json")


def _live(artifact: Mapping[str, Any] | None) -> bool:
    return artifact is not None and artifact.get("lifecycle", {}).get("state", "live") == "live"


def claim_change_row(
    *,
    path: str,
    disposition: str,
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any] | None,
    verdict: str | None,
    cardinality: Callable[[str], str | None],
    base_oid: str,
) -> tuple[tuple[str, ...], str, str | None, str, dict[str, Any]]:
    """One Claim member as a change row: (sort key, Subject kind, field, op, stored data)."""

    artifact = new if new is not None else old
    assert artifact is not None
    statement = artifact["statement"]
    kind, subject = _subject_ref(str(statement["subject"]["artifact_path"]))
    predicate = str(statement["predicate"])
    field = _field(predicate, kind)
    claim = _claim_id(path)
    after_value = None if new is None else _object_value(new["statement"])
    before_value = None if old is None else _object_value(old["statement"])
    if disposition in {"retire", "delete"} or (disposition == "replace" and not _live(new)):
        op, shown_after = "retire", None
        if old is None:
            shown_before = summary_value(after_value)
        else:
            shown_before = summary_value(before_value, read_whole=whole_value_read(claim, base_oid))
        verdict = None
    elif disposition == "replace" and canonical_json(before_value) == canonical_json(after_value):
        op, shown_after, shown_before = "rebind", summary_value(after_value), None
    elif disposition == "replace":
        op = "set"
        shown_after = summary_value(after_value)
        shown_before = summary_value(before_value, read_whole=whole_value_read(claim, base_oid))
    else:
        op = "add" if cardinality(predicate) == "many" else "set"
        shown_after, shown_before = summary_value(after_value), None
    change = ChangeOutcome(
        op=op,  # type: ignore[arg-type]
        subject=subject,
        field=field,
        predicate=predicate,
        before=shown_before,
        after=shown_after,
        claim=claim,
        verdict=verdict,
    )
    backing = (new or {}).get("backing", {}) if op != "retire" else {}
    citations = [
        {"capture": str(item["capture_digest"]), "origin": str(item.get("origin", "legacy"))}
        for item in backing.get("citations", ())
        if isinstance(item, Mapping) and isinstance(item.get("capture_digest"), str)
    ]
    if not citations:
        citations = [
            {"capture": str(digest), "origin": "legacy"}
            for digest in backing.get("capture_digests", ())
        ]
    if old is not None and op != "retire":
        # A revision keeps its earlier revisions' citations: show what it adds.
        kept = set((old.get("backing") or {}).get("capture_digests", ()))
        citations = [item for item in citations if item["capture"] not in kept] or citations
    spans = sorted(
        {
            (str(span["content_digest"]), int(span["start_byte"]), int(span["end_byte"]))
            for mapping in backing.get("source_mappings", ())
            for span in mapping.get("spans", ())
            if isinstance(span, Mapping) and isinstance(span.get("content_digest"), str)
        }
    )
    data = {
        "change": change.model_dump(mode="json"),
        "role": statement.get("role"),
        "path": path,
        "citations": citations,
        "spans": [list(span) for span in spans],
    }
    return (subject, "1", field, claim), kind, field, op, data


def subject_change_row(
    *, path: str, disposition: str
) -> tuple[tuple[str, ...], str, str | None, str, dict[str, Any]]:
    """A Subject created or retired, as the write outcome's create or retire line."""

    kind, subject_id = subject_kind_of(path)
    subject = f"{kind}/{subject_id}"
    if disposition == "create":
        change = ChangeOutcome(op="create", subject=subject, resolved="created")
        op = "create"
    else:
        change = ChangeOutcome(op="retire", subject=subject)
        op = "retire"
    data = {"change": change.model_dump(mode="json"), "path": path, "citations": [], "spans": []}
    return (subject, "0", "", ""), kind, None, op, data


def claim_type_cardinality(read: Callable[[str], bytes | None]) -> Callable[[str], str | None]:
    """A memoized lookup of a ClaimType's cardinality, one blob read per predicate."""

    known: dict[str, str | None] = {}

    def lookup(predicate: str) -> str | None:
        if predicate not in known:
            try:
                content = read(claim_type_path(predicate))
            except ValueError:
                content = None
            value = None if content is None else json.loads(content).get("cardinality")
            known[predicate] = value if isinstance(value, str) else None
        return known[predicate]

    return lookup


class CitedText:
    """Reads the text shown changes cite: one verified body read per Capture body, per page."""

    def __init__(self, instance: Any, access: BodyAccessContext, *, withheld: bool) -> None:
        self._instance = instance
        self._bodies = instance.body_store()
        self._access = access
        self._withheld = withheld
        self._read: dict[str, bytes | None] = {}

    def _body(self, digest: str) -> bytes | None:
        if digest not in self._read:
            from cruxible_core.storage.read_positions import read_body

            try:
                self._read[digest] = read_body(self._bodies, digest, access=_SERVICE_ACCESS)
            except Exception:  # noqa: BLE001 - a missing or damaged body is unavailable here
                self._read[digest] = None
        return self._read[digest]

    def citation(self, raw: Mapping[str, Any], spans: list[list[Any]]) -> ReviewCitation:
        from cruxible_core.storage.capture_metadata import capture_envelope

        digest = str(raw["capture"])
        handle = capture_handle(digest)
        origin = str(raw.get("origin", "legacy"))
        if self._withheld:
            return ReviewCitation(capture=handle, origin=origin, text_withheld=True)
        try:
            envelope = capture_envelope(self._bodies, digest, access=_SERVICE_ACCESS)
        except Exception:  # noqa: BLE001 - an unavailable Capture shows only its handle
            return ReviewCitation(capture=handle, origin=origin, unavailable=True)
        source_ref = envelope.source
        source = getattr(source_ref, "source_identity", None)
        if source is None:
            source = getattr(getattr(envelope, "producer", None), "qualified", None)
        selector = getattr(source_ref, "selector", None)
        window = selector.get("working_selection") if isinstance(selector, Mapping) else None
        section = anchor = None
        if isinstance(window, Mapping):
            label = window.get("section")
            if isinstance(label, Mapping) and isinstance(label.get("path"), list):
                section = tuple(str(part) for part in label["path"])
            if isinstance(window.get("anchor"), str):
                anchor = window["anchor"]
        commitment = getattr(getattr(envelope, "commitment", None), "digest", None)
        ranges = [(int(span[1]), int(span[2])) for span in spans if span[0] == commitment]
        common: dict[str, Any] = {
            "capture": handle,
            "origin": origin,
            "source": source,
            "section": section or None,
            "anchor": None if section else anchor,
        }
        # Presence comes from the landing index, as reads take it (no stat or hash
        # on the clean path); an absent body is erased when an erasure names it.
        from cruxible_core.evidence.capture_erasures import erased_body
        from cruxible_core.storage.read_positions import body_present

        erasure = erased_body(self._instance, envelope)
        if erasure is not None:
            return ReviewCitation(
                **common,
                erased=GetCaptureErasure(
                    by=erasure.erased_by, at=erasure.erased_at, because=erasure.because
                ),
            )
        # Missing or damaged, whether or not it maps an excerpt: the landing index
        # answers, without reading the body.
        if commitment is not None and body_present(self._bodies, str(commitment)) is False:
            return ReviewCitation(**common, unavailable=True)
        if not ranges or commitment is None:
            return ReviewCitation(**common)
        if not self._access.can_read_body:
            return ReviewCitation(**common, text_withheld=True)
        body = self._body(str(commitment))
        if body is None:
            return ReviewCitation(**common, unavailable=True)
        text = " ... ".join(
            body[start:end].decode("utf-8", "replace").strip() for start, end in ranges
        )
        cut = len(text) > REVIEW_CITED_TEXT_MAX_CHARS
        return ReviewCitation(
            **common,
            text=text[:REVIEW_CITED_TEXT_MAX_CHARS].rstrip() if cut else text,
            text_truncated=cut,
        )


def review_change(
    data: Mapping[str, Any],
    *,
    cited: CitedText,
    reason: Callable[[str], str | None],
    withheld: bool,
) -> ReviewChange:
    """One stored change row as a page shows it, with its citations and item reason."""

    change = ChangeOutcome.model_validate(data["change"])
    if withheld:
        change = change.model_copy(update={"after": None, "verdict": None})
    citations = list(data.get("citations", ()))
    shown = tuple(
        cited.citation(item, list(data.get("spans", ())))
        for item in citations[:REVIEW_CITATIONS_MAX]
    )
    rationale = None if withheld or change.op in {"create"} else reason(str(data["path"]))
    return ReviewChange(
        change=change,
        role=data.get("role"),
        rationale=rationale,
        citations=shown,
        citations_more=max(0, len(citations) - REVIEW_CITATIONS_MAX),
    )


# -- definitions -------------------------------------------------------------------


def _identity(artifact: Mapping[str, Any], path: str, artifact_kind: str) -> tuple[str, str]:
    identity = artifact.get("identity")
    if isinstance(identity, Mapping) and identity.get("kind") and identity.get("name"):
        return str(identity["kind"]), str(identity["name"])
    if isinstance(identity, str) and ":" in identity:
        kind, _, name = identity.partition(":")
        return kind[:1].upper() + kind[1:], name
    # Versioned definitions (Capability, Contract) name themselves NAME/vN.
    kind = "".join(part.capitalize() for part in artifact_kind.split("-"))
    stored, version = artifact.get("name"), artifact.get("version")
    if isinstance(stored, str) and isinstance(version, int):
        return kind, f"{stored}/v{version}"
    if isinstance(stored, str):
        return kind, stored
    return kind, path.removesuffix(".json").split("/", 1)[-1]


def _line_detail(spec: LineSpec) -> ReviewLineDefinition:
    parameters = spec.parameters
    return ReviewLineDefinition(
        procedure=spec.procedure.target.name,
        authority=spec.max_authority,
        bindings=tuple(
            ReviewLineBinding(
                slot=slot,
                provider=short_provider_build(binding.build.target.name),
                operation=binding.operation,
            )
            for slot, binding in spec.bindings.items()
        ),
        parameters=(
            {str(name): _value(value) for name, value in parameters.items()}
            if isinstance(parameters, Mapping)
            else {}
        ),
        acquisition={
            output: tuple(getattr(rule, "origins", None) or ())
            for output, rule in spec.acquisition.items()
        },
        budgets={
            name: value
            for name, value in spec.budgets.model_dump(mode="json").items()
            if isinstance(value, int)
        },
        inbox=tuple(spec.findings.inbox),
        trigger_input=spec.trigger_input,
    )


def _mandate_detail(mandate: ProcedureMandate) -> ReviewMandateDefinition:
    return ReviewMandateDefinition(
        line=mandate.line.target.name,
        grants=mandate.grants,
        valid_from=mandate.valid_from,
        expires_at=mandate.expires_at,
        namespace=mandate.namespace,
        scope=tuple(
            MandateScope(
                claim_type=item.claim_type.target.qualified,
                changes=item.changes,
                value=item.value,
                binds=item.binding_subject_role,
            )
            for item in mandate.scope
        ),
        subjects=mandate.subjects,
        condition=None if mandate.condition is None else mandate.condition.query.target.qualified,
        subject_creation=mandate.subject_creation,
        suspended=mandate.suspended,
    )


def _step(step: object) -> str:
    if isinstance(step, LineTarget):
        return step.line.target.qualified
    if isinstance(step, ActionTarget):
        return f"action:{step.action}"
    return str(step)


def _trigger_detail(trigger: Trigger) -> ReviewTriggerDefinition:
    from cruxible_core.service.discovery.operational import schedule_summary

    target = trigger.target
    shown = target.runbook.target.qualified if isinstance(target, RunbookTarget) else _step(target)
    return ReviewTriggerDefinition(schedule=schedule_summary(trigger.schedule), target=shown)


def _claim_type_detail(claim_type: ClaimType) -> ReviewClaimTypeDefinition:
    """A new field's type, kinds, roles and admitted evidence, names public."""

    try:
        value: str = render_type(coerce_field_type(cast(Any, claim_type.value)))
    except (FormatError, ValueError):
        value = canonical_json(claim_type.model_dump(mode="json")["value"])
    default = claim_type.default_role
    roles = tuple(claim_type.permitted_roles)
    if default in roles:
        roles = (default, *(role for role in roles if role != default))
    evidence: list[str] = []
    for rule in claim_type.evidence_admission_policy.rules:
        for item in rule.capture_contracts:
            name = public_name(item.target.qualified).removeprefix("CaptureContract:")
            if name not in evidence:
                evidence.append(name)
        evidence.extend(
            f"family:{family}"
            for family in rule.capture_contract_families
            if f"family:{family}" not in evidence
        )
    return ReviewClaimTypeDefinition(
        value=value,
        cardinality=claim_type.cardinality,
        subject_kinds=tuple(claim_type.allowed_subject_kinds),
        roles=roles,
        default_role=claim_type.default_role,
        evidence_requirement=claim_type.evidence_requirement,
        evidence=tuple(evidence),
        description=None if claim_type.description is None else _value(claim_type.description, 512),
    )


def _document_detail(shell: DocumentShell) -> ReviewDocumentDefinition:
    return ReviewDocumentDefinition(
        title=shell.title,
        document_kind=shell.document_kind,
        links=tuple(
            ReviewLink(relation=link.relation, target=link.target_identity) for link in shell.links
        ),
    )


def _note_detail(note: Note) -> ReviewNoteDefinition:
    return ReviewNoteDefinition(
        file=note.source_id,
        section=note.section,
        about=tuple(item.name for item in note.about),
    )


def _detail(artifact_kind: str, artifact: Mapping[str, Any]) -> ReviewDefinitionDetail | None:
    """The typed text block of a Line, Mandate, Runbook, Trigger, ClaimType, Document or
    note; None for other kinds."""

    parse: dict[str, Callable[[Mapping[str, Any]], BaseModel]] = {
        "line": lambda item: _line_detail(LineSpec.model_validate(item)),
        "procedure-mandate": lambda item: _mandate_detail(ProcedureMandate.model_validate(item)),
        "runbook": lambda item: ReviewRunbookDefinition(
            description=(runbook := Runbook.model_validate(item)).description,
            steps=tuple(_step(step) for step in runbook.steps),
        ),
        "trigger": lambda item: _trigger_detail(Trigger.model_validate(item)),
        "claim-type": lambda item: _claim_type_detail(ClaimType.model_validate(item)),
        "document": lambda item: _document_detail(DocumentShell.model_validate(item)),
        "note": lambda item: _note_detail(Note.model_validate(item)),
    }
    builder = parse.get(artifact_kind)
    if builder is None:
        return None
    try:
        return builder(artifact)  # type: ignore[return-value]
    except ValueError:
        return None


def definition_row(
    *,
    path: str,
    artifact_kind: str,
    disposition: str,
    old: Mapping[str, Any] | None,
    new: Mapping[str, Any] | None,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """One definition member: (sort key, the stored ``ReviewDefinition``).

    ``before`` and ``after`` are a revision's changed top-level fields only.
    """

    artifact = new if new is not None else old
    assert artifact is not None
    kind, name = _identity(artifact, path, artifact_kind)
    action: Literal["created", "revised", "retired"] = _ACTION[disposition]
    if action == "revised" and not _live(new):
        action = "retired"
    detail = (
        None
        if action == "retired"
        or new is None
        # A revised ClaimType lists its changed fields below instead.
        or (artifact_kind == "claim-type" and action != "created")
        else _detail(artifact_kind, new)
    )
    changed: tuple[ReviewFieldChange, ...] = ()
    if action == "revised" and before is not None and after is not None:
        changed = tuple(
            ReviewFieldChange(
                field=field,
                before=None if field not in before else _value(before[field], 512),
                after=None if field not in after else _value(after[field], 512),
            )
            for field in sorted(set(before) | set(after))
            if field != "artifact_digest"
        )
    definition = ReviewDefinition(
        kind=kind, name=name, action=action, detail=detail, changed=changed
    )
    rank = _DETAIL_ORDER.get(artifact_kind, len(_DETAIL_ORDER))
    return (rank, kind, name), definition.model_dump(mode="json")


def subject_revision_row(path: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """A Subject revised in place (its pins): a one-line definition row."""

    kind, subject_id = subject_kind_of(path)
    definition = ReviewDefinition(kind="Subject", name=f"{kind}/{subject_id}", action="revised")
    return (len(_DETAIL_ORDER), "Subject", definition.name), definition.model_dump(mode="json")


__all__ = [
    "CitedText",
    "claim_change_row",
    "claim_type_cardinality",
    "definition_row",
    "review_change",
    "subject_change_row",
    "subject_revision_row",
]
