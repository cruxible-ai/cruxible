"""Lower declared Claims mappings, excluding inadmissible values before any proposal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.authoring.models import ClaimDerivationBinding
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    canonical_bytes,
)
from cruxible_client.contracts.canonical_forms import canonicalize_literal
from cruxible_client.contracts.claim_evidence import (
    ClaimEvidenceReceipt,
    ClaimsDiagnostic,
    FindingKind,
)
from cruxible_client.contracts.claim_types import effective_canonical_form
from cruxible_client.contracts.claims import claim_artifact_digest
from cruxible_client.contracts.content_memo import sealed_memo
from cruxible_client.contracts.procedures.claims import ClaimsNode, rationale_references
from cruxible_client.contracts.procedures.extract import EXTRACTOR_VERSIONS, extract_spec_digest
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ExtractNode,
    InvokeNode,
    ReadNode,
    RepeatNode,
    TransformNode,
)
from cruxible_client.contracts.shapes import (
    FieldInvalid,
    SubjectType,
    check_value,
    claim_has_line_break,
    claim_rationale,
    requires_complete_input,
)
from cruxible_core.procedures.claims_changes import (
    CoreClaimChange,
    CoreClaimsChange,
    CoreReobservationChange,
    CoreSubjectChange,
)
from cruxible_core.procedures.claims_context import ClaimsContext
from cruxible_core.procedures.claims_diagnostics import ClaimsDiagnostics
from cruxible_core.procedures.terminal_dependencies import DependencyToken

if TYPE_CHECKING:
    from cruxible_core.procedures.execution import (
        ProcedureExecutor,
        ProcedureRunAdmission,
        _RunState,
    )


@sealed_memo
def _receipt_extract_digest(node: ExtractNode) -> str:
    """An immutable accepted extract specification is digested once per process."""
    return extract_spec_digest(node)


@dataclass
class ExpandedClaims:
    values: list[CanonicalValue]
    tokens: tuple[frozenset[DependencyToken], ...]
    diagnostics: ClaimsDiagnostics
    evidence: tuple[ClaimEvidenceReceipt | None, ...]
    changes: tuple[CoreClaimsChange, ...]


def expand_claims(
    node: ClaimsNode,
    *,
    executor: ProcedureExecutor,
    admission: ProcedureRunAdmission,
    state: _RunState,
    context: ClaimsContext,
) -> ExpandedClaims:
    from cruxible_core.procedures.execution import (
        _budget_refusal,
        _exact_evidence,
        _fan_out_list,
        _mentions_item_reference,
        _referenced_aliases,
        _referenced_reads,
        _resolve_template,
        _RunRefusal,
    )
    from cruxible_core.procedures.transforms import _record, _truth

    def resolve(value: object, row: CanonicalValue | None = None) -> Any:
        return _resolve_template(
            value,
            input_payload=state.input_payload,
            parameters=state.parameters,
            outputs=state.outputs,
            item=row,
        )

    rows, row_tokens = _fan_out_list(
        node.for_each,
        state=state,
        node_id=node.node_id,
        field="for_each",
        noun="Claim rows",
        invalid_code="proposal_item_invalid",
        ambiguous_code="proposal_item_evidence_ambiguous",
    )
    max_items = admission.budget.max_items or admission.hard_caps.max_items
    # Bound rows before expansion as well as emitted members at the terminal boundary.
    if len(rows) > max_items:
        raise _budget_refusal(
            budget_kind="max_items",
            limit=max_items,
            observed=len(rows),
            node_id=node.node_id,
            boundary=f"terminal:{node.node_id}",
            field_path="for_each",
        )
    diagnostics = state.claims_diagnostics.setdefault(node.node_id, ClaimsDiagnostics())
    evidence: list[ClaimEvidenceReceipt | None] = []
    changes: list[CoreClaimsChange] = []
    values: list[CanonicalValue] = []
    tokens: list[frozenset[DependencyToken]] = []
    created: set[bytes] = set()
    nodes = (
        {}
        if executor.plan is None
        else {getattr(n, "as_", None): n for n in executor.plan.definition.nodes}
    )
    # Runtime execution plans carry their accepted artifact separately on some paths.
    source_cache: dict[str, tuple[tuple[Any, ...], tuple[str, ...], bool]] = {}
    extract_digests = {
        n.node_id: _receipt_extract_digest(n) for n in nodes.values() if isinstance(n, ExtractNode)
    }

    def includes_input(value: object) -> bool:
        from pydantic import BaseModel

        if isinstance(value, str):
            return value == "$input" or value.startswith("$input.")
        if isinstance(value, dict):
            return any(includes_input(v) for v in value.values())
        if isinstance(value, list | tuple):
            return any(includes_input(v) for v in value)
        if isinstance(value, BaseModel):
            return includes_input(value.model_dump(mode="python"))
        return False

    def lineage(alias: str) -> tuple[tuple[Any, ...], tuple[str, ...], bool]:
        if alias in source_cache:
            return source_cache[alias]
        producer = nodes.get(alias)
        if isinstance(producer, ExtractNode):
            found: tuple[tuple[Any, ...], tuple[str, ...], bool] = ((producer,), (), False)
        elif producer is None or isinstance(producer, ReadNode | InvokeNode | RepeatNode):
            # Accepted-state reads and opaque computations cannot be receipted
            # as extraction. A derivation must bind its exact Claim inputs.
            found = ((), (), True)
        else:
            from cruxible_core.procedures.execution import _node_reads

            sources: dict[str, Any] = {}
            transforms: dict[str, None] = {}
            unsupported = includes_input(producer)
            for parent, _ in _node_reads(producer):
                parents, chain, blocked = lineage(parent)
                sources.update((n.node_id, n) for n in parents)
                transforms.update(dict.fromkeys(chain))
                unsupported |= blocked
            if isinstance(producer, CallNode):
                sources = {producer.node_id: producer}
            elif isinstance(producer, TransformNode):
                transforms[producer.node_id] = None
            found = (tuple(sources.values()), tuple(transforms), unsupported)
        source_cache[alias] = found
        return found

    def report(
        kind: FindingKind,
        target: str,
        **details: Any,
    ) -> None:
        diagnostics.add(ClaimsDiagnostic(kind=kind, target=target, **details))

    shared = _exact_evidence(
        [node.subject.key, node.subject.within, node.where],
        state=state,
        node_id=node.node_id,
        field="subject",
    )
    within_paths: set[object] | None = None
    if node.subject.within is not None:
        if _mentions_item_reference(node.subject.within):
            raise _RunRefusal(
                "proposal_item_invalid",
                "subject.within must be independent of $item; project the Subject set first",
                node_id=node.node_id,
            )
        within = resolve(node.subject.within)
        if not isinstance(within, list):
            raise _RunRefusal(
                "proposal_item_invalid",
                "subject.within must be a Subject list",
                node_id=node.node_id,
            )

        def path_of(item: object) -> object:
            if isinstance(item, str):
                if item.startswith("subjects/"):
                    return item
                return (
                    "subjects/"
                    + (item if "/" in item else node.subject.kind + "/" + item)
                    + ".json"
                )
            if isinstance(item, dict):
                if "path" in item:
                    return item["path"]
                if "subject_kind" in item and "subject_id" in item:
                    return f"subjects/{item['subject_kind']}/{item['subject_id']}.json"
            return None

        within_paths = {path_of(item) for item in within}
    attempted = 0
    for row_index, (row, own_tokens) in enumerate(zip(rows, row_tokens, strict=True)):
        record = _record(row, row_index, "claims")
        if (
            node.where is not None
            and _truth(
                node.where, record, state.parameters if isinstance(state.parameters, dict) else {}
            )
            != "true"
        ):
            continue
        key = resolve(node.subject.key, row)
        try:
            subject = context.subject(node.subject.kind, key)
        except ValueError as exc:
            raise _RunRefusal("proposal_item_invalid", str(exc), node_id=node.node_id) from exc
        if subject is None and node.subject.create == "never":
            continue
        if within_paths is not None and (
            subject is None or subject.artifact_path not in within_paths
        ):
            continue
        subject_wire = (
            subject.model_dump(mode="json")
            if subject is not None
            else {
                "subject_kind": node.subject.kind,
                "key": key,
            }
        )
        for template_index, mapping in enumerate(node.claims):
            attempted += 1
            state.observe_items(attempted, f"terminal:{node.node_id}", "claims")
            if attempted > max_items:
                raise _budget_refusal(
                    budget_kind="max_items",
                    limit=max_items,
                    observed=attempted,
                    node_id=node.node_id,
                    boundary=f"terminal:{node.node_id}",
                    field_path="claims",
                )
            target = (
                f"{node.subject.kind}:"
                + canonical_bytes(key).decode("utf-8")
                + f":{mapping.claim_type.target.name}"
            )
            # An invalid mapped field is reported even when present(...) is false.
            raw = resolve(mapping.value, row)
            if isinstance(raw, dict) and raw.get("tag") == "invalid":
                problem = raw.get("problem", {})
                report(
                    "extract_refused",
                    target,
                    field=str(mapping.value),
                    problem=str(problem.get("code", "value_invalid")),
                    lexeme=None if problem.get("lexeme") is None else str(problem["lexeme"])[:512],
                )
                continue
            if (
                mapping.when is not None
                and _truth(
                    mapping.when,
                    record,
                    state.parameters if isinstance(state.parameters, dict) else {},
                )
                != "true"
            ):
                continue
            if isinstance(raw, dict) and raw.get("tag") == "absent":
                continue
            present = isinstance(raw, dict) and raw.get("tag") == "present"
            value = raw["value"] if present else raw
            ct = context.claim_type(mapping.claim_type.target.name)
            if ct is None:
                raise _RunRefusal(
                    "proposal_item_invalid", "ClaimType.value is required", node_id=node.node_id
                )
            role = mapping.role or ct.default_role
            if role not in ct.permitted_roles:
                raise _RunRefusal(
                    "proposal_item_invalid", "claims_role_forbidden", node_id=node.node_id
                )
            try:
                replaces = ct.precedence.replacements(role, mapping.replaces)
            except ValueError as exc:
                raise _RunRefusal("proposal_item_invalid", str(exc), node_id=node.node_id) from exc
            if claim_has_line_break(value):
                report(
                    "extract_refused",
                    target,
                    field=str(mapping.value),
                    problem="multiline_string",
                    lexeme=str(value)[:512],
                )
                continue
            checked = (
                None
                if present
                else check_value(
                    value,
                    ct.value,
                    at=target,
                    resolve=executor.contract_resolver,
                    enum_members=executor.enum_resolver,
                    keys=executor.key_rules,
                )
            )
            if isinstance(ct.value, SubjectType):
                # Values name an accepted Subject, or explicitly name its key.
                try:
                    if isinstance(value, dict) and value.get("kind") in ct.value.subject_kinds:
                        address = context.subject(value["kind"], value.get("key", {}))
                    elif isinstance(value, str):
                        matches = [
                            context.reference(kind, value) for kind in ct.value.subject_kinds
                        ]
                        resolved = [found for found in matches if found is not None]
                        address = resolved[0] if len(resolved) == 1 else None
                    else:
                        address = None
                except ValueError:
                    address = None
                if address is None:
                    report(
                        "extract_refused", target, field=str(mapping.value), problem="value_invalid"
                    )
                    continue
                obj: dict[str, Any] = {
                    "kind": "subject",
                    "address": address.model_dump(mode="json"),
                }
            elif isinstance(checked, FieldInvalid):
                report(
                    "extract_refused",
                    target,
                    field=str(mapping.value),
                    problem="value_invalid",
                    lexeme=str(value)[:512],
                )
                continue
            else:
                from cruxible_core.procedures.extract import wire_value

                value = value if checked is None else wire_value(checked.value)
                obj = {"kind": "literal", "value": value}
            value_reads = _referenced_reads(
                [
                    node.for_each,
                    node.where,
                    node.subject.key,
                    node.subject.within,
                    mapping.value,
                    mapping.when,
                    mapping.qualifier,
                    mapping.effective_from,
                    mapping.effective_until,
                    mapping.inputs,
                ]
            )
            complete = state.completeness.at_sink(value_reads)
            # An invalid used field makes a record incomplete independently of an alias flag.
            metadata: dict[str, Any] = {}
            metadata_invalid = False
            for name, template in (
                ("qualifier", mapping.qualifier),
                ("effective_from", mapping.effective_from),
                ("effective_until", mapping.effective_until),
            ):
                resolved = resolve(template, row)
                if isinstance(resolved, dict) and resolved.get("tag") == "invalid":
                    problem = resolved.get("problem", {})
                    report(
                        "extract_refused",
                        target,
                        field=name,
                        problem=str(problem.get("code", "value_invalid")),
                        lexeme=str(problem.get("lexeme", ""))[:512],
                    )
                    metadata_invalid = True
                metadata[name] = (
                    resolved.get("value")
                    if isinstance(resolved, dict) and resolved.get("tag") in {"present", "absent"}
                    else resolved
                )
            if metadata_invalid:
                continue
            qualifier = metadata["qualifier"]
            held = () if subject is None else context.slot(subject, ct.predicate, qualifier)
            if requires_complete_input(value, ct.requires_complete_input) and not complete:
                incomplete = state.completeness.incomplete_inputs(value_reads)
                report("incomplete_source", target, input=",".join(incomplete) or "control lineage")
                # No destructive replacement over incomplete input, including a downgrade.
                if ct.incomplete_fallback is None or held:
                    continue
                value = ct.incomplete_fallback
                obj = {"kind": "literal", "value": value}
            if obj["kind"] == "literal":
                obj["value"] = canonicalize_literal(obj["value"], effective_canonical_form(ct))
            blocked = [c for c in held if c.statement.role not in replaces]
            same_blocked = [c for c in blocked if c.statement.object.model_dump(mode="json") == obj]
            if blocked and (ct.cardinality == "one" or same_blocked):
                if same_blocked:
                    diagnostics.same += 1
                else:
                    for claim in blocked:
                        report(
                            "held_disagrees",
                            target,
                            claim=claim.identity.name,
                            role=claim.statement.role,
                            held_value=claim.statement.object.model_dump(mode="json"),
                            incoming_value=obj,
                        )
                continue
            eligible = [c for c in held if c.statement.role in replaces]
            if ct.cardinality == "one" and len(eligible) > 1:
                report("slot_contested", target)
                continue
            refresh = [
                c
                for c in eligible
                if c.statement.object.model_dump(mode="json") == obj
                and c.statement.role == role
                and c.statement.effective_from == metadata["effective_from"]
                and c.statement.effective_until == metadata["effective_until"]
            ]
            revises = (
                eligible[0].identity.name
                if ct.cardinality == "one" and eligible
                else refresh[0].identity.name
                if len(refresh) == 1
                else None
            )
            item_templates = [
                mapping.value,
                mapping.qualifier,
                mapping.effective_from,
                mapping.effective_until,
                mapping.when,
                list(rationale_references(mapping.rationale)),
            ]
            own = (
                own_tokens
                | shared
                | _exact_evidence(item_templates, state=state, node_id=node.node_id, field="claims")
            )
            inputs: list[ArtifactPin] = []
            input_captures: set[str] = set()
            for template in mapping.inputs:
                supplied = resolve(template, row)
                try:
                    pin, captures_read = bind_claim_input(
                        supplied,
                        outputs=state.outputs,
                        provenance=state.provenance,
                        read_aliases=frozenset(
                            name
                            for name, n in nodes.items()
                            if isinstance(name, str) and isinstance(n, ReadNode) and n.reads_claims
                        ),
                        own_tokens=own,
                        read_digests={
                            name: entry.result_digest
                            for name, producer in nodes.items()
                            if isinstance(name, str)
                            and isinstance(producer, ReadNode)
                            and producer.reads_claims
                            for entry in admission.accepted_state_inputs
                            if entry.input_name in {name, producer.node_id}
                        },
                    )
                except ValueError as exc:
                    raise _RunRefusal(
                        "proposal_item_invalid", str(exc), node_id=node.node_id
                    ) from exc
                inputs.append(pin)
                input_captures.update(captures_read)
            if inputs and role != "derivation":
                raise _RunRefusal(
                    "proposal_item_invalid",
                    "claims_inputs_invalid: only derivation uses inputs",
                    node_id=node.node_id,
                )
            if len({p.target for p in inputs}) != len(inputs):
                raise _RunRefusal(
                    "proposal_item_invalid",
                    "claims_inputs_invalid: inputs must be distinct",
                    node_id=node.node_id,
                )
            direct_aliases = _referenced_aliases(mapping.value)
            source_aliases = direct_aliases or _referenced_aliases(node.for_each)
            # The interim ruling leaves literal/$params attribution unchanged:
            # those templates still cite the selected record's extract. $item,
            # explicit step reads and $input must prove their own data lineage.
            record_constant = (
                not direct_aliases
                and not _mentions_item_reference(mapping.value)
                and not includes_input(mapping.value)
            )
            sources: dict[str, Any] = {}
            transforms: dict[str, None] = {}
            unsupported = includes_input(mapping.value)
            for alias in source_aliases:
                s, t, blocked_basis = lineage(alias)
                sources.update((n.node_id, n) for n in s)
                transforms.update(dict.fromkeys(t))
                unsupported |= blocked_basis and not record_constant
            if unsupported and not inputs:
                report(
                    "extract_refused", target, field=str(mapping.value), problem="basis_unsupported"
                )
                continue
            extractors = [n for n in sources.values() if isinstance(n, ExtractNode)]
            computed = any(
                isinstance(n, TransformNode)
                and n.node_id in transforms
                and n.transform_kind in {"derive", "aggregate"}
                for n in nodes.values()
            )
            if computed and role != "derivation":
                report(
                    "extract_refused",
                    target,
                    field=str(mapping.value),
                    problem="derived_role_required",
                )
                continue

            if inputs and not any(t.slot in {"produced_capture", "admitted_capture"} for t in own):
                from cruxible_core.procedures.terminal_dependencies import admitted_capture_token

                own |= frozenset(admitted_capture_token(c) for c in input_captures)
            captures = sorted(
                {t.digest for t in own if t.slot in {"produced_capture", "admitted_capture"}}
            )
            if len(captures) != 1:
                raise _RunRefusal(
                    "proposal_item_evidence_missing",
                    "Claims value requires exactly one Capture in its own closure",
                    node_id=node.node_id,
                )
            capture = captures[0]
            basis: dict[str, Any]
            if inputs:
                basis = {
                    "basis": "derived",
                    "inputs": [
                        p.model_dump(mode="json")
                        for p in sorted(inputs, key=lambda p: p.target.qualified)
                    ],
                    "transform_node_ids": list(transforms),
                }
            elif len(extractors) == 1 and len(sources) == 1:
                extracting = extractors[0]
                contract = executor.capture_contracts.get(state.capture_contracts.get(capture, ""))
                basis = {
                    "basis": "extracted",
                    "capture_digest": capture,
                    "extract_spec_digest": extract_digests[extracting.node_id],
                    "extractor_version": EXTRACTOR_VERSIONS[
                        getattr(getattr(contract, "decoding", None), "format", "json")
                    ],
                    "record_locator": str(record["record"]),
                    "record_key": record["key"],
                    "field_path": str(raw.get("at", "")) if present else "",
                    "transform_node_ids": list(transforms),
                }
            else:
                calls = [n for n in sources.values() if isinstance(n, CallNode)]
                binding = executor.slot_bindings.get(calls[0].slot) if len(calls) == 1 else None
                from cruxible_client.contracts.workspace_file import is_workspace_file_signature

                if binding is None and value != capture:
                    raise _RunRefusal(
                        "proposal_item_evidence_missing",
                        "Value basis is unavailable: extract its source "
                        "or bind exact read Claim inputs.",
                        node_id=node.node_id,
                    )
                whole_capture = binding is None or is_workspace_file_signature(
                    binding.operation_signature_digest
                )
                binding = cast(Any, binding)
                basis = (
                    {"basis": "capture", "capture_digest": capture}
                    if whole_capture
                    else {
                        "basis": "measured",
                        "capture_digest": capture,
                        "operation": binding.operation.name,
                        "build": binding.build.model_dump(mode="json"),
                        "enumerates": None
                        if binding.operation.enumerates is None
                        else binding.operation.enumerates.selector,
                        "declared_fields": {}
                        if binding.operation.enumerates is None
                        else {
                            name: f.at for name, f in binding.operation.enumerates.fields.items()
                        },
                        "field_path": str(mapping.value),
                        "transform_node_ids": list(transforms),
                    }
                )
            if computed and basis["basis"] == "extracted":
                basis = {
                    "basis": "derived",
                    "extractions": [basis],
                    "transform_node_ids": list(transforms),
                }
            if role == "derivation" and basis["basis"] != "derived":
                report(
                    "extract_refused",
                    target,
                    field=str(mapping.value),
                    problem="derivation_basis_required",
                )
                continue
            receipt = ClaimEvidenceReceipt.model_validate(
                {
                    "node_id": node.node_id,
                    "template": template_index,
                    "procedure_digest": admission.procedure_artifact_digest,
                    "value": basis,
                }
            )
            if receipt.value.basis == "extracted":
                matched = [
                    c
                    for c in refresh
                    if c.backing.procedure_evidence is not None
                    and c.backing.procedure_evidence.value.basis == "extracted"
                    and c.backing.procedure_evidence.value.record_key == receipt.value.record_key
                ]
                if len(matched) == 1:
                    diagnostics.same += 1
                    held_claim = matched[0]
                    if receipt.value.capture_digest not in held_claim.backing.capture_digests:
                        changes.append(
                            CoreReobservationChange(
                                rationale="Re-observed the same declared value.",
                                claim_id=held_claim.identity.name,
                                artifact_digest=claim_artifact_digest(held_claim).tagged,
                                value=obj,
                            )
                        )
                        values.append(
                            cast(
                                CanonicalValue,
                                {"key": key, "template": template_index, "value": value},
                            )
                        )
                        tokens.append(own)
                        evidence.append(receipt)
                    continue
            rationale = mapping.rationale
            for ref in rationale_references(mapping.rationale):
                quoted = resolve(ref, row)
                if isinstance(quoted, dict) and quoted.get("tag") in {
                    "present",
                    "absent",
                    "invalid",
                }:
                    quoted = quoted.get("value", "unknown")
                rationale = rationale.replace("{" + ref + "}", str(quoted))
            try:
                claim_rationale(rationale)
            except ValueError:
                report("extract_refused", target, field="rationale", problem="rationale_invalid")
                continue
            creation_key = canonical_bytes(key)
            if subject is None and creation_key not in created:
                created.add(creation_key)
                changes.append(
                    CoreSubjectChange(
                        subject_kind=node.subject.kind,
                        key=key,
                        rationale=rationale,
                    )
                )
                values.append(cast(CanonicalValue, {"key": key}))
                tokens.append(own)
                evidence.append(None)
            changes.append(
                CoreClaimChange.model_validate(
                    {
                        "statement": {
                            "subject": subject_wire,
                            "predicate": ct.predicate,
                            "object": obj,
                            "role": role,
                            "qualifier": qualifier,
                            "effective_from": metadata["effective_from"],
                            "effective_until": metadata["effective_until"],
                        },
                        "rationale": rationale,
                        "predecessor": revises,
                        "derivation": None
                        if role != "derivation"
                        else ClaimDerivationBinding(
                            procedure=ArtifactPin(
                                role="reducer",
                                target=admission.procedure_identity,
                                artifact_digest=admission.procedure_artifact_digest,
                            ),
                            inputs=tuple(sorted(inputs, key=lambda p: p.target.qualified)),
                        ),
                    }
                )
            )
            values.append(
                cast(
                    CanonicalValue,
                    {
                        "key": key,
                        "template": template_index,
                        "value": value,
                    },
                )
            )
            tokens.append(own)
            evidence.append(receipt)
    return ExpandedClaims(values, tuple(tokens), diagnostics, tuple(evidence), tuple(changes))


def bind_claim_input(
    supplied: object,
    *,
    outputs: Any,
    provenance: Any,
    read_aliases: frozenset[str],
    own_tokens: frozenset[DependencyToken],
    read_digests: dict[str, str],
) -> tuple[ArtifactPin, tuple[str, ...]]:
    """Bind one cardinality-one Claim read, whole-output equal, in this row's closure.

    The terminal currently requires exactly one Capture across that closure.
    List/cardinality-many reads and multi-Capture derivations fail closed; callers
    must project a supported one-Claim read rather than select an arbitrary id.
    """
    matches = [
        name
        for name in read_aliases
        if outputs.get(name) == supplied
        and name in provenance
        and provenance[name].whole.issubset(own_tokens)
    ]
    if not matches or not isinstance(supplied, dict):
        raise ValueError(
            "claims_inputs_invalid: input is not an exact Claim read in this row's closure"
        )
    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_core.procedures.execution import run_value_digest

    if not any(
        read_digests.get(name) == run_value_digest("state-result", supplied) for name in matches
    ):
        raise ValueError("claims_inputs_invalid: forged Claim version")
    digest = supplied.get("artifact_digest")
    identity = supplied.get("claim_id")
    captures = supplied.get("captures")
    if (
        not isinstance(digest, str)
        or not isinstance(identity, str)
        or not isinstance(captures, list)
        or not all(isinstance(c, str) for c in captures)
    ):
        raise ValueError("claims_inputs_invalid: incomplete Claim read")
    return ArtifactPin(
        role="input-claim",
        target=ArtifactIdentity(kind="Claim", name=identity),
        artifact_digest=digest,
    ), tuple(captures)
