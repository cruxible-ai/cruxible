"""Decision-only ClaimType input and deterministic proposal-time contract lint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
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
from cruxible_client.contracts.canonical_forms import CanonicalTransform
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    COORDINATOR_SELF_SOURCE_CONTRACT_ID,
    capture_contract_digest,
    foreign_source_capture_contract,
    parse_capture_contract,
)
from cruxible_client.contracts.claim_type_structure import ClaimRole
from cruxible_client.contracts.claim_types import (
    CLAIM_TYPE_V9_FORMAT,
    V7_AND_LATER_FORMATS,
    V8_AND_LATER_FORMATS,
    ClaimAttestationConsequencePolicy,
    ClaimFreshnessDuration,
    ClaimInverse,
    ClaimInverseKey,
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
)
from cruxible_client.contracts.public_names import public_name, stored_contract_name
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.temporal import parse_iso_duration_microseconds
from cruxible_client.contracts.types import CompilerCoordinate
from cruxible_core.compiler.compiler import (
    AUTHORITY_VERBS_COMPILER,
    CLAIM_REBIND_COMPILER,
    GOVERNED_TRIGGERS_COMPILER,
    PROVIDER_BUILDS_COMPILER,
    SUBJECT_IDENTITY_COMPILER,
    current_compiler_coordinate,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import ProposalInspection
from cruxible_core.service.discovery.field_names import is_reserved_field_name


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
    "canonical_form",
    "inverse",
)

#: The structural fields an edit keeps from its predecessor when left out.
_STRUCTURAL_INPUT_FIELDS = (
    "allowed_subject_kinds",
    "object_kind",
    "literal_schema",
    "allowed_object_subject_kinds",
    "cardinality",
    "permitted_roles",
    "referent_sensitivity",
    "evidence_admission_policy",
    "admission_policy",
    "resolution_policy",
    "pins",
    "evidence_freshness",
    "attestation_consequence_policy",
    "unsure_hold_for",
)

#: What a new ClaimType must state: no default says what it is about or holds.
_REQUIRED_NEW_FIELDS = ("allowed_subject_kinds", "object_kind", "cardinality")

#: Fields that always have a value: named or left out, never ``null``.
_NEVER_NULL_FIELDS = (
    "allowed_subject_kinds",
    "object_kind",
    "cardinality",
    "permitted_roles",
    "referent_sensitivity",
    "evidence_admission_policy",
    "admission_policy",
    "resolution_policy",
    "evidence_requirement",
    "revision_evidence",
)


def _duration_input(value: object) -> object:
    # An ISO-8601 duration (``P30D``) is the authored spelling; the stored
    # ClaimType keeps whole microseconds, so the bytes are what they were.
    if isinstance(value, str):
        return {"microseconds": parse_iso_duration_microseconds(value)}
    return value


#: A duration as authored: an ISO-8601 string (``P30D``, ``PT12H``) or
#: ``{microseconds: N}``; lowered to microseconds.
ClaimDurationInput = Annotated[
    ClaimFreshnessDuration,
    BeforeValidator(_duration_input, json_schema_input_type=str | ClaimFreshnessDuration),
]


class ClaimEvidenceFreshnessInput(_StrictClaimTypeInputModel):
    """How long a Claim's evidence stays current; ``stale_after`` takes ``P30D``."""

    tag: Literal["playbill-claim-evidence-freshness-v1"] = "playbill-claim-evidence-freshness-v1"
    stale_after: ClaimDurationInput


class ClaimTypeMemberDescriptionInput(_StrictClaimTypeInputModel):
    """What one literal enum member means; lowering normalizes and sorts these."""

    member: str | int | bool | None
    description: str


class ClaimInverseInput(_StrictClaimTypeInputModel):
    """How the type states that a unit holds nothing (ClaimType v9).

    ``key`` names the unit: ``{kind: slot}`` for a one-valued type,
    ``{kind: object}`` or ``{kind: object_key, predicate}`` (a key ClaimType of
    every object SubjectKind) for a many-valued one. ``evidence_rules`` are ids
    of the type's own evidence rules, in any order; only a Capture one of them
    admits establishes a none. ``stale_after`` takes ``P7D`` (absent: the type's
    evidence freshness applies).
    """

    key: ClaimInverseKey
    evidence_rules: tuple[str, ...] = Field(min_length=1)
    stale_after: ClaimDurationInput | None = None
    description: str | None = None

    def lowered(self) -> ClaimInverse:
        return ClaimInverse(
            key=self.key,
            evidence_rules=tuple(
                sorted(set(self.evidence_rules), key=lambda item: item.encode("utf-8"))
            ),
            stale_after=self.stale_after,
            description=(
                None if self.description is None else canonical_description_text(self.description)
            ),
        )


class ClaimTypeInputRecord(_StrictClaimTypeInputModel):
    """One ClaimType, as authored: state what differs from the defaults.

    A new ClaimType states ``predicate``, ``allowed_subject_kinds``,
    ``object_kind`` and ``cardinality`` (and ``literal_schema`` or
    ``allowed_object_subject_kinds`` for its object); every other field takes
    its default, written out in full in the stored ClaimType
    (``cruxible claim-type propose --template --expanded`` prints them):

    - ``permitted_roles``: ``[default_role]`` when one is stated, else
      ``[normative]``.
    - ``evidence_admission_policy``: with ``evidence_requirement`` ``self`` (the
      default) or ``none``, one own-words rule (``self``, contract
      ``cruxible.coordinator-self-source-v1``) for every permitted role but
      ``derivation``, plus one rule per ``anticipated_source_ids`` entry; with
      ``captured``, the source rules only.
    - ``admission_policy``: no corroboration or freeze requirements.
    - ``resolution_policy``: supported, current Claims; ``only_contender``
      (one) or ``all`` (many); a conflict leaves the field unresolved.
    - ``evidence_requirement`` ``self``, ``revision_evidence`` ``replace``.

    An edit (a predicate this instance already holds) follows JSON merge-patch
    (RFC 7396) against the accepted predecessor for every field but
    ``predicate``: a field left out keeps the predecessor's value, so an
    unrelated edit never drops what the type means or how it is backed. An
    explicit ``null`` clears a field that may be empty (``literal_schema``,
    ``evidence_freshness``, ``attestation_consequence_policy``,
    ``unsure_hold_for``, ``description``, ``member_descriptions`` (``[]`` too),
    ``default_role``, ``canonical_form``, ``inverse``); the others always have a value, so
    they are named or left out, never ``null``. Over a ClaimType before v7,
    omitted descriptions start empty, the default role is none, and the
    requirement and revision evidence are the predecessor's meaning: ``self``
    and ``accumulate``. Durations (``evidence_freshness.stale_after``,
    ``unsure_hold_for``) take ISO-8601 (``P30D``) or ``{microseconds: N}``.
    A rule names a contract by its public or stored name.
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
    allowed_subject_kinds: tuple[str, ...] | None = None
    object_kind: Literal["literal", "subject", "exact_content"] | None = None
    literal_schema: dict[str, object] | None = None
    allowed_object_subject_kinds: tuple[str, ...] = ()
    cardinality: Literal["one", "many"] | None = None
    permitted_roles: (
        tuple[Literal["normative", "observation", "environment_binding", "derivation"], ...] | None
    ) = None
    referent_sensitivity: Literal["identity", "shell"] = "identity"
    evidence_admission_policy: dict[str, object] | None = None
    admission_policy: dict[str, object] | None = None
    resolution_policy: dict[str, object] | None = None
    pins: tuple[dict[str, object], ...] = ()
    evidence_freshness: ClaimEvidenceFreshnessInput | None = None
    attestation_consequence_policy: ClaimAttestationConsequencePolicy | None = None
    unsure_hold_for: ClaimDurationInput | None = None
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
    #: The ordered transforms every literal value is stored and compared in
    #: (ClaimType v8; ``contracts.canonical_forms``). Stating it lowers to v8,
    #: which a SubjectKind key needs (``[]`` states no transform).
    #: Omitted: kept from the predecessor (none before v8). ``null`` or ``[]``:
    #: no transform.
    canonical_form: tuple[CanonicalTransform, ...] | None = None
    #: How the type states absence (ClaimType v9): stating it lowers to v9.
    #: Omitted: kept from the predecessor (none before v9). ``null``: cleared.
    inverse: ClaimInverseInput | None = None
    anticipated_source_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _stated_semantics(self) -> "ClaimTypeInputRecord":
        for field in _NEVER_NULL_FIELDS:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null: name a value or leave it out")
        return self

    @model_serializer(mode="wrap")
    def _merge_patch_wire(self, handler: Any) -> dict[str, object]:
        # Absent and null mean different things, so the wire keeps exactly the
        # fields the author stated, null included.
        payload = cast(dict[str, object], handler(self))
        for field in tuple(payload):
            if field != "predicate" and field not in self.model_fields_set:
                payload.pop(field, None)
        return payload

    def states(self, field: str) -> bool:
        """Whether the author stated this field (a ``null`` counts)."""

        return field in self.model_fields_set

    @field_validator("anticipated_source_ids")
    @classmethod
    def _anticipated_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
            raise ValueError("anticipated source IDs must be UTF-8 byte-sorted and unique")
        for source_id in value:
            foreign_source_capture_contract(source_id)
        return value


class ClaimTypeInputIncomplete(FormatError):
    """A new ClaimType leaves out a field no default can fill."""

    error_code = "cruxible.claim_type.input_incomplete"


class ClaimTypeEvidenceRulesRequired(FormatError):
    """A captured-only ClaimType names no source its evidence could come from."""

    error_code = "cruxible.claim_type.evidence_rules_required"


class ClaimTypeNumberSchemaIntegerOnly(FormatError):
    """A literal schema admits non-integer numbers, which no Claim value can hold."""

    error_code = "cruxible.claim_type.number_schema_integer_only"


def _admitted_roles(roles: tuple[str, ...]) -> list[str]:
    return sorted({role for role in roles if role != "derivation"}, key=lambda r: r.encode())


def _default_evidence_policy(
    roles: tuple[str, ...], requirement: str, source_ids: tuple[str, ...]
) -> dict[str, object]:
    """The rules a new ClaimType takes when it states none (names in public spelling)."""

    claim_roles = _admitted_roles(roles)
    rules: list[dict[str, object]] = []
    if requirement != "captured" and claim_roles:
        rules.append(
            {
                "rule_id": "self",
                "claim_roles": claim_roles,
                "capture_contracts": [public_name(COORDINATOR_SELF_SOURCE_CONTRACT_ID)],
                "evidence_kinds": ["self_asserted"],
                "admission": "direct",
                "subject_binding": "exact_claim_subject",
            }
        )
    if claim_roles:
        for source_id in source_ids:
            contract = foreign_source_capture_contract(source_id)
            rules.append(
                {
                    "rule_id": f"source-{source_id}",
                    "claim_roles": claim_roles,
                    "capture_contracts": [public_name(contract.identity.name)],
                    "evidence_kinds": ["self_asserted"],
                    "admission": "direct",
                    "subject_binding": "exact_claim_subject",
                }
            )
    if requirement == "captured" and not rules:
        raise ClaimTypeEvidenceRulesRequired(
            f"{ClaimTypeEvidenceRulesRequired.error_code}: evidence_requirement captured "
            "admits only captured evidence, and this ClaimType names no source: list the "
            "sources in anticipated_source_ids, or state evidence_admission_policy"
        )
    rules.sort(key=lambda rule: str(rule["rule_id"]).encode("utf-8"))
    return {"rules": rules}


def _inherited_policy(policy: dict[str, object]) -> dict[str, object]:
    """A predecessor's rules as input: an exact-digest rule's empty reducer list
    has no identity-rule spelling, so it is dropped; lowering then maps each
    digest to its contract's identity, as for authored digest rules."""

    rules = policy.get("rules")
    if not isinstance(rules, list):
        return policy
    return {
        **policy,
        "rules": [
            {
                key: value
                for key, value in rule.items()
                if not (key == "allowed_reducer_digests" and not value)
            }
            if isinstance(rule, dict)
            else rule
            for rule in rules
        ],
    }


def claim_type_input_defaults(
    value: ClaimTypeInputRecord, predecessor: ClaimType | None
) -> ClaimTypeInputRecord:
    """The same input with every structural field stated.

    Over a predecessor, a field left out takes the predecessor's value
    (merge-patch); over none, it takes its default, and a field no default can
    fill refuses ``cruxible.claim_type.input_incomplete``. A minimal input and
    its spelled-out equivalent therefore lower to one ClaimType and one digest.
    The ClaimType v7 and v8 fields are merged later, by lowering, exactly as
    before; a new ClaimType states its evidence requirement and revision
    evidence here so the expanded form shows them.
    """

    data = value.model_dump(mode="json")
    stated = value.model_fields_set
    if predecessor is not None:
        prior = predecessor.model_dump(mode="json")
        for field in _STRUCTURAL_INPUT_FIELDS:
            if field not in stated and field in prior:
                data[field] = prior[field]
        if "evidence_admission_policy" not in stated:
            data["evidence_admission_policy"] = _inherited_policy(
                cast(dict[str, object], data["evidence_admission_policy"])
            )
        return ClaimTypeInputRecord.model_validate(data)
    missing = [field for field in _REQUIRED_NEW_FIELDS if field not in stated]
    if missing:
        raise ClaimTypeInputIncomplete(
            f"{ClaimTypeInputIncomplete.error_code}: a new ClaimType states "
            f"{', '.join(missing)}; every other field has a default "
            "(`cruxible claim-type propose --template --expanded` shows them)"
        )
    if "permitted_roles" not in stated:
        data["permitted_roles"] = [value.default_role or "normative"]
    requirement = value.evidence_requirement or "self"
    if "evidence_requirement" not in stated:
        data["evidence_requirement"] = requirement
    if "revision_evidence" not in stated:
        data["revision_evidence"] = "replace"
    if "evidence_admission_policy" not in stated:
        data["evidence_admission_policy"] = _default_evidence_policy(
            tuple(data["permitted_roles"]),
            requirement,
            value.anticipated_source_ids,
        )
    if "admission_policy" not in stated:
        data["admission_policy"] = {"corroboration_requirements": [], "freeze_requirements": []}
    if "resolution_policy" not in stated:
        many = value.cardinality == "many"
        data["resolution_policy"] = {
            "cardinality": value.cardinality,
            "eligible_verdicts": ["supported"],
            "required_basis_kinds": [],
            "require_current": True,
            "selector": "all" if many else "only_contender",
            "conflict_result": "unresolved",
        }
    return ClaimTypeInputRecord.model_validate(data)


def _admits_fraction(schema: object) -> bool:
    if isinstance(schema, dict):
        declared = schema.get("type")
        if declared == "number" or (isinstance(declared, list) and "number" in declared):
            return True
        return any(_admits_fraction(item) for item in schema.values())
    if isinstance(schema, list):
        return any(_admits_fraction(item) for item in schema)
    return False


def _refuse_number_schema(value: ClaimTypeInputRecord) -> None:
    """A stated ``type: number`` admits fractions no canonical value can encode."""

    if value.states("literal_schema") and _admits_fraction(value.literal_schema):
        raise ClaimTypeNumberSchemaIntegerOnly(
            f"{ClaimTypeNumberSchemaIntegerOnly.error_code}: literal_schema admits "
            "`type: number`, but Claim values are integers or text (canonical JSON holds no "
            "fractions). Repair: state `type: integer` in a scaled unit (cents, basis points), "
            "or `type: string` with a pattern for a decimal"
        )


#: What a lint warning means, for surfaces that print more than its code.
CLAIM_TYPE_LINT_TEXT: dict[str, str] = {
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


def claim_type_input_template(*, expanded: bool = False) -> ClaimTypeInputRecord:
    """The ClaimType input the CLI template surface prints.

    The minimal form states only what no default can fill, plus
    ``canonical_form: []`` so the literal one-valued field can key a SubjectKind
    (a key needs ClaimType v8). ``expanded`` states every default the minimal
    form takes, from the same function lowering applies.
    """

    minimal = ClaimTypeInputRecord(
        predicate="project.work_item.status",
        allowed_subject_kinds=("project.work_item",),
        object_kind="literal",
        literal_schema={"type": "string"},
        cardinality="one",
        canonical_form=(),
    )
    return claim_type_input_defaults(minimal, None) if expanded else minimal


def identity_rules_supported(compiler: CompilerCoordinate) -> bool:
    """Whether this compiler accepts ClaimType v6/v7 identity evidence rules."""

    return compiler in (
        AUTHORITY_VERBS_COMPILER,
        GOVERNED_TRIGGERS_COMPILER,
        SUBJECT_IDENTITY_COMPILER,
        PROVIDER_BUILDS_COMPILER,
        CLAIM_REBIND_COMPILER,
    )


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
    # A rule names a contract by its public name or its stored identity; both
    # map to the stored identity, which is what evidence is captured under.
    if isinstance(value, dict):
        ref = ArtifactRef.model_validate(value)
        name = stored_contract_name(ref.target.name)
        if name == ref.target.name:
            return ref
        return ref.model_copy(update={"target": ArtifactIdentity(kind=ref.target.kind, name=name)})
    if not isinstance(value, str):
        raise ClaimTypeInputReferenceError("a capture_contracts entry must be a contract name")
    value = stored_contract_name(value)
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


class ClaimTypeInputReferenceError(FormatError):
    """An authored evidence rule names a contract that cannot be referenced."""

    error_code = "cruxible.claim_type.input_invalid"


class ClaimTypeRequiresCompilerUpgrade(FormatError):
    """ClaimType authoring writes v7 with identity rules; this compiler admits neither."""

    error_code = "cruxible.claim_type.compiler_upgrade_required"

    def __init__(self) -> None:
        target = current_compiler_coordinate()
        super().__init__(
            f"{self.error_code}: ClaimType authoring writes ClaimType v7 with identity "
            "evidence rules, which need compiler revision 31 or later; this instance's "
            "accepted compiler predates them. Repair: propose `cruxible compiler upgrade "
            f"--to {target.rule_digest}`, activate it, then author the ClaimType again."
        )
        self.repair = RepairOperation(
            operation="cruxible.compiler.upgrade",
            arguments={"to": target.rule_digest, "name": "upgrade-for-claim-type-v7"},
        )


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
        if predecessor is not None and predecessor.artifact_format in V7_AND_LATER_FORMATS
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
    permitted = value.permitted_roles or ()
    if default_role is not None and (default_role == "derivation" or default_role not in permitted):
        raise ClaimTypeDefaultRoleNotPermitted(
            f"{ClaimTypeDefaultRoleNotPermitted.error_code}: default_role {default_role!r} "
            f"must be one of the permitted roles ({', '.join(permitted)}) and "
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


def _canonical_form(
    value: ClaimTypeInputRecord, predecessor: ClaimType | None
) -> tuple[CanonicalTransform, ...] | None:
    """The canonical form a lowered ClaimType states, or ``None`` to stay v7.

    Stating ``canonical_form`` lowers to v8. Left out, a v8 predecessor's form is
    kept (merge-patch, like the v7 fields), so an unrelated edit never changes
    what the predicate's values compare as; over anything else it stays v7. A v8
    ClaimType never lowers back to v7: ``null`` over one states no transform.
    """

    inherited = (
        predecessor.canonical_form
        if predecessor is not None and predecessor.artifact_format in V8_AND_LATER_FORMATS
        else None
    )
    if value.states("canonical_form"):
        if value.canonical_form is None:
            return None if inherited is None else ()
        return value.canonical_form
    return inherited


def _inverse(
    value: ClaimTypeInputRecord, predecessor: ClaimType | None
) -> tuple[bool, ClaimInverse | None]:
    """Whether a lowered ClaimType is v9, and the inverse it declares.

    Stating ``inverse`` lowers to v9. Left out, a v9 predecessor's inverse is
    kept (merge-patch); ``null`` clears it, and a v9 ClaimType never lowers back
    to v8, as a v8 one never lowers back to v7.
    """

    v9 = predecessor is not None and predecessor.artifact_format == CLAIM_TYPE_V9_FORMAT
    if value.states("inverse"):
        if value.inverse is None:
            return v9, None
        return True, value.inverse.lowered()
    return v9, None if predecessor is None or not v9 else predecessor.inverse


def lower_claim_type_input(
    value: ClaimTypeInputRecord,
    *,
    tree: Mapping[str, bytes],
    identity_rules: bool = False,
) -> ClaimType:
    if not identity_rules:
        # Every ClaimType authoring path writes v7 with identity evidence rules;
        # a compiler before revision 31 admits neither, so it is upgraded first
        # rather than handed a v5 exact-digest ClaimType.
        raise ClaimTypeRequiresCompilerUpgrade()
    path = claim_type_path(value.predicate)
    predecessor = None
    if path in tree:
        predecessor = parse_claim_type(tree[path], path=path)
    _refuse_number_schema(value)
    value = claim_type_input_defaults(value, predecessor)
    payload = value.model_dump(mode="json")
    payload.pop("anticipated_source_ids", None)
    for field in _V7_INPUT_FIELDS:
        payload.pop(field, None)
    payload["artifact_format"] = "playbill-claim-type-v7"
    canonical_form = _canonical_form(value, predecessor)
    if canonical_form is not None:
        payload["artifact_format"] = "playbill-claim-type-v8"
        payload["canonical_form"] = list(canonical_form)
    v9, inverse = _inverse(value, predecessor)
    if v9:
        # v9 is v8 plus an inverse: it states its canonical form too.
        payload["artifact_format"] = CLAIM_TYPE_V9_FORMAT
        payload["canonical_form"] = list(canonical_form or ())
        if inverse is not None:
            payload["inverse"] = inverse.model_dump(mode="json")
    # Every rule names its contracts by identity. A rule naming an exact digest
    # no accepted contract version carries has no identity to follow, so it is
    # refused rather than authored as a v5 exact-digest rule.
    policy = value.evidence_admission_policy or {}
    raw_rules = policy.get("rules", ())
    needs_digest_lookup = isinstance(raw_rules, list | tuple) and any(
        isinstance(rule, dict) and bool(rule.get("capture_contract_digests")) for rule in raw_rules
    )
    identity_policy = _identity_evidence_policy(
        policy,
        identities=(
            _contract_identities(tree, value.anticipated_source_ids) if needs_digest_lookup else {}
        ),
    )
    payload.update(_v7_fields(value, predecessor))
    try:
        payload["evidence_admission_policy"] = ClaimEvidenceAdmissionPolicy.model_validate(
            identity_policy
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
    with instance.bind_accepted_projection(coordinate) as projection:
        accepted_contracts.update(
            (row.artifact_digest, row.identity)
            for row in projection.typed.envelopes(kind="capture-contract")
        )
        if isinstance(value, ClaimTypeInputRecord):
            # Lint what lowering writes: the defaults, or the predecessor's
            # values an edit keeps. One read: this predicate's accepted type.
            prior = projection.typed.source(f"ClaimType:{value.predicate}")
            try:
                effective = claim_type_input_defaults(
                    value, prior if isinstance(prior, ClaimType) else None
                )
            except (FormatError, ValidationError):
                effective = value
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
    schema = effective.literal_schema
    if (
        effective.inverse is not None
        and (effective.cardinality or "") == "one"
        and isinstance(schema, dict)
        and ("enum" in schema or schema.get("type") == "boolean")
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
    "ClaimTypeEvidenceRulesRequired",
    "ClaimTypeInputIncomplete",
    "ClaimTypeInputProposalResultV1",
    "ClaimTypeNumberSchemaIntegerOnly",
    "ClaimTypeInputValidationError",
    "ClaimTypeDefaultRoleNotPermitted",
    "ClaimTypeInputRecord",
    "ClaimTypeMemberDescriptionInput",
    "ClaimTypeMemberDescriptionsStale",
    "ClaimTypeLintWarningV1",
    "ClaimTypeProposalLintV1",
    "claim_type_input_defaults",
    "claim_type_input_template",
    "identity_rules_supported",
    "lint_claim_type_input",
    "lower_claim_type_input",
]
