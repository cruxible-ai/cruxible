"""Coverage-bounded absence inference, using the Claims eligibility and lineage laws."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Literal, NoReturn, Sequence

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claim_evidence import (
    AbsenceWitness,
    CaptureBasis,
    ClaimEvidenceReceipt,
    ClaimsDiagnostic,
)
from cruxible_client.contracts.claims import NoneClaimObject, claim_artifact_digest
from cruxible_client.contracts.content_memo import seal
from cruxible_client.contracts.procedures.models import CallNode, ExtractNode
from cruxible_core.procedures.claims_changes import (
    CoreClaimChange,
    CoreReobservationChange,
    CoreRetirementChange,
)


def object_unit_key(obj: dict[str, Any], claim_type: Any, context: Any) -> bytes:
    """Canonical unit identity for a resolved object, including object-key inverses."""
    from cruxible_client.contracts.semantic import SemanticAddress

    inverse = claim_type.inverse
    if inverse is None:
        return canonical_bytes(obj)
    if inverse.key.kind == "slot":
        return b"slot"
    if obj["kind"] == "none":
        return canonical_bytes(obj.get("of"))
    if inverse.key.kind == "object":
        return canonical_bytes(obj)
    address = SemanticAddress.model_validate(obj["address"])
    held = context.slot(address, inverse.key.predicate, None)
    if len(held) != 1 or held[0].statement.object.kind != "literal":
        raise ValueError("claims_absence_invalid: inverse object key is unresolved")
    return canonical_bytes(held[0].statement.object.model_dump(mode="json"))


def append_absence(
    node: Any,
    *,
    executor: Any,
    admission: Any,
    state: Any,
    context: Any,
    resolve: Callable[..., Any],
    lineage: Callable[..., Any],
    values: list[Any],
    tokens: list[Any],
    evidence: list[Any],
    changes: list[Any],
    diagnostics: Any,
    observed: set[tuple[str, str, bytes, bytes]],
) -> None:
    """One pass over enumeration and covers; every Claim read is an indexed slot read.

    No covers means no absence work. Enumeration includes refused keys, so a
    duplicate/invalid record cannot be mistaken for a missing record. Findings
    are run-owned; the existing Line routing policy alone chooses inbox kinds.
    """
    if node.covers is None:
        return
    from cruxible_core.procedures.execution import (
        _exact_evidence,
        _mentions_item_reference,
        _referenced_aliases,
        _referenced_reads,
        _RunRefusal,
    )
    from cruxible_core.procedures.transforms import _record, _truth

    def refuse(message: str) -> NoReturn:
        raise _RunRefusal("proposal_item_invalid", message, node_id=node.node_id)

    covered = resolve(node.covers)
    enumeration = resolve(node.enumeration)
    if not isinstance(covered, list):
        refuse("claims_absence_invalid: covers must be a Subject list")
    if isinstance(enumeration, dict) and isinstance(enumeration.get("results"), list):
        enumerated = enumeration["results"]
    else:
        selected_records = (
            enumeration.get("records") if isinstance(enumeration, dict) else enumeration
        )
        if not isinstance(selected_records, list):
            refuse("claims_absence_invalid: enumeration must retain typed records/results")
        enumerated = [
            item
            if isinstance(item, dict)
            and item.get("tag") in {"record", "invalid_record", "duplicate"}
            else {"tag": "record", "record": _record(item, index, "enumeration")}
            for index, item in enumerate(selected_records)
        ]
    sources: dict[str, Any] = {}
    for alias in _referenced_aliases(node.enumeration):
        roots, _, _ = lineage(alias)
        sources.update((source.node_id, source) for source in roots)
    if len(sources) != 1 or not isinstance(next(iter(sources.values())), (ExtractNode, CallNode)):
        refuse("claims_absence_invalid: enumeration must name one declared source lineage")
    extracting = next(iter(sources.values()))
    binding = (
        executor.slot_bindings.get(extracting.slot) if isinstance(extracting, CallNode) else None
    )
    if isinstance(extracting, CallNode) and (
        binding is None or binding.operation.enumerates is None
    ):
        refuse("claims_absence_invalid: a provider parser must declare enumerates and field paths")
    own = _exact_evidence(
        [node.enumeration, node.covers], state=state, node_id=node.node_id, field="absence"
    )
    captures = {
        t.digest
        for t in _exact_evidence(
            node.enumeration, state=state, node_id=node.node_id, field="enumeration"
        )
        if t.slot in {"produced_capture", "admitted_capture"}
    }
    if len(captures) != 1:
        refuse("claims_absence_invalid: enumeration needs exactly one Capture")
    capture = next(iter(captures))
    logical_source = context.logical_source(capture)
    if logical_source is None:
        refuse("claims_absence_invalid: enumeration Capture has no logical source")
    from cruxible_client.contracts.procedures.extract import EXTRACTOR_VERSIONS
    from cruxible_core.procedures.claims_terminal import _receipt_extract_digest

    if isinstance(extracting, ExtractNode):
        extract_spec = _receipt_extract_digest(extracting)
        contract = executor.capture_contracts.get(state.capture_contracts.get(capture, ""))
        extractor = EXTRACTOR_VERSIONS[
            getattr(getattr(contract, "decoding", None), "format", "json")
        ]
    else:
        assert binding is not None
        extract_spec = binding.operation_signature_digest
        extractor = f"provider:{binding.operation.name}:{binding.build.artifact_digest}"

    by_subject: dict[str, list[dict[str, Any]]] = {}
    refused: set[str] = set()
    keys = []
    invalid_enumeration = False
    for arm in enumerated:
        if not isinstance(arm, dict):
            refuse("claims_absence_invalid: malformed enumeration arm")
        if arm.get("tag") != "record":
            invalid_enumeration = True
            state.completeness.note(
                extracting.node_id,
                "duplicate_key" if arm.get("tag") == "duplicate" else "invalid_record",
            )
        record = arm.get("record") if arm.get("tag") == "record" else arm
        if not isinstance(record, dict):
            continue
        try:
            key = resolve(node.subject.key, record)
            keys.append(record.get("key") or key)
            subject = context.subject(node.subject.kind, key)
        except (ValueError, KeyError, TypeError):
            invalid_enumeration = True
            continue  # unknown key taints lineage; it never authorizes a deletion
        if subject is None:
            continue
        path = subject.artifact_path
        if arm.get("tag") != "record":
            refused.add(path)
            invalid_enumeration = True
        else:
            by_subject.setdefault(path, []).append(record)

    def cover_subject(row: object) -> Any:
        if isinstance(row, str):
            return context.reference(node.subject.kind, row)
        if isinstance(row, dict):
            reference = row.get("path", row.get("subject_path", row.get("subject_id")))
            if isinstance(reference, str):
                return context.reference(node.subject.kind, reference)
            return context.subject(node.subject.kind, row.get("key", row))
        return None

    covers: dict[str, tuple[Any, Any]] = {}
    for row in covered:
        try:
            subject = cover_subject(row)
        except ValueError as exc:
            refuse(f"claims_absence_invalid: {exc}")
        if subject is None:
            refuse("claims_absence_invalid: covers names an unresolved Subject")
        covers[subject.artifact_path] = (subject, row)
    if isinstance(extracting, ExtractNode) and extracting.only_keys is not None:
        selected = resolve(extracting.only_keys)
        if not isinstance(selected, list):
            refuse("claims_absence_invalid: only_keys must be a key list")
        allowed = set()
        for key in selected:
            subject = context.subject(node.subject.kind, key)
            if subject is not None:
                allowed.add(subject.artifact_path)
        if not covers.keys() <= allowed:
            refuse("claims_covers_outside_only_keys: covers must be a subset of only_keys")
    complete = (
        not invalid_enumeration
        and (enumeration.get("complete", True) if isinstance(enumeration, dict) else True)
        and (
            state.completeness.at_sink(_referenced_reads(node.covers))
            and state.completeness.reads_complete(
                _referenced_reads(node.enumeration), records_kept=True
            )
        )
    )
    witness = None
    if complete:
        witness = AbsenceWitness(
            capture=capture,
            extract_spec=extract_spec,
            extractor=extractor,
            logical_source=logical_source,
            complete_when=True,
            enumerated_key_digest=typed_digest(
                Sha256Value, "cruxible-enumerated-keys-v1", {"keys": keys}
            ).tagged,
            covers_coordinate=admission.accepted_coordinate,
        )
        seal(witness)
    if not complete:
        diagnostics.add(
            ClaimsDiagnostic(
                kind="incomplete_source",
                target=node.node_id,
                problem="source_incomplete",
                source_incomplete=True,
            )
        )
    seen_findings = state.absence_findings
    seen_changes: set[tuple[str, int, bytes, bytes]] = set()
    receipts: dict[int, ClaimEvidenceReceipt] = {}

    def finding(claim: Any, ct: Any, reason: Literal["record_absent", "field_absent"]) -> None:
        if claim is None:
            # No held Claim to flag: incomplete_source explains a skipped initial inverse.
            return
        identity = claim.identity.name
        if identity in seen_findings:
            return
        seen_findings.add(identity)
        diagnostics.add(
            ClaimsDiagnostic(
                kind="claim_not_in_source",
                target=identity,
                claim=None if claim is None else claim.identity.name,
                subject=subject.artifact_path,
                claim_type=ct.predicate,
                qualifier=None if claim is None else claim.statement.qualifier,
                logical_source=logical_source,
                capture=capture,
                extract_spec=extract_spec,
                reason=reason,
                source_incomplete=not complete,
                problem="source_incomplete" if not complete else None,
            )
        )

    def append(change: Any, receipt: Any, value: Any) -> None:
        state.observe_items(len(changes) + 1, f"terminal:{node.node_id}", "claims")
        from cruxible_core.procedures.execution import _budget_refusal

        limit = admission.budget.max_items or admission.hard_caps.max_items
        if len(changes) >= limit:
            raise _budget_refusal(
                budget_kind="max_items",
                limit=limit,
                observed=len(changes) + 1,
                node_id=node.node_id,
                boundary=f"terminal:{node.node_id}",
                field_path="claims",
            )
        changes.append(change)
        evidence.append(receipt)
        tokens.append(own)
        values.append(value)

    for path, (subject, cover_row) in covers.items():
        if path in refused:
            continue
        records = by_subject.get(path, [])
        for index, mapping in enumerate(node.claims):
            if mapping.when_absent == "keep":
                continue
            if not records and any(
                predicate is not None
                and not _mentions_item_reference(predicate)
                and _truth(predicate, {"key": cover_row, "fields": {}}, state.parameters) != "true"
                for predicate in (node.where, mapping.when)
            ):
                continue
            ct = context.claim_type(mapping.claim_type.target.name)
            if ct is None:
                refuse("claims_absence_invalid: ClaimType unavailable")
            role = mapping.role or ct.default_role
            replaces = ct.precedence.replacements(role, mapping.replaces)
            qualifier_dynamic = _mentions_item_reference(mapping.qualifier)
            slots: Iterable[tuple[Any, list[Any], Sequence[Any]]]
            if qualifier_dynamic:
                grouped: dict[bytes, tuple[Any, list[Any], list[Any]]] = {}
                for record in records:
                    q = resolve(mapping.qualifier, record)
                    if isinstance(q, dict) and q.get("tag") == "invalid":
                        grouped = {}
                        break  # the ordinary mapping reports the invalid qualifier
                    q = (
                        q.get("value")
                        if isinstance(q, dict) and q.get("tag") in {"present", "absent"}
                        else q
                    )
                    grouped.setdefault(canonical_bytes(q), (q, [], []))[1].append(record)
                else:
                    for claim in context.slots(subject, ct.predicate):
                        q = claim.statement.qualifier
                        grouped.setdefault(canonical_bytes(q), (q, [], []))[2].append(claim)
                slots = grouped.values()
            else:
                qualifier = resolve(mapping.qualifier)
                slots = ((qualifier, records, context.slot(subject, ct.predicate, qualifier)),)
            for qualifier, slot_records, held in slots:
                # A source row that was filtered, invalid or merely evaluated false
                # suppresses absence. A many-valued slot is checked per held unit.
                field_absent = False
                suppressed = False
                present_values = []
                for record in slot_records:
                    raw = resolve(mapping.value, record)
                    if (
                        node.where is not None
                        and _truth(node.where, record, state.parameters) != "true"
                    ):
                        suppressed = True
                        break
                    if (
                        mapping.when is not None
                        and _truth(mapping.when, record, state.parameters) != "true"
                    ):
                        suppressed = True
                        break
                    if isinstance(raw, dict) and raw.get("tag") == "invalid":
                        suppressed = True
                        break
                    if isinstance(raw, dict) and raw.get("tag") == "absent":
                        field_absent = True
                    else:
                        present_values.append(
                            raw.get("value")
                            if isinstance(raw, dict) and raw.get("tag") == "present"
                            else raw
                        )
                if suppressed:
                    continue
                if slot_records and present_values:
                    # A present value is handled by the ordinary eligibility law;
                    # a one-valued slot cannot also be absent.
                    if ct.cardinality == "one":
                        continue
                elif slot_records and not field_absent:
                    continue
                reason: Literal["field_absent", "record_absent"] = (
                    "field_absent" if field_absent and not present_values else "record_absent"
                )
                if (
                    ct.cardinality == "one"
                    and mapping.when_absent in {"mirror", "inverse"}
                    and len([c for c in held if c.statement.role in replaces]) > 1
                ):
                    diagnostics.add(
                        ClaimsDiagnostic(kind="slot_contested", target=f"{path}:{ct.predicate}")
                    )
                    for claim in held:
                        finding(claim, ct, reason)
                    continue
                blocked_units = set()
                if ct.inverse is not None:
                    blocked_units = {
                        object_unit_key(c.statement.object.model_dump(mode="json"), ct, context)
                        for c in held
                        if c.statement.role not in replaces
                        or (
                            mapping.when_absent == "mirror"
                            and not any(
                                context.logical_source(cap) == logical_source
                                for cap in c.backing.capture_digests
                            )
                        )
                    }
                candidates = list(held)
                if not candidates and mapping.when_absent == "inverse":
                    if present_values:
                        continue
                    candidates = [None]
                for claim in candidates:
                    if claim is not None and ct.cardinality == "many":
                        obj = claim.statement.object
                        unit_key = object_unit_key(obj.model_dump(mode="json"), ct, context)
                        if (
                            path,
                            ct.predicate,
                            canonical_bytes(claim.statement.qualifier),
                            unit_key,
                        ) in observed:
                            continue
                    if mapping.when_absent == "surface" or not complete:
                        if not complete and mapping.when_absent in {"mirror", "inverse"}:
                            state.completeness.skipped += 1
                        finding(claim, ct, reason)
                        continue
                    if (
                        claim is not None
                        and isinstance(claim.statement.object, NoneClaimObject)
                        and claim.statement.role not in replaces
                    ):
                        diagnostics.same += 1
                        continue
                    if (
                        claim is not None
                        and ct.inverse is not None
                        and object_unit_key(
                            claim.statement.object.model_dump(mode="json"), ct, context
                        )
                        in blocked_units
                    ):
                        finding(claim, ct, reason)
                        continue
                    if claim is not None and claim.statement.role not in replaces:
                        if isinstance(claim.statement.object, NoneClaimObject):
                            diagnostics.same += 1
                        else:
                            finding(claim, ct, reason)
                        continue
                    if (
                        claim is not None
                        and mapping.when_absent == "mirror"
                        and not any(
                            context.logical_source(c) == logical_source
                            for c in claim.backing.capture_digests
                        )
                    ):
                        finding(claim, ct, reason)
                        continue
                    assert witness is not None
                    if index not in receipts:
                        receipts[index] = ClaimEvidenceReceipt(
                            node_id=node.node_id,
                            template=index,
                            procedure_digest=admission.procedure_artifact_digest,
                            value=CaptureBasis(capture_digest=capture),
                            absence=witness,
                        )
                        seal(receipts[index])
                    receipt = receipts[index]
                    if (
                        claim is not None
                        and mapping.when_absent == "mirror"
                        and not isinstance(claim.statement.object, NoneClaimObject)
                    ):
                        append(
                            CoreRetirementChange(
                                rationale="Complete source no longer contains this Claim.",
                                claim_id=claim.identity.name,
                                artifact_digest=claim_artifact_digest(claim).tagged,
                                claim_type=ct.predicate,
                                witness=witness,
                            ),
                            receipt,
                            {"retire": claim.identity.name, "template": index},
                        )
                    if ct.inverse is None:
                        continue
                    inverse: dict[str, Any] = {"kind": "none"}
                    if ct.inverse.key.kind != "slot":
                        obj = claim.statement.object if claim is not None else None
                        if isinstance(obj, NoneClaimObject):
                            inverse["of"] = (
                                None if obj.of is None else obj.of.model_dump(mode="json")
                            )
                        elif obj is not None:
                            if ct.inverse.key.kind == "object_key":
                                address = getattr(obj, "address", None)
                                key_claims: Sequence[Any] = (
                                    ()
                                    if address is None
                                    else context.slot(address, ct.inverse.key.predicate, None)
                                )
                                if len(key_claims) != 1:
                                    refuse(
                                        "claims_absence_invalid: inverse object key is unresolved"
                                    )
                                inverse["of"] = key_claims[0].statement.object.model_dump(
                                    mode="json"
                                )
                            else:
                                inverse["of"] = obj.model_dump(mode="json")
                        else:
                            raw = resolve(mapping.value, {"key": cover_row, "fields": cover_row})
                            if isinstance(raw, dict) and raw.get("kind"):
                                named = context.subject(raw["kind"], raw.get("key", {}))
                                if named is not None:
                                    if ct.inverse.key.kind == "object_key":
                                        held_keys = context.slot(
                                            named, ct.inverse.key.predicate, None
                                        )
                                        if (
                                            len(held_keys) != 1
                                            or held_keys[0].statement.object.kind != "literal"
                                        ):
                                            refuse(
                                                "claims_absence_invalid: "
                                                "inverse object key is unresolved"
                                            )
                                        inverse["of"] = held_keys[0].statement.object.model_dump(
                                            mode="json"
                                        )
                                    else:
                                        inverse["of"] = {
                                            "kind": "subject",
                                            "address": named.model_dump(mode="json"),
                                        }
                            elif raw is not None and not isinstance(raw, dict):
                                inverse["of"] = {"kind": "literal", "value": raw}
                            if "of" not in inverse:
                                refuse("claims_absence_invalid: inverse unit cannot be resolved")
                    unit = (path, index, canonical_bytes(qualifier), canonical_bytes(inverse))
                    if unit in seen_changes:
                        if (
                            mapping.when_absent == "inverse"
                            and claim is not None
                            and not isinstance(claim.statement.object, NoneClaimObject)
                        ):
                            append(
                                CoreRetirementChange(
                                    rationale="Complete source no longer "
                                    "contains this inverse unit.",
                                    claim_id=claim.identity.name,
                                    artifact_digest=claim_artifact_digest(claim).tagged,
                                    claim_type=ct.predicate,
                                    witness=witness,
                                ),
                                receipt,
                                {"retire": claim.identity.name, "template": index},
                            )
                        continue
                    seen_changes.add(unit)
                    if claim is not None and isinstance(claim.statement.object, NoneClaimObject):
                        diagnostics.same += 1
                        if capture not in claim.backing.capture_digests:
                            append(
                                CoreReobservationChange(
                                    rationale="Re-confirmed complete source absence.",
                                    claim_id=claim.identity.name,
                                    artifact_digest=claim_artifact_digest(claim).tagged,
                                    value=inverse,
                                ),
                                receipt,
                                {"absence": path, "template": index, "unit": inverse},
                            )
                        continue
                    append(
                        CoreClaimChange.model_validate(
                            {
                                "statement": {
                                    "subject": subject.model_dump(mode="json"),
                                    "predicate": ct.predicate,
                                    "object": inverse,
                                    "role": role,
                                    "qualifier": claim.statement.qualifier
                                    if claim is not None
                                    else qualifier,
                                },
                                "rationale": "Complete source establishes the declared inverse.",
                                "predecessor": claim.identity.name
                                if claim is not None and mapping.when_absent == "inverse"
                                else None,
                            }
                        ),
                        receipt,
                        {"absence": path, "template": index, "unit": inverse},
                    )
