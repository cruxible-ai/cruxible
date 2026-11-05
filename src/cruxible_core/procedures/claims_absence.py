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
from cruxible_client.contracts.procedures.models import ExtractNode
from cruxible_core.procedures.claims_changes import (
    CoreClaimChange,
    CoreReobservationChange,
    CoreRetirementChange,
)
from cruxible_core.procedures.claims_units import (
    ClaimUnitUnresolved,
    claims_item_identity_digest,
    resolve_object,
    unit_key,
)


def append_absence(
    node: Any,
    *,
    enumeration: ExtractNode,
    executor: Any,
    admission: Any,
    state: Any,
    context: Any,
    resolve: Callable[..., Any],
    values: list[Any],
    tokens: list[Any],
    evidence: list[Any],
    changes: list[Any],
    diagnostics: Any,
    reserve: Callable[[], None],
    identities: list[str],
    within: set[object] | None = None,
) -> None:
    """One pass over the enumeration and ``expect``; every Claim read is an indexed slot read.

    The enumeration is the whole output of the extract at the root of
    ``for_each``'s filter/dedupe chain (:func:`absence_enumeration`); it includes
    refused keys, so a duplicate/invalid record cannot be mistaken for a missing
    record. Findings are run-owned; the existing Line routing policy alone
    chooses inbox kinds. Absence obeys ``within`` like every other write: an
    expected Subject outside it is never retired or inverted, only counted.
    """
    from cruxible_core.procedures.execution import (
        _exact_evidence,
        _mentions_item_reference,
        _referenced_reads,
        _RunRefusal,
    )
    from cruxible_core.procedures.transforms import _record, _truth

    def refuse(message: str) -> NoReturn:
        raise _RunRefusal(message.split(":", 1)[0], message, node_id=node.node_id)

    extracting = enumeration
    reference = f"$steps.{extracting.as_}"
    expected = resolve(node.expect)
    listed = resolve(reference)
    if not isinstance(expected, list):
        refuse("claims_absence_invalid: expect must be a Subject list")
    if isinstance(listed, dict) and isinstance(listed.get("results"), list):
        enumerated = listed["results"]
    else:
        selected_records = listed.get("records") if isinstance(listed, dict) else listed
        if not isinstance(selected_records, list):
            refuse("claims_absence_invalid: enumeration must retain typed records/results")
        enumerated = [
            item
            if isinstance(item, dict)
            and item.get("tag") in {"record", "invalid_record", "duplicate"}
            else {"tag": "record", "record": _record(item, index, "enumeration")}
            for index, item in enumerate(selected_records)
        ]
    own = _exact_evidence(
        [reference, node.expect], state=state, node_id=node.node_id, field="absence"
    )
    # The root must be proven over the SAME closure the item's manifest records
    # (``own``: reference and ``expect`` both), not a narrower one -- otherwise
    # the receipt it cites could name a Capture delivery's wider closure check
    # (D-R37 d1) cannot independently prove a root for.
    captures = {t.digest for t in own if t.slot in {"produced_capture", "admitted_capture"}}
    from cruxible_core.procedures.terminal_dependencies import capture_root

    root = capture_root(captures, state.derivations, cache=state.capture_root_cache)
    if root is None:
        refuse(
            "claims_absence_invalid: enumeration needs exactly one root Capture "
            "(one Capture, or one derived from all the others)"
        )
    capture = root
    logical_source = context.logical_source(capture)
    if logical_source is None:
        refuse("claims_absence_invalid: enumeration Capture has no logical source")
    from cruxible_client.contracts.procedures.extract import EXTRACTOR_VERSIONS
    from cruxible_core.procedures.claims_terminal import _receipt_extract_digest

    extract_spec = _receipt_extract_digest(extracting)
    contract = executor.capture_contracts.get(state.capture_contracts.get(capture, ""))
    if contract is None or contract.decoding is None:
        refuse("claims_absence_invalid: enumeration CaptureKind decoding is unavailable")
    extractor = EXTRACTOR_VERSIONS[contract.decoding.format]
    check = state.extraction_checks.get(extracting.as_)
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

    def expected_subject(row: object) -> Any:
        if isinstance(row, str):
            return context.reference(node.subject.kind, row)
        if isinstance(row, dict):
            reference = row.get("path", row.get("subject_path", row.get("subject_id")))
            if isinstance(reference, str):
                return context.reference(node.subject.kind, reference)
            return context.subject(node.subject.kind, row.get("key", row))
        return None

    expects: dict[str, tuple[Any, Any]] = {}
    for row in expected:
        try:
            subject = expected_subject(row)
        except ValueError as exc:
            refuse(f"claims_absence_invalid: {exc}")
        if subject is None:
            refuse("claims_absence_invalid: expect names an unresolved Subject")
        expects[subject.artifact_path] = (subject, row)
    if extracting.only_keys is not None:
        selected = resolve(extracting.only_keys)
        if not isinstance(selected, list):
            refuse("claims_absence_invalid: only_keys must be a key list")
        allowed = set()
        for key in selected:
            subject = context.subject(node.subject.kind, key)
            if subject is not None:
                allowed.add(subject.artifact_path)
        if not expects.keys() <= allowed:
            refuse("claims_expect_outside_only_keys: expect must be a subset of only_keys")
    complete = (
        (check is None or check.held)
        and not invalid_enumeration
        and (listed.get("complete", True) if isinstance(listed, dict) else True)
        and (
            state.completeness.at_sink(_referenced_reads(node.expect))
            and state.completeness.reads_complete(_referenced_reads(reference), records_kept=True)
        )
    )
    witness = None
    if complete:
        witness = AbsenceWitness(
            run_id=admission.run_id,
            procedure_digest=admission.procedure_artifact_digest,
            node_id=node.node_id,
            capture=capture,
            extract_spec=extract_spec,
            extractor=extractor,
            logical_source=logical_source,
            complete_when=check,
            enumerated_key_digest=typed_digest(
                Sha256Value, "cruxible-enumerated-keys-v1", {"keys": keys}
            ).tagged,
            expect_coordinate=admission.accepted_coordinate,
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
                claim=claim.identity.name,
                subject=subject.artifact_path,
                claim_type=ct.predicate,
                qualifier=claim.statement.qualifier,
                logical_source=logical_source,
                capture=capture,
                extract_spec=extract_spec,
                reason=reason,
                source_incomplete=not complete,
                problem="source_incomplete" if not complete else None,
            )
        )

    def held_identity(subject: Any, ct: Any, claim: Any) -> str:
        """A held Claim's item identity: the one its positive or inverse write had (g1)."""

        return claims_item_identity_digest(
            node_id=node.node_id,
            subject=subject,
            claim_type=ct,
            qualifier=claim.statement.qualifier,
            obj=claim.statement.object,
            context=context,
        )

    def append(change: Any, receipt: Any, value: Any, identity: str) -> None:
        reserve()
        changes.append(change)
        identities.append(identity)
        evidence.append(receipt)
        tokens.append(own)
        values.append(value)

    for path, (subject, expect_row) in expects.items():
        if within is not None and path not in within:
            state.completeness.expect_outside_within += 1
            continue
        if path in refused:
            continue
        records = by_subject.get(path, [])
        for index, mapping in enumerate(node.claims):
            if mapping.when_absent == "keep":
                continue
            if not records and any(
                predicate is not None
                and not _mentions_item_reference(predicate)
                and _truth(predicate, {"key": expect_row, "fields": {}}, state.parameters) != "true"
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
                present_units: set[bytes] = set()
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
                        trusted = isinstance(raw, dict) and raw.get("tag") == "present"
                        present_values.append((raw.get("value") if trusted else raw, trusted))
                if not suppressed:
                    # Presence comes from the enumeration, independent of for_each,
                    # within, write admission, metadata and per-value completeness.
                    for raw_value, trusted in present_values:
                        obj = resolve_object(raw_value, ct, context, executor, trusted=trusted)
                        if obj is None:
                            suppressed = True
                            break
                        try:
                            present_units.add(unit_key(obj, ct, context))
                        except ClaimUnitUnresolved:
                            suppressed = True
                            break
                    for record in slot_records:
                        if any(
                            isinstance(resolve(metadata, record), dict)
                            and resolve(metadata, record).get("tag") == "invalid"
                            for metadata in (mapping.effective_from, mapping.effective_until)
                        ):
                            suppressed = True
                            break
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
                if mapping.when_absent == "inverse":
                    # An inverse unit another role or source holds is never overwritten.
                    blocked_units = {
                        unit_key(c.statement.object.model_dump(mode="json"), ct, context)
                        for c in held
                        if c.statement.role not in replaces
                        or not any(
                            context.logical_source(cap) == logical_source
                            for cap in c.backing.capture_digests
                        )
                    }
                candidates = list(held)
                if not candidates and mapping.when_absent == "inverse":
                    if present_values:
                        continue
                    candidates = [None]
                for claim in candidates:
                    if (
                        mapping.when_absent == "mirror"
                        and claim is not None
                        and isinstance(claim.statement.object, NoneClaimObject)
                    ):
                        continue  # a held inverse agrees with the absence; mirror only retires
                    if claim is not None and ct.cardinality == "many":
                        try:
                            held_unit = unit_key(claim.statement.object, ct, context)
                        except ClaimUnitUnresolved:
                            refuse(
                                "claims_inverse_unit_unresolved: held object key cannot be resolved"
                            )
                        if held_unit in present_units:
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
                        and blocked_units
                        and unit_key(claim.statement.object.model_dump(mode="json"), ct, context)
                        in blocked_units
                    ):
                        finding(claim, ct, reason)
                        continue
                    if claim is not None and claim.statement.role not in replaces:
                        finding(claim, ct, reason)
                        continue
                    if (
                        claim is not None
                        and mapping.when_absent in {"mirror", "inverse"}
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
                    if mapping.when_absent == "mirror":
                        # One meaning per word: mirror only retires, whatever the type declares.
                        if claim is not None:
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
                                held_identity(subject, ct, claim),
                            )
                        continue
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
                            raw = resolve(mapping.value)
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
                        if claim is not None and not isinstance(
                            claim.statement.object, NoneClaimObject
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
                                held_identity(subject, ct, claim),
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
                                held_identity(subject, ct, claim),
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
                                "predecessor": None if claim is None else claim.identity.name,
                            }
                        ),
                        receipt,
                        {"absence": path, "template": index, "unit": inverse},
                        claims_item_identity_digest(
                            node_id=node.node_id,
                            subject=subject,
                            claim_type=ct,
                            qualifier=claim.statement.qualifier if claim is not None else qualifier,
                            obj=inverse,
                            context=context,
                        ),
                    )
