"""Decision-only ClaimType input and deterministic proposal-time contract lint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactRef,
    parse_artifact_identity,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    foreign_source_capture_contract,
    parse_capture_contract,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicy,
    ClaimEvidenceFreshness,
    ClaimFreshnessDuration,
    ClaimType,
    EvidenceRequirement,
    RevisionEvidence,
    canonical_description_text,
    claim_type_digest,
    claim_type_path,
    effective_evidence_requirement,
    effective_revision_evidence,
    parse_claim_type,
)
from cruxible_client.contracts.codes import CurrentCode
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionPolicyV2,
)
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import AUTHORITY_VERBS_COMPILER, GOVERNED_TRIGGERS_COMPILER
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import ProposalInspection


class _StrictClaimTypeInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimTypeInputValidationError(FormatError):
    """A decision-only ClaimType input violates its final artifact contract."""

    error_code = "cruxible.claim_type.input_invalid"

    def __init__(self, validation_error: ValidationError) -> None:
        details = []
        for error in validation_error.errors(include_url=False):
            path = "$" + "".join(
                f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"]
            )
            details.append(f"{path}: {error['msg']}")
        super().__init__(f"{self.error_code}: {'; '.join(details)}")


_V7_INPUT_FIELDS = (
    "description",
    "member_descriptions",
    "default_role",
    "evidence_requirement",
    "revision_evidence",
)


class ClaimTypeMemberDescriptionInput(_StrictClaimTypeInputModel):
    """What one literal enum member means; lowering normalizes and sorts these."""

    member: str | int | bool | None
    description: str


class ClaimTypeInputRecord(_StrictClaimTypeInputModel):
    """One complete ClaimType, as authored.

    The five ClaimType v7 fields follow JSON merge-patch (RFC 7396) against the
    accepted predecessor: a field left out keeps the predecessor's value, so an
    unrelated edit never drops what the type means or how it is backed. An
    explicit ``null`` clears ``description``, ``member_descriptions`` (``[]``
    too) or ``default_role``. ``evidence_requirement`` and ``revision_evidence``
    always have a value, so they are named or left out, never ``null``. Over a
    ClaimType before v7 (or none), omitted descriptions start empty, the default
    role is none, and the requirement and revision evidence are the
    predecessor's meaning: ``self`` and ``accumulate`` (a new ClaimType takes
    ``self`` and ``replace``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # The merge-patch serializer only omits unstated fields; it does not turn
        # the input into an arbitrary dictionary. Keep the declared grammar.
        def declared(node: CoreSchema) -> CoreSchema:
            result = dict(node)
            if result.get("type") == "model":
                result.pop("serialization", None)
            elif isinstance(result.get("schema"), dict):
                result["schema"] = declared(result["schema"])
            return cast(CoreSchema, result)

        return handler(declared(schema))

    predicate: str
    allowed_subject_kinds: tuple[str, ...]
    object_kind: Literal["literal", "subject", "exact_content"]
    literal_schema: dict[str, object] | None = None
    allowed_object_subject_kinds: tuple[str, ...] = ()
    cardinality: Literal["one", "many"]
    permitted_roles: tuple[
        Literal["normative", "observation", "environment_binding", "derivation"], ...
    ]
    referent_sensitivity: Literal["identity", "shell"] = "identity"
    evidence_admission_policy: dict[str, object]
    admission_policy: dict[str, object]
    resolution_policy: dict[str, object]
    pins: tuple[dict[str, object], ...] = ()
    evidence_freshness: ClaimEvidenceFreshness | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    attestation_consequence_policy: ClaimAttestationConsequencePolicy | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    unsure_hold_for: ClaimFreshnessDuration | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    #: What the predicate means (ClaimType v7), NFC-normalized and trimmed.
    #: Omitted: kept from the predecessor. ``null``: cleared.
    description: str | None = None
    #: What each literal enum member means (ClaimType v7), in any order.
    #: Omitted: kept from the predecessor. ``null`` or ``[]``: cleared.
    member_descriptions: tuple[ClaimTypeMemberDescriptionInput, ...] | None = None
    #: The role a write takes when it names none (ClaimType v7).
    #: Omitted: kept from the predecessor. ``null``: cleared.
    default_role: ClaimRole | None = None
    #: Omitted: kept from the predecessor (``self`` before v7); a new ClaimType
    #: takes ``self``. Never ``null``.
    evidence_requirement: EvidenceRequirement | None = None
    #: Omitted: kept from the predecessor (``accumulate`` before v7); a new
    #: ClaimType takes ``replace``. Never ``null``.
    revision_evidence: RevisionEvidence | None = None
    anticipated_source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _stated_semantics(self) -> "ClaimTypeInputRecord":
        for field in ("evidence_requirement", "revision_evidence"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null: name a value or leave it out")
        return self

    @model_serializer(mode="wrap")
    def _merge_patch_wire(self, handler: Any) -> dict[str, object]:
        # Absent and null mean different things for the v7 fields, so the wire
        # keeps exactly the ones the author stated, null included.
        payload = cast(dict[str, object], handler(self))
        for field in _V7_INPUT_FIELDS:
            if field not in self.model_fields_set:
                payload.pop(field, None)
        return payload

    def states(self, field: str) -> bool:
        """Whether the author stated this v7 field (a ``null`` counts)."""

        return field in self.model_fields_set

    @field_validator("anticipated_source_ids")
    @classmethod
    def _anticipated_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("anticipated source IDs must be UTF-8 byte-sorted and unique")
        for source_id in value:
            foreign_source_capture_contract(source_id)
        return value


class ClaimTypeLintWarningV1(_StrictClaimTypeInputModel):
    code: Annotated[
        Literal[
            "cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
            "cruxible.claim_type.anticipated_source_contract_omitted",
            "cruxible.claim_type.attestation_threshold_disabled",
        ],
        CurrentCode,
    ]
    field_path: str
    source_id: str | None = None
    # Evidence-policy warnings name the contract they concern; others name none.
    contract_identity: str | None = None
    contract_digest: str | None = None
    replacement_rule_fragment: dict[str, object]


class ClaimTypeProposalLintV1(_StrictClaimTypeInputModel):
    tag: Literal["playbill-claim-type-proposal-lint-v1"] = "playbill-claim-type-proposal-lint-v1"
    warnings: tuple[ClaimTypeLintWarningV1, ...]


class ClaimTypeInputProposalResultV1(_StrictClaimTypeInputModel):
    tag: Literal["playbill-claim-type-input-proposal-result-v1"] = (
        "playbill-claim-type-input-proposal-result-v1"
    )
    proposal: ProposalInspection
    lint: ClaimTypeProposalLintV1


def claim_type_input_template() -> ClaimTypeInputRecord:
    """Return the complete literal ClaimType input shown by the CLI template surface."""

    source_id = "repo.replace-me"
    contract = foreign_source_capture_contract(source_id)
    return ClaimTypeInputRecord(
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        permitted_roles=("normative", "observation"),
        evidence_admission_policy={
            "rules": [
                {
                    "rule_id": f"source-{source_id}",
                    "claim_roles": ["normative", "observation"],
                    "capture_contracts": [contract.identity.qualified],
                    "evidence_kinds": ["self_asserted"],
                    "admission": "direct",
                    "subject_binding": "exact_claim_subject",
                }
            ]
        },
        admission_policy={
            "corroboration_requirements": [],
            "freeze_requirements": [],
        },
        resolution_policy={
            "cardinality": "one",
            "eligible_verdicts": ["supported"],
            "required_basis_kinds": [],
            "require_current": True,
            "selector": "only_contender",
            "conflict_result": "unresolved",
        },
        anticipated_source_ids=(source_id,),
    )


def identity_rules_supported(compiler: CompilerCoordinate) -> bool:
    """Whether this compiler accepts ClaimType v6/v7 identity evidence rules."""

    return compiler in (AUTHORITY_VERBS_COMPILER, GOVERNED_TRIGGERS_COMPILER)


def _contract_identities(tree: Mapping[str, bytes], source_ids: tuple[str, ...]) -> dict[str, str]:
    """Accepted (and anticipated foreign-source) contract digests to their identities."""

    identities: dict[str, str] = {}
    for source_id in source_ids:
        contract = foreign_source_capture_contract(source_id)
        identities[capture_contract_digest(contract).tagged] = contract.identity.qualified
    for path in tree:
        if path.startswith("capture-contracts/") and path.endswith(".json"):
            contract = parse_capture_contract(tree[path], path=path)
            identities[capture_contract_digest(contract).tagged] = contract.identity.qualified
    return identities


def _contract_ref(value: object) -> ArtifactRef:
    if isinstance(value, dict):
        return ArtifactRef.model_validate(value)
    if not isinstance(value, str):
        raise ClaimTypeInputReferenceError("a capture_contracts entry must be a contract name")
    identity = (
        parse_artifact_identity(value)
        if value.partition(":")[0] == "CaptureContract" and ":" in value
        else ArtifactIdentity(kind="CaptureContract", name=value)
    )
    return ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=identity)


def _identity_evidence_policy(
    raw: Mapping[str, object], *, identities: Mapping[str, str]
) -> dict[str, object]:
    """Lower authored evidence rules to identity rules.

    A rule names contracts by `capture_contracts` (a name, `CaptureContract:<name>`
    or a reference). An exact `capture_contract_digests` list is still accepted as
    input and lowered to the identities those accepted versions belong to.
    """

    rules: list[object] = []
    raw_rules = raw.get("rules", [])
    for raw_rule in raw_rules if isinstance(raw_rules, list | tuple) else ():
        if not isinstance(raw_rule, dict):
            rules.append(raw_rule)
            continue
        rule = dict(raw_rule)
        refs = [_contract_ref(item) for item in rule.pop("capture_contracts", []) or []]
        for digest in rule.pop("capture_contract_digests", []) or []:
            identity = identities.get(digest) if isinstance(digest, str) else None
            if identity is None:
                raise ClaimTypeInputReferenceError(
                    f"capture contract digest {digest!r} is not an accepted CaptureContract; "
                    "name the contract in capture_contracts instead"
                )
            refs.append(_contract_ref(identity))
        unique = {item.target.qualified: item for item in refs}
        rule["capture_contracts"] = [
            unique[key].model_dump(mode="json")
            for key in sorted(unique, key=lambda item: item.encode("utf-8"))
        ]
        rule.pop("tag", None)
        rules.append(rule)
    return {**{k: v for k, v in raw.items() if k not in {"rules", "tag"}}, "rules": rules}


def _digest_evidence_policy(
    raw: Mapping[str, object], *, identities: Mapping[str, str]
) -> Mapping[str, object]:
    """Lower identity-named rules to exact digests for a compiler without v6.

    Each named contract resolves to its accepted (or anticipated) version, so an
    input written for identity rules still lowers where only exact rules exist.
    """

    raw_rules = raw.get("rules", [])
    rule_list = raw_rules if isinstance(raw_rules, list | tuple) else ()
    if not any(isinstance(rule, dict) and "capture_contracts" in rule for rule in rule_list):
        return raw
    digest_for = {identity: digest for digest, identity in identities.items()}
    rules: list[object] = []
    for raw_rule in rule_list:
        if not isinstance(raw_rule, dict) or "capture_contracts" not in raw_rule:
            rules.append(raw_rule)
            continue
        rule = dict(raw_rule)
        digests = set(rule.pop("capture_contract_digests", []) or [])
        for item in rule.pop("capture_contracts") or []:
            identity = _contract_ref(item).target.qualified
            if identity not in digest_for:
                raise ClaimTypeInputReferenceError(
                    f"{identity} is not an accepted or anticipated CaptureContract"
                )
            digests.add(digest_for[identity])
        rule["capture_contract_digests"] = sorted(digests)
        rules.append(rule)
    return {**raw, "rules": rules}


class ClaimTypeInputReferenceError(FormatError):
    """An authored evidence rule names a contract that cannot be referenced."""

    error_code = "cruxible.claim_type.input_invalid"


class ClaimTypeMemberDescriptionsStale(FormatError):
    """Member descriptions name values the edited enum no longer admits."""

    error_code = "cruxible.claim_type.member_descriptions_stale"


class ClaimTypeDefaultRoleNotPermitted(FormatError):
    """The default role is not one of the edited ClaimType's authorable roles."""

    error_code = "cruxible.claim_type.default_role_not_permitted"


def _v7_fields(value: ClaimTypeInputRecord, predecessor: ClaimType | None) -> dict[str, object]:
    """The v7 fields a lowered ClaimType states: merge-patched onto its predecessor.

    A field the input leaves out keeps the predecessor's value, so an unrelated
    edit never changes what the type means, what a Claim needs or what a
    revision keeps. A predecessor before v7 has no descriptions and no default
    role, and means ``self`` and ``accumulate``; a new ClaimType takes ``self``
    and ``replace``. Inherited descriptions and default role are checked against
    the edited structure and refused, never silently dropped, when stale.
    """

    v7 = (
        predecessor
        if predecessor is not None and predecessor.artifact_format == "playbill-claim-type-v7"
        else None
    )
    if value.states("description"):
        description = (
            None if value.description is None else canonical_description_text(value.description)
        )
    else:
        description = None if v7 is None else v7.description
    if value.states("member_descriptions"):
        members: list[dict[str, object]] = [
            {"member": item.member, "description": canonical_description_text(item.description)}
            for item in value.member_descriptions or ()
        ]
    else:
        members = (
            [] if v7 is None else [item.model_dump(mode="json") for item in v7.member_descriptions]
        )
    members.sort(key=lambda item: canonical_bytes(item["member"]))
    enum = None if value.literal_schema is None else value.literal_schema.get("enum")
    admitted = {canonical_bytes(item) for item in enum} if isinstance(enum, list) else set()
    stale = [item["member"] for item in members if canonical_bytes(item["member"]) not in admitted]
    if stale:
        raise ClaimTypeMemberDescriptionsStale(
            f"{ClaimTypeMemberDescriptionsStale.error_code}: member_descriptions name "
            f"{', '.join(repr(item) for item in stale)}, which the literal_schema enum no "
            "longer admits; describe the current members, or pass member_descriptions: null "
            "to clear them"
        )
    if value.states("default_role"):
        default_role = value.default_role
    else:
        default_role = None if v7 is None else v7.default_role
    if default_role is not None and (
        default_role == "derivation" or default_role not in value.permitted_roles
    ):
        raise ClaimTypeDefaultRoleNotPermitted(
            f"{ClaimTypeDefaultRoleNotPermitted.error_code}: default_role {default_role!r} "
            f"must be one of the permitted roles ({', '.join(value.permitted_roles)}) and "
            "cannot be derivation; name another, or pass default_role: null to clear it"
        )
    return {
        "description": description,
        "member_descriptions": members,
        "default_role": default_role,
        "evidence_requirement": value.evidence_requirement
        or ("self" if predecessor is None else effective_evidence_requirement(predecessor)),
        "revision_evidence": value.revision_evidence
        or ("replace" if predecessor is None else effective_revision_evidence(predecessor)),
    }


def _refuse_v5_fallback(value: ClaimTypeInputRecord, predecessor: ClaimType | None) -> None:
    """A v5 ClaimType cannot say what v7 says, so lowering never silently drops it."""

    if predecessor is not None and predecessor.artifact_format == "playbill-claim-type-v7":
        raise ClaimTypeInputReferenceError(
            f"{ClaimTypeInputReferenceError.error_code}: ClaimType:{value.predicate} is v7; "
            "every evidence rule must name its contracts by identity (capture_contracts), "
            "because a v5 successor would silently return it to accumulating evidence"
        )
    named = [field for field in _V7_INPUT_FIELDS if getattr(value, field) not in (None, ())]
    if named:
        raise ClaimTypeInputReferenceError(
            f"{ClaimTypeInputReferenceError.error_code}: {', '.join(named)} need ClaimType v7, "
            "whose evidence rules name contracts by identity (capture_contracts)"
        )


def lower_claim_type_input(
    value: ClaimTypeInputRecord,
    *,
    tree: Mapping[str, bytes],
    identity_rules: bool = False,
) -> ClaimType:
    path = claim_type_path(value.predicate)
    predecessor = None
    if path in tree:
        predecessor = parse_claim_type(tree[path], path=path)
    payload = value.model_dump(mode="json")
    payload.pop("anticipated_source_ids", None)
    for field in _V7_INPUT_FIELDS:
        payload.pop(field, None)
    payload["artifact_format"] = (
        "playbill-claim-type-v7" if identity_rules else "playbill-claim-type-v5"
    )
    identities = _contract_identities(tree, value.anticipated_source_ids)
    identity_policy = None
    if identity_rules:
        try:
            identity_policy = _identity_evidence_policy(
                value.evidence_admission_policy, identities=identities
            )
        except ClaimTypeInputReferenceError:
            # A rule names an exact version that is not accepted yet, so it has no
            # identity to follow; it keeps its exact meaning as a v5 rule.
            payload["artifact_format"] = "playbill-claim-type-v5"
    if payload["artifact_format"] == "playbill-claim-type-v7":
        payload.update(_v7_fields(value, predecessor))
    else:
        _refuse_v5_fallback(value, predecessor)
    try:
        if identity_policy is not None:
            payload["evidence_admission_policy"] = ClaimEvidenceAdmissionPolicy.model_validate(
                identity_policy
            ).model_dump(mode="json")
        else:
            payload["evidence_admission_policy"] = ClaimEvidenceAdmissionPolicyV2.model_validate(
                _digest_evidence_policy(value.evidence_admission_policy, identities=identities)
            ).model_dump(mode="json")
    except ValidationError as exc:
        raise ClaimTypeInputValidationError(exc) from exc
    payload["identity"] = ArtifactIdentity(kind="ClaimType", name=value.predicate).model_dump(
        mode="json"
    )
    payload["lifecycle"] = ArtifactLifecycle(
        predecessor_digest=(None if predecessor is None else claim_type_digest(predecessor).tagged)
    ).model_dump(mode="json")
    try:
        return ClaimType.model_validate(payload)
    except ValidationError as exc:
        raise ClaimTypeInputValidationError(exc) from exc


def lint_claim_type_input(
    instance: PlaybillInstance,
    value: ClaimTypeInputRecord | ClaimType,
    *,
    coordinate: AcceptedProjectionCoordinate,
    anticipated_source_ids: tuple[str, ...] = (),
) -> ClaimTypeProposalLintV1:
    accepted_contracts: dict[str, str] = {}
    source_ids = set(anticipated_source_ids)
    if isinstance(value, ClaimTypeInputRecord):
        source_ids.update(value.anticipated_source_ids)
    # Flow-A binding derives this exact deterministic contract and carries it in
    # the governed Claim candidate. The dormant direct-self-asserted constant has
    # no production producer or acceptor, so it is intentionally not resolvable.
    resolvable_contracts: dict[str, str] = {}
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        contract = foreign_source_capture_contract(source_id)
        resolvable_contracts[capture_contract_digest(contract).tagged] = contract.identity.qualified
    with instance.bind_accepted_projection(coordinate) as projection:
        accepted_contracts.update(
            (row.artifact_digest, row.identity)
            for row in projection.typed.envelopes(kind="capture-contract")
        )
    resolvable_contracts.update(accepted_contracts)

    policy = (
        value.evidence_admission_policy
        if isinstance(value, ClaimTypeInputRecord)
        else value.evidence_admission_policy.model_dump(mode="json")
    )
    raw_rules = policy.get("rules", [])
    rules = raw_rules if isinstance(raw_rules, list | tuple) else []
    warnings: list[ClaimTypeLintWarningV1] = []
    admitted: set[str] = set()
    admitted_identities: set[str] = set()
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
    if (
        not admitted.intersection(resolvable_contracts)
        and not admitted_identities.intersection(resolvable_identities)
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
    for source_id in sorted(source_ids, key=lambda item: item.encode("utf-8")):
        contract = foreign_source_capture_contract(source_id)
        contract_digest = capture_contract_digest(contract).tagged
        if contract_digest in admitted or contract.identity.qualified in admitted_identities:
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
    consequence = (
        value.attestation_consequence_policy
        if isinstance(value, ClaimTypeInputRecord)
        else value.attestation_consequence_policy
    )
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
    warnings.sort(key=lambda item: canonical_bytes(item.model_dump(mode="json")))
    return ClaimTypeProposalLintV1(warnings=tuple(warnings))


__all__ = [
    "ClaimTypeInputProposalResultV1",
    "ClaimTypeInputValidationError",
    "ClaimTypeDefaultRoleNotPermitted",
    "ClaimTypeInputRecord",
    "ClaimTypeMemberDescriptionInput",
    "ClaimTypeMemberDescriptionsStale",
    "ClaimTypeLintWarningV1",
    "ClaimTypeProposalLintV1",
    "claim_type_input_template",
    "identity_rules_supported",
    "lint_claim_type_input",
    "lower_claim_type_input",
]
