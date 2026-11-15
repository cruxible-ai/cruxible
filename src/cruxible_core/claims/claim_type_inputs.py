"""Deterministic proposal-time ClaimType lint, beside the authored input it reads.

The input record and its lowering live in
``cruxible_client.contracts.claim_type_inputs`` -- one implementation of the
short form's defaults for the daemon and the SDK alike -- and are re-exported
here for the daemon's callers.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_type_inputs import (
    ClaimDurationInput,
    ClaimEvidenceFreshnessInput,
    ClaimInverseInput,
    ClaimTypeDefaultRoleNotPermitted,
    ClaimTypeEvidenceRulesRequired,
    ClaimTypeInputIncomplete,
    ClaimTypeInputRecord,
    ClaimTypeInputReferenceError,
    ClaimTypeInputValidationError,
    ClaimTypeMemberDescriptionInput,
    ClaimTypeMemberDescriptionsStale,
    _admitted_roles,
    _contract_ref,
    _StrictClaimTypeInputModel,
    claim_type_input_defaults,
    claim_type_input_template,
    default_evidence_policy,
    lower_claim_type_input,
)
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.codes import CurrentCode
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.policies import CAPTURE_CONTRACT_FAMILY_SHAPES
from cruxible_client.contracts.shapes import BoolType, EnumType
from cruxible_client.contracts.subject_kinds import SubjectKind
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import ProposalInspection
from cruxible_core.service.discovery.field_names import is_reserved_field_name

#: What a lint warning means, for surfaces that print more than its code.
CLAIM_TYPE_LINT_TEXT: dict[str, str] = {
    "cruxible.claim_type.description_missing": "state what one value of this field means",
    "cruxible.claim_type.evidence_policy_admits_no_accepted_contract": (
        "no evidence rule admits a contract this instance holds, so every value written "
        "lands uncovered: add the own-words rule (the replacement names it) or a source rule"
    ),
    "cruxible.claim_type.subject_kind_undeclared": (
        "the inferred Subject kind has no live SubjectKind: declare it in this change set "
        "or state allowed_subject_kinds"
    ),
    "cruxible.claim_type.inverse_on_enum_or_boolean": (
        "an inverse on a one-valued enum or boolean field: a member states the negative"
    ),
    "cruxible.claim_type.stale_eligible_never_current": (
        "eligible_verdicts lists stale while require_current is true: stale contenders "
        "never resolve"
    ),
}


class ClaimTypeLintWarningV1(_StrictClaimTypeInputModel):
    code: Annotated[
        Literal[
            "cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
            "cruxible.claim_type.anticipated_source_contract_omitted",
            "cruxible.claim_type.attestation_threshold_disabled",
            "cruxible.claim_type.permitted_role_unadmitted",
            "cruxible.claim_type.field_name_reserved",
            "cruxible.claim_type.inverse_on_enum_or_boolean",
            "cruxible.claim_type.stale_eligible_never_current",
            "cruxible.claim_type.description_missing",
            "cruxible.claim_type.subject_kind_undeclared",
        ],
        CurrentCode,
    ]
    field_path: str
    source_id: str | None = None
    # Evidence-policy warnings name the contract they concern; others name none.
    contract_identity: str | None = None
    contract_digest: str | None = None
    replacement_rule_fragment: dict[str, object]
    #: The definition warned about (``ClaimType:<predicate>``,
    #: ``SubjectKind:<name>``) when one lint covers several: a change set's.
    artifact: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ClaimTypeProposalLintV1(_StrictClaimTypeInputModel):
    tag: Literal["playbill-claim-type-proposal-lint-v1"] = "playbill-claim-type-proposal-lint-v1"
    warnings: tuple[ClaimTypeLintWarningV1, ...]


class ClaimTypeInputProposalResultV1(_StrictClaimTypeInputModel):
    tag: Literal["playbill-claim-type-input-proposal-result-v1"] = (
        "playbill-claim-type-input-proposal-result-v1"
    )
    proposal: ProposalInspection
    lint: ClaimTypeProposalLintV1


def lint_description(description: str | None) -> tuple[ClaimTypeLintWarningV1, ...]:
    """One advisory repair shared by authored ClaimTypes and SubjectKinds."""

    if description is not None and description.strip():
        return ()
    return (
        ClaimTypeLintWarningV1(
            code="cruxible.claim_type.description_missing",
            field_path="$.description",
            replacement_rule_fragment={"description": "<what one value of this field means>"},
        ),
    )


def accepted_capture_contracts(
    instance: PlaybillInstance, *, coordinate: AcceptedProjectionCoordinate
) -> dict[str, str]:
    """Every accepted CaptureContract at ``coordinate``: digest to qualified identity."""

    with instance.bind_accepted_projection(coordinate) as projection:
        return {
            row.artifact_digest: row.identity
            for row in projection.typed.envelopes(kind="capture-contract")
        }


def _lint_reads(
    instance: PlaybillInstance,
    value: ClaimTypeInputRecord | ClaimType,
    *,
    coordinate: AcceptedProjectionCoordinate,
    inventory: dict[str, str] | None,
    declared_subject_kinds: frozenset[str],
) -> tuple[ClaimTypeInputRecord | ClaimType, str | None, str | None]:
    """What the lint reads at the coordinate: the effective type, its description
    and an undeclared inferred kind; fills ``inventory`` when one is passed."""

    effective = value
    description = value.description
    undeclared_kind: str | None = None
    with instance.bind_accepted_projection(coordinate) as projection:
        if inventory is not None:
            inventory.update(
                (row.artifact_digest, row.identity)
                for row in projection.typed.envelopes(kind="capture-contract")
            )
        if isinstance(value, ClaimTypeInputRecord):
            # Lint what lowering writes: the defaults, or the predecessor's
            # values an edit keeps. One read: this predicate's accepted type.
            prior = projection.typed.source(f"ClaimType:{value.predicate}")
            if isinstance(prior, ClaimType) and not value.states("description"):
                description = prior.description
            # Only new inputs infer a kind; edits keep the predecessor's kinds.
            # One indexed lookup for this kind, never a walk over Subjects or kinds.
            if prior is None and not value.states("allowed_subject_kinds"):
                kind = value.predicate.rpartition(".")[0]
                if kind and kind not in declared_subject_kinds:
                    declaration = projection.typed.source(f"SubjectKind:{kind}")
                    if (
                        not isinstance(declaration, SubjectKind)
                        or declaration.lifecycle.state != "live"
                    ):
                        undeclared_kind = kind
            try:
                effective = claim_type_input_defaults(
                    value, prior if isinstance(prior, ClaimType) else None
                )
            except (FormatError, ValidationError):
                effective = value
    return effective, description, undeclared_kind


def lint_claim_type_input(
    instance: PlaybillInstance,
    value: ClaimTypeInputRecord | ClaimType,
    *,
    coordinate: AcceptedProjectionCoordinate,
    anticipated_source_ids: tuple[str, ...] = (),
    declared_subject_kinds: frozenset[str] = frozenset(),
    key_predicates: frozenset[str] = frozenset(),
    accepted_contracts: Mapping[str, str] | None = None,
) -> ClaimTypeProposalLintV1:
    """Advisory warnings for one authored ClaimType, read at one coordinate.

    ``key_predicates`` names the key ClaimTypes the same change set declares:
    a key's Claims are covered by their Subject's creation, so a key with no
    evidence rules is a deliberate shape and does not warn.
    ``accepted_contracts`` (digest to identity, from
    :func:`accepted_capture_contracts`) lets a caller linting several members
    read the contract inventory once; a whole ClaimType then reads nothing.
    """

    inventory: dict[str, str] = {} if accepted_contracts is None else dict(accepted_contracts)
    source_ids = set(anticipated_source_ids)
    if isinstance(value, ClaimTypeInputRecord):
        source_ids.update(value.anticipated_source_ids)
    # The first own-words write installs the coordinator self-source contract
    # (Flow-A binding derives these exact deterministic bytes), so a rule naming
    # it resolves on a fresh instance too. The dormant direct-self-asserted
    # constant has no production producer or acceptor, so it is not resolvable.
    resolvable_contracts: dict[str, str] = {
        capture_contract_digest(
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
        ).tagged: COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.qualified
    }
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        contract = foreign_source_capture_contract(source_id)
        resolvable_contracts[capture_contract_digest(contract).tagged] = contract.identity.qualified
    effective = value
    description = value.description
    undeclared_kind: str | None = None
    if accepted_contracts is None or isinstance(value, ClaimTypeInputRecord):
        effective, description, undeclared_kind = _lint_reads(
            instance,
            value,
            coordinate=coordinate,
            inventory=inventory if accepted_contracts is None else None,
            declared_subject_kinds=declared_subject_kinds,
        )
    accepted_contracts = inventory
    resolvable_contracts.update(accepted_contracts)

    if isinstance(effective, ClaimTypeInputRecord):
        policy = effective.evidence_admission_policy or {}
        roles: tuple[str, ...] = effective.permitted_roles or ()
        subject_kinds: tuple[str, ...] = effective.allowed_subject_kinds or ()
    else:
        policy = effective.evidence_admission_policy.model_dump(mode="json")
        roles = effective.permitted_roles
        subject_kinds = effective.allowed_subject_kinds
    raw_rules = policy.get("rules", [])
    rules = raw_rules if isinstance(raw_rules, list | tuple) else []
    warnings: list[ClaimTypeLintWarningV1] = []
    admitted: set[str] = set()
    admitted_identities: set[str] = set()
    # A family entry's membership is open (the catalog, not this lint, decides
    # who joins it), so it is tracked by the shape it admits, never by a
    # specific resolved contract identity or digest.
    admitted_family_shapes: set[str] = set()
    resolvable_identities = set(resolvable_contracts.values())
    for index, raw_rule in enumerate(rules):
        if not isinstance(raw_rule, dict):
            continue
        named: list[str] = []
        for item in raw_rule.get("capture_contracts", []) or []:
            try:
                named.append(_contract_ref(item).target.qualified)
            except (FormatError, ValueError):
                continue
        admitted_identities.update(named)
        for family in raw_rule.get("capture_contract_families", []) or []:
            shape = CAPTURE_CONTRACT_FAMILY_SHAPES.get(family)
            if shape is not None:
                admitted_family_shapes.add(shape)
        for identity in named:
            if identity not in resolvable_identities:
                warnings.append(
                    ClaimTypeLintWarningV1(
                        code="cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
                        field_path=f"$.evidence_admission_policy.rules[{index}].capture_contracts",
                        contract_identity=identity,
                        replacement_rule_fragment={
                            "capture_contracts": sorted(
                                resolvable_identities, key=lambda item: item.encode("utf-8")
                            )
                        },
                    )
                )
        raw_digests = raw_rule.get("capture_contract_digests", [])
        digests = tuple(item for item in raw_digests if isinstance(item, str))
        admitted.update(digests)
        if digests and not set(digests).intersection(resolvable_contracts):
            for digest in digests:
                warnings.append(
                    ClaimTypeLintWarningV1(
                        code="cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
                        field_path=(
                            f"$.evidence_admission_policy.rules[{index}].capture_contract_digests"
                        ),
                        contract_identity="unresolved",
                        contract_digest=digest,
                        replacement_rule_fragment={
                            "capture_contract_digests": sorted(resolvable_contracts)
                        },
                    )
                )
    authorable = _admitted_roles(tuple(roles))
    if not rules:
        # No rule at all: every write in an authorable role lands uncovered,
        # whatever evidence it cites. The repair is the rule the short form
        # (`claim-type propose`, `changes.field`) gives a new field by default.
        # A key's Claims are covered by their Subject's creation instead.
        if authorable and value.predicate not in key_predicates:
            warnings.append(
                ClaimTypeLintWarningV1(
                    code="cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
                    field_path="$.evidence_admission_policy.rules",
                    contract_identity=COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.qualified,
                    replacement_rule_fragment={
                        "rules": default_evidence_policy(tuple(roles), "self", ())["rules"]
                    },
                )
            )
    elif (
        not admitted.intersection(resolvable_contracts)
        and not admitted_identities.intersection(resolvable_identities)
        and not admitted_family_shapes
        and accepted_contracts
        and not warnings
    ):
        contract_digest = sorted(accepted_contracts)[0]
        warnings.append(
            ClaimTypeLintWarningV1(
                code="cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
                field_path="$.evidence_admission_policy.rules",
                contract_identity=accepted_contracts[contract_digest],
                contract_digest=contract_digest,
                replacement_rule_fragment={"capture_contract_digests": [contract_digest]},
            )
        )
    warnings.extend(lint_description(description))
    if undeclared_kind is not None:
        warnings.append(
            ClaimTypeLintWarningV1(
                code="cruxible.claim_type.subject_kind_undeclared",
                field_path="$.allowed_subject_kinds",
                replacement_rule_fragment={"allowed_subject_kinds": [undeclared_kind]},
            )
        )
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        contract = foreign_source_capture_contract(source_id)
        contract_digest = capture_contract_digest(contract).tagged
        if (
            contract_digest in admitted
            or contract.identity.qualified in admitted_identities
            or contract.shape in admitted_family_shapes
        ):
            continue
        warnings.append(
            ClaimTypeLintWarningV1(
                code="cruxible.claim_type.anticipated_source_contract_omitted",
                field_path="$.evidence_admission_policy.rules",
                source_id=source_id,
                contract_identity=contract.identity.qualified,
                contract_digest=contract_digest,
                replacement_rule_fragment={"capture_contract_digests": [contract_digest]},
            )
        )
    rule_roles = {
        role
        for raw_rule in rules
        if isinstance(raw_rule, dict)
        for role in (raw_rule.get("claim_roles") or ())
        if isinstance(role, str)
    }
    for index, role in enumerate(roles):
        # A write in a permitted role no rule admits lands uncovered. A policy
        # with no rules at all is a deliberate shape (a key ClaimType's Claims
        # are covered by their creation), so only a role/rule mismatch warns.
        if rule_roles and role not in rule_roles:
            warnings.append(
                ClaimTypeLintWarningV1(
                    code="cruxible.claim_type.permitted_role_unadmitted",
                    field_path=f"$.permitted_roles[{index}]",
                    replacement_rule_fragment={
                        "claim_roles": sorted(rule_roles | {role}, key=lambda r: r.encode())
                    },
                )
            )
    for kind in subject_kinds:
        short = value.predicate.removeprefix(f"{kind}.")
        if short != value.predicate and is_reserved_field_name(short):
            # The read verbs reserve this name, so the field always reads under
            # its full predicate.
            warnings.append(
                ClaimTypeLintWarningV1(
                    code="cruxible.claim_type.field_name_reserved",
                    field_path="$.predicate",
                    replacement_rule_fragment={"reserved_field_name": short},
                )
            )
    consequence = effective.attestation_consequence_policy
    for index, rule in enumerate(() if consequence is None else consequence.rules):
        # A threshold of zero escalates nothing: `next` treats the rule as disabled.
        if rule.minimum_independent_control_components == 0:
            warnings.append(
                ClaimTypeLintWarningV1(
                    code="cruxible.claim_type.attestation_threshold_disabled",
                    field_path=(
                        f"$.attestation_consequence_policy.rules[{index}]"
                        ".minimum_independent_control_components"
                    ),
                    replacement_rule_fragment={"minimum_independent_control_components": 1},
                )
            )
    resolution = effective.resolution_policy
    eligible = (
        resolution.get("eligible_verdicts")
        if isinstance(resolution, dict)
        else None
        if resolution is None
        else resolution.eligible_verdicts
    )
    require_current = (
        resolution.get("require_current", True)
        if isinstance(resolution, dict)
        else resolution is not None and resolution.require_current
    )
    if isinstance(eligible, list | tuple) and "stale" in eligible and require_current:
        # require_current keeps stale contenders out of resolution, so listing
        # stale as eligible admits nothing.
        warnings.append(
            ClaimTypeLintWarningV1(
                code="cruxible.claim_type.stale_eligible_never_current",
                field_path="$.resolution_policy.eligible_verdicts",
                replacement_rule_fragment={"require_current": False},
            )
        )
    value_type = effective.value
    if (
        effective.inverse is not None
        and (effective.cardinality or "") == "one"
        and isinstance(value_type, EnumType | BoolType)
    ):
        # A member of the enum (or false) states the negative as a found value.
        warnings.append(
            ClaimTypeLintWarningV1(
                code="cruxible.claim_type.inverse_on_enum_or_boolean",
                field_path="$.inverse",
                replacement_rule_fragment={"inverse": None},
            )
        )
    warnings.sort(key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    return ClaimTypeProposalLintV1(warnings=tuple(warnings))


__all__ = [
    "ClaimDurationInput",
    "ClaimEvidenceFreshnessInput",
    "ClaimInverseInput",
    "ClaimTypeEvidenceRulesRequired",
    "ClaimTypeInputReferenceError",
    "ClaimTypeInputIncomplete",
    "ClaimTypeInputProposalResultV1",
    "ClaimTypeInputValidationError",
    "ClaimTypeDefaultRoleNotPermitted",
    "ClaimTypeInputRecord",
    "ClaimTypeMemberDescriptionInput",
    "ClaimTypeMemberDescriptionsStale",
    "ClaimTypeLintWarningV1",
    "ClaimTypeProposalLintV1",
    "claim_type_input_defaults",
    "claim_type_input_template",
    "accepted_capture_contracts",
    "default_evidence_policy",
    "lint_claim_type_input",
    "lint_description",
    "lower_claim_type_input",
]


def install_builtin_capture_dependencies(
    claim_type: ClaimType, tree: MutableMapping[str, bytes]
) -> set[str]:
    """Install compiler-owned profiles first named by this authored type."""
    from cruxible_client.contracts.captures import (
        FOREIGN_SOURCE_CONTRACT_PREFIX,
        builtin_contract_for_write,
        capture_contract_path,
        render_capture_contract,
    )
    from cruxible_client.contracts.dependencies import dependency_pins

    installed: set[str] = set()
    for pin in dependency_pins(claim_type):
        if pin.target.kind != "CaptureContract":
            continue
        path = capture_contract_path(pin.target.name)
        if path in tree:
            continue
        if pin.target == COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity:
            contract = builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, None)
        elif pin.target.name.startswith(FOREIGN_SOURCE_CONTRACT_PREFIX):
            source = pin.target.name.removeprefix(FOREIGN_SOURCE_CONTRACT_PREFIX)
            contract = foreign_source_capture_contract(source)
        else:
            continue
        tree[path] = render_capture_contract(contract)
        installed.add(path)
    return installed
